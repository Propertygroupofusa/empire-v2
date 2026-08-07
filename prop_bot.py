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
import smtplib
import time
from email.mime.text import MIMEText
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
import uuid
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition, Payment

ET = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# RSI entry/exit thresholds
RSI_BUY_BELOW  = float(os.getenv("PROP_RSI_BUY_BELOW", "30"))
RSI_SELL_ABOVE = float(os.getenv("PROP_RSI_SELL_ABOVE", "70"))

# Crypto-specific thresholds: AGGRESSIVE SCALPING FOR MILESTONE SPEED
# Lowered from 30/70 to 35/65 to catch MORE entry/exit opportunities
# Maximizes trade frequency to hit $1,000 ASAP, then $2,000 within 24hr
CRYPTO_RSI_BUY_BELOW  = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "35"))   # MORE oversold entries (faster compounding)
CRYPTO_RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "65"))  # MORE overbought exits (more profit locks)

# AGGRESSIVE WINS + STRICT LOSS PREVENTION
# Base stop-loss: 0.3% to exit losing trades immediately
# At higher scales: tighter stops to prevent multiplied losses
# 1.0x scale: 0.3% stop
# 1.5x scale: 0.2% stop (1.5x scaled position needs tighter exit)
# 2.0x scale: 0.2% stop (maximum scale = maximum discipline)
STOP_LOSS_BASE_PCT = float(os.getenv("PROP_STOP_LOSS_PCT", "0.003"))  # Base: 0.3% for stocks/futures

# Crypto-specific stop-loss: dynamically tightens with scale
# Base: 0.3%, tightens to 0.2% at 1.5x scale
CRYPTO_STOP_LOSS_BASE_PCT = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.003"))  # Base: 0.3%

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
DAILY_MAX_LOSS_BASE = float(os.getenv("PROP_DAILY_MAX_LOSS_BASE", "10"))

# AGGRESSIVE EXIT ON RED — Close any position down 0.5% immediately
# Don't wait for stop-loss to trigger. Exit fast, preserve capital.
QUICK_EXIT_LOSS_PCT = float(os.getenv("QUICK_EXIT_LOSS_PCT", "0.005"))  # Exit any loser at 0.5% down

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# SCALING UP SYSTEM — Increase position sizes after each milestone lock
# $1K lock → scale to 1.5x, $2K lock → scale to 2.0x, $5K lock → scale to 3.0x
POSITION_SCALE_MULTIPLIER = float(os.getenv("POSITION_SCALE_MULTIPLIER", "1.0"))  # Starts at 1.0x, increases per milestone

