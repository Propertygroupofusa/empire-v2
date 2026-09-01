"""
DEL'S TRADING EMPIRE — PROP BOT v3
=====================================
APEX $25K Futures evaluation — MES, MNQ, MGC
Account: APEX_589296
Rule: 7 consecutive profitable days before going live
"""

import os
import asyncio
import logging
import math
import smtplib
import time
import traceback
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import aiohttp
import uuid
from sqlalchemy import select, desc, func, case
from database import AsyncSessionLocal
from models import BotPosition, Payment, AlpacaBacktestRun, TradingBotState, AlpacaBranch, AlpacaBranchTradeHistory
import bot_mandates
from bot_mandates import APEX_MANDATE, validate_entry, MOMENTUM_ENTRY, MEAN_REVERSION_ENTRY
from alpaca_mean_reversion import should_exit_position_momentum, should_exit_position
from profit_tracker import FiveHourProfitTracker
from opening_bar_signals import (
    ELEPHANT_BAR_LOOKBACK, OPENING_BAR_MAX_ENTRIES_PER_DAY,
    _group_bars_by_day, _replay_opening_bar_breakout_multi_entry,
)

# Measurement system: Trade logging with full signal context
try:
    from measurement_system import SignalContext, TradeLog, trade_logger, StatisticalAnalyzer
    MEASUREMENT_AVAILABLE = True
except ImportError:
    MEASUREMENT_AVAILABLE = False
    log.warning("measurement_system not available - trade logging disabled") if 'log' in dir() else None

ET = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

def _safe_float_env(name: str, default: str) -> float:
    """Parse a Railway env var as float, falling back to the numeric default
    and logging a warning instead of crashing on a bad value (e.g. someone
    pastes 'default: 40' or a stray label into the Variables tab). These all
    run at module import time, so an unguarded float()/int() here used to
    mean one bad Railway variable could crash the entire bot before its
    first cycle - see the RSI_LONG_THRESHOLD incident in alpaca_mean_reversion.py."""
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except ValueError:
        log.warning(f"{name}={raw!r} is not a valid number - using default {default} instead. Fix this in Railway's Variables tab.")
        return float(default)


def _safe_int_env(name: str, default: str) -> int:
    """Same as _safe_float_env but for integer-valued env vars."""
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        log.warning(f"{name}={raw!r} is not a valid integer - using default {default} instead. Fix this in Railway's Variables tab.")
        return int(default)


def get_headers():
    """Dynamically read Alpaca API credentials from env vars on every call.
    This ensures fresh credentials even if env vars are set after module load."""
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        "Content-Type": "application/json"
    }

def get_base_url():
    """Dynamically read Alpaca base URL from env var. Default: production API (not paper trading)."""
    return os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")

# Live trading mode
LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# RSI entry/exit thresholds
RSI_BUY_BELOW  = _safe_float_env("PROP_RSI_BUY_BELOW", "30")
RSI_SELL_ABOVE = _safe_float_env("PROP_RSI_SELL_ABOVE", "70")

# Crypto-specific thresholds: AGGRESSIVE SCALPING FOR MILESTONE SPEED
# Lowered from 30/70 to 35/65 to catch MORE entry/exit opportunities
# Maximizes trade frequency to hit $1,000 ASAP, then $2,000 within 24hr
CRYPTO_RSI_BUY_BELOW  = _safe_float_env("CRYPTO_RSI_BUY_BELOW", "35")   # MORE oversold entries (faster compounding)
CRYPTO_RSI_SELL_ABOVE = _safe_float_env("CRYPTO_RSI_SELL_ABOVE", "65")  # MORE overbought exits (more profit locks)

# AGGRESSIVE WINS + STRICT LOSS PREVENTION
# Base stop-loss: 0.3% to exit losing trades immediately
# At higher scales: tighter stops to prevent multiplied losses
# 1.0x scale: 0.3% stop
# 1.5x scale: 0.2% stop (1.5x scaled position needs tighter exit)
# 2.0x scale: 0.2% stop (maximum scale = maximum discipline)
STOP_LOSS_BASE_PCT = _safe_float_env("PROP_STOP_LOSS_PCT", "0.003")  # Base: 0.3% for stocks/futures

# Crypto-specific stop-loss: dynamically tightens with scale
# Base: 0.3%, tightens to 0.2% at 1.5x scale
CRYPTO_STOP_LOSS_BASE_PCT = _safe_float_env("CRYPTO_STOP_LOSS_PCT", "0.003")  # Base: 0.3%

# MULTI-TIMEFRAME CONFIRMATION CONTROL
# Set to "false" to disable 1H trend checking (let all RSI signals through)
# Set to "true" to enforce 1H trend alignment (default: avoid fighting higher trend)
REQUIRE_HIGHER_TF_CONFIRMATION = os.getenv("REQUIRE_HIGHER_TF_CONFIRMATION", "true").lower() == "true"

def get_dynamic_stop_loss(scale: float) -> float:
    """Tighten stop-loss as positions scale up (1.5x+ = tighter discipline)"""
    if scale >= 1.5:
        return 0.002  # 0.2% at 1.5x scale and higher
    return STOP_LOSS_BASE_PCT  # 0.3% baseline

def get_dynamic_crypto_stop_loss(scale: float) -> float:
    """Crypto: same dynamic tightening as stocks"""
    if scale >= 1.5:
        return 0.002  # 0.2% at 1.5x scale and higher
    return CRYPTO_STOP_LOSS_BASE_PCT  # 0.3% baseline

# Daily maximum loss in dollars — DYNAMIC CIRCUIT BREAKER
# Base: $10 daily max loss at 1.0x scale. SCALES with position multiplier.
# 1.0x scale → $10 loss limit
# 1.5x scale → $15 loss limit (larger positions = larger max loss allowed)
# 2.0x scale → $20 loss limit (can absorb bigger drawdowns while scaling)
# Prevents catastrophic losses but allows survival of losing streaks at higher scales
DAILY_MAX_LOSS_BASE = _safe_float_env("PROP_DAILY_MAX_LOSS_BASE", "10")

# Max hold time before a position is force-exited regardless of price. Was
# hardcoded to 7200s (2hr) - now configurable, default 1800s (30min) for
# faster in-and-out turnover.
PROP_MAX_HOLD_SECONDS = _safe_int_env("PROP_MAX_HOLD_SECONDS", "1800")

# AGGRESSIVE EXIT ON RED — Close any position down 0.5% immediately
# Don't wait for stop-loss to trigger. Exit fast, preserve capital.
QUICK_EXIT_LOSS_PCT = _safe_float_env("QUICK_EXIT_LOSS_PCT", "0.005")  # Exit any loser at 0.5% down

# SCALING UP SYSTEM — Increase position sizes after each milestone lock
# $1K lock → scale to 1.5x, $2K lock → scale to 2.0x, $5K lock → scale to 3.0x
POSITION_SCALE_MULTIPLIER = _safe_float_env("POSITION_SCALE_MULTIPLIER", "1.0")  # Starts at 1.0x, increases per milestone

# APEX futures — use micro contracts (lower risk during evaluation)
# Stock/futures only (Alpaca crypto is blocked for this account)
# Crypto trading handled separately by crypto_coinbase_bot.py
FUTURES = {
    # American indices
    "MES": {"name": "Micro E-mini S&P 500", "qty": 1, "symbol": "SPY"},
    "MNQ": {"name": "Micro E-mini Nasdaq",  "qty": 1, "symbol": "QQQ"},
    "MYM": {"name": "Micro E-mini Dow",     "qty": 1, "symbol": "DIA"},
    "M2K": {"name": "Micro E-mini Russell", "qty": 1, "symbol": "IWM"},
    # Commodities
    "MGC": {"name": "Micro Gold",           "qty": 1, "symbol": "GLD"},
    "MCL": {"name": "Micro Crude Oil",      "qty": 1, "symbol": "USO"},
    "SIL": {"name": "Micro Silver",         "qty": 1, "symbol": "SLV"},
    # 1x inverse ETFs - bought LONG like everything else here, but they
    # move opposite their index, so this is how the bot profits on a
    # downtrend without shorting or margin. No futures-proxy contract code
    # exists for these, so the ETF ticker is used as both the key and the
    # traded symbol.
    "SH":  {"name": "Short S&P 500 (inverse of SPY)",   "qty": 1, "symbol": "SH"},
    "PSQ": {"name": "Short Nasdaq (inverse of QQQ)",    "qty": 1, "symbol": "PSQ"},
    "DOG": {"name": "Short Dow 30 (inverse of DIA)",    "qty": 1, "symbol": "DOG"},
    "RWM": {"name": "Short Russell 2000 (inverse of IWM)", "qty": 1, "symbol": "RWM"},
    # Individual mega-cap tech equities - per the account owner's explicit
    # request ("I want the Twitter one I want the Facebook one I want it
    # all") while describing a real opening-bar breakout setup. Twitter/X
    # is not addable - it delisted from public markets in 2022 and can't
    # be traded through Alpaca or any other broker; told to the account
    # owner directly rather than silently dropped. Facebook is Meta
    # Platforms today (META). No futures-proxy contract code exists for
    # a real equity, so - same pattern already used for the inverse
    # ETFs above - the ticker is its own key and its own traded symbol.
    # Real Alpaca equity orders (size_position()'s dollar-based
    # fractional-share sizing already handles any real symbol
    # identically - the "qty" field here is informational only, never
    # used for real order sizing).
    "MSFT":  {"name": "Microsoft",       "qty": 1, "symbol": "MSFT"},
    "META":  {"name": "Meta Platforms (Facebook)", "qty": 1, "symbol": "META"},
    "AAPL":  {"name": "Apple",           "qty": 1, "symbol": "AAPL"},
    "GOOGL": {"name": "Alphabet (Google)", "qty": 1, "symbol": "GOOGL"},
    "AMZN":  {"name": "Amazon",          "qty": 1, "symbol": "AMZN"},
    "NVDA":  {"name": "Nvidia",          "qty": 1, "symbol": "NVDA"},
}

# Max concurrent open positions. Explicit request: don't cap this below
# what the account can actually afford - open as many of the tracked
# symbols as there's real cash and a signal for, not an arbitrary count.
# Defaults to every symbol currently tracked (len(FUTURES)) rather than a
# fixed number below that, so it never artificially blocks a signal on a
# symbol that isn't already held - real cash (see size_position/
# MIN_POSITION_NOTIONAL) is the actual limiting factor. The rotation
# logic in run_prop_cycle (swap out a losing position for a fresh signal)
# still exists as a safety net, but can't trigger at this default since
# there's no 8th symbol to need a slot from.
# Max concurrent positions: SCALED WITH POSITION MULTIPLIER
# Base: 2 positions at 1.0x scale
# At 1.5x scale: reduce to 1 position (1.5x loss on 1 trade < 1.0x loss on 2 trades)
# At 2.0x scale: stay at 1 position (conservative with max scaling)
# This prevents multiplied losses across multiple scaled positions
BASE_MAX_POSITIONS = _safe_int_env("PROP_MAX_POSITIONS", "8")  # Increased from 3 to 8: more concurrent positions for faster capital deployment

def get_dynamic_max_positions(scale: float) -> int:
    """Allow more positions at all scales to maximize capital deployment"""
    if scale >= 2.0:
        return 6  # Still allow 6 at 2.0x scale (was 1)
    if scale >= 1.5:
        return 7  # Allow 7 at 1.5x scale (was 1)
    return BASE_MAX_POSITIONS  # 8 positions at baseline 1.0x scale (was 3)

# Profit target, in REAL DOLLARS of profit on the position (not a raw
# AGGRESSIVE PROFIT LOCKING - Close trades early to compound faster
# Smaller positions at small equity = quick wins reinvested = exponential growth
# At $1K: aim for $1-2 per trade (0.75% targets on $130-170 positions)
# At $5K: aim for $5-15 per trade (1% targets on $500+ positions)
# At $25K: aim for $50-100 per trade (1% targets on $5,000+ positions)
PROFIT_TARGET_DOLLARS_MILESTONES = [
    (0,     1.50),      # Under $500: $1.50 target (fast compounding)
    (500,   2.00),      # $500-$1K: $2 target
    (1000,  3.00),      # $1K-$5K: $3 target (current account level)
    (5000,  10.00),     # $5K-$10K: $10 target
    (10000, 25.00),     # $10K-$25K: $25 target
    (25000, 75.00),     # $25K+: $75 target
]

# Crypto-specific LOWER profit targets for fast compounding & high frequency
# Crypto trades faster, so close positions sooner to reinvest quicker
# At $1K: aim for $2-3 per trade (1-1.5% on crypto)
# At $5K: aim for $10-20 per trade
CRYPTO_PROFIT_TARGET_MILESTONES = [
    (0,     2.00),      # Under $500: $2 target (very fast compounding)
    (500,   3.00),      # $500-$1K: $3 target
    (1000,  5.00),      # $1K-$5K: $5 target (current level)
    (5000,  15.00),     # $5K-$10K: $15 target
    (10000, 50.00),     # $10K-$25K: $50 target
    (25000, 100.00),    # $25K+: $100 target
]

# Crypto-specific AGGRESSIVE tiered exits — lock wins faster, reinvest sooner
# Tier 1: Exit 50% at 50% of target (very early win lock)
# Tier 2: Exit 25% at 75% of target (partial second exit)
# Tier 3: Exit final 25% at 100% of target (close all, start fresh)
CRYPTO_TIER_LEVELS = [0.50, 0.75, 1.00]  # multipliers of crypto profit target

# Professional tiered exit levels for stocks - lock in profits at milestones, let winners run
# Tier 1: Exit 1/3 at 50% of target (lock in early win)
# Tier 2: Exit 1/3 at 100% of target (take second third)
# Tier 3: Exit final 1/3 at 150% of target (let winners run to max)
TIER_LEVELS = [0.50, 1.00, 1.50]  # multipliers of profit target


def get_profit_target_dollars(equity, is_crypto=False):
    """Get profit target based on account equity. Crypto uses lower targets for fast compounding."""
    milestones = CRYPTO_PROFIT_TARGET_MILESTONES if is_crypto else PROFIT_TARGET_DOLLARS_MILESTONES
    if equity is None:
        return milestones[0][1]
    target = milestones[0][1]
    for threshold, t in milestones:
        if equity >= threshold:
            target = t
    return target

# Track profitable days for APEX 7-day rule
profitable_days = []

# Bot lifecycle tracking for $1M goal
bot_start_time = None  # When bot started trading
bot_start_equity = None  # Starting equity when bot began
checkpoint_alerts_sent = set()  # Track which milestones we've alerted on (avoid duplicates)
daily_pnl = 0.0
daily_account_equity_start = None  # For daily 2% loss limit
open_prop_positions = {}

# 5-hour rolling profit tracking
profit_tracker = FiveHourProfitTracker()

BOT_NAME = "prop_apex"

# EQUITY FLOOR — a ratchet: once equity crosses a $1K tier, the floor locks
# to that tier and never goes back down, even across a Railway restart.
# $500 baseline until equity first reaches $1,000; then $1,000 becomes the
# floor for good; then $2,000 once reached, and so on. Breaching the floor
# closes every open position immediately and halts new entries until equity
# is back above it — same mechanism as the daily circuit breaker, just keyed
# to the account's all-time high instead of today's start.
EQUITY_FLOOR_TIER = _safe_float_env("PROP_EQUITY_FLOOR_TIER", "1000")
EQUITY_FLOOR_BASE = _safe_float_env("PROP_EQUITY_FLOOR_BASE", "500")
EQUITY_FLOOR_STATE_KEY = "prop_apex_equity_floor"
equity_floor = EQUITY_FLOOR_BASE


async def load_equity_floor():
    """Reload the ratcheted equity floor from the DB at startup, so a
    Railway restart can't reset the ladder back down to the base level."""
    global equity_floor
    try:
        from models import TradingBotState
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == EQUITY_FLOOR_STATE_KEY))
            row = result.scalar_one_or_none()
            if row and row.base_capital is not None:
                equity_floor = max(EQUITY_FLOOR_BASE, row.base_capital)
                log.info(f"[APEX_589296] 🪜 Reloaded equity floor from DB: ${equity_floor:,.0f}")
    except Exception as e:
        log.error(f"[APEX_589296] Failed to reload equity floor from DB: {e}")


async def save_equity_floor(new_floor: float):
    """Persist a raised equity floor so it survives restarts."""
    try:
        from models import TradingBotState
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == EQUITY_FLOOR_STATE_KEY))
            row = result.scalar_one_or_none()
            if row:
                row.base_capital = new_floor
            else:
                db.add(TradingBotState(bot_name=EQUITY_FLOOR_STATE_KEY, base_capital=new_floor, starting_capital=EQUITY_FLOOR_BASE))
            await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to persist equity floor: {e}")


async def load_open_positions():
    """Reload open_prop_positions from the DB once at startup, before the
    first cycle runs - otherwise a Railway restart wipes this dict while
    the position is still open for real on Alpaca, and the bot can never
    take profit or cut losses on it again (see BotPosition in models.py)."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
            rows = result.scalars().all()
            for row in rows:
                # A real production bug (confirmed live: exit-check logs
                # showing "None (LONG)" instead of a real contract code)
                # traced back to here - a BotPosition row with a missing/
                # NULL symbol gets loaded under the literal key None,
                # which can never match a FUTURES contract again, so this
                # position can be managed for exit signals but can never
                # actually be closed through the normal contract-keyed
                # path. Skip it and warn loudly instead of silently
                # corrupting open_prop_positions with an unmanageable key.
                if not row.symbol or row.symbol not in FUTURES:
                    log.error(
                        f"[APEX_589296] ⚠️ Skipping BotPosition id={row.id} with invalid symbol "
                        f"{row.symbol!r} (not a real FUTURES contract) - this position needs a "
                        f"manual look, it will not be managed automatically"
                    )
                    continue
                # Real production crash found live: BotPosition.opened_at is
                # stored via SQLAlchemy's plain DateTime column
                # (default=datetime.utcnow) - a NAIVE datetime, implicitly
                # UTC. run_prop_cycle's own `now` is timezone-AWARE
                # (datetime.now(ET)), so subtracting a naive open_time
                # reloaded here from an aware `now` after a real Railway
                # restart raised "TypeError: can't subtract offset-naive
                # and offset-aware datetimes" on every single cycle for any
                # position that survived the restart - the exit-management
                # passes never crashed outright (caught by run_prop_cycle's
                # own outer try/except), but max-hold/trailing-stop age
                # tracking for that position silently never worked.
                # Reattach UTC tzinfo to the naive value it always
                # represented, rather than leaving it naive.
                open_time = row.opened_at
                if open_time is not None and open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                open_prop_positions[row.symbol] = {"side": row.side, "entry": row.entry_price, "qty": row.qty, "open_time": open_time, "peak_pnl_pct": row.peak_pct or 0.0}
            if rows:
                log.info(f"[APEX_589296] Reloaded {len(open_prop_positions)} open position(s) from DB: {list(open_prop_positions.keys())}")
    except Exception as e:
        log.error(f"[APEX_589296] Failed to reload open positions from DB: {e}")


ALPACA_PASSIVE_MODE_KEY = "alpaca_passive_mode"


async def is_alpaca_passive_mode() -> bool:
    """True once the account owner has retired active Alpaca trading in
    favor of a real buy-and-hold SPY position (see the real dashboard
    action that closes every open position and buys SPY with the freed
    cash). Checked by both this bot's and alpaca_swing_bot.py's own main
    loops to permanently stop opening OR managing any new real trade on
    this account - a real, deliberate one-way decision, not a pause.

    DB-persisted (reusing the same generic TradingBotState bucket
    locked_usd/equity-floor state already lives in) rather than a Railway
    env var like STOP_TRADING - this avoids the exact stray-quote-character
    class of bug that silently disabled the crypto coordinator earlier
    this session (a manually-pasted env var is a real, recurring failure
    mode; a value this code sets itself isn't)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_PASSIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        return bool(row and row.base_capital and row.base_capital >= 1.0)


async def set_alpaca_passive_mode(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_PASSIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=ALPACA_PASSIVE_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


async def _db_save_open(contract: str, side: str, entry: float, qty: float):
    """Save opened position to database and log to measurement system."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=contract, side=side, entry_price=entry, qty=qty))
            await db.commit()

        # Log to measurement system
        if MEASUREMENT_AVAILABLE:
            trade_id = f"{contract}_{entry}_{datetime.now(timezone.utc).timestamp()}"  # Unique trade ID
            signal_context = SignalContext(
                symbol=contract,
                price=entry,
                trend=side,  # Use side as trend indicator
                regime="bull" if side == "long" else "bear",
            )
            trade_logger.record_entry(
                trade_id=trade_id[:16],  # Truncate to 16 chars for storage
                strategy="apex_mean_reversion",
                symbol=contract,
                entry_price=entry,
                entry_quantity=qty,
                signal_context=signal_context,
                expected_value=None,
                mode="live" if LIVE_TRADE else "paper",
            )
    except Exception as e:
        log.error(f"[APEX_589296] Failed to persist opened position {contract}: {e}")


async def _db_update_peak_pct(contract: str, peak_pnl_pct: float):
    """Persists a position's new high-water mark for its unrealized %
    return - see should_exit_position's breakeven-ratchet/peak-giveback
    rules. Without this, a Railway restart would wipe the in-memory peak
    and silently disarm both rules on exactly the positions that had run
    up the most - same reasoning as _db_save_open existing at all."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.symbol == contract))
            row = result.scalar_one_or_none()
            if row:
                row.peak_pct = peak_pnl_pct
                await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to persist peak_pct for {contract}: {e}")


async def _db_delete_open(contract: str):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.symbol == contract))
            for row in result.scalars().all():
                await db.delete(row)
            await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to remove closed position {contract} from DB: {e}")


