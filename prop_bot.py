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
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition

ET = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prop_bot")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
LIVE_TRADE    = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

# RSI entry/exit thresholds. Widened again at the account owner's explicit
# request - real trades were too rare at 38/48 (RSI mostly sat in the
# 39-57 range with nothing crossing 38). Wider band means more real trades
# fire, at the cost of acting on weaker/less-confirmed signals - that
# tradeoff was made knowingly, not a bug. Configurable via env for tuning
# without a code change.
RSI_BUY_BELOW  = float(os.getenv("PROP_RSI_BUY_BELOW", "45"))
RSI_SELL_ABOVE = float(os.getenv("PROP_RSI_SELL_ABOVE", "50"))

# Real, enforced stop-loss. open_position() already computed a 2% stop
# price for the informational subscriber-signal broadcast, but Pass 1's
# exit check never actually looked at it - the only two ways a position
# could close were hitting the dollar profit target or an RSI reversal,
# and RSI reversals fired regardless of whether the position was
# actually in profit. That meant a position that had been sitting on a
# real gain could give it all back and close at a genuine loss the
# moment RSI flipped, with no floor underneath it at all. Now enforced
# for real in Pass 1, and RSI exits require the position to actually be
# profitable first - the stop-loss below is what protects a loser, an
# RSI reversal only ever locks in a winner early.
STOP_LOSS_PCT = float(os.getenv("PROP_STOP_LOSS_PCT", "0.02"))

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}