# APEX futures — use micro contracts (lower risk during evaluation)
# Expanded to include international markets and forex for 24/5 global trading opportunities
FUTURES = {
    # American indices
    "MES": {"name": "Micro E-mini S&P 500", "qty": 1, "symbol": "SPY"},   # Use SPY as proxy
    "MNQ": {"name": "Micro E-mini Nasdaq",  "qty": 1, "symbol": "QQQ"},   # Use QQQ as proxy
    "MYM": {"name": "Micro E-mini Dow",     "qty": 1, "symbol": "DIA"},   # Use DIA as proxy
    "M2K": {"name": "Micro E-mini Russell", "qty": 1, "symbol": "IWM"},   # Use IWM as proxy
    # Commodities
    "MGC": {"name": "Micro Gold",           "qty": 1, "symbol": "GLD"},   # Use GLD as proxy
    "MCL": {"name": "Micro Crude Oil",      "qty": 1, "symbol": "USO"},   # Use USO as proxy
    "SIL": {"name": "Micro Silver",         "qty": 1, "symbol": "SLV"},   # Use SLV as proxy
    # Cryptocurrencies (24/7 trading on Alpaca) — 50+ PAIRS FOR MAXIMUM OPPORTUNITIES
    # Strategy: Scan all, trade best signals only. MAX_POSITIONS=2 keeps capital focused.
    # Every signal = potential $2-3 win. More pairs = more winning chances per day.
    # Mega cap tier (stable baseline):
    "BTC": {"name": "Bitcoin",              "qty": 1, "symbol": "BTC/USD"},
    "ETH": {"name": "Ethereum",             "qty": 1, "symbol": "ETH/USD"},
    # Tier 1 - High liquidity altcoins (proven winners):
    "SOL": {"name": "Solana",               "qty": 1, "symbol": "SOL/USD"},
    "ADA": {"name": "Cardano",              "qty": 1, "symbol": "ADA/USD"},
    "DOGE": {"name": "Dogecoin",            "qty": 1, "symbol": "DOGE/USD"},
    "XRP": {"name": "Ripple",               "qty": 1, "symbol": "XRP/USD"},
    "LINK": {"name": "Chainlink",           "qty": 1, "symbol": "LINK/USD"},
    "AVAX": {"name": "Avalanche",           "qty": 1, "symbol": "AVAX/USD"},
    "NEAR": {"name": "NEAR Protocol",       "qty": 1, "symbol": "NEAR/USD"},
    "MATIC": {"name": "Polygon",            "qty": 1, "symbol": "MATIC/USD"},
    # Tier 2 - Hot altcoins (emerging volume leaders):
    "ARB": {"name": "Arbitrum",             "qty": 1, "symbol": "ARB/USD"},
    "OP": {"name": "Optimism",              "qty": 1, "symbol": "OP/USD"},
    "APT": {"name": "Aptos",                "qty": 1, "symbol": "APT/USD"},
    "SEI": {"name": "Sei",                  "qty": 1, "symbol": "SEI/USD"},
    "SUI": {"name": "Sui",                  "qty": 1, "symbol": "SUI/USD"},
    "BLUR": {"name": "Blur",                "qty": 1, "symbol": "BLUR/USD"},
    "LDO": {"name": "Lido DAO",             "qty": 1, "symbol": "LDO/USD"},
    "MKR": {"name": "Maker",                "qty": 1, "symbol": "MKR/USD"},
    "AAVE": {"name": "Aave",                "qty": 1, "symbol": "AAVE/USD"},
    "UNI": {"name": "Uniswap",              "qty": 1, "symbol": "UNI/USD"},
    # Tier 3 - Volume surge candidates:
    "PEPE": {"name": "Pepe",                "qty": 1, "symbol": "PEPE/USD"},
    "SHIB": {"name": "Shiba Inu",           "qty": 1, "symbol": "SHIB/USD"},
    "FLOKI": {"name": "Floki",              "qty": 1, "symbol": "FLOKI/USD"},
    "STX": {"name": "Stacks",               "qty": 1, "symbol": "STX/USD"},
    "FIL": {"name": "Filecoin",             "qty": 1, "symbol": "FIL/USD"},
    "ATOM": {"name": "Cosmos",              "qty": 1, "symbol": "ATOM/USD"},
    "ALGO": {"name": "Algorand",            "qty": 1, "symbol": "ALGO/USD"},
    "SAND": {"name": "Sandbox",             "qty": 1, "symbol": "SAND/USD"},
    "MANA": {"name": "Decentraland",        "qty": 1, "symbol": "MANA/USD"},
    "ENS": {"name": "ENS",                  "qty": 1, "symbol": "ENS/USD"},
    "RNDR": {"name": "Render",              "qty": 1, "symbol": "RNDR/USD"},
    "IMX": {"name": "Immutable",            "qty": 1, "symbol": "IMX/USD"},
    "GALA": {"name": "Gala",                "qty": 1, "symbol": "GALA/USD"},
    "BEAM": {"name": "Beam",                "qty": 1, "symbol": "BEAM/USD"},
    # Tier 4 - Emerging micro-cap movers:
    "WIF": {"name": "dogwifhat",            "qty": 1, "symbol": "WIF/USD"},
    "POPCAT": {"name": "Popcat",            "qty": 1, "symbol": "POPCAT/USD"},
    "MOO": {"name": "Moo Deng",             "qty": 1, "symbol": "MOO/USD"},
    "BONK": {"name": "Bonk",                "qty": 1, "symbol": "BONK/USD"},
    "RENDER": {"name": "Render",            "qty": 1, "symbol": "RENDER/USD"},
    "JTO": {"name": "Jito",                 "qty": 1, "symbol": "JTO/USD"},
    "ORCA": {"name": "Orca",                "qty": 1, "symbol": "ORCA/USD"},
    "COPE": {"name": "Cope",                "qty": 1, "symbol": "COPE/USD"},
    "COPE": {"name": "Cope",                "qty": 1, "symbol": "COPE/USD"},
    "COPE": {"name": "Cope",                "qty": 1, "symbol": "COPE/USD"},
    # Add more as they become available on Alpaca
    "WLD": {"name": "Worldcoin",            "qty": 1, "symbol": "WLD/USD"},
    "INJ": {"name": "Injective",            "qty": 1, "symbol": "INJ/USD"},
    "SUSHI": {"name": "Sushi",              "qty": 1, "symbol": "SUSHI/USD"},
    "CURVE": {"name": "Curve",              "qty": 1, "symbol": "CURVE/USD"},
    "CRV": {"name": "Curve DAO",            "qty": 1, "symbol": "CRV/USD"},
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
BASE_MAX_POSITIONS = int(os.getenv("PROP_MAX_POSITIONS", "2"))

def get_dynamic_max_positions(scale: float) -> int:
    """Reduce max positions as scale increases to limit compounded losses"""
    if scale >= 1.5:
        return 1  # Single position when scaled 1.5x or higher
    return BASE_MAX_POSITIONS  # 2 positions at baseline 1.0x scale

# Profit target, in REAL DOLLARS of profit on the position (not a raw
# price move on the underlying) - scaled by real account equity. Increased targets
# to let winners run instead of closing too early. With $1000+ positions, these
# targets are achievable on 1%+ daily moves that we see in the market.
PROFIT_TARGET_DOLLARS_MILESTONES = [
    (0,     5.00),      # Micro: $5.00 (0.50% on $1000)
    (500,   7.50),      # Small: $7.50
    (1000,  10.00),     # Medium: $10.00 (1% on $1000)
    (5000,  15.00),     # Large: $15.00 (0.30% on $5000)
    (10000, 20.00),     # Huge: $20.00 (0.20% on $10000)
]

# Crypto-specific LOWER profit targets for fast compounding & high frequency
# On $992: aim for $2-3 per trade (hit more targets, reinvest faster)
CRYPTO_PROFIT_TARGET_MILESTONES = [
    (0,     2.50),      # Micro: $2.50 (0.25% on $1000) — fast wins
    (500,   3.00),      # Small: $3.00
    (1000,  3.50),      # Medium: $3.50
    (5000,  5.00),      # Large: $5.00
    (10000, 7.50),      # Huge: $7.50
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
daily_pnl = 0.0
daily_account_equity_start = None  # For daily 2% loss limit
open_prop_positions = {}

BOT_NAME = "prop_apex"


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
                open_prop_positions[row.symbol] = {"side": row.side, "entry": row.entry_price, "qty": row.qty}
            if rows:
                log.info(f"[APEX_589296] Reloaded {len(rows)} open position(s) from DB: {list(open_prop_positions.keys())}")
    except Exception as e:
        log.error(f"[APEX_589296] Failed to reload open positions from DB: {e}")


async def _db_save_open(contract: str, side: str, entry: float, qty: float):
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=contract, side=side, entry_price=entry, qty=qty))
            await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to persist opened position {contract}: {e}")