async def _db_save_closed_trade(contract: str, side: str, entry_price: float, exit_price: float, qty: float, profit_loss: float, reason: str):
    """Record a completed trade to closed_trades table for historical audit trail and log to measurement system."""
    try:
        from models import ClosedTrade
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        async with AsyncSessionLocal() as db:
            trade = ClosedTrade(
                bot=BOT_NAME,
                symbol=contract,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                qty=qty,
                pnl=profit_loss,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                closed_at=datetime.now(timezone.utc)
            )
            db.add(trade)
            await db.commit()
            log.info(f"[AUDIT] Closed trade recorded: {contract} {side} | Entry ${entry_price:.2f} → Exit ${exit_price:.2f} | P&L: ${profit_loss:.2f}")

        # Log to measurement system
        if MEASUREMENT_AVAILABLE:
            trade_id = f"{contract}_{entry_price}"  # Simple trade ID matching entry
            trade_logger.record_exit(
                trade_id=trade_id[:16],  # Truncate to 16 chars
                exit_price=exit_price,
                exit_reason=reason,
                entry_slippage=None,
                exit_slippage=None,
            )
    except Exception as e:
        log.error(f"[APEX_589296] Failed to log closed trade {contract}: {e}")

# Latest per-symbol scan snapshot, read by routers/trading_dashboard.py's
# GET /signals so the dashboard can show live price/RSI/trend instead of
# that only being visible in Railway logs. Written once per symbol per
# cycle in run_prop_cycle - this is a live-view read model, not the source
# of truth for trading decisions (open_prop_positions is).
latest_signals = {}
last_cycle_at = None
last_market_open = None

# Email alert on real fills/exits - reuses the same GMAIL_EMAIL/GMAIL_PASSWORD
# SMTP creds routers/orders.py already uses for order emails, no new
# credentials to configure. No-ops quietly (just a log line) if they aren't
# set, same as that existing code path.
TRADE_ALERT_EMAIL = os.getenv("TRADE_ALERT_EMAIL", "delfarrell591@gmail.com")


def send_trade_alert(subject: str, body: str):
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    if not sender_email or not sender_password:
        log.info(f"(trade alert email skipped - GMAIL_EMAIL/GMAIL_PASSWORD not set) {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = TRADE_ALERT_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, TRADE_ALERT_EMAIL, msg.as_string())
        log.info(f"📧 Trade alert emailed to {TRADE_ALERT_EMAIL}")
    except Exception as e:
        log.warning(f"Trade alert email failed: {e}")


_price_rsi_last_failure = {}


async def get_price_rsi(session, symbol):
    """Get price and RSI for futures proxy symbol, including SMA50 for mean reversion validation.

    Records the specific reason for the last failure per symbol in
    _price_rsi_last_failure (HTTP status, bar count, etc.) - the automatic
    scan cycle only ever checks truthiness of the return value and doesn't
    need this, but the manual "Trade this" endpoint surfaces it in its
    error response so a real fetch failure is diagnosable from the
    dashboard itself, without needing server log access.
    """
    try:
        # feed=iex matches get_higher_tf_trend below - without an explicit
        # feed, Alpaca's default depends on the account's data
        # subscription tier, which previously made this endpoint
        # inconsistent with the (working) 1-hour trend check right below it.
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=50&feed=iex"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                try:
                    error_text = await r.text()
                except Exception:
                    error_text = "(could not read response)"
                log.warning(f"Alpaca API error for {symbol}: HTTP {r.status}: {error_text[:200]}")
                _price_rsi_last_failure[symbol] = f"Alpaca returned HTTP {r.status}: {error_text[:200]}"
                return None

            try:
                data = await r.json()
            except Exception as e:
                log.warning(f"Failed to parse JSON for {symbol}: {type(e).__name__}: {e}")
                _price_rsi_last_failure[symbol] = f"Could not parse Alpaca's response: {type(e).__name__}"
                return None

            if not isinstance(data, dict):
                log.warning(f"Invalid API response for {symbol}: expected dict, got {type(data).__name__}")
                _price_rsi_last_failure[symbol] = f"Unexpected response shape from Alpaca: {type(data).__name__}"
                return None

            bars = data.get("bars")
            if not isinstance(bars, list):
                log.warning(f"Invalid bars format for {symbol}: expected list, got {type(bars).__name__}")
                _price_rsi_last_failure[symbol] = f"Unexpected bars format from Alpaca: {type(bars).__name__}"
                return None

            # Real bug found live: this used to hard-require 50 bars before
            # returning anything at all, because sma50 needs all 50 - but
            # the 14-period RSI itself only needs 15 closes, and the entry
            # validator that called this at the time (mean-reversion's
            # validate_dual_direction, since replaced by the momentum
            # strategy's get_price_momentum/direct SMA20 check - see
            # CLAUDE.md) was already written to tolerate a missing sma50
            # via data.get("sma50", price). The practical effect: for roughly
            # the first ~4 hours of every single trading day (until 50 real
            # 5-min bars exist), get_price_rsi() returned None outright -
            # the scanner skipped every symbol and "Trade this" refused
            # every manual click with "Only N of the required 50 5-min bars
            # are available right now", real signal or real cash be damned.
            # Confirmed live: USO's own real 69.2%-win-rate backtest ranking
            # was unusable this morning purely because the session was only
            # ~2 hours old (23 of 50 bars). Lowered the hard floor to what
            # RSI actually needs; sma50 is now None below 50 real bars
            # (letting the existing downstream fallback take over, same as
            # it already did for a hard failure) rather than blocking
            # everything else on it too.
            MIN_BARS_FOR_RSI = 15
            if len(bars) < MIN_BARS_FOR_RSI:
                bar_count = len(bars)
                log.debug(f"Insufficient bars for {symbol}: got {bar_count}, need at least {MIN_BARS_FOR_RSI}")
                _price_rsi_last_failure[symbol] = f"Only {bar_count} of the required {MIN_BARS_FOR_RSI} 5-min bars are available right now"
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            sma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else price
            sma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else price
            sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
            trend = "bullish" if sma5 > sma10 else "bearish"

            # Momentum: price change over last 3 bars (shows direction/strength)
            momentum = ((price - closes[-3]) / closes[-3]) * 100 if len(closes) >= 3 and closes[-3] > 0 else 0

            _price_rsi_last_failure.pop(symbol, None)
            return {"price": price, "rsi": round(rsi, 1), "trend": trend, "momentum": round(momentum, 2), "sma50": sma50}
    except Exception as e:
        log.error(f"Price error {symbol}: {e}")
        _price_rsi_last_failure[symbol] = f"{type(e).__name__}: {e}"
        return None


MOMENTUM_RSI_ENTRY = 55.0
MOMENTUM_SMA_PERIOD = 20
MOMENTUM_TRAIL_PCT = 0.03
MOMENTUM_MAX_HOLD_SECONDS = 86400  # 24 real hours - a backstop only, not the primary exit

# Real, backtested entry-gate variants (see alpaca_selection_backtest.py's
# ENTRY_VARIANTS / run_entry_signal_ab_test) - cumulative, each level adds
# one more real filter on top of the previous. Values match exactly what
# the backtest actually tested, so promoting a variant to live can never
# run an untested combination.
ENTRY_VARIANT_LEVELS = ["A", "B", "C", "D"]
SMA_SLOPE_LOOKBACK_BARS = 4  # ~1 real hour on 15-min bars, matching alpaca_selection_backtest.py's SMA_SLOPE_LOOKBACK_BARS
MAX_EXTENSION_PCT = 0.03  # matching alpaca_selection_backtest.py's MAX_EXTENSION_PCT
ALPACA_ENTRY_VARIANT_KEY = "alpaca_entry_variant"


async def get_live_entry_variant() -> str:
    """Which of the 4 real, backtested entry-gate variants (A = today's
    original rule, B/C/D layer on RSI-rising / SMA20-rising / an
    overextension cap) the live bot currently requires. DB-persisted
    (same generic TradingBotState bucket is_alpaca_passive_mode() and
    every other real-time flag in this file already uses) rather than a
    Railway env var - avoids the exact stray-quote-character class of bug
    that silently disabled the crypto coordinator earlier this session.
    Defaults to "A" (today's live rule, unchanged) if never explicitly
    promoted - a fresh deployment never silently runs an unvalidated
    variant."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_ENTRY_VARIANT_KEY))
        row = result.scalar_one_or_none()
        if row is None or row.base_capital is None:
            return "A"
        level = int(row.base_capital)
        if 0 <= level < len(ENTRY_VARIANT_LEVELS):
            return ENTRY_VARIANT_LEVELS[level]
        return "A"


async def set_live_entry_variant(variant: str):
    if variant not in ENTRY_VARIANT_LEVELS:
        raise ValueError(f"unknown entry variant {variant!r} - must be one of {ENTRY_VARIANT_LEVELS}")
    level = float(ENTRY_VARIANT_LEVELS.index(variant))
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_ENTRY_VARIANT_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=ALPACA_ENTRY_VARIANT_KEY, base_capital=level)
            db.add(row)
        else:
            row.base_capital = level
        await db.commit()


# Real, DB-persisted choice of which strategy FAMILY is live - momentum
# (buy strength, trailing stop) or mean-reversion (buy oversold, fixed
# target/stop/breakeven/giveback). Built after a real, repeated finding:
# run_momentum_vs_mean_reversion_multi_window() (3 separate real 30-day
# windows) showed mean-reversion winning all 3, $77.51 vs momentum's
# $14.30 total - directly contradicting the single-window comparison that
# originally justified switching TO momentum. Per the account owner's
# explicit real decision from that evidence, this is a real, reversible
# toggle (same DB-not-env-var reasoning as get_live_entry_variant) rather
# than a one-way code change, since tonight already showed this same
# comparison can flip between real windows - a future re-run showing
# momentum ahead again should be just as easy to act on.
MEAN_REVERSION_RSI_ENTRY = 40.0  # matches alpaca_selection_backtest.py's RSI_LONG_THRESHOLD exactly - the real value validated 3-for-3, not the original pre-momentum live value (30)
MEAN_REVERSION_RSI_PROFIT_THRESHOLD = 60.0
MEAN_REVERSION_STOP_LOSS_PCT = 0.015
MEAN_REVERSION_PROFIT_TARGET_PCT = 0.03  # "moderate" - already the account's own prior real decision (see CLAUDE.md), reconfirmed by tonight's fresh exit-rule-sensitivity re-run
MEAN_REVERSION_GIVEBACK_PCT = 0.015
MEAN_REVERSION_BREAKEVEN_TRIGGER_PCT = 0.01
MEAN_REVERSION_MAX_HOLD_SECONDS = 7200  # matches should_exit_position()'s own default, the same one every backtest run tonight implicitly used
STRATEGY_FAMILIES = ["momentum", "mean_reversion"]
ALPACA_STRATEGY_FAMILY_KEY = "alpaca_strategy_family"


async def get_live_strategy_family() -> str:
    """Which real strategy family is currently live - "momentum" (the
    default, unchanged behavior if never explicitly switched) or
    "mean_reversion". Also re-syncs bot_mandates.APEX_MANDATE["entry"] to
    match on every call (this function is already called once per real
    prop_bot cycle) - APEX_MANDATE is a plain in-process module dict, so a
    Railway restart would otherwise silently reset it to the momentum
    default even after a real mean-reversion switch was persisted to the
    DB, leaving validate_entry()'s own mandate check out of sync with
    every other part of this file."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_STRATEGY_FAMILY_KEY))
        row = result.scalar_one_or_none()
        family = "mean_reversion" if (row is not None and row.base_capital == 1.0) else "momentum"
    APEX_MANDATE["entry"] = MEAN_REVERSION_ENTRY if family == "mean_reversion" else MOMENTUM_ENTRY
    return family


async def set_live_strategy_family(family: str):
    if family not in STRATEGY_FAMILIES:
        raise ValueError(f"unknown strategy family {family!r} - must be one of {STRATEGY_FAMILIES}")
    level = 1.0 if family == "mean_reversion" else 0.0
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_STRATEGY_FAMILY_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=ALPACA_STRATEGY_FAMILY_KEY, base_capital=level)
            db.add(row)
        else:
            row.base_capital = level
        await db.commit()
    APEX_MANDATE["entry"] = MEAN_REVERSION_ENTRY if family == "mean_reversion" else MOMENTUM_ENTRY


def check_mean_reversion_entry_gate(rsi: float):
    """The mean-reversion counterpart to check_momentum_entry_gate() -
    the ONE real place every entry path checks the mean-reversion signal,
    so the automatic scan, manual "Trade this", and the "Right now"
    dry-run can never disagree. A real oversold RSI is the only real
    condition (no trend/SMA requirement - that's a momentum-specific
    idea). Returns (passes: bool, reason: str)."""
    if rsi >= MEAN_REVERSION_RSI_ENTRY:
        return False, f"RSI {rsi:.1f} not oversold (threshold: <{MEAN_REVERSION_RSI_ENTRY:.0f})"
    return True, "oversold - real mean-reversion signal"


def check_momentum_entry_gate(data: dict, variant: str):
    """The ONE real place every entry path (the automatic Pass 2 scan,
    manual_open_prop_position's "Trade this", and alpaca_entry_eligibility's
    "Right now" dry-run in routers/trading_dashboard.py) evaluates the
    momentum entry gate - so all three can never drift out of sync with
    each other or with whichever variant is currently promoted to live.
    Returns (passes: bool, reason: str). `data` is get_price_momentum()'s
    real return dict (price/rsi/sma20/rsi_prev/sma20_prev)."""
    price = data["price"]
    rsi = data["rsi"]
    sma20 = data.get("sma20") or price

    if not (rsi > MOMENTUM_RSI_ENTRY and price > sma20):
        return False, (
            f"RSI {rsi:.1f} not above {MOMENTUM_RSI_ENTRY} or price ${price:.2f} not above its own "
            f"20-bar average ${sma20:.2f}"
        )

    if variant in ("B", "C", "D"):
        rsi_prev = data.get("rsi_prev")
        if rsi_prev is None or not (rsi > rsi_prev):
            rsi_prev_str = f"{rsi_prev:.1f}" if rsi_prev is not None else "unknown"
            return False, f"RSI {rsi:.1f} isn't rising (was {rsi_prev_str} the prior bar) - variant {variant} requires real upward RSI momentum, not just a level above {MOMENTUM_RSI_ENTRY}"

    if variant in ("C", "D"):
        sma20_prev = data.get("sma20_prev")
        if sma20_prev is None or not (sma20 > sma20_prev):
            sma20_prev_str = f"${sma20_prev:.2f}" if sma20_prev is not None else "unknown"
            return False, f"SMA20 isn't rising (now ${sma20:.2f} vs {sma20_prev_str} ~1h ago) - variant {variant} requires a real rising trend, not just RSI momentum"

    if variant == "D":
        extension_pct = (price - sma20) / sma20 if sma20 else 0.0
        if extension_pct > MAX_EXTENSION_PCT:
            return False, f"price is {extension_pct * 100:.1f}% above its own SMA20 - variant D refuses an entry already this stretched (cap {MAX_EXTENSION_PCT * 100:.0f}%)"

    return True, "OK"