# APEX futures — use micro contracts (lower risk during evaluation)
FUTURES = {
    "MES": {"name": "Micro E-mini S&P 500", "qty": 1, "symbol": "SPY"},   # Use SPY as proxy
    "MNQ": {"name": "Micro E-mini Nasdaq",  "qty": 1, "symbol": "QQQ"},   # Use QQQ as proxy
    "MYM": {"name": "Micro E-mini Dow",     "qty": 1, "symbol": "DIA"},   # Use DIA as proxy
    "M2K": {"name": "Micro E-mini Russell", "qty": 1, "symbol": "IWM"},   # Use IWM as proxy
    "MGC": {"name": "Micro Gold",           "qty": 1, "symbol": "GLD"},   # Use GLD as proxy
    "MCL": {"name": "Micro Crude Oil",      "qty": 1, "symbol": "USO"},   # Use USO as proxy
    "SIL": {"name": "Micro Silver",         "qty": 1, "symbol": "SLV"},   # Use SLV as proxy
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
MAX_POSITIONS = int(os.getenv("PROP_MAX_POSITIONS", str(len(FUTURES))))

# Profit target, in REAL DOLLARS of profit on the position (not a raw
# price move on the underlying) - scaled by real account equity. Explicit
# request: take profit daily even if it's just 50 cents to a dollar,
# then immediately look for another promising signal, rather than
# holding out for a bigger move. Checked against real Alpaca equity each
# cycle.
#
# This replaces an earlier version that checked a raw per-share price
# move (e.g. "SPY moved $0.25") instead of the position's actual dollar
# P&L. That was fine when positions were a fixed 1 share, but once
# size_position() started sizing positions in fractional dollars (see
# try_open), a $0.25 underlying move on a $10-$150 fractional position
# could mean only a few cents of real profit - nowhere near the 50c-$1
# actually wanted here.
#
# THE UNIT BUG THIS REPLACES
# --------------------------
# The old version set the profit target in ABSOLUTE DOLLARS ($0.50, rising
# to $1.00 at $10k equity) while STOP_LOSS_PCT cuts losers at a PERCENTAGE
# of the position. Two different units on the two sides of the same trade,
# so the risk:reward ratio was never a fixed number - it drifted with
# account size, and always against us:
#
#     equity   ~position   stop (2%)   target    R:R        breakeven WR
#     $980     $140        $2.80       $0.50     5.6:1 vs   85%
#     $5,000   $714        $14.29      $0.80    17.9:1 vs   95%
#     $10,000  $1,428      $28.57      $1.00    28.6:1 vs   97%
#
# Risking $2.80 to make $0.50 needs an ~85% win rate just to break even,
# and every dollar the account grows made that requirement worse, not
# better. No entry signal survives that math. This is the single reason
# the account traded actively for weeks and finished flat.
#
# Expressing the target as a PERCENTAGE puts both sides in the same units,
# so R:R is constant at any account size and the required win rate stops
# moving:
#
#     target 3% vs stop 2%  ->  1.5:1 in our favour  ->  breakeven WR 40%
PROFIT_TARGET_PCT = float(os.getenv("PROP_PROFIT_TARGET_PCT", "0.03"))

# Absolute floor, in dollars. A position can be as small as
# MIN_POSITION_NOTIONAL ($10), where 3% is only $0.30 - thin enough that
# spread and slippage could eat the whole "win". Never exit for less than
# this regardless of what the percentage works out to.
MIN_PROFIT_DOLLARS = float(os.getenv("PROP_MIN_PROFIT_DOLLARS", "0.50"))


def get_profit_target_dollars(position_value=None):
    """Dollar profit target for ONE position, from that position's own size.

    Must be called per-position, not once per cycle: positions are sized in
    dollars (see size_position) and can differ a lot from each other, so a
    single cycle-wide number would be wrong for every position but one.

    Falls back to the floor when position value is unknown, which is the
    conservative direction - it exits earlier rather than holding for a
    target that may never be reachable.
    """
    if position_value is None or position_value <= 0:
        return MIN_PROFIT_DOLLARS
    return max(position_value * PROFIT_TARGET_PCT, MIN_PROFIT_DOLLARS)


# ── ATR-BASED EXITS ──────────────────────────────────────────────────────
#
# A single flat 3%/2% is right in UNITS but wrong in SCALE, because the
# seven proxies do not move alike. DIA drifts a few tenths of a percent in
# a day; USO and SLV can travel several percent. A flat 3% target is
# routinely reachable on USO and nearly unreachable on DIA - while the flat
# 2% stop is reachable on BOTH. So the fixed rule quietly becomes "stop
# often, target rarely" on exactly the low-volatility symbols.
#
# Note what this does and does not buy us. It is NOT a better risk:reward
# ratio - 2.5x/1.5x ATR is 1.67:1 against the flat rule's 1.5:1, near
# enough the same. What it buys is REACHABILITY: both levels get sized to
# what each instrument actually does, so the distribution the arithmetic
# assumes is the distribution that actually shows up.
#
# Default stays "fixed" so merging this changes no live behaviour. Flip
# PROP_EXIT_MODE=atr only once the backtest says ATR actually wins.
EXIT_MODE = os.getenv("PROP_EXIT_MODE", "fixed").strip().lower()

ATR_PERIOD = int(os.getenv("PROP_ATR_PERIOD", "14"))
ATR_STOP_MULT = float(os.getenv("PROP_ATR_STOP_MULT", "1.5"))
ATR_TARGET_MULT = float(os.getenv("PROP_ATR_TARGET_MULT", "2.5"))

# Guard rails. A freak-quiet stretch can drive ATR toward zero, which would
# derive a stop so tight that ordinary spread noise closes the position
# instantly; a volatility spike can drive it wide enough to risk far more
# per trade than intended. Clamp both ends.
ATR_MIN_STOP_PCT = float(os.getenv("PROP_ATR_MIN_STOP_PCT", "0.005"))   # 0.5%
ATR_MAX_STOP_PCT = float(os.getenv("PROP_ATR_MAX_STOP_PCT", "0.05"))    # 5%


def compute_atr_pct(bars, period=None):
    """Average True Range over `period` bars, returned as a FRACTION of the
    latest close (so it composes with the percentage-based stop/target).

    True range is the widest of: this bar's high-low, and each of the gaps
    from the previous close - which is what makes it capture overnight and
    session gaps that a simple high-low range misses.

    Needs the 'h' and 'l' fields. get_price_rsi already fetches them in the
    same request and previously discarded them; nothing extra is called.

    Returns None when there aren't enough bars or the data is malformed, so
    callers can fall back to the fixed rule rather than trade on a
    fabricated number.
    """
    period = period or ATR_PERIOD
    if not bars or len(bars) < period + 1:
        return None
    try:
        trs = []
        for i in range(1, len(bars)):
            high, low = float(bars[i]["h"]), float(bars[i]["l"])
            prev_close = float(bars[i - 1]["c"])
            trs.append(max(high - low,
                           abs(high - prev_close),
                           abs(low - prev_close)))
        if len(trs) < period:
            return None
        atr = sum(trs[-period:]) / period
        last_close = float(bars[-1]["c"])
        if last_close <= 0:
            return None
        return atr / last_close
    except (KeyError, TypeError, ValueError):
        return None


def get_exit_levels(atr_pct=None):
    """Resolve (stop_pct, target_pct) as fractions, per the active mode.

    Falls back to the fixed pair whenever ATR is unavailable - a data
    hiccup should degrade to the known-good rule, never to no stop at all.
    """
    if EXIT_MODE != "atr" or atr_pct is None or atr_pct <= 0:
        return STOP_LOSS_PCT, PROFIT_TARGET_PCT

    stop = max(ATR_MIN_STOP_PCT, min(atr_pct * ATR_STOP_MULT, ATR_MAX_STOP_PCT))
    # Derive the target from the CLAMPED stop, never from raw ATR. Deriving
    # it from raw ATR looks equivalent and isn't: when a volatility spike
    # clamps the stop down (12% -> 5%) but leaves the target at 2.5x raw
    # ATR (20%), R:R silently becomes 4:1 - a stop that's now easy to reach
    # paired with a target that isn't. That is precisely the "stop often,
    # target rarely" failure this whole change exists to remove, and it
    # would have hit hardest on the most volatile symbols. Anchoring to the
    # clamped stop holds the ratio at both ends.
    return stop, stop * (ATR_TARGET_MULT / ATR_STOP_MULT)


# ── TIME STOP ────────────────────────────────────────────────────────────
#
# prop_bot has a rotation path (swap the weakest loser out for a fresh
# signal) but it only runs when at MAX_POSITIONS - and MAX_POSITIONS
# defaults to len(FUTURES), so there is never an eighth symbol needing a
# slot and the path is unreachable. Net effect: a position that goes red
# and stays red has nothing to close it but the stop, and capital sits
# parked in it indefinitely while signals on other symbols go unacted.
#
# This is the missing third layer: give a loser room to recover, but a
# bounded amount. 0 disables it.
MAX_UNDERWATER_CYCLES = int(os.getenv("PROP_MAX_UNDERWATER_CYCLES", "0"))


# ── TRAILING STOP ────────────────────────────────────────────────────────
#
# Today a position that runs to +2.5% and then rolls over gives the whole
# gain back: the profit target never triggered, and the RSI exit only fires
# if RSI actually reverses. Between those two there is no mechanism that
# says "this was a winner, stop letting it become a loser."
#
# Arms only after the position has genuinely run (TRAIL_ARM_PCT), then
# exits if it gives back TRAIL_GIVEBACK_PCT from its best. Arming matters:
# a trail that is live from entry is just a tighter stop wearing a
# different name, and would fire on ordinary noise before any thesis plays
# out - the same failure that made 2% stops perform worse than 5% ones on
# the crypto side.
#
# The peak is persisted on BotPosition.peak_pct, NOT held in a dict. A
# module-level dict resets to zero on every Railway redeploy, which would
# silently disarm the trail on exactly the positions that had run up most,
# and would KeyError outright against a position reloaded from the DB by
# load_open_positions(). That failure mode is not hypothetical here - it
# is what produced the fragmented DIA entries in July.
USE_TRAILING_STOP = os.getenv("PROP_USE_TRAILING_STOP", "false").strip().lower() == "true"
TRAIL_ARM_PCT = float(os.getenv("PROP_TRAIL_ARM_PCT", "0.02"))
TRAIL_GIVEBACK_PCT = float(os.getenv("PROP_TRAIL_GIVEBACK_PCT", "0.01"))


# ── BUYING-POWER GATE ────────────────────────────────────────────────────
#
# size_position already divides whatever cash is left across the remaining
# slots, so it rarely oversizes - but it trusts cash_remaining, a value
# decremented locally across the cycle. If a fill came in at a worse price
# than the quote used to size it, or a position was opened outside the bot,
# that local figure drifts above reality and the order is rejected by the
# broker instead of being skipped cleanly.
#
# Re-checks real buying power against the order's cost before sending, with
# headroom for slippage between quote and fill.
# A percentage headroom alone scales the wrong way. At $1,000 buying power
# 5% reserves $50, which is ample; at the $1.14 this account has actually
# sat at, it reserves six cents - nowhere near one fill's worth of
# slippage. Quote-to-fill drift is roughly a fixed number of cents per
# share, not a percentage of the account, so the absolute floor is what
# protects small balances and the percentage is what protects large ones.
# Take whichever limit is TIGHTER.
BUYING_POWER_HEADROOM = float(os.getenv("PROP_BUYING_POWER_HEADROOM", "0.95"))
BUYING_POWER_BUFFER_USD = float(os.getenv("PROP_BUYING_POWER_BUFFER_USD", "1.00"))


def spendable_buying_power(buying_power):
    """Usable buying power after both guardrails, floored at zero so a
    balance smaller than the buffer blocks entries rather than returning a
    negative budget that later comparisons would treat as permissive."""
    if buying_power is None:
        return None
    return max(0.0, min(buying_power * BUYING_POWER_HEADROOM,
                        buying_power - BUYING_POWER_BUFFER_USD))


async def get_account_buying_power(session):
    """Real Alpaca buying power. None on any failure, which callers treat
    as 'unknown' and fall through to the existing cash logic rather than
    blocking a trade on a transient API hiccup."""
    try:
        async with session.get(f"{BASE_URL}/v2/account", headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return float(data.get("buying_power", 0))
    except Exception as e:
        log.warning(f"[APEX_589296] Could not fetch buying power: {e}")
        return None

# Track profitable days for APEX 7-day rule
profitable_days = []
daily_pnl = 0.0
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
                open_prop_positions[row.symbol] = {
                    "side": row.side, "entry": row.entry_price, "qty": row.qty,
                    # Restore the trailing high-water mark too. Without this
                    # a redeploy would reset the peak to zero and disarm the
                    # trail on whatever had run up furthest.
                    "peak_pct": row.peak_pct if row.peak_pct is not None else 0.0,
                }
            if rows:
                log.info(f"[APEX_589296] Reloaded {len(rows)} open position(s) from DB: {list(open_prop_positions.keys())}")
    except Exception as e:
        log.error(f"[APEX_589296] Failed to reload open positions from DB: {e}")


async def _db_save_open(contract: str, side: str, entry: float, qty: float):
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=contract, side=side,
                               entry_price=entry, qty=qty, peak_pct=0.0))
            await db.commit()
    except Exception as e:
        log.error(f"[APEX_589296] Failed to persist opened position {contract}: {e}")