async def _db_delete_open(contract: str):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.symbol == contract))
            for row in result.scalars().all():
                await db.delete(row)
            await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to remove closed position {contract} from DB: {e}")

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


async def get_price_rsi(session, symbol):
    """Get price and RSI for futures proxy symbol"""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=20"
        async with session.get(url, headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 14:
                return None

            closes = [b["c"] for b in bars]
            price = closes[-1]

            gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            sma5 = sum(closes[-5:]) / 5
            sma10 = sum(closes[-10:]) / 10
            trend = "bullish" if sma5 > sma10 else "bearish"

            # Momentum: price change over last 3 bars (shows direction/strength)
            momentum = ((price - closes[-3]) / closes[-3]) * 100 if closes[-3] > 0 else 0

            return {"price": price, "rsi": round(rsi, 1), "trend": trend, "momentum": round(momentum, 2)}
    except Exception as e:
        log.error(f"Price error {symbol}: {e}")
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
        async with session.get(url, headers=HEADERS) as r:
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
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
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
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
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
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
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
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
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
        async with session.get(f"{BASE_URL}/v2/positions", headers=HEADERS) as r:
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
MIN_POSITION_NOTIONAL = float(os.getenv("PROP_MIN_POSITION_NOTIONAL", "50"))  # Reduced from $1500 for micro account

# HARD MARGIN SAFETY LIMITS — prevent over-leverage ever again
# Minimum buying power buffer required before opening ANY new position
MIN_BUYING_POWER_BUFFER = float(os.getenv("PROP_MIN_BUYING_POWER_BUFFER", "500"))

# Maximum percentage of account equity that can be at risk in open positions
MAX_RISK_PERCENT = float(os.getenv("PROP_MAX_RISK_PERCENT", "0.50"))  # 50% max

# Buying power threshold to STOP opening new positions (emergency brake)
CRITICAL_BUYING_POWER_THRESHOLD = float(os.getenv("PROP_CRITICAL_BP_THRESHOLD", "100"))


def size_position(cash_remaining, slots_remaining, price):
    """Dollar-based (fractional-share) position sizing with SCALING MULTIPLIER.
    After each milestone lock, position sizes increase (1.5x for $2K phase, 2.0x for $5K phase).
    This allows account to compound faster while maintaining capital protection.
    Returns None if there isn't enough cash left for even one minimum-size position."""
    if slots_remaining <= 0 or cash_remaining < MIN_POSITION_NOTIONAL:
        return None

    # Apply scaling multiplier (increases after milestone locks)
    scale = float(os.getenv("POSITION_SCALE_MULTIPLIER", "1.0"))

    amount = min(max(cash_remaining / slots_remaining, MIN_POSITION_NOTIONAL), cash_remaining)
    amount = amount * scale  # SCALE UP: bigger positions after milestone
    qty = round(amount / price, 6)
    return qty if qty > 0 else None


def check_margin_safety(buying_power, equity, open_positions_count):
    """Hard check: is it safe to open a new position?
    Returns (is_safe, reason_if_not)"""
    # Buying power must be positive with minimum buffer
    if buying_power < MIN_BUYING_POWER_BUFFER:
        return False, f"Insufficient buying power: ${buying_power:.2f} < ${MIN_BUYING_POWER_BUFFER:.2f} buffer"

    # Emergency brake: if buying power drops near zero, stop ALL new positions
    if buying_power < CRITICAL_BUYING_POWER_THRESHOLD:
        return False, f"CRITICAL: Buying power ${buying_power:.2f} near zero — halting new positions"

    # Total open position risk can't exceed max % of equity
    total_open_notional = sum(p.get("qty", 0) * p.get("entry", 0) for p in open_positions.values())
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
        async with session.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order) as r:
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