async def get_price_momentum(session, symbol):
    """The live counterpart to get_price_rsi() above, but on real 15-min
    bars with a real 20-bar SMA - deliberately matching
    alpaca_selection_backtest.py's _replay_symbol_momentum() exactly
    (same timeframe, same SMA period, same RSI formula), since that's the
    real, validated evidence this live strategy is based on. get_price_rsi()'s
    5-min/SMA50 shape was built for mean-reversion and would be a
    different, unvalidated variant if reused here instead.

    Returns {"price", "rsi", "trend", "momentum", "sma20"} - same key
    names as get_price_rsi() where they overlap, so existing logging/
    display code that reads data["price"]/["rsi"]/["trend"] keeps working
    unchanged. Reuses the same _price_rsi_last_failure dict for the same
    diagnosability the dashboard's error messages already rely on."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=15Min&limit=100&feed=iex"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                try:
                    error_text = await r.text()
                except Exception:
                    error_text = "(could not read response)"
                log.warning(f"Alpaca API error for {symbol}: HTTP {r.status}: {error_text[:200]}")
                _price_rsi_last_failure[symbol] = f"Alpaca returned HTTP {r.status}: {error_text[:200]}"
                return None

            try:
                data = await r.json()
            except Exception as e:
                log.warning(f"Failed to parse JSON for {symbol}: {type(e).__name__}: {e}")
                _price_rsi_last_failure[symbol] = f"Could not parse Alpaca's response: {type(e).__name__}"
                return None

            bars = data.get("bars")
            if not isinstance(bars, list):
                log.warning(f"Invalid bars format for {symbol}: expected list, got {type(bars).__name__}")
                _price_rsi_last_failure[symbol] = f"Unexpected bars format from Alpaca: {type(bars).__name__}"
                return None

            MIN_BARS = MOMENTUM_SMA_PERIOD + 1
            if len(bars) < MIN_BARS:
                bar_count = len(bars)
                log.debug(f"Insufficient 15-min bars for {symbol}: got {bar_count}, need at least {MIN_BARS}")
                _price_rsi_last_failure[symbol] = f"Only {bar_count} of the required {MIN_BARS} 15-min bars are available right now"
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            def _rsi_of(cs, period=14):
                if len(cs) < period + 1:
                    return None
                gains = [max(cs[i] - cs[i - 1], 0) for i in range(1, len(cs))]
                losses = [max(cs[i - 1] - cs[i], 0) for i in range(1, len(cs))]
                avg_gain = sum(gains[-period:]) / period
                avg_loss = sum(losses[-period:]) / period
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                return 100 - (100 / (1 + rs))

            rsi = _rsi_of(closes)
            # RSI/SMA one real bar (rsi_prev) / SMA_SLOPE_LOOKBACK_BARS bars
            # (sma20_prev) back - only ever consulted by
            # check_momentum_entry_gate() when the live variant actually
            # requires "rising" confirmation (B/C/D); None-safe otherwise so
            # variant A's behavior is completely unchanged.
            rsi_prev = _rsi_of(closes[:-1]) if len(closes) > 1 else None

            sma20 = sum(closes[-MOMENTUM_SMA_PERIOD:]) / MOMENTUM_SMA_PERIOD
            sma20_prev = None
            if len(closes) >= MOMENTUM_SMA_PERIOD + SMA_SLOPE_LOOKBACK_BARS:
                prev_closes = closes[:-SMA_SLOPE_LOOKBACK_BARS]
                sma20_prev = sum(prev_closes[-MOMENTUM_SMA_PERIOD:]) / MOMENTUM_SMA_PERIOD

            trend = "bullish" if price > sma20 else "bearish"
            momentum = ((price - closes[-3]) / closes[-3]) * 100 if len(closes) >= 3 and closes[-3] > 0 else 0

            _price_rsi_last_failure.pop(symbol, None)
            return {
                "price": price, "rsi": round(rsi, 1) if rsi is not None else None,
                "trend": trend, "momentum": round(momentum, 2), "sma20": sma20,
                "rsi_prev": round(rsi_prev, 1) if rsi_prev is not None else None,
                "sma20_prev": sma20_prev,
            }
    except Exception as e:
        log.error(f"Momentum price error {symbol}: {e}")
        _price_rsi_last_failure[symbol] = f"{type(e).__name__}: {e}"
        return None


async def get_higher_tf_trend(session, symbol):
    """1-hour timeframe trend, used ONLY as a confirmation filter on new
    entries (see run_prop_cycle's Pass 2) - never on exits, so an
    already-open position is never held or closed differently because
    of this. A 5-minute RSI dip against a strong 1-hour downtrend is a
    much weaker signal than the same dip with the 1-hour trend flat or
    favorable - this is the "don't fight the higher timeframe" idea,
    simplified from a full multi-timeframe confluence system down to a
    single confirming check, since a 3-tier confidence-scoring system
    (as reviewed elsewhere) is more machinery than a 7-symbol watchlist
    needs. Returns "UP"/"DOWN"/"SIDEWAYS"/"UNKNOWN" - UNKNOWN on any
    fetch failure so a data hiccup never blocks a trade outright, only
    a genuinely confirmed opposing trend does."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Hour&limit=50&feed=iex"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                return "UNKNOWN"
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 50:
                return "UNKNOWN"

            closes = [b["c"] for b in bars]
            sma20 = sum(closes[-20:]) / 20
            sma50 = sum(closes[-50:]) / 50
            if sma50 == 0:
                return "UNKNOWN"

            diff_pct = (sma20 - sma50) / sma50
            if diff_pct > 0.015:
                return "UP"
            if diff_pct < -0.015:
                return "DOWN"
            return "SIDEWAYS"
    except Exception as e:
        log.warning(f"Could not fetch 1H trend for {symbol}: {e}")
        return "UNKNOWN"


async def get_account_equity(session):
    """Real Alpaca account equity, used to scale the profit-target
    increment (see PROFIT_INCREMENT_MILESTONES). Falls back to None (base
    tier) on any failure - a scaling hiccup shouldn't block trading."""
    try:
        url = f"{get_base_url()}/v2/account"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                try:
                    error_text = await r.text()
                except:
                    error_text = "(could not read response)"
                log.warning(f"Alpaca /v2/account returned HTTP {r.status}: {error_text[:200]}")
                return None
            data = await r.json()
            return float(data.get("equity", 0))
    except Exception as e:
        log.warning(f"Could not fetch account equity for profit-target scaling: {e}")
        return None


async def get_account_cash(session):
    """Real Alpaca cash balance, used to size new positions in dollars
    rather than a fixed share count (see size_position). Falls back to
    None on any failure - callers fall back to the fixed 1-share size."""
    try:
        url = f"{get_base_url()}/v2/account"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                try:
                    error_text = await r.text()
                except:
                    error_text = "(could not read response)"
                log.warning(f"Alpaca /v2/account (cash) returned HTTP {r.status}: {error_text[:200]}")
                return None
            data = await r.json()
            return float(data.get("cash", 0))
    except Exception as e:
        log.warning(f"Could not fetch account cash for position sizing: {e}")
        return None


async def get_account_buying_power(session):
    """Real Alpaca buying power. Returns buying power or None on failure.
    Used for hard margin safety checks to prevent over-leverage."""
    try:
        url = f"{get_base_url()}/v2/account"
        async with session.get(url, headers=get_headers()) as r:
            if r.status != 200:
                try:
                    error_text = await r.text()
                except:
                    error_text = "(could not read response)"
                log.warning(f"Alpaca /v2/account (buying_power) returned HTTP {r.status}: {error_text[:200]}")
                return None
            data = await r.json()
            bp = float(data.get("buying_power", 0))
            return bp
    except Exception as e:
        log.warning(f"Could not fetch buying power for margin safety: {e}")
        return None


async def get_account_shorting_enabled(session):
    """Real Alpaca account.shorting_enabled flag. Discovered in production
    that every single short attempt was failing with "account is not
    allowed to short" - a real Alpaca account-level restriction (commonly
    tied to margin/equity requirements, not something a code fix can
    override) rather than a bug in the order-placement code. Checking this
    upfront means new short entries are skipped cleanly (with one clear
    log line) instead of repeatedly attempting - and failing - orders
    every cycle. Falls back to True (attempt as before) on fetch failure,
    so a transient API hiccup doesn't silently disable a feature that may
    actually be working."""
    try:
        async with session.get(f"{get_base_url()}/v2/account", headers=get_headers()) as r:
            if r.status != 200:
                return True
            data = await r.json()
            return bool(data.get("shorting_enabled", True))
    except Exception as e:
        log.warning(f"Could not fetch account shorting_enabled status: {e}")
        return True


# Reverse of FUTURES: proxy ETF symbol -> contract code, so a real Alpaca
# position can be matched back to the contract key open_prop_positions
# tracks by.
_SYMBOL_TO_CONTRACT = {config["symbol"]: contract for contract, config in FUTURES.items()}


# Per the account owner's explicit request, mirroring the crypto family
# tree's own two-layer coin exclusion (see crypto_family_tree_bot.py):
# the real alpaca_selection_backtest.py results shown on the dashboard
# already exist, but nothing previously READ them automatically - a
# symbol could sit at deeply negative real backtested ROI (e.g. the
# inverse ETFs PSQ/SH/RWM/DOG on a real 30-day window where the market
# didn't actually fall) and prop_bot.py would still be willing to enter
# it on the next RSI-oversold signal. This closes that gap: the
# coordinator re-runs the real backtest on its own every
# AUTO_BACKTEST_INTERVAL_SECONDS and auto-excludes a symbol once its
# last AUTO_EXCLUDE_RUN_WINDOW real runs were ALL negative-ROI,
# un-excluding it the instant its most recent run turns positive again -
# contestable/self-healing, never a one-way verdict, same philosophy as
# the crypto side. Requiring several consecutive bad runs (not one) is
# deliberate, same reasoning as the crypto side: a single 30-day window
# is noisy enough that one bad run alone shouldn't blacklist a symbol.
AUTO_BACKTEST_INTERVAL_SECONDS = _safe_int_env("PROP_AUTO_BACKTEST_INTERVAL_SECONDS", str(24 * 60 * 60))
AUTO_EXCLUDE_RUN_WINDOW = _safe_int_env("PROP_AUTO_EXCLUDE_RUN_WINDOW", "3")

_last_auto_backtest_at = 0.0

# Per the account owner's explicit request, mirroring the crypto family
# tree's own top-N coin rotation (see TOP_N_ELIGIBLE_COINS in
# crypto_family_tree_bot.py): rather than spread capital evenly across
# every symbol in the universe (a real, strong performer like USO - 74.2%
# win rate, +19.4% ROI in one real run - diluted by weak ones like DOG or
# RWM), concentrate new entries on only the top TOP_N_ELIGIBLE_SYMBOLS
# symbols by latest real backtested ROI. 5 of 11 (roughly half) is the
# default - a smaller, tighter universe than crypto's 15-of-37, matching
# how much smaller this real universe is to begin with.
TOP_N_ELIGIBLE_SYMBOLS = _safe_int_env("PROP_TOP_N_SYMBOLS", "5")


async def _compute_top_ranked_symbols():
    """Real, live ranking by latest AlpacaBacktestRun.roi_pct_of_spend per
    symbol - the exact same real backtest data get_effective_excluded_
    symbols() and the dashboard's own table already read, not a new or
    separately-computed number. Returns the set of the top
    TOP_N_ELIGIBLE_SYMBOLS symbols by ROI, or None if fewer than
    TOP_N_ELIGIBLE_SYMBOLS symbols have ANY real backtest run yet - a
    deliberate cold-start guard, same reasoning as the crypto side's
    _compute_top_ranked_coins(): with too little real evidence to
    meaningfully fill a top-N cut, get_effective_excluded_symbols() skips
    this filter entirely rather than accidentally excluding most of the
    real universe because most symbols still show as "unranked". One
    query regardless of universe size (ordered by run_at descending,
    first row per product_id kept) rather than one query per symbol."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AlpacaBacktestRun.product_id, AlpacaBacktestRun.roi_pct_of_spend)
            .order_by(AlpacaBacktestRun.product_id, desc(AlpacaBacktestRun.run_at))
        )
        rows = result.all()
    latest_roi = {}
    for product_id, roi in rows:
        if product_id not in latest_roi:
            latest_roi[product_id] = roi
    if len(latest_roi) < TOP_N_ELIGIBLE_SYMBOLS:
        return None
    ranked = sorted(latest_roi.items(), key=lambda kv: kv[1], reverse=True)
    return {product_id for product_id, _roi in ranked[:TOP_N_ELIGIBLE_SYMBOLS]}


async def get_effective_excluded_symbols() -> set:
    """Real tickers (e.g. "PSQ", not a contract code) currently excluded
    from new entries - both the automatic path (try_open's MANDATE CHECK)
    and the manual "Trade this" endpoint check this before acting. A
    symbol with fewer than AUTO_EXCLUDE_RUN_WINDOW real backtest runs on
    record is never excluded by the auto-exclusion layer - there isn't
    enough evidence yet.

    Unions two real layers: the existing auto-exclusion (a symbol whose
    last AUTO_EXCLUDE_RUN_WINDOW runs were ALL negative-ROI) and the
    top-N concentration filter (see _compute_top_ranked_symbols) - a
    symbol outside the current top TOP_N_ELIGIBLE_SYMBOLS by real ROI is
    excluded here too, once enough real evidence exists to rank the whole
    universe. Neither layer force-closes an existing position - both only
    ever stop NEW entries, same "never one-way, never touches what's
    already open" philosophy as every exclusion layer on the crypto side."""
    excluded = set()
    async with AsyncSessionLocal() as db:
        for symbol in {config["symbol"] for config in FUTURES.values()}:
            result = await db.execute(
                select(AlpacaBacktestRun.roi_pct_of_spend)
                .where(AlpacaBacktestRun.product_id == symbol)
                .order_by(desc(AlpacaBacktestRun.run_at))
                .limit(AUTO_EXCLUDE_RUN_WINDOW)
            )
            recent = result.scalars().all()
            if len(recent) >= AUTO_EXCLUDE_RUN_WINDOW and all(roi < 0 for roi in recent):
                excluded.add(symbol)

    top_ranked = await _compute_top_ranked_symbols()
    if top_ranked is not None:
        for symbol in {config["symbol"] for config in FUTURES.values()}:
            if symbol not in top_ranked:
                excluded.add(symbol)

    return excluded


async def describe_symbol_exclusion_reason(symbol: str) -> str:
    """The real, specific reason a symbol is currently in
    get_effective_excluded_symbols()'s set - which of the two real layers
    (negative-ROI auto-exclusion, or outside the top-N ROI ranking)
    actually applies - so the dashboard can show an accurate reason
    instead of always assuming it's the auto-exclusion layer. Only
    meaningful to call on a symbol already confirmed excluded; returns a
    generic fallback otherwise."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AlpacaBacktestRun.roi_pct_of_spend)
            .where(AlpacaBacktestRun.product_id == symbol)
            .order_by(desc(AlpacaBacktestRun.run_at))
            .limit(AUTO_EXCLUDE_RUN_WINDOW)
        )
        recent = result.scalars().all()
    if len(recent) >= AUTO_EXCLUDE_RUN_WINDOW and all(roi < 0 for roi in recent):
        return f"last {AUTO_EXCLUDE_RUN_WINDOW} real backtest runs were all negative ROI"
    top_ranked = await _compute_top_ranked_symbols()
    if top_ranked is not None and symbol not in top_ranked:
        return f"outside the current top {TOP_N_ELIGIBLE_SYMBOLS} symbols by real backtested ROI"
    return "currently excluded"


async def _run_scheduled_backtest_and_update_exclusions():
    """Called from run_prop_cycle(), throttled to once per
    AUTO_BACKTEST_INTERVAL_SECONDS. Runs the exact same real backtest the
    manual dashboard button triggers, persists every symbol's result, then
    logs the resulting auto-excluded set so it's visible in the real
    deploy logs without needing the dashboard."""
    try:
        # Deferred import - alpaca_selection_backtest.py imports FUTURES
        # etc. from this module, so importing it at the top of this file
        # would be a circular import. By the time this function actually
        # runs, both modules are already fully loaded.
        import alpaca_selection_backtest
    except Exception as e:
        log.warning(f"[APEX_589296] scheduled backtest skipped - alpaca_selection_backtest not available ({e})")
        return
    log.info("[APEX_589296] 🔄 running the scheduled real symbol-selection backtest...")
    try:
        output = await alpaca_selection_backtest.run_full_backtest()
    except Exception as e:
        log.warning(f"[APEX_589296] scheduled backtest failed: {e}")
        return
    async with AsyncSessionLocal() as db:
        for r in output["ranked"]:
            db.add(AlpacaBacktestRun(
                product_id=r["product_id"], num_trades=r["num_trades"],
                win_rate=r["win_rate"], roi_pct_of_spend=r["roi_pct_of_spend"],
            ))
        await db.commit()
    auto_excluded = await get_effective_excluded_symbols()
    log.info(
        f"[APEX_589296] 🔄 scheduled backtest done - {output['coins_with_results']} symbols scored. "
        f"Auto-excluded (last {AUTO_EXCLUDE_RUN_WINDOW} runs all negative): "
        f"{sorted(auto_excluded) if auto_excluded else 'none'}"
    )


async def reconcile_positions_with_broker(session):
    """Confirmed in production: a real Alpaca position (USO/MCL) sat at
    -4.9% - more than double STOP_LOSS_PCT - completely unmanaged, because
    it was opened before the BotPosition table existed (pre position-
    persistence fix) and so was never in the DB for load_open_positions()
    to reload. The bot's own state said that slot was empty (it kept
    trying to open a fresh MCL entry) while a real, real-money position
    sat on the broker losing more than the stop-loss should ever allow -
    the stop-loss protection is worthless if the bot doesn't know a
    position exists to apply it to.

    Runs every cycle (one extra cheap GET, not just at startup) so any
    future desync - from any cause, not just this specific historical
    bug - self-heals within one cycle instead of persisting indefinitely:
    - A real position on a tracked symbol that open_prop_positions doesn't
      know about gets ADOPTED (entry price taken from Alpaca's own
      avg_entry_price, so the stop-loss/profit-target math is correct
      from the moment it's adopted) and persisted to the DB.
    - A tracked position whose real Alpaca position has vanished (closed
      manually, liquidated, stopped out some other way) gets DROPPED from
      tracking so the bot doesn't keep thinking it holds something it
      doesn't.

    Only ever touches contracts in FUTURES/_SYMBOL_TO_CONTRACT - a real
    position on a symbol this bot doesn't trade (e.g. a manual purchase
    unrelated to this bot) is left completely alone, adopted or not."""
    try:
        async with session.get(f"{get_base_url()}/v2/positions", headers=get_headers()) as r:
            if r.status != 200:
                return
            broker_positions = await r.json()
    except Exception as e:
        log.warning(f"[APEX_589296] Could not fetch broker positions for reconciliation: {e}")
        return

    broker_by_contract = {}
    for p in broker_positions:
        contract = _SYMBOL_TO_CONTRACT.get(p.get("symbol"))
        if contract:
            broker_by_contract[contract] = p

    for contract, p in broker_by_contract.items():
        if contract in open_prop_positions:
            continue
        qty = float(p.get("qty", 0))
        if qty == 0:
            continue
        side = "long" if qty > 0 else "short"
        entry = float(p.get("avg_entry_price", 0))
        open_prop_positions[contract] = {"side": side, "entry": entry, "qty": abs(qty)}
        await _db_save_open(contract, side, entry, abs(qty))
        log.warning(f"[APEX_589296] 🔧 Adopted orphaned {side} {contract} position found on Alpaca but not tracked (entry ${entry:.2f}, qty {abs(qty)}) - stop-loss/profit-target now apply to it")

    for contract in list(open_prop_positions.keys()):
        if contract not in broker_by_contract:
            log.warning(f"[APEX_589296] 🔧 Tracked {contract} position no longer exists on Alpaca (closed outside the bot) - dropping from tracking")
            open_prop_positions.pop(contract, None)
            await _db_delete_open(contract)


# Floor on a single position's dollar size. Below this, a position is too
# small to bother with (order fees/slippage would dominate) - skip the
# entry rather than place a near-zero fractional order.
# Micro-account ($978): $50 minimum allows actual execution while maintaining profitability
# $50 trade × 1-2% move = $0.50-$1.00 profit (after $0.05 fees = $0.45-0.95 net)
MIN_POSITION_NOTIONAL = _safe_float_env("PROP_MIN_POSITION_NOTIONAL", "50")  # Reduced from $1500 for micro account

# HARD MARGIN SAFETY LIMITS — prevent over-leverage ever again
# Minimum buying power buffer required before opening ANY new position
# For $980 account: $150 buffer = 15% locked, allows ~$830 deployable
# This is still conservative (don't deploy 100%), but allows actual trading
MIN_BUYING_POWER_BUFFER = _safe_float_env("PROP_MIN_BUYING_POWER_BUFFER", "150")

# Maximum percentage of account equity that can be at risk in open positions
# Lowered from 50% to 20% for micro-account safety - 50% risk-at-once was
# sized for a much larger evaluation account, not a ~$1K live account.
MAX_RISK_PERCENT = _safe_float_env("PROP_MAX_RISK_PERCENT", "0.20")  # 20% max

# Buying power threshold to STOP opening new positions (emergency brake)
CRITICAL_BUYING_POWER_THRESHOLD = _safe_float_env("PROP_CRITICAL_BP_THRESHOLD", "100")


def size_position(cash_remaining, slots_remaining, price, account_equity=None, already_open_notional=0.0):
    """Dollar-based (fractional-share) position sizing with AGGRESSIVE COMPOUNDING.

    Position size scales with account growth:
    - Under $5K: 20% of remaining cash per slot
    - $5K-$10K: 25% of remaining cash per slot
    - $10K-$25K: 30% of remaining cash per slot
    - $25K+: 35-40% of remaining cash per slot

    Plus POSITION_SCALE_MULTIPLIER from env var (milestone-based scaling).
    This enables exponential compounding as capital grows.

    `already_open_notional` (real dollar value already held across every
    other open position) clamps the result to whatever real room is
    actually left under `account_equity * MAX_RISK_PERCENT` - the exact
    same real total-risk ceiling check_margin_safety() enforces
    afterward. Real, confirmed-live bug this closes: this function's own
    sizing was never coordinated with that ceiling at all, so a single
    real entry could size itself well past the ENTIRE total-risk budget
    on its own (confirmed against real account numbers: one real position
    sized at ~54% of account equity, more than 2.5x the 20% total cap) -
    permanently maxing out the real risk budget on one or two oversized
    positions and leaving check_margin_safety blocking every later real
    signal from that point on, which is exactly how real account growth
    can stall for weeks even while individual trades are fine. Clamped
    here so a position is never even SIZED past the real room actually
    left, given what's already open - the account naturally spreads
    across more, smaller real positions (as dynamic_max_positions already
    intends) instead of a couple of oversized ones exhausting the whole
    real budget up front. This never raises the real risk ceiling itself
    - only makes new entries respect the existing one from the moment
    they're sized, not just get rejected after the fact.
    `already_open_notional=0.0` (every existing caller that doesn't pass
    it) still applies the real ceiling to a single new position - a
    tighter, but strictly safer, real behavior than before this fix,
    never a looser one."""
    if slots_remaining <= 0 or cash_remaining < MIN_POSITION_NOTIONAL:
        return None

    # Determine allocation percentage based on account equity
    if account_equity is None:
        account_equity = cash_remaining

    if account_equity < 5000:
        allocation_pct = 0.20  # 20% of remaining cash
    elif account_equity < 10000:
        allocation_pct = 0.25  # 25% of remaining cash
    elif account_equity < 25000:
        allocation_pct = 0.30  # 30% of remaining cash
    else:
        allocation_pct = 0.35  # 35% of remaining cash at scale

    # Calculate position size: (cash / slots) with aggressive % allocation
    amount = (cash_remaining / slots_remaining) * (1.0 + (allocation_pct - 0.15))  # Base 15% + aggressive boost
    amount = max(amount, MIN_POSITION_NOTIONAL)
    amount = min(amount, cash_remaining * 0.4)  # Cap at 40% to maintain safety

    # Apply additional scaling multiplier (increases after milestone locks)
    scale = _safe_float_env("POSITION_SCALE_MULTIPLIER", "1.0")
    amount = amount * scale  # SCALE UP: bigger positions after milestone

    # Real, hard backstop - applied LAST, after the scale multiplier, so no
    # combination of allocation tier + scale can ever size a position past
    # the real total-risk room actually left. See the docstring above.
    risk_room = max(0.0, account_equity * MAX_RISK_PERCENT - already_open_notional)
    amount = min(amount, risk_room)

    qty = round(amount / price, 6)
    return qty if qty > 0 else None


def check_margin_safety(buying_power, equity, open_positions_count, extra_open_notional=0.0):
    """Hard check: is it safe to open a new position?
    Returns (is_safe, reason_if_not)

    extra_open_notional: real notional value held OUTSIDE open_prop_positions
    that also needs counting against the same real account-wide risk cap -
    added for the Alpaca branch system (see run_alpaca_branch_cycle), whose
    positions live in a separate open_alpaca_branch_positions dict so they
    were previously invisible to this check entirely, letting the real
    total risk across the whole account exceed MAX_RISK_PERCENT without
    this function ever seeing it. Defaults to 0.0 so the existing
    prop_apex-only call site (open_prop_positions alone) is byte-for-byte
    unchanged."""
    # Buying power must be positive with minimum buffer
    if buying_power < MIN_BUYING_POWER_BUFFER:
        return False, f"Insufficient buying power: ${buying_power:.2f} < ${MIN_BUYING_POWER_BUFFER:.2f} buffer"

    # Emergency brake: if buying power drops near zero, stop ALL new positions
    if buying_power < CRITICAL_BUYING_POWER_THRESHOLD:
        return False, f"CRITICAL: Buying power ${buying_power:.2f} near zero — halting new positions"

    # Total open position risk can't exceed max % of equity
    total_open_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in open_prop_positions.values()) + extra_open_notional
    if equity > 0 and total_open_notional > (equity * MAX_RISK_PERCENT):
        return False, f"Risk limit exceeded: ${total_open_notional:.2f} > {MAX_RISK_PERCENT*100:.0f}% of ${equity:.2f} equity"

    return True, "OK"


async def broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss=None, target=None):
    """Broadcast signal to all trading signal subscribers via API."""
    try:
        api_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        signal_data = {
            "contract": contract,
            "action": action,
            "entry_price": price,
            "stop_loss": stop_loss or (price * 0.98),  # 2% stop loss if not specified
            "target_price": target or (price * 1.03 if action == "BUY" else price * 0.97),  # 3% target
            "rsi": rsi,
            "trend": trend,
            "confidence": 0.85,
        }

        async with session.post(f"{api_url}/trading/signals/broadcast", json=signal_data) as r:
            if r.status == 200:
                result = await r.json()
                log.info(f"📡 Signal broadcast complete: {result.get('subscribers_notified', 0)} subscribers notified")
                return True
            else:
                log.warning(f"Signal broadcast failed: {r.status}")
                return False
    except Exception as e:
        log.warning(f"Could not broadcast signal: {e}")
        return False


async def execute_futures_trade(session, contract, action, qty, price, rsi, trend, stop_loss=None, target=None):
    """Place a real order via Alpaca. `action` is the literal order side
    ("BUY" or "SELL") - what that *means* (open a long, open a short, close
    a long, cover a short) depends on the caller's position state, tracked
    in run_prop_cycle, not here. This function just places the order,
    broadcasts the signal, and reports success/failure - it doesn't touch
    open_prop_positions or send fill emails, since a single fill can mean
    different things (new entry vs. exit) that the caller knows and this
    function doesn't."""
    symbol = FUTURES[contract]["symbol"]
    side = "buy" if action == "BUY" else "sell"

    # Use GTC (Good Till Canceled) for profit-taking sells to let orders persist until profit target hits
    # Use DAY for entry buys to avoid holding stale orders overnight
    time_in_force = "gtc" if action == "SELL" else "day"

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
    }

    mode = "LIVE" if LIVE_TRADE else "PAPER"

    try:
        async with session.post(f"{get_base_url()}/v2/orders", headers=get_headers(), json=order) as r:
            result = await r.json()
            if r.status in (200, 201):
                log.info(f"✅ FUTURES TRADE | {mode} | {action} {qty} {contract} ({symbol}) @ ${price:.2f} | APEX_589296")
                await broadcast_signal_to_subscribers(session, contract, action, price, rsi, trend, stop_loss, target)
                return True
            else:
                log.error(f"❌ Futures order failed: {result.get('message', result)}")
                return False
    except Exception as e:
        log.error(f"Futures trade error: {e}")
        return False