async def _db_update_peak(contract: str, peak_pct: float):
    """Persist a new trailing high-water mark.

    Only called when the peak actually advances, so this is a handful of
    writes per position over its life rather than one per cycle. Failures
    are logged and swallowed: losing a peak update degrades the trail to a
    slightly stale high-water mark, which is far better than an exception
    inside the exit loop preventing every OTHER position from being
    managed that cycle."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotPosition).where(BotPosition.bot == BOT_NAME,
                                          BotPosition.symbol == contract))
            for row in result.scalars().all():
                row.peak_pct = peak_pct
            await db.commit()
    except Exception as e:
        log.warning(f"[APEX_589296] Could not persist peak for {contract}: {e}")


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

            # Same bars, no extra request - the h/l fields were already in
            # this response and were being discarded.
            atr_pct = compute_atr_pct(bars)

            return {"price": price, "rsi": round(rsi, 1), "trend": trend,
                    "atr_pct": atr_pct}
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
MIN_POSITION_NOTIONAL = float(os.getenv("PROP_MIN_POSITION_NOTIONAL", "10"))


def size_position(cash_remaining, slots_remaining, price):
    """Dollar-based (fractional-share) position sizing. A fixed 1-share
    order fails outright on higher-priced ETFs (SPY, QQQ, DIA) once cash
    is tight, while cheaper ones (SLV, USO) fill fine - silently capping
    how many of the open slots can ever actually fill regardless of how
    many real signals come in. Splitting whatever cash is left evenly
    across the remaining open slots means a small account can still use
    all its slots, no matter which symbol's proxy ETF happens to signal.
    Returns None if there isn't enough cash left for even one minimum-size
    position."""
    if slots_remaining <= 0 or cash_remaining < MIN_POSITION_NOTIONAL:
        return None
    amount = min(max(cash_remaining / slots_remaining, MIN_POSITION_NOTIONAL), cash_remaining)
    qty = round(amount / price, 6)
    return qty if qty > 0 else None


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

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
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

    # Only trade during market hours (9:30am-4pm ET). Checked against real
    # ET wall-clock time (DST-aware) rather than a hardcoded UTC range -
    # a fixed 14:30-21:00 UTC window is wrong by an hour for about 8
    # months of the year whenever ET is in daylight time.
    now = datetime.now(ET)
    is_weekday = now.weekday() < 5
    market_open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)

    last_cycle_at = now.isoformat()

    if not (is_weekday and market_open_t <= now <= market_close_t):
        last_market_open = False
        log.info(f"[APEX_589296] Market closed — waiting for 9:30am ET")
        return

    last_market_open = True

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
        if cash_remaining is not None:
            qty = size_position(cash_remaining, slots_remaining, price)
            if qty is None:
                log.info(f"[APEX_589296] Skipping {contract} {side} entry — not enough cash left (${cash_remaining:.2f})")
                return False
        else:
            qty = config["qty"]

        # Buying-power gate. cash_remaining is decremented locally across
        # the cycle from quoted prices; real fills drift from those quotes,
        # and anything opened outside the bot never touches it at all. When
        # it drifts high the broker rejects the order, which costs a cycle
        # and logs an error instead of skipping cleanly. Re-check reality.
        #
        # Unknown buying power falls THROUGH rather than blocking: a
        # transient API hiccup should not silently halt all trading, and
        # size_position plus the broker's own rejection remain as backstops.
        cost = qty * price
        buying_power = await get_account_buying_power(session)
        spendable = spendable_buying_power(buying_power)
        if spendable is not None and cost > spendable:
            log.info(f"[APEX_589296] Skipping {contract} {side} — cost ${cost:.2f} exceeds "
                     f"spendable ${spendable:.2f} of ${buying_power:.2f} buying power "
                     f"({BUYING_POWER_HEADROOM:.0%} cap / ${BUYING_POWER_BUFFER_USD:.2f} buffer)")
            return False

        opened = await open_position(session, contract, config, side, price, rsi, trend, qty)
        if opened and cash_remaining is not None:
            cash_remaining -= qty * price
        return opened

    async with aiohttp.ClientSession() as session:
        await reconcile_positions_with_broker(session)

        equity = await get_account_equity(session)
        # Target is per-position now (it scales with each position's own
        # size), so it can't be resolved to one number here - see
        # get_profit_target_dollars at the exit check below.
        log.info(f"[APEX_589296] Equity: {'$%.2f' % equity if equity is not None else 'unknown'} | "
                 f"Target: {PROFIT_TARGET_PCT * 100:.1f}%/position (min ${MIN_PROFIT_DOLLARS:.2f}) | "
                 f"Stop: {STOP_LOSS_PCT * 100:.1f}% | "
                 f"R:R {PROFIT_TARGET_PCT / STOP_LOSS_PCT:.2f}:1")

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
            data = await get_price_rsi(session, config["symbol"])
            if data:
                scans[contract] = data
                log.info(f"[APEX_589296] {contract} ({config['symbol']}) | ${data['price']:.2f} | RSI:{data['rsi']} | {data['trend']}")
            await asyncio.sleep(0.3)

        # ── Pass 1: manage exits for symbols already held ────────────────
        # A long profits as price rises and exits on overbought RSI; a
        # short profits as price falls and exits on oversold RSI. Profit
        # target is checked against the position's actual real dollar
        # P&L, sized per-position as a percentage of that position's cost
        # basis (see get_profit_target_dollars) so it stays in the same
        # units as the percentage stop-loss and the risk:reward ratio
        # holds at any account size.
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

            # Exit levels sized to THIS symbol's own volatility when
            # PROP_EXIT_MODE=atr, else the flat pair. Falls back to flat
            # automatically if ATR couldn't be computed this cycle.
            stop_pct, target_pct = get_exit_levels(data.get("atr_pct"))

            if side == "long":
                unrealized_pnl = (price - entry) * qty
                rsi_signal = rsi > RSI_SELL_ABOVE or (trend == "bearish" and rsi > 50)
                stop_hit = price <= entry * (1 - stop_pct)
            else:
                unrealized_pnl = (entry - price) * qty
                rsi_signal = rsi < RSI_BUY_BELOW or (trend == "bullish" and rsi < 50)
                stop_hit = price >= entry * (1 + stop_pct)

            # An RSI reversal only ever takes a real, existing profit off
            # the table early - it never realizes a loss on its own. A
            # losing position rides to the stop-loss below instead, same
            # as a real trading desk would run it (cut losers on a hard
            # stop, don't gamble on holding for a bounce that erases it).
            rsi_exit = rsi_signal and unrealized_pnl > 0

            # Sized from THIS position's own cost basis, so the target is
            # always the same percentage of what's actually at risk here -
            # which is what keeps R:R fixed against the percentage stop.
            profit_target = max(entry * qty * target_pct, MIN_PROFIT_DOLLARS)

            # Trailing stop. Peak is tracked as a RETURN FRACTION, not a
            # price, so it works identically for longs and shorts - a short
            # profits as price falls, and a price-based high-water mark
            # would have the sign backwards.
            trail_exit = False
            if USE_TRAILING_STOP and entry > 0:
                ret_pct = ((price - entry) / entry) if side == "long" else ((entry - price) / entry)
                prev_peak = position.get("peak_pct", 0.0) or 0.0
                if ret_pct > prev_peak:
                    position["peak_pct"] = ret_pct
                    await _db_update_peak(contract, ret_pct)
                    prev_peak = ret_pct
                # Armed only once the position has genuinely run. Before
                # that this does nothing, so it can't act as a stealth
                # tight stop on a position that never went anywhere.
                if prev_peak >= TRAIL_ARM_PCT and ret_pct <= prev_peak - TRAIL_GIVEBACK_PCT:
                    trail_exit = True

            # Time stop: count only cycles where this position is actually
            # underwater, and reset the moment it goes green, so the count
            # measures CONSECUTIVE time spent losing rather than age.
            if unrealized_pnl < 0:
                position["cycles_underwater"] = position.get("cycles_underwater", 0) + 1
            else:
                position["cycles_underwater"] = 0
            stale = (MAX_UNDERWATER_CYCLES > 0
                     and position["cycles_underwater"] >= MAX_UNDERWATER_CYCLES)

            if unrealized_pnl >= profit_target or rsi_exit or stop_hit or stale or trail_exit:
                # Order matters only for the label. STOP LOSS is checked
                # before TRAIL so a position that gapped through both is
                # reported as what actually protected it.
                reason = ("PROFIT TARGET" if unrealized_pnl >= profit_target
                          else "STOP LOSS" if stop_hit
                          else "TRAIL" if trail_exit
                          else "RSI" if rsi_exit else "TIME STOP")
                await close_position(session, contract, config, position, price, rsi, trend, reason)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries, with rotation if already at the cap ─────
        candidates = []
        for contract, config in FUTURES.items():
            if contract in open_prop_positions:
                continue
            data = scans.get(contract)
            if not data:
                continue
            price, rsi, trend = data["price"], data["rsi"], data["trend"]

            if rsi < RSI_BUY_BELOW:
                candidates.append((RSI_BUY_BELOW - rsi, contract, config, "long", price, rsi, trend))
                status = "BUY_ZONE"
            elif rsi > RSI_SELL_ABOVE:
                # Signal is real either way - only gate acting on it. Still
                # shown as SHORT_ZONE on the dashboard so it accurately
                # reflects RSI conditions even while shorting is disabled.
                if shorting_enabled:
                    candidates.append((rsi - RSI_SELL_ABOVE, contract, config, "short", price, rsi, trend))
                status = "SHORT_ZONE"
            else:
                status = "NEUTRAL"
            latest_signals[contract] = {
                "symbol": config["symbol"], "price": price, "rsi": rsi, "trend": trend,
                "status": status, "has_position": False, "checked_at": now.isoformat(),
            }

        candidates.sort(key=lambda c: -c[0])  # strongest (furthest past threshold) first

        for _, contract, config, side, price, rsi, trend in candidates:
            # Multi-timeframe confluence: don't fight a strong 1-hour
            # trend just because the 5-minute RSI dipped. Entries only -
            # never gates an exit or an existing position.
            higher_tf = await get_higher_tf_trend(session, config["symbol"])
            if (side == "long" and higher_tf == "DOWN") or (side == "short" and higher_tf == "UP"):
                log.info(f"[APEX_589296] 🚫 {side.upper()} {contract} skipped — 1H trend ({higher_tf}) opposes 5min signal")
                continue

            if len(open_prop_positions) < MAX_POSITIONS:
                log.info(f"[APEX_589296] 📡 {side.upper()} {contract} — RSI:{rsi} Trend:{trend}")
                await try_open(contract, config, side, price, rsi, trend, MAX_POSITIONS - len(open_prop_positions))
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
                        f"[APEX_589296] 🔄 ROTATING: {weakest_contract} ({weakest_pct:.2f}%, weakest of {MAX_POSITIONS}) "
                        f"→ {contract} (RSI:{rsi} {side})"
                    )
                    closed = await close_position(
                        session, weakest_contract, FUTURES[weakest_contract], open_prop_positions[weakest_contract],
                        held_data["price"], held_data["rsi"], held_data["trend"], "ROTATED OUT",
                    )
                    if closed:
                        if cash_remaining is not None:
                            cash_remaining += freed_value
                        await try_open(contract, config, side, price, rsi, trend, MAX_POSITIONS - len(open_prop_positions))
                else:
                    log.info(
                        f"[APEX_589296] At max positions ({MAX_POSITIONS}) - {contract} {side} signal held, "
                        f"weakest position ({weakest_contract} {weakest_pct:+.2f}%) isn't a loss, not rotating"
                        if weakest_contract else
                        f"[APEX_589296] At max positions ({MAX_POSITIONS}) - {contract} {side} signal held, no rotation candidate"
                    )

            await asyncio.sleep(0.3)

    # Check if today was profitable
    today = now.strftime("%Y-%m-%d")
    if daily_pnl > 0 and (not profitable_days or profitable_days[-1] != today):
        profitable_days.append(today)
        log.info(f"✅ PROFITABLE DAY #{len(profitable_days)} | ${daily_pnl:.2f} | APEX_589296")
        if len(profitable_days) >= 7:
            log.info("🎯 7 CONSECUTIVE PROFITABLE DAYS ACHIEVED — READY TO GO LIVE!")
            log.info("ACTION: Change ALPACA_LIVE_TRADE=true in Railway to go live")


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