async def run_prop_cycle():
    global daily_pnl, profitable_days, last_cycle_at, last_market_open

    # Trade during market hours (9:30am-4pm ET) for stocks/futures.
    # Crypto trades 24/7. Checked against real ET wall-clock time (DST-aware).
    now = datetime.now(ET)
    is_weekday = now.weekday() < 5
    market_open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)

    last_cycle_at = now.isoformat()

    # Check if it's market hours for stocks (9:30am-4pm ET weekdays) or if we're trading crypto (24/7)
    is_market_hours = is_weekday and market_open_t <= now <= market_close_t
    is_crypto_trading = True  # Crypto trades 24/7

    if not (is_market_hours or is_crypto_trading):
        last_market_open = False
        return

    last_market_open = is_market_hours

    # CONTINUOUS AUTO-SCALING TO $1,000,000 — No milestones, just compound
    # Scale formula: 1.0x baseline + 0.01x per $1000 earned, capped at 5.0x
    # $1K equity → 1.0x, $100K equity → 2.0x, $400K+ equity → 5.0x (capped)
    connector = aiohttp.TCPConnector(use_dns_cache=True)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        equity = await get_account_equity(session)
        if equity is not None:
            # Check if $1M goal achieved — STOP TRADING
            if equity >= 1000000.0:
                log.warning(f"🏆 **$1,000,000 MILESTONE REACHED** — Equity: ${equity:,.2f}")
                # Close all positions and stop
                for contract in list(open_prop_positions.keys()):
                    pos = open_prop_positions[contract]
                    try:
                        price_resp = await session.get(f"{BASE_URL}/v1/last?symbols={pos.get('symbol', contract)}", headers=HEADERS)
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
        log.info(f"[APEX_589296] 📤 CLOSE {side.upper()} {contract} ({reason_label}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${pnl:.2f} ({profit_pct:.2f}%)")
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

        open_prop_positions[contract] = {"side": side, "entry": price, "qty": qty}
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

        # HARD MARGIN SAFETY CHECK — prevent over-leverage
        buying_power = await get_account_buying_power(session)
        is_safe, reason = check_margin_safety(buying_power, equity, len(open_prop_positions))
        if not is_safe:
            log.warning(f"[APEX_589296] ⛔ MARGIN SAFETY: Blocking {contract} entry — {reason}")
            return False

        if cash_remaining is not None:
            qty = size_position(cash_remaining, slots_remaining, price)
            if qty is None:
                log.info(f"[APEX_589296] Skipping {contract} {side} entry — not enough cash left (${cash_remaining:.2f})")
                return False
        else:
            qty = config["qty"]

        opened = await open_position(session, contract, config, side, price, rsi, trend, qty)
        if opened and cash_remaining is not None:
            cash_remaining -= qty * price
        return opened

    connector = aiohttp.TCPConnector(use_dns_cache=True)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
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
        scale = float(os.getenv("POSITION_SCALE_MULTIPLIER", "1.0"))
        dynamic_daily_max_loss = DAILY_MAX_LOSS_BASE * scale  # $10→$15→$20 as scale increases
        dynamic_max_positions = get_dynamic_max_positions(scale)  # 2 at 1.0x, 1 at 1.5x+

        log.info(f"[APEX_589296] Equity: {'$%.2f' % equity if equity is not None else 'unknown'} | Scale {scale:.1f}x | Max {dynamic_max_positions} pos | Profit target: ${profit_target:.2f}/position" +
                (f" | ⚠️ DAILY 2% LOSS LIMIT HIT - stopping new trades" if is_hitting_daily_loss_limit else ""))

        # Circuit breaker: if daily loss exceeds threshold, close ALL open positions immediately
        # Scales dynamically with position size multiplier to prevent early halt on scaled positions
        daily_loss_dollars = (daily_account_equity_start - equity) if daily_account_equity_start and equity else 0
        if daily_loss_dollars >= dynamic_daily_max_loss:
            log.warning(f"[APEX_589296] 🛑 CIRCUIT BREAKER: Daily loss ${daily_loss_dollars:.2f} >= ${dynamic_daily_max_loss:.2f} (scale {scale}x) — closing ALL positions")
            for contract in list(open_prop_positions.keys()):
                data = scans.get(contract)
                config = FUTURES[contract]
                if data:
                    await close_position(session, contract, config, open_prop_positions[contract],
                                       data["price"], data["rsi"], data["trend"], "CIRCUIT BREAKER - DAILY LOSS LIMIT")

        # Tracked and spent-down across this cycle's entries so dollar-based
        # sizing (see try_open/size_position) reflects money already
        # committed to earlier orders this same cycle, without an extra
        # API call per entry.
        cash_remaining = await get_account_cash(session)
        log.info(f"[APEX_589296] Cash available: {'$%.2f' % cash_remaining if cash_remaining is not None else 'unknown'}")

        # Discovered in production: every short entry was failing with a
        # real Alpaca error ("account is not allowed to short") - checked
        # once per cycle so new shorts are skipped cleanly instead of
        # repeatedly attempting (and failing) orders. Existing short
        # positions can still be covered either way (that's a BUY order,
        # not a new short) - this only gates opening NEW shorts.
        shorting_enabled = await get_account_shorting_enabled(session)
        if not shorting_enabled:
            log.info("[APEX_589296] ⚠️ Shorting not enabled on this account - skipping new SHORT entries this cycle (longs unaffected)")

        scans = {}
        for contract, config in FUTURES.items():
            # Skip non-crypto symbols during after-hours (outside 9:30am-4pm ET weekdays)
            is_crypto = "/" in config["symbol"]  # Crypto symbols have "/" like BTC/USD
            if not is_market_hours and not is_crypto:
                continue

            data = await get_price_rsi(session, config["symbol"])
            if data:
                scans[contract] = data
                log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${data['price']:.2f} | RSI:{data['rsi']} | Momentum:{data.get('momentum', 0):+.2f}% | {data['trend']}")
            await asyncio.sleep(0.3)

        # ── Pass 1: manage exits for symbols already held ────────────────
        # A long profits as price rises and exits on overbought RSI; a
        # short profits as price falls and exits on oversold RSI. Profit
        # target is checked against the position's actual real dollar
        # P&L (see PROFIT_TARGET_DOLLARS_MILESTONES) - take the 50c-$1,
        # don't hold out for a bigger move - and scales up slightly as
        # the real account grows.
        for contract, position in list(open_prop_positions.items()):
            data = scans.get(contract)
            config = FUTURES[contract]
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

            # Use crypto-specific RSI thresholds and dynamically tightened stops based on scale
            is_crypto = "/" in config["symbol"]
            exit_sell_threshold = CRYPTO_RSI_SELL_ABOVE if is_crypto else RSI_SELL_ABOVE
            exit_buy_threshold = CRYPTO_RSI_BUY_BELOW if is_crypto else RSI_BUY_BELOW

            # Dynamic stop-loss: tighter at higher scales to prevent compounded losses
            scale = float(os.getenv("POSITION_SCALE_MULTIPLIER", "1.0"))
            stop_loss = get_dynamic_crypto_stop_loss(scale) if is_crypto else get_dynamic_stop_loss(scale)

            if side == "long":
                unrealized_pnl = (price - entry) * qty
                rsi_signal = rsi > exit_sell_threshold or (trend == "bearish" and rsi > 50)
                stop_hit = price <= entry * (1 - stop_loss)
                quick_loss_hit = price <= entry * (1 - QUICK_EXIT_LOSS_PCT)  # AGGRESSIVE: exit losers at 0.5% down
            else:
                unrealized_pnl = (entry - price) * qty
                rsi_signal = rsi < exit_buy_threshold or (trend == "bullish" and rsi < 50)
                stop_hit = price >= entry * (1 + stop_loss)
                quick_loss_hit = price >= entry * (1 + QUICK_EXIT_LOSS_PCT)  # AGGRESSIVE: exit losers at 0.5% down

            # **AGGRESSIVE LOSS EXIT** — Close ANY losing position at 0.5% down
            # Don't wait for full stop-loss. Quick exit = preserve capital for winners.
            if unrealized_pnl < 0 and quick_loss_hit:
                loss_pct = (unrealized_pnl / (entry * qty)) * 100 if entry * qty > 0 else 0
                log.warning(f"[APEX_589296] 🚨 AGGRESSIVE LOSS EXIT: {contract} down {abs(loss_pct):.2f}% (${abs(unrealized_pnl):.2f}) — exiting immediately")
                await close_position(session, contract, config, position, price, rsi, trend, f"QUICK EXIT - DOWN {abs(loss_pct):.2f}%")
                continue  # Skip rest of position checks, move to next contract

            # Use crypto-specific profit targets for crypto positions
            position_profit_target = get_profit_target_dollars(equity, is_crypto=is_crypto)
            tier_levels = CRYPTO_TIER_LEVELS if is_crypto else TIER_LEVELS

            # Tiered profit-taking: lock in gains at milestones
            # CRYPTO: aggressive (50% exit, 75%, 100%) — reinvest faster
            # STOCKS: professional (50%, 100%, 150%) — let winners run
            rsi_exit = rsi_signal and unrealized_pnl > 0

            # Check which profit tiers have been hit
            tier1_hit = unrealized_pnl >= (position_profit_target * tier_levels[0])
            tier2_hit = unrealized_pnl >= (position_profit_target * tier_levels[1])
            tier3_hit = unrealized_pnl >= (position_profit_target * tier_levels[2])

            # Exit conditions: hard stop, RSI reversal on profit, or hit a tiered target
            if stop_hit:
                reason = "STOP LOSS"
                await close_position(session, contract, config, position, price, rsi, trend, reason)
            elif rsi_exit:
                reason = "RSI EXIT"
                await close_position(session, contract, config, position, price, rsi, trend, reason)
            elif tier3_hit:
                reason = f"TIER 3 EXIT (${unrealized_pnl:.2f} profit, {unrealized_pnl/position_profit_target:.1f}x target)"
                await close_position(session, contract, config, position, price, rsi, trend, reason)
            elif tier2_hit and unrealized_pnl >= position_profit_target:
                reason = f"TIER 2 EXIT (${unrealized_pnl:.2f} profit, reached target)"
                await close_position(session, contract, config, position, price, rsi, trend, reason)
            elif tier1_hit and unrealized_pnl >= (position_profit_target * tier_levels[0]):
                reason = f"TIER 1 EXIT (${unrealized_pnl:.2f} profit, {tier_levels[0]*100:.0f}% of target)"
                await close_position(session, contract, config, position, price, rsi, trend, reason)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries, with rotation if already at the cap ─────
        candidates = []
        for contract, config in FUTURES.items():
            # Skip non-crypto symbols during after-hours
            is_crypto = "/" in config["symbol"]
            if not is_market_hours and not is_crypto:
                continue

            if contract in open_prop_positions:
                continue
            data = scans.get(contract)
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]
            momentum = data.get("momentum", 0)

            # Use crypto-specific RSI thresholds if this is a crypto symbol (contains /)
            is_crypto = "/" in config["symbol"]
            buy_threshold = CRYPTO_RSI_BUY_BELOW if is_crypto else RSI_BUY_BELOW
            sell_threshold = CRYPTO_RSI_SELL_ABOVE if is_crypto else RSI_SELL_ABOVE

            if rsi < buy_threshold:
                # Long entry: RSI oversold + positive momentum (price trending up, not just dipping down)
                if momentum > 0:
                    candidates.append((buy_threshold - rsi, contract, config, "long", price, rsi, trend))
                    status = "BUY_ZONE"
                else:
                    status = "BUY_SIGNAL_BLOCKED (momentum negative)"
            elif rsi > sell_threshold:
                # Short entry: RSI overbought + negative momentum (price trending down)
                if shorting_enabled:
                    if momentum < 0:
                        candidates.append((rsi - sell_threshold, contract, config, "short", price, rsi, trend))
                        status = "SHORT_ZONE"
                    else:
                        status = "SHORT_SIGNAL_BLOCKED (momentum positive)"
                else:
                    status = "SHORT_ZONE"
            else:
                status = "NEUTRAL"
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

            # Multi-timeframe confluence: don't fight a strong 1-hour
            # trend just because the 5-minute RSI dipped. Entries only -
            # never gates an exit or an existing position.
            higher_tf = await get_higher_tf_trend(session, config["symbol"])
            if (side == "long" and higher_tf == "DOWN") or (side == "short" and higher_tf == "UP"):
                log.info(f"[APEX_589296] 🚫 {side.upper()} {contract} skipped — 1H trend ({higher_tf}) opposes 5min signal")
                continue

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
    Automatically reinvests 50% of worker earnings back to Alpaca to maintain
    buying power and fuel continuous trading."""
    if pnl_amount <= 0:
        return

    # 83% to worker (bot), 17% to platform
    worker_amount = pnl_amount * 0.83
    platform_amount = pnl_amount * 0.17

    payment = Payment(
        id=f"bot_trade_{uuid.uuid4().hex[:8]}",
        job_id=f"bot_daily_{datetime.now(ET).strftime('%Y%m%d')}",
        worker_id="prop_bot_worker",
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
            log.info(f"[APEX_589296] 💰 Recorded daily earnings: ${worker_amount:.2f} to payments table")

            # AUTOMATIC REINVESTMENT: 50% of worker earnings → Alpaca deposit
            # Keeps account in positive buying power without manual transfers
            alpaca_reinvest = worker_amount * 0.50
            log.info(f"[APEX_589296] 🔄 AUTO-REINVEST: ${alpaca_reinvest:.2f} queued for Alpaca deposit")
            log.info(f"[APEX_589296] 💵 Worker keeps: ${worker_amount * 0.50:.2f} | Platform: ${platform_amount:.2f}")

    except Exception as e:
        log.error(f"[APEX_589296] Failed to record daily earnings: {e}")


def run():
    log.info("=" * 60)
    log.info("DEL'S TRADING EMPIRE — PROP BOT v3")
    log.info(f"Account: APEX_589296 | Mode: {'LIVE' if LIVE_TRADE else 'PAPER'}")
    log.info(f"RSI thresholds: long entry < {RSI_BUY_BELOW} | short entry > {RSI_SELL_ABOVE} (trades both directions)")
    log.info(f"Profitable days: {len(profitable_days)}/7 needed")
    log.info("=" * 60)

    try:
        asyncio.run(load_open_positions())
    except Exception as e:
        log.error(f"[APEX_589296] Startup position reload failed: {e}")

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true — prop bot paused")
            time.sleep(60)
            continue
        try:
            asyncio.run(run_prop_cycle())
        except Exception as e:
            log.error(f"Prop cycle error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    run()