def check_kill_conditions(buying_power, equity, daily_loss, open_position_count):
    """Check if any kill conditions have been triggered. Return (should_halt, reason)"""
    mandate = APEX_MANDATE
    capital = mandate["capital"]

    # Kill condition 1: Daily loss limit hit
    if daily_loss < -capital["max_daily_loss"]:
        return True, f"Daily loss limit hit: ${daily_loss:.2f} <= -${capital['max_daily_loss']}"

    # Kill condition 2: Buying power below critical threshold
    if buying_power < capital["critical_buying_power"]:
        return True, f"Buying power critical: ${buying_power:.2f} < ${capital['critical_buying_power']}"

    # Kill condition 3: Equity fallen below survival level (80% of starting)
    if equity < 800:
        return True, f"Equity below survival level: ${equity:.2f} < $800"

    # Kill condition 4: Too many open positions (shouldn't happen, but check)
    if open_position_count > capital["max_open_positions"]:
        return True, f"Too many open positions: {open_position_count} > {capital['max_open_positions']}"

    return False, None

async def run_prop_cycle():
    global daily_pnl, profitable_days, last_cycle_at, last_market_open, bot_start_time, bot_start_equity, checkpoint_alerts_sent

    # 24/7 Trading: Scan crypto, commodities, and indices across all hours
    # Alpaca supports round-the-clock trading on crypto and extended hours on commodities
    now = datetime.now(ET)
    last_cycle_at = now.isoformat()

    # CONTINUOUS AUTO-SCALING TO $1,000,000 — No milestones, just compound
    # Scale formula: 1.0x baseline + 0.01x per $1000 earned, capped at 5.0x
    # $1K equity → 1.0x, $100K equity → 2.0x, $400K+ equity → 5.0x (capped)
    connector = aiohttp.TCPConnector(use_dns_cache=True, limit=20, limit_per_host=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
        equity = await get_account_equity(session)

        # MANDATE: Check kill conditions before trading
        if equity is not None:
            buying_power = await get_account_buying_power(session)
            should_halt, halt_reason = check_kill_conditions(
                buying_power=buying_power,
                equity=equity,
                daily_loss=daily_pnl,
                open_position_count=len(open_prop_positions)
            )
            if should_halt:
                log.critical(f"[KILL CONDITION] Halting bot: {halt_reason}")
                return

        global _last_auto_backtest_at
        now_ts = time.time()
        if now_ts - _last_auto_backtest_at >= AUTO_BACKTEST_INTERVAL_SECONDS:
            _last_auto_backtest_at = now_ts
            try:
                await _run_scheduled_backtest_and_update_exclusions()
            except Exception as e:
                log.warning(f"[APEX_589296] scheduled backtest/exclusion update failed: {e}")

        if equity is not None:
            # AGGRESSIVE GROWTH STRATEGY: Let account compound until real milestone hit
            # OLD: $1000.15 threshold kept triggering at $1004.77, closing $10, dropping to $994, looping infinitely
            # NEW: Wait for 50% growth milestone ($1,500+) so profit-taking captures real gains, not noise
            ALPACA_FLOOR = 700.00  # If drops below $700, switch to strict preservation mode
            ALPACA_PROFIT_ACTIVATION = 1500.00  # Only take profits after 50%+ growth (avoid infinite loop)
            is_alpaca_at_floor = equity <= ALPACA_FLOOR
            should_alpaca_take_profits = equity >= ALPACA_PROFIT_ACTIVATION

            if is_alpaca_at_floor:
                log.info(f"🚀 ALPACA SURVIVAL MODE: ${equity:.2f} ≤ ${ALPACA_FLOOR:.2f} | Protecting capital, minimal risk trades only")
            if should_alpaca_take_profits:
                log.info(f"💰 ALPACA MILESTONE HIT: ${equity:.2f} ≥ ${ALPACA_PROFIT_ACTIVATION:.2f} | Taking profits on top 10 winners")
                # Actually close the most profitable positions to lock in gains
                profit_positions = []
                for contract, pos in open_prop_positions.items():
                    if pos.get("pnl", 0) > 0:  # Only profitable positions
                        profit_positions.append((contract, pos, pos.get("pnl", 0)))

                # Sort by profit (highest first) and close top 10
                profit_positions.sort(key=lambda x: x[2], reverse=True)
                trades_closed = 0
                for contract, pos, pnl in profit_positions[:10]:  # Top 10 most profitable
                    try:
                        # Get current price
                        try:
                            price_resp = await session.get(f"{get_base_url()}/v1/last?symbols={pos.get('symbol', contract)}", headers=get_headers())
                            if price_resp.status == 200:
                                data = await price_resp.json()
                                price = data.get("last", {}).get("price", pos["entry"])
                            else:
                                price = pos["entry"]
                        except:
                            price = pos["entry"]

                        # Close position and realize profit
                        await close_position(session, contract, FUTURES.get(contract, {}), pos, price, 0, "profit_taking", f"PROFIT LOCK: +${pnl:.2f}")
                        trades_closed += 1

                        # Update cash_remaining to reflect realized profit
                        if cash_remaining is not None:
                            cash_remaining += pnl

                    except Exception as e:
                        log.error(f"[APEX_589296] Failed to close profitable position {contract}: {e}")

                if trades_closed > 0:
                    log.info(f"✅ PROFIT LOCK EXECUTED: Closed {trades_closed} positions, cash_remaining: ${cash_remaining:.2f}")

            # Check if $1M goal achieved — STOP TRADING
            if equity >= 1000000.0:
                log.warning(f"🏆 **$1,000,000 MILESTONE REACHED** — Equity: ${equity:,.2f}")
                # Close all positions and stop
                for contract in list(open_prop_positions.keys()):
                    pos = open_prop_positions[contract]
                    try:
                        price_resp = await session.get(f"{get_base_url()}/v1/last?symbols={pos.get('symbol', contract)}", headers=get_headers())
                        if price_resp.status == 200:
                            data = await price_resp.json()
                            price = data.get("last", {}).get("price", pos["entry"])
                        else:
                            price = pos["entry"]
                    except:
                        price = pos["entry"]
                    await close_position(session, contract, FUTURES.get(contract, {}), pos, price, 0, "milestone", "PROFIT LOCK - $1M REACHED")

                send_trade_alert(
                    "🏆 EMPIRE BOT — $1,000,000 ACHIEVED!",
                    f"**ULTIMATE GOAL UNLOCKED**\n\n"
                    f"Account Equity: ${equity:,.2f}\n"
                    f"🎉 ONE MILLION DOLLARS!\n\n"
                    f"Daily P&L: ${daily_pnl:.2f}\n"
                    f"Status: TRADING STOPPED. All positions closed.\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
                )
                return  # STOP — goal achieved

            # Auto-scale formula: increase scale as equity grows
            # 1.0x at $1K, 1.5x at $50K, 2.0x at $100K, 5.0x at $400K+
            def get_auto_scale(eq: float) -> str:
                scale = 1.0 + (eq / 100000.0)  # +0.01x per $1K earned
                scale = min(scale, 5.0)  # Cap at 5.0x max
                return f"{scale:.2f}"

            current_scale = float(get_auto_scale(equity))
            os.environ["POSITION_SCALE_MULTIPLIER"] = str(current_scale)

            # Initialize bot lifecycle tracking when equity first hits $1K
            if equity >= 1000.0 and bot_start_time is None:
                bot_start_time = now
                bot_start_equity = equity
                log.info(f"🚀 BOT COMPOUNDING START: ${equity:,.2f} | Tracking $1M goal (120-day timeout)")

            # 120-DAY SAFETY TIMEOUT - Professional guardrail
            if bot_start_time is not None:
                elapsed_days = (now - bot_start_time).total_seconds() / 86400
                if elapsed_days > 120 and equity < 1000000.0:
                    log.error(f"⏰ 120-DAY TIMEOUT REACHED | Elapsed: {elapsed_days:.1f} days | Equity: ${equity:,.2f}")
                    send_trade_alert(
                        "⏰ BOT SAFETY TIMEOUT — 120 DAYS REACHED",
                        f"**SAFETY TIMEOUT TRIGGERED**\n\n"
                        f"Elapsed: {elapsed_days:.1f} days\n"
                        f"Target: $1,000,000\n"
                        f"Achieved: ${equity:,.2f}\n\n"
                        f"Trading stopped per safety protocol.\n"
                        f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
                    )
                    return  # STOP — timeout reached. Positions handled naturally by exit logic

            # CHECKPOINT ALERTS — Monitor progress to $1M (alert once per milestone)
            if bot_start_time is not None and bot_start_equity is not None:
                for milestone in [10000, 50000, 100000]:
                    if equity >= milestone and milestone not in checkpoint_alerts_sent:
                        progress = equity - bot_start_equity
                        checkpoint_alerts_sent.add(milestone)
                        log.info(f"✅ MILESTONE UNLOCKED: ${equity:,.0f} | Profit: ${progress:,.0f} | Scale: {current_scale:.2f}x")
                        send_trade_alert(
                            f"🎯 MILESTONE CHECKPOINT — ${milestone:,}",
                            f"**EQUITY MILESTONE REACHED**\n\n"
                            f"Current: ${equity:,.2f}\n"
                            f"Profit: ${progress:,.2f}\n"
                            f"Scale: {current_scale:.2f}x\n"
                            f"Elapsed: {(now - bot_start_time).total_seconds() / 86400:.1f} days\n"
                            f"Progress to $1M: {(equity/1000000)*100:.1f}%\n\n"
                            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
                        )

            # Win rate monitor — pause aggressive scaling if performance drops
            if len(profitable_days) >= 7:
                win_rate = sum(1 for day in profitable_days[-7:] if day) / 7
                if win_rate < 0.45:
                    log.warning(f"⚠️  WIN RATE LOW ({win_rate*100:.0f}%) - Pausing aggressive trades")
                    # In production, set a flag to reduce position sizes or pause new entries

            # Log current auto-scale status
            if equity >= 1000.0:
                log.info(f"[APEX_589296] AUTO-SCALE: Equity ${equity:,.0f} → Scale {current_scale:.2f}x | Progress to $1M: {(equity/1000000)*100:.1f}%")

    log.info(f"[APEX_589296] Scanning futures markets ({', '.join(FUTURES)})... | Daily P&L: ${daily_pnl:.2f}")

    async def close_position(session, contract, config, position, price, rsi, trend, reason_label):
        """Shared close/cover path for both a normal exit and a rotation
        exit - same order placement, P&L accounting, and alert either way."""
        side = position["side"]
        entry = position["entry"]
        qty = position["qty"]
        close_action = "SELL" if side == "long" else "BUY"
        profit_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
        # Real dollar P&L on the actual fractional-share qty held - not a
        # futures-contract point value multiplier (that "*50" was left
        # over from before size_position() made qty a real fractional
        # share count instead of a fixed 1-contract quantity, and was
        # inflating every logged/emailed P&L figure ~50x above the real
        # fill amount).
        pnl = (price - entry) * qty if side == "long" else (entry - price) * qty

        filled = await execute_futures_trade(session, contract, close_action, qty, price, rsi, trend, target=price)
        if not filled:
            return False

        global daily_pnl
        daily_pnl += pnl

        # Track profit in 5-hour rolling window
        if pnl > 0:
            profit_tracker.add_profit(pnl, contract, side, reason_label)

        log.info(f"[APEX_589296] 📤 CLOSE {side.upper()} {contract} ({reason_label}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f} ({profit_pct:.2f}%) | {profit_tracker.get_five_hour_summary()}")

        # CRITICAL: Log closed trade to database for audit trail
        await _db_save_closed_trade(contract, side, entry, price, qty, pnl, reason_label)

        send_trade_alert(
            f"🤖 Bare Metal Builders — {contract} {side} closed ({reason_label})",
            f"{side.capitalize()} position closed on APEX_589296:\n\n"
            f"{contract} | Entry: ${entry:.2f} | Exit: ${price:.2f}\n"
            f"P&L: ${pnl:.2f} ({profit_pct:.2f}%) | Reason: {reason_label}\n\n"
            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
        )
        open_prop_positions.pop(contract, None)
        await _db_delete_open(contract)
        return True

    async def open_position(session, contract, config, side, price, rsi, trend, qty):
        action = "BUY" if side == "long" else "SELL"
        if side == "long":
            stop_loss, target = price * 0.98, price * 1.03
        else:
            stop_loss, target = price * 1.02, price * 0.97

        filled = await execute_futures_trade(session, contract, action, qty, price, rsi, trend, stop_loss, target)
        if not filled:
            return False

        open_prop_positions[contract] = {"side": side, "entry": price, "qty": qty, "open_time": now}
        await _db_save_open(contract, side, price, qty)
        send_trade_alert(
            f"🤖 Bare Metal Builders — {side.upper()} {contract} opened",
            f"{'LIVE' if LIVE_TRADE else 'PAPER'} {side} opened on APEX_589296:\n\n"
            f"{action} {qty} {contract} ({config['symbol']}) @ ${price:.2f}\n"
            f"RSI: {rsi} | Trend: {trend}\n\n"
            f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
        )
        return True

    async def try_open(contract, config, side, price, rsi, trend, slots_remaining):
        """Wraps open_position with dollar-based sizing against whatever
        cash is actually left this cycle (tracked in cash_remaining, closed
        over from run_prop_cycle) - falls back to the fixed 1-share size
        if the real cash balance couldn't be fetched this cycle."""
        nonlocal cash_remaining

        # MANDATE CHECK 1: Universe enforcement (only approved symbols)
        approved_universe = (
            APEX_MANDATE["universe"]["futures"] +
            APEX_MANDATE["universe"]["crypto"] +
            APEX_MANDATE["universe"]["commodities"] +
            APEX_MANDATE["universe"]["inverse_etfs"] +
            APEX_MANDATE["universe"]["equities"]
        )
        if contract not in approved_universe:
            log.warning(f"[MANDATE] {contract} NOT in approved universe - SKIPPING")
            return False

        # MANDATE CHECK 1.5: real-backtest auto-exclusion + top-N ROI
        # concentration (see get_effective_excluded_symbols) - a symbol
        # whose last AUTO_EXCLUDE_RUN_WINDOW real backtest runs were all
        # negative-ROI, OR that's currently outside the top
        # TOP_N_ELIGIBLE_SYMBOLS by real ROI, is skipped here, same as the
        # crypto side's coin exclusion + top-N rotation.
        excluded_symbols = await get_effective_excluded_symbols()
        if config["symbol"] in excluded_symbols:
            reason = await describe_symbol_exclusion_reason(config["symbol"])
            log.warning(f"[MANDATE] {contract} ({config['symbol']}) excluded - {reason} - SKIPPING")
            return False

        # A real Alpaca branch (see the ALPACA BRANCHES section below) may
        # have claimed this exact contract as its own dedicated symbol -
        # skip it here so the whole-account scan and that branch's own
        # independent cycle can never both decide to buy the same real
        # contract at once. A no-op call (empty set) whenever branch mode
        # is off or no branch has claimed anything.
        branch_claimed = await get_alpaca_branch_claimed_contracts()
        if contract in branch_claimed:
            log.info(f"[MANDATE] {contract} is claimed by a real Alpaca branch - SKIPPING (managed independently)")
            return False

        # The opening-bar live system (see that section below) may already
        # hold a real position in this exact contract - skip it here too,
        # same reasoning as the branch check just above: two independent
        # decision processes can never both buy the same real contract at
        # once. A no-op check whenever that system is off or holds nothing.
        if contract in open_opening_bar_positions:
            log.info(f"[MANDATE] {contract} is held by the opening-bar live system - SKIPPING (managed independently)")
            return False

        # MANDATE CHECK 2: Entry conditions validation
        total_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in open_prop_positions.values())
        is_valid, mandate_reason = validate_entry(
            bot_name="prop_bot",
            symbol=contract,
            rsi=rsi,
            volume_ratio=1.0,  # TODO: calculate from bars
            buying_power=await get_account_buying_power(session),
            open_positions=len(open_prop_positions),
            total_notional=total_notional,
            equity=equity
        )
        if not is_valid:
            log.warning(f"[APEX_589296] ⛔ MANDATE BLOCKED: {contract} {side} — {mandate_reason}")
            return False

        # HARD MARGIN SAFETY CHECK — prevent over-leverage. Real notional
        # held by the Alpaca branch system AND the opening-bar live system
        # (see those sections below) both count against this same real
        # account-wide risk cap too - those positions live in separate
        # dicts, invisible to this check otherwise.
        buying_power = await get_account_buying_power(session)
        is_safe, reason = check_margin_safety(
            buying_power, equity, len(open_prop_positions),
            extra_open_notional=_total_alpaca_branch_notional() + _total_opening_bar_notional(),
        )
        if not is_safe:
            log.warning(f"[APEX_589296] ⛔ MARGIN SAFETY: Blocking {contract} entry — {reason}")
            return False

        if cash_remaining is not None:
            # Real total already at risk across every real position this
            # account holds - open_prop_positions itself plus the Alpaca
            # branch and opening-bar systems (separate dicts, otherwise
            # invisible here) - the exact same real figure
            # check_margin_safety just confirmed leaves SOME room. Passed
            # through so size_position sizes this new position to fit
            # inside that real remaining room, not an independent guess
            # that could still overshoot it. See size_position's own
            # docstring for the real bug this closes.
            already_open_notional = total_notional + _total_alpaca_branch_notional() + _total_opening_bar_notional()
            qty = size_position(cash_remaining, slots_remaining, price, account_equity=equity, already_open_notional=already_open_notional)
            if qty is None:
                log.warning(f"[APEX_589296] ⛔ INSUFFICIENT CASH: {contract} {side} skipped — only ${cash_remaining:.2f} left (need ${config.get('min_cash', 1000):.2f})")
                return False
        else:
            qty = config["qty"]
            log.warning(f"[APEX_589296] ⚠️  Cash unavailable from API, using default qty: {qty}")

        log.info(f"[APEX_589296] 🟢 READY TO ENTER: {side.upper()} {contract} | Price: ${price:.2f} | Qty: {qty} | Risk: ${qty * price:.2f}")
        opened = await open_position(session, contract, config, side, price, rsi, trend, qty)
        if opened and cash_remaining is not None:
            cash_remaining -= qty * price
            log.info(f"[APEX_589296] 💳 POSITION OPENED | Cash remaining after position: ${cash_remaining:.2f}")
        return opened

    connector = aiohttp.TCPConnector(use_dns_cache=True, limit=20, limit_per_host=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
        await reconcile_positions_with_broker(session)

        equity = await get_account_equity(session)
        profit_target = get_profit_target_dollars(equity)

        # Professional daily loss limit: stop trading after losing 2% of account in a day
        global daily_account_equity_start
        if daily_account_equity_start is None and equity is not None:
            daily_account_equity_start = equity

        daily_loss_limit_pct = 0.02  # 2% daily loss limit
        daily_loss_limit = (daily_account_equity_start * daily_loss_limit_pct) if daily_account_equity_start else None
        is_hitting_daily_loss_limit = (
            daily_loss_limit and equity is not None and
            (daily_account_equity_start - equity) >= daily_loss_limit
        )

        # Dynamic circuit breaker: scales with position multiplier
        # Larger positions require larger loss threshold to avoid premature halt
        scale = _safe_float_env("POSITION_SCALE_MULTIPLIER", "1.0")
        dynamic_daily_max_loss = DAILY_MAX_LOSS_BASE * scale  # $10→$15→$20 as scale increases
        dynamic_max_positions = get_dynamic_max_positions(scale)  # 2 at 1.0x, 1 at 1.5x+

        log.info(f"[APEX_589296] Equity: {'$%.2f' % equity if equity is not None else 'unknown'} | Scale {scale:.1f}x | Max {dynamic_max_positions} pos | Profit target: ${profit_target:.2f}/position" +
                (f" | ⚠️ DAILY 2% LOSS LIMIT HIT - stopping new trades" if is_hitting_daily_loss_limit else ""))

        # Daily loss threshold check — the actual position-closing loop runs
        # further down, after `scans` is populated (referencing it here, before
        # it exists, used to throw UnboundLocalError the first time this ever
        # tripped — the safety mechanism would crash instead of firing).
        daily_loss_dollars = (daily_account_equity_start - equity) if daily_account_equity_start and equity else 0
        daily_circuit_breaker_tripped = daily_loss_dollars >= dynamic_daily_max_loss

        # EQUITY FLOOR RATCHET — once equity crosses a $1K tier it locks in
        # as the new floor and can never go back down, even across restarts.
        global equity_floor
        if equity is not None and equity >= EQUITY_FLOOR_TIER:
            candidate_floor = math.floor(equity / EQUITY_FLOOR_TIER) * EQUITY_FLOOR_TIER
            if candidate_floor > equity_floor:
                equity_floor = candidate_floor
                await save_equity_floor(equity_floor)
                log.info(f"[APEX_589296] 🪜 EQUITY FLOOR RAISED to ${equity_floor:,.0f} — will not trade below this again")
                send_trade_alert(
                    f"🪜 EQUITY FLOOR RAISED — ${equity_floor:,.0f}",
                    f"Account equity crossed ${equity_floor:,.0f}.\n\n"
                    f"Current equity: ${equity:,.2f}\n"
                    f"New floor locked in: ${equity_floor:,.0f}\n\n"
                    f"The bot will halt and close all positions if equity ever drops "
                    f"back below this floor — it only climbs from here.\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
                )
        equity_floor_breached = equity is not None and equity < equity_floor

        # Tracked and spent-down across this cycle's entries so dollar-based
        # sizing (see try_open/size_position) reflects money already
        # committed to earlier orders this same cycle, without an extra
        # API call per entry.
        cash_remaining = await get_account_cash(session)
        log.info(f"[APEX_589296] Cash available: {'$%.2f' % cash_remaining if cash_remaining is not None else 'unknown'}")

        # Long-only strategy: Shorting not available on this account.
        # Running 3 concurrent longs with mean reversion discipline:
        # - Entry: RSI < 30 (oversold)
        # - Exit: 2%+ profit target, -1.5% hard stop, RSI > 70, or 2-hour timeout
        shorting_enabled = await get_account_shorting_enabled(session)
        if not shorting_enabled:
            log.info("[APEX_589296] 📈 LONG-ONLY MODE: 3 concurrent long positions | RSI < 30 entry, 2% profit target, -1.5% stop")

        # Which real strategy family is live right now - read once per
        # cycle (also re-syncs APEX_MANDATE["entry"] as a side effect, see
        # get_live_strategy_family()'s own docstring). Defaults to
        # "momentum" (today's unchanged behavior) until explicitly
        # switched via the dashboard.
        strategy_family = await get_live_strategy_family()

        scans = {}
        for contract, config in FUTURES.items():
            # Scan all symbols 24/7 — crypto, commodities, indices all available on Alpaca
            # Momentum strategy uses real 15-min bars/20-bar SMA
            # (get_price_momentum); mean-reversion uses the original
            # 5-min/SMA50 fetch (get_price_rsi) - see MEAN_REVERSION_RSI_ENTRY
            # / MOMENTUM_RSI_ENTRY above for why each strategy needs its own.
            data = await (get_price_rsi(session, config["symbol"]) if strategy_family == "mean_reversion" else get_price_momentum(session, config["symbol"]))
            if data:
                scans[contract] = data
                log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${data['price']:.2f} | RSI:{data['rsi']} | Momentum:{data.get('momentum', 0):+.2f}% | {data['trend']}")
            await asyncio.sleep(0.5)  # 500ms between requests to prevent rate limiting

        # ── Pass 0: circuit breakers — close ALL positions if tripped ────
        # Daily 2%/dollar loss limit and the equity-floor ratchet both land
        # here since this is the first point in the cycle `scans` actually
        # has data to close positions against.
        if daily_circuit_breaker_tripped:
            log.warning(f"[APEX_589296] 🛑 CIRCUIT BREAKER: Daily loss ${daily_loss_dollars:.2f} >= ${dynamic_daily_max_loss:.2f} (scale {scale}x) — closing ALL positions")
            for contract in list(open_prop_positions.keys()):
                config = FUTURES.get(contract)
                if config is None:
                    log.error(f"[APEX_589296] ⚠️ {contract!r} in open_prop_positions is not a real FUTURES contract - skipping, needs a manual look")
                    continue
                data = scans.get(contract)
                if data:
                    await close_position(session, contract, config, open_prop_positions[contract],
                                       data["price"], data["rsi"], data["trend"], "CIRCUIT BREAKER - DAILY LOSS LIMIT")

        if equity_floor_breached:
            log.warning(f"[APEX_589296] 🛑 EQUITY FLOOR BREACH: ${equity:.2f} < locked floor ${equity_floor:,.0f} — closing ALL positions, halting new entries")
            send_trade_alert(
                f"🛑 EQUITY FLOOR BREACH — ${equity_floor:,.0f}",
                f"Equity dropped below the locked floor of ${equity_floor:,.0f}.\n\n"
                f"Current equity: ${equity:,.2f}\n\n"
                f"All open positions are being closed and new entries are halted "
                f"until equity recovers above ${equity_floor:,.0f}.\n\n"
                f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard"
            )
            for contract in list(open_prop_positions.keys()):
                config = FUTURES.get(contract)
                if config is None:
                    log.error(f"[APEX_589296] ⚠️ {contract!r} in open_prop_positions is not a real FUTURES contract - skipping, needs a manual look")
                    continue
                data = scans.get(contract)
                if data:
                    await close_position(session, contract, config, open_prop_positions[contract],
                                       data["price"], data["rsi"], data["trend"], "EQUITY FLOOR BREACH")

        # ── Pass 1: manage exits for symbols already held ────────────────
        # A long profits as price rises and exits on overbought RSI; a
        # short profits as price falls and exits on oversold RSI. Profit
        # target is checked against the position's actual real dollar
        # P&L (see PROFIT_TARGET_DOLLARS_MILESTONES) - take the 50c-$1,
        # don't hold out for a bigger move - and scales up slightly as
        # the real account grows.
        for contract, position in list(open_prop_positions.items()):
            config = FUTURES.get(contract)
            if config is None:
                log.error(f"[APEX_589296] ⚠️ {contract!r} in open_prop_positions is not a real FUTURES contract - skipping, needs a manual look")
                continue
            data = scans.get(contract)
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]
            latest_signals[contract] = {
                "symbol": config["symbol"], "price": price, "rsi": rsi, "trend": trend,
                "status": "HOLDING_LONG" if position["side"] == "long" else "HOLDING_SHORT",
                "has_position": True, "checked_at": now.isoformat(),
            }

            side = position["side"]
            entry = position["entry"]
            qty = position["qty"]

            # Calculate position age in seconds (track when position opened)
            position_open_time = position.get("open_time", now)
            # Defense in depth alongside the fix in load_open_positions():
            # `now` is always timezone-aware (datetime.now(ET)) - a naive
            # open_time reaching this point from any other path would raise
            # a real, UNCAUGHT TypeError here that aborts run_prop_cycle's
            # entire pass (every position, every new entry) for that cycle,
            # confirmed live via a real Railway traceback. Normalize rather
            # than crash - the exact tz is irrelevant to an age-in-seconds
            # calculation, only that both sides agree on being aware.
            if position_open_time.tzinfo is None:
                position_open_time = position_open_time.replace(tzinfo=timezone.utc)
            position_age_seconds = int((now - position_open_time).total_seconds())

            # Momentum Exit Decision - a real trailing stop off this
            # position's own real peak price since entry, not a small
            # fixed target. Replaced mean-reversion's should_exit_position()
            # after a real, live head-to-head comparison on the same real
            # 30-day Alpaca history: momentum made $68.08 (67 trades,
            # 56.7% win rate) vs mean-reversion's $48.52 (357 trades,
            # 51.3% win rate) - more real money, far fewer trades (less
            # fee drag), better win rate. See MOMENTUM_RSI_ENTRY above and
            # CLAUDE.md for the full real comparison. peak_pnl_pct is this
            # position's real high-water mark, persisted to
            # BotPosition.peak_pct so a Railway restart can't wipe it and
            # silently reset the trailing stop on exactly the positions
            # that ran up the most.
            if strategy_family == "mean_reversion":
                should_exit, reason, exit_type, new_peak_pnl_pct = should_exit_position(
                    symbol=contract,
                    entry_price=entry,
                    current_price=price,
                    current_rsi=rsi,
                    position_age_seconds=position_age_seconds,
                    direction="long",
                    max_hold_seconds=MEAN_REVERSION_MAX_HOLD_SECONDS,
                    stop_loss_pct=MEAN_REVERSION_STOP_LOSS_PCT,
                    min_profit_target_pct=MEAN_REVERSION_PROFIT_TARGET_PCT,
                    rsi_profit_threshold_long=MEAN_REVERSION_RSI_PROFIT_THRESHOLD,
                    peak_pnl_pct=position.get("peak_pnl_pct", 0.0),
                    breakeven_trigger_pct=MEAN_REVERSION_BREAKEVEN_TRIGGER_PCT,
                    max_giveback_pct=MEAN_REVERSION_GIVEBACK_PCT,
                )
            else:
                should_exit, reason, exit_type, new_peak_pnl_pct = should_exit_position_momentum(
                    symbol=contract,
                    entry_price=entry,
                    current_price=price,
                    position_age_seconds=position_age_seconds,
                    peak_pnl_pct=position.get("peak_pnl_pct", 0.0),
                    max_hold_seconds=MOMENTUM_MAX_HOLD_SECONDS,
                    trail_pct=MOMENTUM_TRAIL_PCT,
                )
            if new_peak_pnl_pct > position.get("peak_pnl_pct", 0.0):
                position["peak_pnl_pct"] = new_peak_pnl_pct
                await _db_update_peak_pct(contract, new_peak_pnl_pct)

            if should_exit:
                await close_position(session, contract, config, position, price, rsi, trend, reason)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries, with rotation if already at the cap ─────
        # Which of the 4 real, backtested entry variants (see
        # get_live_entry_variant's docstring) is currently promoted to
        # live - read once per cycle, not once per symbol. Defaults to
        # "A" (today's original rule) until the account owner explicitly
        # promotes a different one from the backtest page.
        live_entry_variant = await get_live_entry_variant()
        candidates = []
        for contract, config in FUTURES.items():
            # Skip non-crypto symbols during after-hours
            if contract in open_prop_positions:
                continue
            data = scans.get(contract)
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]
            momentum = data.get("momentum", 0)

            # Entry validation - reuses the exact same real gate function
            # (check_momentum_entry_gate / check_mean_reversion_entry_gate)
            # the manual "Trade this" endpoint and the "Right now"
            # eligibility dry-run both call, so all three can never drift
            # out of sync with each other or with whichever strategy
            # family/variant is currently live. Long-only, matching this
            # account's real shorting-disabled constraint (unchanged from
            # before).
            if strategy_family == "mean_reversion":
                should_enter, reason = check_mean_reversion_entry_gate(rsi)
            else:
                should_enter, reason = check_momentum_entry_gate(data, live_entry_variant)
            direction = "long" if should_enter else "hold"

            if should_enter:
                # Confidence ranking: momentum ranks by how far ABOVE the
                # threshold RSI is (stronger move = higher confidence);
                # mean-reversion ranks by how far BELOW its own threshold
                # RSI is (more oversold = higher confidence) - the mirror
                # image, so a stronger, more confirmed signal is
                # prioritized first either way when several symbols
                # qualify the same cycle.
                confidence = (MEAN_REVERSION_RSI_ENTRY - rsi) if strategy_family == "mean_reversion" else (rsi - MOMENTUM_RSI_ENTRY)
                candidates.append((confidence, contract, config, direction, price, rsi, trend))
                status = f"{direction.upper()}_SETUP"
            else:
                status = f"NEUTRAL ({reason})"

            latest_signals[contract] = {
                "symbol": config["symbol"], "price": price, "rsi": rsi, "trend": trend,
                "momentum": momentum, "status": status, "has_position": False, "checked_at": now.isoformat(),
            }

        candidates.sort(key=lambda c: -c[0])  # strongest (furthest past threshold) first

        for _, contract, config, side, price, rsi, trend in candidates:
            # Professional risk management: stop new entries if daily 2% loss limit hit
            if is_hitting_daily_loss_limit:
                log.info(f"[APEX_589296] 🛑 {side.upper()} {contract} blocked — daily 2% loss limit reached, no new entries")
                continue

            # Equity floor ratchet: no new entries while equity is below the
            # locked floor — only resumes once equity recovers above it.
            if equity_floor_breached:
                log.info(f"[APEX_589296] 🛑 {side.upper()} {contract} blocked — equity ${equity:.2f} below locked floor ${equity_floor:,.0f}")
                continue

            # Multi-timeframe confluence: don't fight a strong 1-hour
            # trend just because the 5-minute RSI dipped. Entries only -
            # never gates an exit or an existing position.
            # This can be disabled via REQUIRE_HIGHER_TF_CONFIRMATION=false env var
            higher_tf = "DISABLED" if not REQUIRE_HIGHER_TF_CONFIRMATION else await get_higher_tf_trend(session, config["symbol"])
            log.info(f"[APEX_589296] 📊 {side.upper()} {contract} — 5min trend: {trend}, 1H trend: {higher_tf}, RSI: {rsi:.1f}")

            if REQUIRE_HIGHER_TF_CONFIRMATION and ((side == "long" and higher_tf == "DOWN") or (side == "short" and higher_tf == "UP")):
                log.warning(f"[APEX_589296] 🚫 {side.upper()} {contract} BLOCKED — 1H trend ({higher_tf}) opposes 5min ({trend}) signal")
                continue
            elif not REQUIRE_HIGHER_TF_CONFIRMATION:
                log.info(f"[APEX_589296] ✅ {side.upper()} {contract} READY (1H check disabled) — 5min {trend} | RSI:{rsi:.1f} | attempting entry...")
            else:
                # If we get here, higher TF confirms the signal — ready to try entry
                log.info(f"[APEX_589296] ✅ {side.upper()} {contract} CONFIRMED — 5min {trend} + 1H {higher_tf} align | RSI:{rsi:.1f} | attempting entry...")

            if len(open_prop_positions) < dynamic_max_positions:
                scan_data = scans.get(contract)
                momentum = scan_data.get("momentum", 0) if scan_data else 0
                log.info(f"[APEX_589296] 📡 {side.upper()} {contract} — RSI:{rsi} Momentum:{momentum:+.2f}% Trend:{trend}")
                await try_open(contract, config, side, price, rsi, trend, dynamic_max_positions - len(open_prop_positions))
            else:
                # At the cap - find the weakest held position (lowest
                # unrealized P&L). Only rotate out of it if it's a genuine
                # loss (strictly negative) - never sell a winning position,
                # and never a merely-flat one either, since a position that
                # was *just* opened this same cycle reads as exactly 0% and
                # would otherwise get rotated out seconds after opening
                # whenever several signals fire in the same cycle.
                weakest_contract, weakest_pct = None, None
                for held_contract, held_pos in open_prop_positions.items():
                    held_data = scans.get(held_contract)
                    if not held_data:
                        continue
                    held_price = held_data["price"]
                    held_pct = (
                        (held_price - held_pos["entry"]) / held_pos["entry"] * 100 if held_pos["side"] == "long"
                        else (held_pos["entry"] - held_price) / held_pos["entry"] * 100
                    )
                    if weakest_pct is None or held_pct < weakest_pct:
                        weakest_pct, weakest_contract = held_pct, held_contract

                if weakest_contract is not None and weakest_pct is not None and weakest_pct < 0:
                    held_data = scans[weakest_contract]
                    # Capture the dollar value being freed up before closing
                    # (close_position pops it from open_prop_positions), so
                    # try_open's sizing reflects the cash rotation frees, not
                    # just what was already sitting uninvested.
                    freed_value = open_prop_positions[weakest_contract]["qty"] * held_data["price"]
                    log.info(
                        f"[APEX_589296] 🔄 ROTATING: {weakest_contract} ({weakest_pct:.2f}%, weakest of {dynamic_max_positions}) "
                        f"→ {contract} (RSI:{rsi} {side})"
                    )
                    closed = await close_position(
                        session, weakest_contract, FUTURES[weakest_contract], open_prop_positions[weakest_contract],
                        held_data["price"], held_data["rsi"], held_data["trend"], "ROTATED OUT",
                    )
                    if closed:
                        if cash_remaining is not None:
                            cash_remaining += freed_value
                        await try_open(contract, config, side, price, rsi, trend, dynamic_max_positions - len(open_prop_positions))
                else:
                    log.info(
                        f"[APEX_589296] At max positions ({dynamic_max_positions}) - {contract} {side} signal held, "
                        f"weakest position ({weakest_contract} {weakest_pct:+.2f}%) isn't a loss, not rotating"
                        if weakest_contract else
                        f"[APEX_589296] At max positions ({dynamic_max_positions}) - {contract} {side} signal held, no rotation candidate"
                    )

            await asyncio.sleep(0.3)

    # Check if today was profitable
    today = now.strftime("%Y-%m-%d")
    if daily_pnl > 0 and (not profitable_days or profitable_days[-1] != today):
        profitable_days.append(today)
        log.info(f"✅ PROFITABLE DAY #{len(profitable_days)} | ${daily_pnl:.2f} | APEX_589296")
        # Record daily earnings to database as a payment
        await record_daily_earnings(daily_pnl)
        if len(profitable_days) >= 7:
            log.info("🎯 7 CONSECUTIVE PROFITABLE DAYS ACHIEVED — READY TO GO LIVE!")
            log.info("ACTION: Change ALPACA_LIVE_TRADE=true in Railway to go live")


async def record_daily_earnings(pnl_amount):
    """Record daily bot trading profits as a payment in the database.
    Earnings accumulate in Alpaca account for compounding.
    Manual transfers can be made later as needed."""
    if pnl_amount <= 0:
        return

    # 83% to worker (bot), 17% to platform
    worker_amount = pnl_amount * 0.83
    platform_amount = pnl_amount * 0.17

    payment = Payment(
        id=f"bot_trade_{uuid.uuid4().hex[:8]}",
        job_id=f"bot_daily_{datetime.now(ET).strftime('%Y%m%d')}",
        worker_id="bot@pgusa.local",  # Use consistent worker ID matching system
        client_id="alpaca_trading",
        gross_amount=pnl_amount,
        worker_amount=worker_amount,
        platform_amount=platform_amount,
        payout_status="pending",
    )

    try:
        async with AsyncSessionLocal() as session:
            session.add(payment)
            await session.commit()
            log.info(f"[APEX_589296] 💰 Earnings recorded: ${worker_amount:.2f} | Platform fee: ${platform_amount:.2f}")
            log.info(f"[APEX_589296] 📊 Money accumulates in Alpaca | Total balance growing")

    except Exception as e:
        log.error(f"[APEX_589296] Failed to record daily earnings: {e}")


# ============================================================================
# ALPACA BRANCHES - a real, smaller first slice toward something like the
# crypto family tree's compounding branches, per the account owner's
# explicit request ("is there any way we can make something like that
# happen with those alpaca bots"). OFF by default
# (is_alpaca_branch_mode_active) - this whole section is a true no-op
# until the account owner explicitly turns it on, exactly like the
# strategy-family toggle before it.
#
# The real architectural gap this closes: the existing 8 bot_N "buckets"
# (routers/trading_dashboard.py) are proportional SHARES of one real
# account - prop_bot.py sizes every real order off the account's single
# real buying-power number, so there's no such thing as "bucket 3's
# trade." A branch here is different: it's a real, independent capital
# slice with its OWN dedicated contract and its OWN position tracking,
# sized only against min(its own allocated_usd, real buying power) - the
# same real-balance clamp crypto's place_market_buy() already uses, so
# branches can never collectively overspend the real account.
#
# Deliberately scoped down from the full crypto-tree design, by explicit
# agreement: no spawn-on-milestone yet, no coin-switching - just proving
# real capital partitioning and independent per-branch tracking work
# safely on 2-3 real branches first. Reuses the EXACT SAME real functions
# the account-wide scan already uses for market data, entry/exit signals,
# and order placement - never a separate, reimplemented copy of this
# codebase's real trading logic.
# ============================================================================

# Real bounded multi-hop reinforcement chain for Alpaca branches, the
# direct counterpart to crypto_family_tree_bot.py's own
# _maybe_spawn_child() chain - built after the account owner explicitly
# asked for the same "chain reaction" mechanism on this side too, with
# one deliberate addition specific to this design: a reinforcement
# target must independently re-qualify against the SAME real entry gate
# (check_momentum_entry_gate/check_mean_reversion_entry_gate) a normal
# flat-branch entry would need to pass - "chain opportunity is not an
# automatic trade." Scoped narrower than the crypto side in two ways,
# both explicit, both stated plainly rather than hidden:
# 1. Reinforcement only ever targets a FLAT branch (opens a fresh
#    position). Blending more real capital into an ALREADY-HELD futures
#    position would mean re-deriving its stop/target and margin exposure
#    mid-trade - real complexity this first version doesn't take on;
#    a branch already holding is simply not an eligible reinforcement
#    target this pass.
# 2. If no other real branch is currently eligible (none flat, or none
#    pass the real entry gate), the seed is refunded and the tier
#    increment reverted - same real money-safety pattern as the crypto
#    side - but there is no automatic "spawn a brand-new branch" fallback
#    yet, since Alpaca branches are manually created (a specific real
#    contract chosen by the account owner), unlike crypto's uniform $50
#    auto-spawn onto the next eligible coin. The account owner creates
#    more branches by hand via the dashboard.
ALPACA_REINFORCEMENT_SEED_USD = _safe_float_env("ALPACA_REINFORCEMENT_SEED_USD", "50")
ALPACA_UNLOCK_TIER_USD = _safe_float_env("ALPACA_UNLOCK_TIER_USD", "100")
ALPACA_MAX_CHAIN_HOPS = _safe_int_env("ALPACA_MAX_CHAIN_HOPS", "5")

# ============================================================================
# IDLE-CASH SWEEP - real buying power not yet claimed by any active branch,
# put to work automatically instead of sitting uninvested. Per the account
# owner's explicit request ("don't allow the market to close with money
# sitting uninvested... auto-start new branches too"), and their explicit
# choice, when asked directly, to also auto-open brand-new branches (not
# just top up existing ones) once idle cash builds up - real, independent
# exposure, not just deposits into what already exists.
#
# Runs every real cycle alongside the reinforcement chain (see
# run_alpaca_branches_cycle), but does at most ONE real deployment per
# cycle - same "don't rush several real orders off one pass" discipline
# the reinforcement chain already follows. Never runs while a real
# account-wide kill condition is active (kill_halted) - new capital is
# never deployed while the account itself is in trouble, matching every
# other capital-deployment path in this file.
ALPACA_IDLE_SWEEP_SEED_USD = _safe_float_env("ALPACA_IDLE_SWEEP_SEED_USD", "50")
ALPACA_IDLE_SWEEP_MIN_SPENDABLE_USD = _safe_float_env("ALPACA_IDLE_SWEEP_MIN_SPENDABLE_USD", "75")
# Real floor-cushion guard, per the account owner's explicit follow-up
# ("make sure that we don't hit it... just make sure it don't happen"),
# raised after being told the account's real equity ($1,001.50) sits
# almost exactly on its own real ratcheting floor ($1,000.00 - the same
# `equity_floor` global the EQUITY FLOOR BREACH close-everything logic
# already uses elsewhere in this file). Depositing cash into a branch
# doesn't itself move real equity (it converts cash into a position of
# equal notional) - the real risk is adding MORE leveraged exposure while
# there's little to no real room left before a normal adverse swing
# breaches the floor. The sweep now refuses outright unless real equity
# is at least this many dollars ABOVE the real floor - real protection
# checked fresh every cycle, not a one-time setting.
ALPACA_IDLE_SWEEP_MIN_EQUITY_CUSHION_USD = _safe_float_env("ALPACA_IDLE_SWEEP_MIN_EQUITY_CUSHION_USD", "100")

ALPACA_BRANCH_MODE_KEY = "alpaca_branch_mode"


async def is_alpaca_branch_mode_active() -> bool:
    """DB-persisted (same generic TradingBotState bucket pattern every
    other real-time flag in this file already uses, not a Railway env var
    - avoids the exact stray-quote-character bug class that silently
    disabled the crypto coordinator earlier this session). False (off) by
    default - the whole Alpaca branch system is a true no-op until the
    account owner explicitly turns it on."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_BRANCH_MODE_KEY))
        row = result.scalar_one_or_none()
        return bool(row and row.base_capital and row.base_capital >= 1.0)


async def set_alpaca_branch_mode(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == ALPACA_BRANCH_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=ALPACA_BRANCH_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


# Real per-branch position tracking, kept SEPARATE from open_prop_positions
# (which is keyed by contract, account-wide, and every existing part of
# this file assumes exactly one position per contract there) - keyed by
# branch bot_name instead, so a branch's own position can never collide
# with or be silently overwritten by the whole-account scan's own state.
open_alpaca_branch_positions = {}


def _total_alpaca_branch_notional() -> float:
    """Real notional value currently held across every open Alpaca branch
    position - see check_margin_safety's extra_open_notional param."""
    return sum(p.get("qty", 0) * p.get("entry", 0) for p in open_alpaca_branch_positions.values())


async def get_alpaca_branch_claimed_contracts() -> set:
    """Real FUTURES contract keys currently claimed by an ACTIVE branch -
    checked by try_open's own MANDATE CHECK 1.5 so the whole-account scan
    never independently buys a contract a branch already owns (which would
    double up on the same real symbol from two different, uncoordinated
    decision processes). A disabled branch (active=False) releases its
    claim."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch.contract).where(AlpacaBranch.active == True))
        return {row[0] for row in result.all()}


async def get_alpaca_branches() -> list:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch).order_by(AlpacaBranch.bot_name))
        return list(result.scalars().all())


async def create_alpaca_branch(contract: str, allocated_usd: float) -> AlpacaBranch:
    """Creates a real new branch claiming `contract` with a real, initial
    virtual capital slice - a pure bookkeeping operation, never a trade
    (mirrors CryptoTreeBranch's own "spawning is a bookkeeping transfer,
    not a trade" reasoning - the real dollars this slice represents were
    already sitting in the one real Alpaca account before this call, and
    still are after it)."""
    if contract not in FUTURES:
        raise ValueError(f"{contract!r} is not a real FUTURES contract")
    if allocated_usd <= 0:
        raise ValueError("allocated_usd must be positive")
    claimed = await get_alpaca_branch_claimed_contracts()
    if contract in claimed:
        raise ValueError(f"{contract} is already claimed by an active branch")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch))
        existing = list(result.scalars().all())
        used_nums = {
            int(b.bot_name.rsplit("_", 1)[-1])
            for b in existing if b.bot_name.rsplit("_", 1)[-1].isdigit()
        }
        next_num = 1
        while next_num in used_nums:
            next_num += 1
        bot_name = f"alpaca_branch_{next_num}"
        branch = AlpacaBranch(
            bot_name=bot_name, contract=contract, allocated_usd=allocated_usd, active=True,
            next_unlock_tier=allocated_usd + ALPACA_UNLOCK_TIER_USD,
        )
        db.add(branch)
        await db.commit()
        await db.refresh(branch)
    log.info(f"[ALPACA-BRANCH] 🌱 Created {bot_name} on {contract} ({FUTURES[contract]['symbol']}) with ${allocated_usd:.2f}")
    return branch


async def _db_save_branch_open(bot_name: str, contract: str, side: str, entry: float, qty: float):
    # Self-heals before inserting (same pattern the crypto side's
    # _save_branch_position already uses, after a real production
    # incident there): clears any stale row(s) under this exact
    # bot_name+contract first, so this branch can never accumulate a
    # duplicate BotPosition row under its own name.
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name, BotPosition.symbol == contract))
            for row in result.scalars().all():
                await db.delete(row)
            db.add(BotPosition(bot=bot_name, symbol=contract, side=side, entry_price=entry, qty=qty))
            await db.commit()
    except Exception as e:
        log.error(f"[ALPACA-BRANCH] Failed to persist opened position for {bot_name}: {e}")


async def _db_update_branch_peak_pct(bot_name: str, contract: str, peak_pnl_pct: float):
    # Real production lesson already learned once on the crypto side
    # (a duplicate BotPosition row under one bot_name crashed
    # scalar_one_or_none() with MultipleResultsFound, every cycle,
    # forever) - defense in depth here from the start: order by id desc
    # and take the most recent row instead of assuming exactly 0-or-1.
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotPosition).where(BotPosition.bot == bot_name, BotPosition.symbol == contract).order_by(BotPosition.id.desc())
            )
            row = result.scalars().first()
            if row:
                row.peak_pct = peak_pnl_pct
                await db.commit()
    except Exception as e:
        log.error(f"[ALPACA-BRANCH] Failed to persist peak_pct for {bot_name}: {e}")


async def _db_delete_branch_open(bot_name: str, contract: str):
    # Deletes EVERY matching row, not just one - same defense-in-depth
    # reasoning as _db_update_branch_peak_pct above, so this branch can
    # never leave a stray duplicate row behind under its own bot_name.
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == bot_name, BotPosition.symbol == contract))
            rows = result.scalars().all()
            for row in rows:
                await db.delete(row)
            if rows:
                await db.commit()
    except Exception as e:
        log.error(f"[ALPACA-BRANCH] Failed to delete closed position for {bot_name}: {e}")


async def load_alpaca_branch_positions():
    """Reload open_alpaca_branch_positions from the DB once at startup -
    same reasoning as load_open_positions() for prop_apex: a Railway
    restart must not wipe a real open branch position while it's still
    open for real on Alpaca."""
    try:
        branches = await get_alpaca_branches()
        bot_names = {b.bot_name for b in branches}
        if not bot_names:
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot.in_(bot_names)))
            rows = result.scalars().all()
            for row in rows:
                if not row.symbol or row.symbol not in FUTURES:
                    log.error(f"[ALPACA-BRANCH] ⚠️ Skipping BotPosition id={row.id} (bot={row.bot!r}) with invalid symbol {row.symbol!r}")
                    continue
                open_time = row.opened_at
                if open_time is not None and open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                open_alpaca_branch_positions[row.bot] = {
                    "side": row.side, "entry": row.entry_price, "qty": row.qty,
                    "open_time": open_time, "peak_pnl_pct": row.peak_pct or 0.0,
                }
            if rows:
                log.info(f"[ALPACA-BRANCH] Reloaded {len(open_alpaca_branch_positions)} open branch position(s) from DB")
    except Exception as e:
        log.error(f"[ALPACA-BRANCH] Failed to reload branch positions from DB: {e}")


async def _log_alpaca_branch_trade(bot_name: str, contract: str, symbol: str, entry_price: float, exit_price: float, qty: float, pnl: float, exit_reason: str, opened_at):
    """Real, persisted record of one completed Alpaca branch round-trip -
    per the account owner's explicit request to see real Capital and win
    rate "adding up" for a branch, not just the current allocated_usd
    number with no history behind it. Best-effort, deliberately never
    allowed to raise: a logging failure here must never affect the real
    trade or the real allocated_usd update that already happened at the
    call site - same defensive pattern crypto_family_tree_bot.py's own
    _log_activity() already uses."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(AlpacaBranchTradeHistory(
                bot_name=bot_name, contract=contract, symbol=symbol,
                entry_price=entry_price, exit_price=exit_price, qty=qty, pnl=pnl,
                exit_reason=exit_reason, opened_at=opened_at,
            ))
            await db.commit()
    except Exception as e:
        log.warning(f"[ALPACA-BRANCH] trade-history log failed for {bot_name} (non-fatal, real trade unaffected): {e}")


async def get_alpaca_branch_trade_history(limit_recent: int = 50):
    """Real, per-branch trade-history aggregation - the direct Alpaca-side
    counterpart to crypto_family_tree_bot.get_coin_trade_history(). Reads
    AlpacaBranchTradeHistory (written the moment a real branch sell
    fills) and returns, per bot_name: real trade_count/win_rate/total_pnl/
    avg_pnl (via a real SQL GROUP BY, not computed row-by-row in Python),
    plus the most recent individual trades overall for a detail view.
    Read-only - never places an order."""
    async with AsyncSessionLocal() as db:
        agg_result = await db.execute(
            select(
                AlpacaBranchTradeHistory.bot_name,
                func.count(AlpacaBranchTradeHistory.id).label("trade_count"),
                func.sum(AlpacaBranchTradeHistory.pnl).label("total_pnl"),
                func.avg(AlpacaBranchTradeHistory.pnl).label("avg_pnl"),
                func.sum(case((AlpacaBranchTradeHistory.pnl > 0, 1), else_=0)).label("wins"),
            ).group_by(AlpacaBranchTradeHistory.bot_name)
        )
        branches = []
        for bot_name, trade_count, total_pnl, avg_pnl, wins in agg_result.all():
            branches.append({
                "bot_name": bot_name,
                "trade_count": trade_count,
                "total_pnl": round(total_pnl, 2) if total_pnl is not None else 0.0,
                "avg_pnl": round(avg_pnl, 2) if avg_pnl is not None else 0.0,
                "win_rate": round(wins / trade_count * 100, 1) if trade_count else 0.0,
            })
        branches.sort(key=lambda b: b["total_pnl"], reverse=True)

        recent_result = await db.execute(
            select(AlpacaBranchTradeHistory).order_by(desc(AlpacaBranchTradeHistory.closed_at)).limit(limit_recent)
        )
        recent_trades = [row.to_dict() for row in recent_result.scalars().all()]

    return {"branches": branches, "recent_trades": recent_trades}


async def run_alpaca_branch_cycle(session, branch, equity, buying_power, strategy_family, live_entry_variant, kill_halted: bool):
    """One real cycle for one Alpaca branch - reuses the exact same real
    entry/exit gate functions and order-placement path the whole-account
    scan uses (get_price_momentum/get_price_rsi, check_momentum_entry_gate/
    check_mean_reversion_entry_gate, should_exit_position_momentum/
    should_exit_position, execute_futures_trade), sized only against this
    branch's own real capital slice.

    kill_halted: the SAME real account-wide kill-condition check
    (check_kill_conditions) the caller already computed once this outer
    cycle - a branch NEVER opens a new position while it's true. Real
    protection is never weaker for a branch than for the main account. An
    EXISTING held position still gets its own real exit check regardless
    - a kill condition halts new entries, it doesn't strand real risk
    unmanaged."""
    # Real catch-up chain check, every cycle - mirrors the crypto side's
    # own "_maybe_spawn_child() called every cycle, not just after a
    # sell" reasoning, so a branch that crossed its tier gets a real
    # chance to reinforce a sibling even if the first attempt (right
    # after its own last trade) found nothing eligible. Reload branch
    # fresh afterward since this may have changed its own allocated_usd
    # and next_unlock_tier.
    await _alpaca_maybe_spawn_or_reinforce(branch)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == branch.bot_name))
        fresh_branch = result.scalar_one_or_none()
    if fresh_branch is None:
        return
    branch = fresh_branch

    contract = branch.contract
    config = FUTURES.get(contract)
    if config is None:
        log.error(f"[ALPACA-BRANCH] {branch.bot_name}: {contract!r} is not a real FUTURES contract - skipping, needs a manual look")
        return

    data = await (get_price_rsi(session, config["symbol"]) if strategy_family == "mean_reversion" else get_price_momentum(session, config["symbol"]))
    if not data:
        log.warning(f"[ALPACA-BRANCH] {branch.bot_name}: could not fetch live data for {config['symbol']} - skipping this cycle")
        return
    price, rsi, trend = data["price"], data["rsi"], data["trend"]

    position = open_alpaca_branch_positions.get(branch.bot_name)

    if position is not None:
        # ---- Holding: real exit check, identical logic to the whole-account scan ----
        now = datetime.now(ET)
        position_open_time = position.get("open_time", now)
        if position_open_time.tzinfo is None:
            position_open_time = position_open_time.replace(tzinfo=timezone.utc)
        position_age_seconds = int((now - position_open_time).total_seconds())

        if strategy_family == "mean_reversion":
            should_exit, reason, exit_type, new_peak_pnl_pct = should_exit_position(
                symbol=contract, entry_price=position["entry"], current_price=price, current_rsi=rsi,
                position_age_seconds=position_age_seconds, direction="long",
                max_hold_seconds=MEAN_REVERSION_MAX_HOLD_SECONDS, stop_loss_pct=MEAN_REVERSION_STOP_LOSS_PCT,
                min_profit_target_pct=MEAN_REVERSION_PROFIT_TARGET_PCT, rsi_profit_threshold_long=MEAN_REVERSION_RSI_PROFIT_THRESHOLD,
                peak_pnl_pct=position.get("peak_pnl_pct", 0.0), breakeven_trigger_pct=MEAN_REVERSION_BREAKEVEN_TRIGGER_PCT,
                max_giveback_pct=MEAN_REVERSION_GIVEBACK_PCT,
            )
        else:
            should_exit, reason, exit_type, new_peak_pnl_pct = should_exit_position_momentum(
                symbol=contract, entry_price=position["entry"], current_price=price,
                position_age_seconds=position_age_seconds, peak_pnl_pct=position.get("peak_pnl_pct", 0.0),
                max_hold_seconds=MOMENTUM_MAX_HOLD_SECONDS, trail_pct=MOMENTUM_TRAIL_PCT,
            )
        if new_peak_pnl_pct > position.get("peak_pnl_pct", 0.0):
            position["peak_pnl_pct"] = new_peak_pnl_pct
            await _db_update_branch_peak_pct(branch.bot_name, contract, new_peak_pnl_pct)

        if should_exit:
            entry = position["entry"]
            qty = position["qty"]
            pnl = (price - entry) * qty
            filled = await execute_futures_trade(session, contract, "SELL", qty, price, rsi, trend, target=price)
            if filled:
                open_alpaca_branch_positions.pop(branch.bot_name, None)
                await _db_delete_branch_open(branch.bot_name, contract)
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == branch.bot_name))
                    fresh = result.scalar_one_or_none()
                    new_balance = branch.allocated_usd + pnl
                    if fresh:
                        fresh.allocated_usd += pnl
                        await db.commit()
                        new_balance = fresh.allocated_usd
                await _log_alpaca_branch_trade(
                    branch.bot_name, contract, config["symbol"], entry, price, qty, pnl, reason,
                    position.get("open_time"),
                )
                log.info(
                    f"[ALPACA-BRANCH] {'📈' if pnl >= 0 else '📉'} {branch.bot_name} SOLD {contract} ({config['symbol']}) "
                    f"@ ${price:.2f} ({reason}) | entry ${entry:.2f} | P&L: ${pnl:+.2f} | branch now ${new_balance:.2f}"
                )
            else:
                log.warning(f"[ALPACA-BRANCH] {branch.bot_name}: real sell into {contract} did not fill - will retry next cycle")
        return

    # ---- Flat: real entry check, identical gates to the whole-account scan ----
    if kill_halted:
        return
    excluded_symbols = await get_effective_excluded_symbols()
    if config["symbol"] in excluded_symbols:
        return  # stays flat and waits - fixed to this one contract in this first slice, doesn't switch
    if strategy_family == "mean_reversion":
        should_enter, _reason = check_mean_reversion_entry_gate(rsi)
    else:
        should_enter, _reason = check_momentum_entry_gate(data, live_entry_variant)
    if not should_enter:
        return

    is_safe, margin_reason = check_margin_safety(
        buying_power, equity, len(open_prop_positions),
        extra_open_notional=_total_alpaca_branch_notional() + _total_opening_bar_notional(),
    )
    if not is_safe:
        log.warning(f"[ALPACA-BRANCH] {branch.bot_name}: margin safety blocked entry - {margin_reason}")
        return

    spend = min(branch.allocated_usd, buying_power)
    if spend < MIN_POSITION_NOTIONAL:
        log.info(f"[ALPACA-BRANCH] {branch.bot_name}: only ${spend:.2f} real spendable (min ${MIN_POSITION_NOTIONAL:.2f}) - waiting")
        return
    qty = round(spend / price, 6)
    if qty <= 0:
        return

    filled = await execute_futures_trade(session, contract, "BUY", qty, price, rsi, trend, stop_loss=price * 0.98, target=price * 1.03)
    if not filled:
        log.warning(f"[ALPACA-BRANCH] {branch.bot_name}: real buy into {contract} did not fill - will retry next cycle")
        return
    open_alpaca_branch_positions[branch.bot_name] = {"side": "long", "entry": price, "qty": qty, "open_time": datetime.now(ET), "peak_pnl_pct": 0.0}
    await _db_save_branch_open(branch.bot_name, contract, "long", price, qty)
    log.info(f"[ALPACA-BRANCH] 🟢 {branch.bot_name} BOUGHT {qty} {contract} ({config['symbol']}) @ ${price:.2f} (${spend:.2f} deployed)")


async def _pick_weakest_alpaca_branch_for_reinforcement(exclude_bot_name: str, also_exclude_bot_names: frozenset = frozenset()):
    """The direct Alpaca-side counterpart to crypto_family_tree_bot.py's
    _pick_weakest_branch_for_reinforcement() - picks the real branch with
    the lowest allocated_usd/next_unlock_tier ratio (the same real
    percentage the dashboard's own progress bars would show), excluding
    the branch doing the reinforcing and every bot_name already touched
    in this chain. A branch with no real next_unlock_tier yet (a legacy
    row created before this column existed) is never picked - it isn't
    participating in the chain mechanism until it has one. Returns the
    AlpacaBranch row, or None if there's no other real eligible branch."""
    branches = await get_alpaca_branches()
    candidates = [
        b for b in branches
        if b.active and b.bot_name != exclude_bot_name and b.bot_name not in also_exclude_bot_names
        and b.next_unlock_tier and b.next_unlock_tier > 0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda b: b.allocated_usd / b.next_unlock_tier)


async def _deploy_seed_into_weakest_alpaca_branch(
    session, target_branch, usd_amount: float, strategy_family: str, live_entry_variant: str
) -> bool:
    """Places a real market buy for usd_amount into target_branch's own
    fixed contract - but ONLY if target_branch is currently FLAT and its
    contract independently passes the exact same real entry gate a normal
    flat-branch cycle would require (check_momentum_entry_gate/
    check_mean_reversion_entry_gate off a fresh live price/RSI/trend
    fetch). Per the account owner's own explicit design: "chain
    opportunity is not an automatic trade" - reinforcement money is never
    forced into a contract that doesn't currently qualify on its own
    merits, even though the whole point of this call is to give it real
    capital. Returns True on a real successful fill; False (not flat,
    doesn't qualify right now, real order didn't fill, no live data)
    leaves the seed for the caller to refund."""
    if target_branch.bot_name in open_alpaca_branch_positions:
        log.info(f"[ALPACA-BRANCH] reinforcement: {target_branch.bot_name} is already holding a position - not an eligible target this pass")
        return False

    contract = target_branch.contract
    config = FUTURES.get(contract)
    if config is None:
        log.error(f"[ALPACA-BRANCH] reinforcement: {target_branch.bot_name}'s contract {contract!r} is not real - skipping")
        return False

    excluded_symbols = await get_effective_excluded_symbols()
    if config["symbol"] in excluded_symbols:
        log.info(f"[ALPACA-BRANCH] reinforcement: {config['symbol']} is currently auto-excluded - {target_branch.bot_name} doesn't qualify")
        return False

    data = await (get_price_rsi(session, config["symbol"]) if strategy_family == "mean_reversion" else get_price_momentum(session, config["symbol"]))
    if not data:
        log.warning(f"[ALPACA-BRANCH] reinforcement: could not fetch live data for {config['symbol']} - {target_branch.bot_name} doesn't qualify this cycle")
        return False
    price, rsi = data["price"], data["rsi"]

    if strategy_family == "mean_reversion":
        should_enter, reason = check_mean_reversion_entry_gate(rsi)
    else:
        should_enter, reason = check_momentum_entry_gate(data, live_entry_variant)
    if not should_enter:
        log.info(f"[ALPACA-BRANCH] reinforcement: {config['symbol']} does not currently qualify for entry ({reason}) - {target_branch.bot_name} skipped")
        return False

    qty = round(usd_amount / price, 6)
    if qty <= 0:
        return False
    filled = await execute_futures_trade(session, contract, "BUY", qty, price, rsi, data["trend"], stop_loss=price * 0.98, target=price * 1.03)
    if not filled:
        log.warning(f"[ALPACA-BRANCH] reinforcement: real buy into {target_branch.bot_name} ({contract}) did not fill")
        return False

    open_alpaca_branch_positions[target_branch.bot_name] = {"side": "long", "entry": price, "qty": qty, "open_time": datetime.now(ET), "peak_pnl_pct": 0.0}
    await _db_save_branch_open(target_branch.bot_name, contract, "long", price, qty)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == target_branch.bot_name))
        fresh = result.scalar_one_or_none()
        if fresh:
            fresh.allocated_usd += usd_amount
            await db.commit()
    return True


async def _alpaca_maybe_spawn_or_reinforce(branch, chain_visited: frozenset = frozenset(), chain_hops_remaining: int = None):
    """The Alpaca-side counterpart to crypto_family_tree_bot.py's
    _maybe_spawn_child() bounded chain - per the account owner's explicit
    request to bring the same "chain reaction" mechanism here. Called at
    the top of every real branch cycle (mirrors the crypto side's
    per-cycle catch-up spawn check, not just right after a sell).

    Same two independent real guarantees against a runaway chain as the
    crypto side:
    1. chain_visited - every bot_name touched anywhere in this chain is
       permanently excluded from being reinforced again in that same
       chain - a bounce-back to an earlier branch is structurally
       impossible.
    2. chain_hops_remaining (ALPACA_MAX_CHAIN_HOPS, 5 default) - a hard
       cap that strictly decrements every real hop.

    Deliberately narrower than the crypto side in two explicit ways (see
    the constants' own comments above): only ever reinforces a FLAT
    branch, gated by a real, independent re-check of the entry-quality
    gate; and there is no automatic new-branch spawn fallback yet - if no
    real eligible candidate exists, the seed is refunded and the tier
    reverted, same as a real order-fill failure."""
    if branch.allocated_usd is None or branch.next_unlock_tier is None:
        return
    if branch.allocated_usd < branch.next_unlock_tier:
        return

    effective_hops_remaining = chain_hops_remaining if chain_hops_remaining is not None else ALPACA_MAX_CHAIN_HOPS
    if effective_hops_remaining <= 0:
        return

    weakest = await _pick_weakest_alpaca_branch_for_reinforcement(
        exclude_bot_name=branch.bot_name, also_exclude_bot_names=chain_visited
    )
    if weakest is None:
        log.info(f"[ALPACA-BRANCH] {branch.bot_name} crossed ${branch.next_unlock_tier:,.2f} but no other real branch is eligible to reinforce right now")
        return

    milestone = branch.next_unlock_tier
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == branch.bot_name))
        fresh = result.scalar_one_or_none()
        if not fresh or fresh.allocated_usd < fresh.next_unlock_tier:
            return
        fresh.allocated_usd -= ALPACA_REINFORCEMENT_SEED_USD
        fresh.next_unlock_tier += ALPACA_UNLOCK_TIER_USD
        await db.commit()
        remaining = fresh.allocated_usd

    strategy_family = await get_live_strategy_family()
    live_entry_variant = await get_live_entry_variant()

    tried_bot_names = {weakest.bot_name}
    connector = aiohttp.TCPConnector(use_dns_cache=True, limit=10, limit_per_host=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=60, connect=20, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
        deployed = await _deploy_seed_into_weakest_alpaca_branch(session, weakest, ALPACA_REINFORCEMENT_SEED_USD, strategy_family, live_entry_variant)
        while not deployed:
            fallback = await _pick_weakest_alpaca_branch_for_reinforcement(
                exclude_bot_name=branch.bot_name, also_exclude_bot_names=chain_visited | tried_bot_names
            )
            if fallback is None:
                break
            weakest = fallback
            tried_bot_names.add(weakest.bot_name)
            deployed = await _deploy_seed_into_weakest_alpaca_branch(session, weakest, ALPACA_REINFORCEMENT_SEED_USD, strategy_family, live_entry_variant)

    if deployed:
        log.info(
            f"[ALPACA-BRANCH] 🌱💪 {branch.bot_name} crossed ${milestone:,.2f} - its ${ALPACA_REINFORCEMENT_SEED_USD:.2f} seed "
            f"went into {weakest.bot_name} ({weakest.contract}) | {branch.bot_name} continues with ${remaining:.2f}"
        )
        new_chain_visited = chain_visited | {branch.bot_name} | tried_bot_names
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == weakest.bot_name))
            fresh_recipient = result.scalar_one_or_none()
        if fresh_recipient is not None:
            await _alpaca_maybe_spawn_or_reinforce(
                fresh_recipient, chain_visited=new_chain_visited, chain_hops_remaining=effective_hops_remaining - 1
            )
    else:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == branch.bot_name))
            fresh2 = result.scalar_one_or_none()
            if fresh2:
                fresh2.allocated_usd += ALPACA_REINFORCEMENT_SEED_USD
                fresh2.next_unlock_tier -= ALPACA_UNLOCK_TIER_USD
                await db.commit()
        log.warning(
            f"[ALPACA-BRANCH] ⚠️ {branch.bot_name} crossed ${milestone:,.2f} but no real candidate could be reinforced "
            f"(none flat and qualifying) - refunded the ${ALPACA_REINFORCEMENT_SEED_USD:.2f} seed, will retry next cycle"
        )


async def get_next_eligible_alpaca_contract_for_new_branch():
    """Real, live pick of the best real FUTURES contract for a brand-new
    Alpaca branch to auto-spawn on - genuinely unclaimed
    (get_alpaca_branch_claimed_contracts) and not currently excluded
    (get_effective_excluded_symbols - the exact same real auto-exclusion +
    top-N-by-ROI ranking every other entry path already respects, so this
    can never pick a contract the live bot itself wouldn't otherwise be
    willing to enter). Among what's left, prefers the real highest latest
    backtested ROI (AlpacaBacktestRun) when any exists - a candidate with
    NO real backtest run yet ranks LAST, not first, since "no data" isn't
    "good data". Returns a real contract code (e.g. "MCL"), or None if
    nothing eligible remains."""
    claimed = await get_alpaca_branch_claimed_contracts()
    excluded_symbols = await get_effective_excluded_symbols()
    candidates = [
        contract for contract, config in FUTURES.items()
        if contract not in claimed and config["symbol"] not in excluded_symbols
    ]
    if not candidates:
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AlpacaBacktestRun.product_id, AlpacaBacktestRun.roi_pct_of_spend)
            .order_by(AlpacaBacktestRun.product_id, desc(AlpacaBacktestRun.run_at))
        )
        rows = result.all()
    latest_roi = {}
    for product_id, roi in rows:
        if product_id not in latest_roi:
            latest_roi[product_id] = roi

    def _rank_key(contract):
        roi = latest_roi.get(FUTURES[contract]["symbol"])
        return (roi is None, -(roi if roi is not None else 0.0))

    candidates.sort(key=_rank_key)
    return candidates[0]


async def _alpaca_idle_cash_sweep(session, buying_power: float, equity, active_branches: list, strategy_family: str, live_entry_variant: str, kill_halted: bool):
    """Real, per-cycle sweep of genuinely idle Alpaca buying power - real
    money not already allocated to any active branch - into real trading,
    instead of it sitting uninvested while an active branch mode is on.
    Per the account owner's explicit request, and their explicit choice
    when asked directly: top up an existing branch first, but also
    auto-open a brand-new branch on a real, currently-eligible contract
    once idle cash builds up beyond what existing branches need.

    At most ONE real deployment per cycle - same "don't rush several real
    orders off one pass" discipline the reinforcement chain already uses.
    Never runs while kill_halted (a real account-wide kill condition) -
    new capital is never deployed while the account itself is in trouble.

    Never runs unless real equity is at least ALPACA_IDLE_SWEEP_MIN_EQUITY_
    CUSHION_USD above the real, ratcheting equity_floor (the same global
    the account-wide EQUITY FLOOR BREACH close-everything logic already
    uses) - per the account owner's explicit follow-up request to make
    sure new capital deployment can never itself contribute to pushing
    the account into or through its own real floor."""
    if equity is None or (equity - equity_floor) < ALPACA_IDLE_SWEEP_MIN_EQUITY_CUSHION_USD:
        log.info(
            f"[ALPACA-BRANCH] Idle-cash sweep skipped - real equity "
            f"{'unavailable' if equity is None else f'${equity:.2f}'} isn't at least "
            f"${ALPACA_IDLE_SWEEP_MIN_EQUITY_CUSHION_USD:.2f} above the real floor ${equity_floor:,.2f}"
        )
        return
    if kill_halted:
        return
    already_allocated = sum(b.allocated_usd for b in active_branches)
    idle = buying_power - already_allocated
    if idle < ALPACA_IDLE_SWEEP_MIN_SPENDABLE_USD:
        return
    seed = ALPACA_IDLE_SWEEP_SEED_USD

    # Step 1: top up the real current weakest active branch, if any exists
    # and its own contract currently qualifies on its own merits (reuses
    # the exact same real entry-quality gate reinforcement already uses -
    # "idle cash isn't an automatic trade" either).
    weakest = await _pick_weakest_alpaca_branch_for_reinforcement(exclude_bot_name="")
    if weakest is not None:
        deployed = await _deploy_seed_into_weakest_alpaca_branch(session, weakest, seed, strategy_family, live_entry_variant)
        if deployed:
            log.info(
                f"[ALPACA-BRANCH] 💰 Idle-cash sweep: deposited ${seed:.2f} of real idle buying power "
                f"(${idle:.2f} was idle) into {weakest.bot_name} ({weakest.contract})"
            )
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(AlpacaBranch).where(AlpacaBranch.bot_name == weakest.bot_name))
                fresh_recipient = result.scalar_one_or_none()
            if fresh_recipient is not None:
                await _alpaca_maybe_spawn_or_reinforce(fresh_recipient)
            return

    # Step 2: no existing branch could take it right now (none flat and
    # qualifying, or no branches at all) - open a real brand-new branch on
    # the best currently-eligible unclaimed contract, if any.
    contract = await get_next_eligible_alpaca_contract_for_new_branch()
    if contract is None:
        log.info(f"[ALPACA-BRANCH] Idle-cash sweep: ${idle:.2f} idle but no real eligible branch or contract to deploy it into right now")
        return
    try:
        branch = await create_alpaca_branch(contract, seed)
    except ValueError as e:
        log.warning(f"[ALPACA-BRANCH] Idle-cash sweep: could not open a new branch on {contract}: {e}")
        return
    log.info(
        f"[ALPACA-BRANCH] 💰🌱 Idle-cash sweep: opened a real new branch {branch.bot_name} on {contract} "
        f"with ${seed:.2f} of real idle buying power (${idle:.2f} was idle)"
    )


async def run_alpaca_branches_cycle():
    """Real per-cycle driver for every active Alpaca branch - a true
    no-op unless is_alpaca_branch_mode_active() is on. Runs sequentially
    in the same real event loop run_prop_cycle() already uses (this file
    is deliberately single-threaded - see run()'s own comment on why one
    persistent loop matters here), right after the whole-account scan
    each pass. Respects the exact same STOP_TRADING and passive-mode
    checks the whole-account scan's own outer loop already does."""
    if not await is_alpaca_branch_mode_active():
        return
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return
    if await is_alpaca_passive_mode():
        return
    # Deliberately NOT early-returning when there are zero active branches
    # (unlike before the idle-cash sweep existed) - the sweep below needs
    # to be able to open a real FIRST branch too, not just top up ones
    # that already exist.
    branches = [b for b in await get_alpaca_branches() if b.active]

    connector = aiohttp.TCPConnector(use_dns_cache=True, limit=10, limit_per_host=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=60, connect=20, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
        equity = await get_account_equity(session)
        if equity is None:
            log.warning("[ALPACA-BRANCH] could not fetch real account equity - skipping this cycle")
            return
        buying_power = await get_account_buying_power(session)
        should_halt, halt_reason = check_kill_conditions(
            buying_power=buying_power, equity=equity, daily_loss=daily_pnl, open_position_count=len(open_prop_positions),
        )
        if should_halt:
            log.warning(f"[ALPACA-BRANCH] real account-wide kill condition active ({halt_reason}) - no branch entries this cycle")
        strategy_family = await get_live_strategy_family()
        live_entry_variant = await get_live_entry_variant()

        for branch in branches:
            try:
                await run_alpaca_branch_cycle(session, branch, equity, buying_power, strategy_family, live_entry_variant, kill_halted=should_halt)
            except Exception as e:
                log.error(f"[ALPACA-BRANCH] {branch.bot_name} cycle error: {e}")
            await asyncio.sleep(0.5)

        if buying_power is not None:
            try:
                await _alpaca_idle_cash_sweep(session, buying_power, equity, branches, strategy_family, live_entry_variant, kill_halted=should_halt)
            except Exception as e:
                log.error(f"[ALPACA-BRANCH] Idle-cash sweep error: {e}")


# ============================================================================
# OPENING-BAR LIVE TRADING (multi-entry elephant/tail breakout)
# ============================================================================
# Real, live connection of the validated opening-bar multi-entry backtest
# (see opening_bar_signals.py + alpaca_selection_backtest.py's own
# run_opening_bar_multi_entry_comparison - real 30-day/12-symbol results:
# multi-entry made $944.34/21 trades/57.1% win rate vs single-entry's
# $570.64/15 trades/60.0% win rate) into real order placement - per the
# account owner's own explicit "yes it's better... build this and get this
# live" authorization.
#
# Kept in a SEPARATE dict (open_opening_bar_positions), never
# open_prop_positions - the same "separate dict, own risk logic" isolation
# the ALPACA BRANCHES section above already established for a different
# reason (per-branch capital instead of account-wide). Here the reason is
# different: open_prop_positions' Pass 1 exit management unconditionally
# applies the whole-account RSI/momentum exit rules to EVERY position in
# that dict - an opening-bar position needs its own real STOP/PUSH exit
# logic instead, which would conflict if it shared that dict.
#
# LIVE EXECUTION MODEL: rather than reimplement the validated backward-
# looking replay (_replay_opening_bar_breakout_multi_entry) as a second,
# separately-written live streaming state machine - real risk of the two
# silently diverging, with real money on the line - this re-runs the EXACT
# SAME real function every cycle against TODAY's real bars fetched fresh
# (yesterday's session + today's session so far), and diffs its own output
# against open_opening_bar_positions to decide what to do: a leg the
# replay shows open right now (its last trade exits "SESSION_END", meaning
# the replay ran out of real data before finding a real exit) gets a real
# order placed if none is open yet; a leg the replay shows has since
# exited (STOP or a PUSH) gets a real order placed to close it if one is
# open. The live fill happens at real current market price, not the
# replay's own historical trigger/exit price - an honest, small,
# unavoidable gap from the backtest, the same kind of real fill-vs-backtest
# gap already true of every other live strategy in this file.
#
# Real, honest limitations, stated plainly rather than hidden:
# - A real held position here is tracked in-memory only (not persisted to
#   BotPosition/reloaded on restart the way open_prop_positions/AlpacaBranch
#   positions are) - a Railway restart mid-leg means this dict comes back
#   empty even though Alpaca itself still holds the real shares. Accepted
#   for this first live version, same as AlpacaBranch's own narrower
#   first-slice scope was explicitly accepted earlier this session - a
#   real gap to close in a later pass, not silently ignored.
# - A PUSH exit and the next leg's own entry can land in different real
#   cycles (this cycle exits leg N; a LATER cycle enters leg N+1 once the
#   replay shows its own trigger has fired) - a brief, honestly-accepted
#   flat gap versus the backtest's perfectly seamless roll, chosen
#   deliberately over a more complex same-cycle roll that would be harder
#   to verify correct without live data to test against.
# ============================================================================

OPENING_BAR_LIVE_MODE_KEY = "opening_bar_live_mode"


async def is_opening_bar_live_active() -> bool:
    """DB-persisted (same generic TradingBotState bucket pattern every
    other real-time flag in this file already uses, not a Railway env var
    - avoids the exact stray-quote-character bug class that silently
    disabled the crypto coordinator earlier this session). False (off) by
    default - a true no-op until the account owner explicitly turns it on
    from the dashboard."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == OPENING_BAR_LIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        return bool(row and row.base_capital and row.base_capital >= 1.0)


async def set_opening_bar_live_active(enabled: bool):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == OPENING_BAR_LIVE_MODE_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            row = TradingBotState(bot_name=OPENING_BAR_LIVE_MODE_KEY, base_capital=0.0)
            db.add(row)
        row.base_capital = 1.0 if enabled else 0.0
        await db.commit()


# Real per-symbol live opening-bar positions, kept SEPARATE from
# open_prop_positions and open_alpaca_branch_positions - see the section
# docstring above for why.
open_opening_bar_positions = {}


def _total_opening_bar_notional() -> float:
    """Real notional value currently held across every open opening-bar
    position - see check_margin_safety's extra_open_notional param."""
    return sum(p.get("qty", 0) * p.get("entry_price", 0) for p in open_opening_bar_positions.values())


async def _fetch_live_2min_bars_for_opening_bar(session, symbol: str, days: int = 3):
    """Real, live 2-minute OHLC bars + real UTC timestamps, same
    timeframe/feed as the already-validated backtest's own
    _fetch_bars_2min_with_ohlc_and_times() - a live-fetch duplicate rather
    than a shared import, since that function lives in
    alpaca_selection_backtest.py, which already imports FROM this file (a
    circular import the other way otherwise). days=3 is enough to always
    include yesterday's full real session plus today's real bars so far,
    regardless of a weekend/holiday skipping a day. Returns (bars, None)
    or (None, reason)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=2Min&start={start}&limit=10000&feed=iex"
    try:
        async with session.get(url, headers=get_headers(), timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                body = (await r.text())[:200]
                return None, f"HTTP {r.status}: {body}"
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 20:
                return None, f"only {len(bars)} bars (need 20+)"
            return [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars], None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


async def run_opening_bar_symbol_cycle(session, contract: str, config: dict, equity: float, buying_power: float, kill_halted: bool):
    """One real cycle for one real symbol's opening-bar signal - see the
    section docstring above for the live execution model. Long-only,
    matching this account's real shorting-disabled constraint (unchanged
    from every other strategy in this file)."""
    symbol = config["symbol"]
    bars, err = await _fetch_live_2min_bars_for_opening_bar(session, symbol)
    if bars is None:
        return

    days_grouped = _group_bars_by_day(bars)
    if len(days_grouped) < 2:
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date, session_bars = days_grouped[-1]
    if last_date != today_str:
        return  # market hasn't produced a real bar today yet
    preceding_bars = days_grouped[-2][1][-ELEPHANT_BAR_LOOKBACK * 2:]

    trades = _replay_opening_bar_breakout_multi_entry(session_bars, preceding_bars, max_entries_per_day=OPENING_BAR_MAX_ENTRIES_PER_DAY)
    if not trades:
        return  # bar 1 never qualified today, or no leg has triggered yet

    position = open_opening_bar_positions.get(contract)
    last_trade = trades[-1]
    current_price = session_bars[-1]["c"]

    if last_trade["exit_reason"] == "SESSION_END":
        # This leg is open right now - the real replay ran out of real
        # data before finding a real exit, meaning it's still live.
        if position is not None:
            return  # already holding this leg
        if kill_halted:
            return
        if contract in open_prop_positions:
            return  # the whole-account scan already holds this real contract
        if contract in await get_alpaca_branch_claimed_contracts():
            return  # a real Alpaca branch already claims this contract
        is_safe, reason = check_margin_safety(
            buying_power, equity, len(open_prop_positions),
            extra_open_notional=_total_alpaca_branch_notional() + _total_opening_bar_notional(),
        )
        if not is_safe:
            log.warning(f"[OPENING-BAR] {contract} ({symbol}): margin safety blocked entry - {reason}")
            return
        if buying_power is None:
            return
        qty = size_position(buying_power, 1, current_price, account_equity=equity)
        if qty is None:
            log.info(f"[OPENING-BAR] {contract} ({symbol}): only ${buying_power:.2f} real buying power - skipping entry")
            return
        filled = await execute_futures_trade(
            session, contract, "BUY", qty, current_price, 0.0, f"OPENING_BAR_{last_trade['qualifies_as'].upper()}",
            stop_loss=last_trade["stop_price"], target=None,
        )
        if filled:
            open_opening_bar_positions[contract] = {
                "entry_price": current_price, "qty": qty, "stop_price": last_trade["stop_price"],
                "leg_number": last_trade["leg_number"], "qualifies_as": last_trade["qualifies_as"],
                "open_time": datetime.now(ET),
            }
            log.info(
                f"[OPENING-BAR] 🟢 {contract} ({symbol}) leg {last_trade['leg_number']} ({last_trade['qualifies_as']}) "
                f"OPENED @ ${current_price:.2f} | qty {qty} | real stop ${last_trade['stop_price']:.2f}"
            )
        else:
            log.warning(f"[OPENING-BAR] {contract} ({symbol}): real buy did not fill - will retry next cycle")
    else:
        # STOP or a real PUSH - this leg has exited.
        if position is None:
            return  # nothing real currently held here to close
        entry = position["entry_price"]
        qty = position["qty"]
        pnl = (current_price - entry) * qty
        filled = await execute_futures_trade(session, contract, "SELL", qty, current_price, 0.0, "OPENING_BAR_EXIT", target=current_price)
        if filled:
            open_opening_bar_positions.pop(contract, None)
            log.info(
                f"[OPENING-BAR] {'📈' if pnl >= 0 else '📉'} {contract} ({symbol}) leg {position['leg_number']} "
                f"CLOSED ({last_trade['exit_reason']}) @ ${current_price:.2f} | entry ${entry:.2f} | P&L: ${pnl:+.2f}"
            )
            global daily_pnl
            daily_pnl += pnl
            try:
                await _db_save_closed_trade(contract, "long", entry, current_price, qty, pnl, f"OPENING_BAR {last_trade['exit_reason']}")
            except Exception as e:
                log.warning(f"[OPENING-BAR] {contract}: failed to persist closed trade: {e}")
        else:
            log.warning(f"[OPENING-BAR] {contract} ({symbol}): real sell did not fill - will retry next cycle")


async def run_opening_bar_live_cycle():
    """Real per-cycle driver for the opening-bar live system - a true
    no-op unless is_opening_bar_live_active() is on. Runs right after the
    Alpaca branches cycle, same real single-threaded event loop, same
    STOP_TRADING/passive-mode checks every other real-time subsystem in
    this file already respects."""
    if not await is_opening_bar_live_active():
        return
    if os.getenv("STOP_TRADING", "false").lower() == "true":
        return
    if await is_alpaca_passive_mode():
        return

    connector = aiohttp.TCPConnector(use_dns_cache=True, limit=10, limit_per_host=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=60, connect=20, sock_read=30, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
        equity = await get_account_equity(session)
        if equity is None:
            log.warning("[OPENING-BAR] could not fetch real account equity - skipping this cycle")
            return
        buying_power = await get_account_buying_power(session)
        should_halt, halt_reason = check_kill_conditions(
            buying_power=buying_power, equity=equity, daily_loss=daily_pnl, open_position_count=len(open_prop_positions),
        )
        if should_halt:
            log.warning(f"[OPENING-BAR] real account-wide kill condition active ({halt_reason}) - no new opening-bar entries this cycle")

        for contract, config in FUTURES.items():
            try:
                await run_opening_bar_symbol_cycle(session, contract, config, equity, buying_power, kill_halted=should_halt)
            except Exception as e:
                log.error(f"[OPENING-BAR] {contract} cycle error: {e}")
            await asyncio.sleep(0.3)


def check_credentials():
    """Verify Alpaca API credentials are configured before starting."""
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    base_url = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")

    if not api_key or not secret_key:
        log.error("❌ ALPACA CREDENTIALS NOT SET")
        log.error("   ALPACA_API_KEY is missing or empty")
        log.error("   ALPACA_SECRET_KEY is missing or empty")
        log.error("   Bot will NOT be able to authenticate to Alpaca")
        log.error("   Set these in Railway Variables and redeploy")
        return False

    log.info(f"✅ Credentials configured")
    log.info(f"   API Key (first 10 chars): {api_key[:10]}...")
    log.info(f"   Base URL: {base_url}")
    return True


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — PROP BOT v3")
    log.info(f"Account: APEX_589296 | Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"RSI thresholds: long entry < {RSI_BUY_BELOW} | short entry > {RSI_SELL_ABOVE} (trades both directions)")
    log.info(f"Profitable days: {len(profitable_days)}/7 needed")
    log.info("=" * 60)

    # Verify credentials before starting
    if not check_credentials():
        log.error("⚠️  STARTUP BLOCKED - Credentials not configured")
        log.error("   Waiting for manual credential setup...")
        while True:
            time.sleep(60)
            if os.getenv("ALPACA_API_KEY", "").strip() and os.getenv("ALPACA_SECRET_KEY", "").strip():
                log.info("✅ Credentials detected! Restarting bot...")
                break

    try:
        asyncio.run(load_open_positions())
    except Exception as e:
        log.error(f"[APEX_589296] Startup position reload failed: {e}")

    try:
        asyncio.run(load_equity_floor())
    except Exception as e:
        log.error(f"[APEX_589296] Startup equity floor reload failed: {e}")

    try:
        asyncio.run(load_alpaca_branch_positions())
    except Exception as e:
        log.error(f"[APEX_589296] Startup Alpaca branch position reload failed: {e}")

    # One persistent event loop for this thread's entire life, not a fresh
    # asyncio.run() per cycle - the same repeated create/destroy pattern
    # already caused a full thread crash in alpaca_swing_bot.py today
    # ("Task ... got Future ... attached to a different loop"), traced to
    # main.py's uvicorn server installing uvloop's event loop policy
    # process-wide. A single loop, reused via run_until_complete(), avoids it.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue
        if loop.run_until_complete(is_alpaca_passive_mode()):
            log.info("Alpaca passive mode active - active trading retired, holding a real buy-and-hold SPY position only")
            time.sleep(300)
            continue
        try:
            loop.run_until_complete(run_prop_cycle())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[APEX_589296] Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"Prop cycle error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"Prop cycle error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")

        # Real Alpaca branches (see the ALPACA BRANCHES section above) -
        # a true no-op unless explicitly turned on. Run right after the
        # whole-account scan, in the same real event loop/single-threaded
        # design as everything else in this file.
        try:
            loop.run_until_complete(run_alpaca_branches_cycle())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[ALPACA-BRANCH] Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"Alpaca branch cycle error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"Alpaca branch cycle error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")

        # Real opening-bar live trading (see that section above) - a true
        # no-op unless explicitly turned on. Run right after the Alpaca
        # branches cycle, same real event loop/single-threaded design.
        try:
            loop.run_until_complete(run_opening_bar_live_cycle())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[OPENING-BAR] Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"Opening-bar live cycle error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"Opening-bar live cycle error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")

        time.sleep(30)


if __name__ == "__main__":
    run()       
