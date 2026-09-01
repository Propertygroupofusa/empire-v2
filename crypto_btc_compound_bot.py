"""
BTC COMPOUNDING LOOP BOT — Coinbase Advanced Trade, single-asset, single-position

Replaces crypto_coinbase_bot.py's 28-pair RSI strategy as the live Coinbase
strategy (see CRYPTO_STRATEGY_MODE in main.py - flip it back to "multi_pair"
to revert to the old bot; nothing here deletes or modifies that file).

Strategy, as specified by the account owner:
  BUY (100% of available USD) -> RECORD ACTUAL FILL PRICE -> CALCULATE AN
  ADAPTIVE PROFIT TARGET FROM CURRENT VOLATILITY -> HOLD, CHECKING EVERY
  CYCLE -> SELL ONLY WHEN THE TARGET (OR STOP-LOSS) IS REACHED -> VERIFY
  THE SELL FILLED -> NEXT CYCLE BUYS AGAIN WITH WHATEVER BALANCE RESULTS.

This is deliberately NOT "buy and hope" - the position is never marked
profitable until an actual sell fill confirms it, and the next entry price
is always the real fill price, not an assumption. Only one position is ever
open at a time (MAX CONCURRENT TRADES = 1, per spec), and every dollar in
the account is what gets deployed each cycle, so a winning trade's profit
compounds into the next trade's size automatically - no manual reinvestment
step, no fixed position size to outgrow.

Profit target is adaptive rather than fixed, because a fixed percentage
doesn't reflect what BTC is actually doing: in a quiet market a big target
may never get hit, and in a volatile market a small target undersells the
move. Volatility is measured as ATR (Average True Range) as a percentage of
price, using 5-minute candles - the same measure crypto_coinbase_bot.py
already uses for its own exits - bucketed into three target tiers.

Auth, order placement, and balance-fetching reuse the same Coinbase CDP
JWT approach already proven working in crypto_coinbase_bot.py and
scripts/coinbase_manual_trade.py tonight. Implemented standalone here
(not imported from crypto_coinbase_bot.py) so this bot has no coupling to
that module's own in-memory state or its RSI/tiered-exit logic - the two
strategies are meant to be swapped, not blended.
"""
import base64
import math
import os
import asyncio
import logging
import secrets
import time
import traceback
import uuid
from datetime import datetime, timezone

import aiohttp
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_btc_compound_bot")


def _safe_float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except ValueError:
        log.warning(f"{name}={raw!r} is not a valid number - using default {default} instead. Fix this in Railway's Variables tab.")
        return float(default)


def _safe_int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        log.warning(f"{name}={raw!r} is not a valid integer - using default {default} instead. Fix this in Railway's Variables tab.")
        return int(default)


COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
PRODUCT_ID = "BTC-USD"
SYMBOL = "BTC/USD"
BOT_NAME = "crypto_btc_compound"

CYCLE_SECONDS = _safe_int_env("BTC_COMPOUND_CYCLE_SECONDS", "30")
MIN_TRADE_USD = _safe_float_env("BTC_COMPOUND_MIN_TRADE_USD", "5.00")
STOP_LOSS_PCT = _safe_float_env("BTC_COMPOUND_STOP_LOSS_PCT", "0.02")  # -2% default

# Breakeven stop ratchet, per the account owner: a fresh position keeps the
# full -STOP_LOSS_PCT room to work (pinning the stop at entry from tick one
# would trip on ordinary bid/ask noise almost immediately, and still costs
# the real ~ROUND_TRIP_FEE_RATE fee every time it does - more losing trades,
# not fewer). Once a position has moved into profit by this much, its stop
# is raised to its own entry price so it can never close below (about)
# breakeven again - the account owner explicitly accepted eating the
# round-trip fee on those as the cost of cutting off the rest of the
# downside. Only ever moves up, checked every cycle.
BREAKEVEN_TRIGGER_PCT = _safe_float_env("BTC_COMPOUND_BREAKEVEN_TRIGGER_PCT", "0.01")  # +1% default

# A real, known gap (documented before this fix): find_most_volatile_unclaimed_coin()
# in crypto_family_tree_bot.py only ever checked "bullish over the last
# ~25 hours" - a coarse, medium-term signal with no short-term
# overbought/extended check, unlike prop_bot.py's own RSI-gated entries on
# the Alpaca side. That gap meant a branch could switch straight into a
# coin that had already pumped hard and was due to mean-revert - the exact
# shape of loss the real coin-trade-history evidence showed (PEPE, DOGE,
# one of two XRP trades, all quick losers). This mirrors prop_bot.py's
# existing RSI_SELL_ABOVE=70 / CRYPTO_RSI_SELL_ABOVE=65 overbought-exit
# convention, adapted as an overbought-ENTRY guard here instead: this
# engine buys momentum (already-bullish coins), not dips, so the fix isn't
# "wait for oversold" (that would fight the bullish-only selection this
# engine is built around) - it's "don't buy a bullish coin that's ALREADY
# extended right now," using the same 65 threshold prop_bot.py already
# uses for crypto specifically (tighter than stocks' 70, matching crypto's
# higher volatility).
ENTRY_MAX_RSI = _safe_float_env("BTC_COMPOUND_ENTRY_MAX_RSI", "65")

# Adaptive profit-target tiers, chosen by current ATR% (volatility):
#   ATR% < VOL_LOW_THRESHOLD          -> TARGET_LOW_PCT   (quiet market)
#   VOL_LOW_THRESHOLD..VOL_HIGH_THRESHOLD -> TARGET_MED_PCT (normal)
#   ATR% >= VOL_HIGH_THRESHOLD         -> TARGET_HIGH_PCT  (volatile)
VOL_LOW_THRESHOLD = _safe_float_env("BTC_COMPOUND_VOL_LOW_THRESHOLD", "0.01")   # 1% ATR
VOL_HIGH_THRESHOLD = _safe_float_env("BTC_COMPOUND_VOL_HIGH_THRESHOLD", "0.02")  # 2% ATR
TARGET_LOW_PCT = _safe_float_env("BTC_COMPOUND_TARGET_LOW_PCT", "0.015")   # 1.5%
TARGET_MED_PCT = _safe_float_env("BTC_COMPOUND_TARGET_MED_PCT", "0.025")   # 2.5%
TARGET_HIGH_PCT = _safe_float_env("BTC_COMPOUND_TARGET_HIGH_PCT", "0.04")  # 4%

ROUND_TRIP_FEE_RATE = _safe_float_env("BTC_COMPOUND_ROUND_TRIP_FEE_RATE", "0.008")  # ~0.4% each way, taker

# Per the account owner: a percentage-only target can "hit" on a small
# position and still barely clear the real sell-side fee, or lose to it
# outright - e.g. the old flat 1.5% target on a fresh $50 branch nets
# under $0.55 after the ~0.8% round-trip fee, which isn't a real win.
# Every TARGET-HIT exit must clear a minimum of real net profit, in
# dollars, not just percent.
#
# That dollar floor is tiered by the SAME ATR% volatility bands
# pick_target_pct() already uses, per the account owner: a highly
# volatile coin can genuinely swing far enough to be worth demanding a
# bigger real dollar win for, but pinning every coin to that same high
# bar would make TARGET unreachable on a quiet, barely-moving coin -
# pick_min_profit_usd() mirrors pick_target_pct()'s own tiering so a
# quiet coin keeps the low, easily-reachable floor.
#
# min_profit_target_pct() is the exact inverse of the net-P&L formula
# _sell_and_settle/_branch_sell_and_settle use (pnl = qty*T*(1-fee_rate/2)
# - qty*entry), solved for the target_pct that makes that pnl equal the
# picked dollar floor on a position of a given size. The buy path then
# uses max(pick_target_pct(atr_pct), min_profit_target_pct(spend,
# atr_pct)) - so this only ever RAISES the target on positions too small
# for the adaptive ATR target to clear the dollar floor on its own; a
# big-enough branch (whose normal target already nets well over its
# floor) is untouched, since pick_target_pct's result already wins the
# max().
#
# The real tradeoff, and it's a structural one no percentage tweak
# removes: a small branch now needs a bigger price move to ever reach
# TARGET, so it holds longer and is more likely to hit its stop first
# instead. That's the actual cost of insisting every declared "win" be a
# real one - the account owner explicitly chose that trade-off over a
# thin/negative "win" that fees eat.
MIN_PROFIT_USD_LOW = _safe_float_env("BTC_COMPOUND_MIN_PROFIT_USD_LOW", "2.50")    # quiet coins (ATR < VOL_LOW_THRESHOLD)
MIN_PROFIT_USD_MED = _safe_float_env("BTC_COMPOUND_MIN_PROFIT_USD_MED", "4.00")    # normal coins
MIN_PROFIT_USD_HIGH = _safe_float_env("BTC_COMPOUND_MIN_PROFIT_USD_HIGH", "6.00")  # volatile coins (ATR >= VOL_HIGH_THRESHOLD)


def pick_min_profit_usd(atr_pct: float) -> float:
    if atr_pct < VOL_LOW_THRESHOLD:
        return MIN_PROFIT_USD_LOW
    if atr_pct < VOL_HIGH_THRESHOLD:
        return MIN_PROFIT_USD_MED
    return MIN_PROFIT_USD_HIGH


def min_profit_target_pct(spend_usd: float, atr_pct: float) -> float:
    if spend_usd <= 0:
        return 0.0
    min_profit_usd = pick_min_profit_usd(atr_pct)
    k = 1 - (ROUND_TRIP_FEE_RATE / 2)
    return max(0.0, (1 + min_profit_usd / spend_usd) / k - 1)

# EQUITY FLOOR RATCHET - same mechanism prop_bot.py uses, and for the same
# reason: this does NOT make losing impossible (nothing can), but it stops
# the account from giving back progress past a locked-in checkpoint. Every
# time total account value (cash + any open position, marked to market)
# crosses a new $EQUITY_FLOOR_TIER milestone, that milestone becomes the
# new floor - permanently, it only ever moves up. If total value ever
# drops below the CURRENT floor, the bot force-sells any open position
# immediately (crystallizing whatever P&L exists at that instant) and
# refuses new entries until value recovers back above the floor. There is
# still a lag between price moving and the bot noticing (it checks once
# per CYCLE_SECONDS), so a breach can still realize a real loss right at
# the trigger - the floor bounds how far back you can slide, it doesn't
# make each individual trade risk-free.
EQUITY_FLOOR_TIER = _safe_float_env("BTC_COMPOUND_EQUITY_FLOOR_TIER", "50")
EQUITY_FLOOR_BASE = _safe_float_env("BTC_COMPOUND_EQUITY_FLOOR_BASE", "0")
EQUITY_FLOOR_STATE_KEY = "crypto_btc_compound_equity_floor"
equity_floor = EQUITY_FLOOR_BASE

# Module-level status, read by the trading dashboard - mirrors the pattern
# crypto_coinbase_bot.py uses, so routers/trading_dashboard.py could read
# this bot's status the same way once wired up.
last_cycle_at = None
daily_pnl = 0.0


def _load_signing_key():
    raw = COINBASE_API_PRIVATE_KEY.strip()
    if not raw:
        raise ValueError("COINBASE_API_PRIVATE_KEY not set")
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) != 64:
        raise ValueError(f"Ed25519 key must be 64 bytes decoded, got {len(decoded)}")
    return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


def _build_jwt(method: str, path: str) -> str:
    private_key, algorithm = _load_signing_key()
    now = int(time.time())
    payload = {
        "sub": COINBASE_API_KEY_NAME,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {COINBASE_HOST}{path}",
    }
    headers = {"kid": COINBASE_API_KEY_NAME, "nonce": secrets.token_hex(16)}
    return pyjwt.encode(payload, private_key, algorithm=algorithm, headers=headers)


def _auth_headers(method: str, path: str) -> dict:
    return {"Authorization": f"Bearer {_build_jwt(method, path)}", "Content-Type": "application/json"}


async def get_asset_balance(session, currency: str) -> tuple:
    """Real available balance of a given asset currency (e.g. 'USD', 'DOT',
    'LDO'). Returns (balance, None) or (None, reason)."""
    path = "/api/v3/brokerage/accounts"
    cursor = None
    try:
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), params=params, timeout=15) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    return None, f"HTTP {r.status}: {body}"
                data = await r.json()
                for account in data.get("accounts", []):
                    if account.get("currency") == currency:
                        return float(account["available_balance"]["value"]), None
                if not data.get("has_next") or not data.get("cursor"):
                    break
                cursor = data.get("cursor")
        return None, f"no {currency} account found on this key"
    except asyncio.TimeoutError:
        return None, "Coinbase API timeout"
    except aiohttp.ClientError as e:
        return None, f"Coinbase connection failed: {type(e).__name__}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


async def get_all_asset_balances(session) -> tuple:
    """Real available balance of EVERY currency on this account in one
    paginated walk through /accounts, returned as ({currency: balance},
    None) or (None, reason). Built for get_reconciliation_report(), which
    needs several currencies' real balances at once (potentially one per
    coin the tree currently holds) - calling get_asset_balance() in a loop
    would re-fetch and re-paginate the full account list from scratch for
    EVERY currency, and that report is polled by the dashboard every 15s,
    which would otherwise turn one dashboard refresh into a dozen-plus
    redundant real Coinbase API calls. This walks the pages exactly once
    regardless of how many currencies the caller ultimately looks up."""
    path = "/api/v3/brokerage/accounts"
    cursor = None
    balances = {}
    try:
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), params=params, timeout=15) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    return None, f"HTTP {r.status}: {body}"
                data = await r.json()
                for account in data.get("accounts", []):
                    currency = account.get("currency")
                    if currency:
                        balances[currency] = float(account["available_balance"]["value"])
                if not data.get("has_next") or not data.get("cursor"):
                    break
                cursor = data.get("cursor")
        return balances, None
    except asyncio.TimeoutError:
        return None, "Coinbase API timeout"
    except aiohttp.ClientError as e:
        return None, f"Coinbase connection failed: {type(e).__name__}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


async def get_usd_balance(session) -> tuple:
    """Real available USD balance. Returns (balance, None) or (None, reason)."""
    return await get_asset_balance(session, "USD")


async def get_usdc_balance(session) -> tuple:
    """Real available USDC balance. Returns (balance, None) or (None, reason).

    Real, confirmed-live gap this closes VISIBILITY into (not yet the
    trading behavior itself - see the account owner's own documented
    choice below): get_usd_balance() only ever reads the literal "USD"
    Coinbase account. If a meaningful chunk of real cash sits in USDC
    (Coinbase's own "Earn 3.50% APY by converting USD to USDC" prompt,
    or auto-rewards enrollment, can do this), every downstream real-cash
    calculation that uses get_usd_balance() alone - spendable_for_spawn,
    buy sizing, the dust sweep - is blind to it, and can make a real,
    healthy account look like it has $0 or even negative real spendable
    cash. Confirmed live: the account owner's own real Coinbase screen
    showed $698.43 in USDC + $150.33 in USD ($848.76 total real cash),
    while the dashboard's manual "Trade this"/"Add cash"/"Start new
    branch" actions were all refusing for lack of real spendable cash -
    because real_balance (USD-only) minus the two flat branches' own
    allocated_usd came out deeply negative, with the $698.43 in USDC
    never once part of that math.

    Deliberately NOT wired into spendable_for_spawn or any real order-
    execution path here - Coinbase's BTC-USD market orders need real USD
    as the quote currency; whether the API can fund one directly from a
    USDC balance instead is unconfirmed (this sandbox has no live
    Coinbase access to test it), and guessing wrong on a real-money order
    path is exactly the kind of risk this whole codebase's history argues
    against. The account owner's own explicit, already-documented choice
    for this exact scenario is "convert back to USD manually when this
    happens" - this function exists so that choice can be made with the
    real number in front of them (surfaced on the dashboard) instead of
    a confusing "why does it say I have no money" moment."""
    return await get_asset_balance(session, "USDC")


async def get_real_fee_tier(session) -> tuple:
    """Real, live Coinbase fee tier for this account right now - hits
    Coinbase's own /transaction_summary endpoint, which reports the
    account's real 30-day trailing volume-based tier directly (maker/
    taker rates + a tier name), rather than this codebase trying to
    separately track real trading volume itself and guess which tier
    that implies - Coinbase's own number is the one real source of
    truth. Returns (maker_fee_rate, taker_fee_rate, tier_name, None) or
    (None, None, None, reason) on a real fetch failure - never a
    fabricated tier.

    Built for crypto_grid_bot.py's real fee-tier-aware grid spacing
    feature (see compute_dynamic_grid_pct there) - every real order this
    codebase places is a MARKET order, so taker_fee_rate is the real
    rate that actually applies; maker_fee_rate is returned too for
    completeness/future use but not consumed by anything yet."""
    path = "/api/v3/brokerage/transaction_summary"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                body = (await r.text())[:300]
                return None, None, None, f"HTTP {r.status}: {body}"
            data = await r.json()
            fee_tier = data.get("fee_tier") or {}
            maker = fee_tier.get("maker_fee_rate")
            taker = fee_tier.get("taker_fee_rate")
            tier_name = fee_tier.get("pricing_tier") or fee_tier.get("usd_from") or None
            if maker is None or taker is None:
                return None, None, None, "real response had no fee_tier data"
            return float(maker), float(taker), tier_name, None
    except asyncio.TimeoutError:
        return None, None, None, "Coinbase API timeout"
    except aiohttp.ClientError as e:
        return None, None, None, f"Coinbase connection failed: {type(e).__name__}"
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {str(e)[:150]}"


async def get_product_size_decimals(session, product_id: str) -> int:
    """How many decimal places Coinbase allows for order size on this
    product, from its base_increment (e.g. BTC-USD allows 8 decimals but a
    lower-priced/higher-supply coin like LDO-USD may allow only 2 or 4).
    Selling with more decimals than the product allows is rejected outright
    (INVALID_SIZE_PRECISION) - defaults to 8 (the most permissive real
    value seen on Coinbase) if the lookup fails, matching prior behavior."""
    path = f"/api/v3/brokerage/products/{product_id}"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                return 8
            data = await r.json()
            increment = data.get("base_increment", "0.00000001")
            return len(increment.split(".")[1]) if "." in increment else 0
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] size-precision fetch failed for {product_id}, defaulting to 8 decimals: {e}")
        return 8


async def _fetch_candles(session, product_id: str):
    """Fetches ~25 hours of 5-minute candles (Coinbase's public,
    unauthenticated market-data endpoint - same one crypto_coinbase_bot.py
    uses for its own ATR). Returns (closes, highs, lows), oldest-first, or
    None on any failure or insufficient data."""
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity=300"
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < 20:
                return None
            # Coinbase returns newest-first: [time, low, high, open, close, volume]
            candles = list(reversed(data))
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[1]) for c in candles]
            return closes, highs, lows
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Candle fetch failed for {product_id}: {e}")
        return None


def _atr_pct_from_candles(closes, highs, lows) -> float:
    price = closes[-1]
    period = 14
    if len(closes) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        true_ranges.append(tr)
    atr = sum(true_ranges[-period:]) / period
    return atr / price if price else 0.0


def _rsi_from_closes(closes, period: int = 14):
    """Same simple-moving-average RSI formula prop_bot.py's get_price_rsi()
    already uses on the Alpaca side (not Wilder's smoothing) - kept
    identical on purpose so this is a real analogous adaptation, not a
    different indicator with the same name. Returns None if there aren't
    enough closes yet (mirrors ATR's own len-guard above)."""
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return 100 - (100 / (1 + rs))


async def get_price_and_volatility(session, product_id: str = PRODUCT_ID) -> tuple:
    """Current price and ATR% (volatility as a fraction of price) for any
    Coinbase product. Returns (price, atr_pct) or (None, None) on failure.
    product_id defaults to BTC-USD so this bot's own run_cycle doesn't need
    to change; crypto_family_tree_bot.py passes each branch's own
    product_id explicitly."""
    candles = await _fetch_candles(session, product_id)
    if candles is None:
        return None, None
    closes, highs, lows = candles
    return closes[-1], _atr_pct_from_candles(closes, highs, lows)


async def get_price_volatility_and_trend(session, product_id: str = PRODUCT_ID) -> tuple:
    """Same as get_price_and_volatility, plus whether the coin is currently
    bullish - price now higher than it was at the start of the same
    ~25-hour candle window used for the ATR calculation - its current RSI
    (see _rsi_from_closes, ENTRY_MAX_RSI), and its real simple return over
    that same ~25-hour window (coin_return - the raw number
    find_most_volatile_unclaimed_coin() needs to compute BTC-relative
    alpha against BTC-USD's own return over the identical window, the same
    real comparison crypto_selection_backtest.py's
    calculate_relative_strength() already validated offline on 30 real
    days of history before this was wired into live selection). Only used
    by find_most_volatile_unclaimed_coin() in crypto_family_tree_bot.py to
    pick a coin after a floor-breach loss; every other caller keeps using
    plain get_price_and_volatility, unaffected by this. Returns
    (price, atr_pct, is_bullish, rsi, coin_return) or
    (None, None, None, None, None) on failure - rsi itself can
    independently be None (too little history) even when the other fields
    are real."""
    candles = await _fetch_candles(session, product_id)
    if candles is None:
        return None, None, None, None, None
    closes, highs, lows = candles
    atr_pct = _atr_pct_from_candles(closes, highs, lows)
    is_bullish = closes[-1] > closes[0]
    rsi = _rsi_from_closes(closes)
    coin_return = (closes[-1] - closes[0]) / closes[0] if closes[0] else None
    return closes[-1], atr_pct, is_bullish, rsi, coin_return


async def _fetch_hourly_closes(session, product_id: str, count: int = 50):
    """Fetches the most recent `count` real hourly candles (Coinbase public
    candles endpoint, granularity=3600) - used ONLY by get_higher_tf_trend()
    below for its SMA20/SMA50 trend check. Deliberately separate from
    _fetch_candles() (5-min candles, ~25h window): the higher-timeframe
    filter needs a real 50-HOUR window to match exactly what
    crypto_selection_backtest.py's _make_higher_tf_trend_gate() validated
    offline before this was wired into live selection - a 5-min-candle
    substitute would be a different, untested filter, not the one the real
    30-day comparison actually backed. Returns closes (oldest-first,
    trimmed to the most recent `count`) or None on failure/insufficient
    data."""
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity=3600"
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < count:
                return None
            candles = list(reversed(data))[-count:]
            return [float(c[4]) for c in candles]
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Hourly candle fetch failed for {product_id}: {e}")
        return None


async def _fetch_hourly_candles(session, product_id: str, count: int = 120):
    """Fetches the most recent `count` real hourly candles (Coinbase public
    candles endpoint, granularity=3600), including highs/lows - the live
    counterpart to crypto_selection_backtest.py's own paginated hourly
    fetch, just a single-page real fetch since `count` stays well under
    Coinbase's real 300-candle-per-page limit for any practical window.
    Separate from _fetch_hourly_closes() above (which only ever returns
    closes, for the SMA20/SMA50 trend filter) since computing a real
    average True Range needs highs/lows too. Returns (closes, highs, lows),
    oldest-first, or None on failure/insufficient real data."""
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity=3600"
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < 20:
                return None
            candles = list(reversed(data))[-count:]
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[1]) for c in candles]
            return closes, highs, lows
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Hourly OHLC fetch failed for {product_id}: {e}")
        return None


def _average_hourly_swing_pct(closes, highs, lows) -> float:
    """Real "average swing" for one coin over the given real hourly
    window - the mean real True Range (as a % of price) across every
    candle, matching crypto_selection_backtest.py's own
    _average_hourly_swing_pct() formula EXACTLY (same True-Range
    definition, same mean-over-window approach) so the live version stays
    a faithful match to the real, already-validated backtest rather than
    a different calculation wearing the same name. Returns 0.0 on too
    little real data - callers should treat that as "no real reading
    yet," not "genuinely zero volatility."""
    n = len(closes)
    if n < 2:
        return 0.0
    true_ranges_pct = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        if closes[i]:
            true_ranges_pct.append(tr / closes[i])
    if not true_ranges_pct:
        return 0.0
    return sum(true_ranges_pct) / len(true_ranges_pct)


async def get_average_hourly_swing_pct(session, product_id: str, count: int = 120):
    """Real, live "what does this coin typically swing" reading - fetches
    the most recent `count` real hourly candles (120 = ~5 real days, the
    same practical lookback crypto_selection_backtest.py's own
    STRATEGY_LAB_SWING_LOOKBACK_HOURS uses for a "recent behavior" window,
    not a claim this matches the real 30-day backtest window exactly - a
    live feature can't practically re-fetch 30 real days of hourly
    candles every trading cycle) and returns the real average True-Range
    % across it. Returns None (not 0.0) on a real fetch failure or too
    little history - callers must fail OPEN on None, matching every other
    "don't block on missing data" gate in this codebase."""
    candles = await _fetch_hourly_candles(session, product_id, count=count)
    if candles is None:
        return None
    closes, highs, lows = candles
    return _average_hourly_swing_pct(closes, highs, lows)


async def get_higher_tf_trend(session, product_id: str, sma_short: int = 20, sma_long: int = 50):
    """Real, live SMA20/SMA50 (hourly) trend confirmation - the crypto-side
    analog of prop_bot.py's get_higher_tf_trend(), promoted from shadow-mode
    backtest to live entry selection after a real 30-day/18-coin comparison
    (crypto_selection_backtest.py's run_higher_tf_trend_comparison(), run
    live on the backtest page) showed a net-positive ROI change on 15 of 18
    coins - several substantially (ADA +24.6pp, DOT +23.2pp, SHIB +18.8pp,
    TIA +16.5pp, UNI +15.0pp) - against only 3 coins made worse (SOL -4.9pp,
    DOGE -8.0pp, LINK -5.7pp). Returns True (uptrend, SMA20 > SMA50), False
    (downtrend), or None if there isn't yet enough real hourly history -
    callers must fail OPEN on None, matching every other "don't block on
    missing data" gate in this codebase."""
    closes = await _fetch_hourly_closes(session, product_id, count=sma_long)
    if closes is None:
        return None
    sma20 = sum(closes[-sma_short:]) / sma_short
    sma50 = sum(closes[-sma_long:]) / sma_long
    return sma20 > sma50


# Real RSI(30)+support-zone filter - mirrors crypto_selection_backtest.py's
# own SR_LOOKBACK_HOURS/SR_RSI_OVERSOLD/SR_SUPPORT_PROXIMITY_PCT exactly
# (deliberately duplicated constants, not a shared import - that module
# imports FROM crypto_family_tree_bot.py, so the reverse would be a real
# circular import; kept in lockstep by value, same pattern already used for
# STOP_HIT_REVERSAL_TARGET_PCT/STOP_HIT_REVERSAL_STOP_PCT elsewhere in this
# codebase).
SR_LOOKBACK_HOURS = 72
SR_RSI_OVERSOLD = 30
SR_SUPPORT_PROXIMITY_PCT = 0.02


async def get_support_resistance_signal(session, product_id: str, lookback_hours: int = SR_LOOKBACK_HOURS,
                                          rsi_oversold: float = SR_RSI_OVERSOLD, proximity_pct: float = SR_SUPPORT_PROXIMITY_PCT):
    """Real, live counterpart to crypto_selection_backtest.py's own
    _make_support_resistance_gate() - promoted to live entry selection
    after a real 30-day comparison (run_support_resistance_comparison(),
    run live on the backtest page) showed a net-positive ROI change on
    most coins tested, several by 20+ percentage points (BCH, AVAX, SEI,
    PEPE among them), per the account owner's own explicit "yes" after
    being shown that real evidence.

    Requires the candidate's real hourly RSI(14) to be genuinely oversold
    (below rsi_oversold) AND its real most recent hourly close to be
    sitting within proximity_pct of its own real lookback_hours support
    level (the lowest real hourly close in that window) - the same real
    "buy an oversold dip that's also sitting at a real historical floor"
    idea the backtest validated, not a new invented rule. Deliberately
    uses the hourly series' own close as "current price" for the
    proximity check (not a separate live tick price) - the exact same
    real comparison the backtest itself replayed, so this stays an
    apples-to-apples match to the validated evidence rather than a subtly
    different, unvalidated combination.

    Returns True (both real conditions met - the signal is present),
    False (a confirmed real hourly history exists but the signal isn't
    there right now), or None if there isn't yet enough real hourly
    history to judge - callers must fail OPEN on None, matching every
    other "don't block on missing data" gate in this codebase."""
    closes = await _fetch_hourly_closes(session, product_id, count=lookback_hours)
    if closes is None:
        return None
    rsi = _rsi_from_closes(closes)
    if rsi is None:
        return None
    if rsi >= rsi_oversold:
        return False
    support = min(closes)
    current_price = closes[-1]
    return current_price <= support * (1 + proximity_pct)


def pick_target_pct(atr_pct: float) -> float:
    if atr_pct < VOL_LOW_THRESHOLD:
        return TARGET_LOW_PCT
    if atr_pct < VOL_HIGH_THRESHOLD:
        return TARGET_MED_PCT
    return TARGET_HIGH_PCT


async def place_market_buy(session, usd_amount: float, product_id: str = PRODUCT_ID):
    """Spends usd_amount on product_id at market. Returns (filled_qty, filled_price) or None.

    Before placing, clamps usd_amount to the real current USD cash
    balance - the buy-side mirror of place_market_sell()'s existing qty
    clamp against real held balance, added after real, live
    INSUFFICIENT_FUND rejections showed up on several branches at once
    (screenshot evidence: POL/DOGE/XRP branches all rejecting in the same
    window). Root cause: every branch computes its own spend amount
    against its own snapshot of the real balance (see run_branch_cycle's
    flat-branch buy path), but with many branches running as independent,
    jittered threads and nothing coordinating the shared real cash pool
    between them, several can genuinely decide "I can afford this" off
    the same stale snapshot at nearly the same real moment - Coinbase
    itself has no concept of "reserved" cash between branches, so
    whichever order lands second gets a real, honest rejection. This
    clamp doesn't eliminate that race outright (two branches could still
    both clamp against the same real balance before either order lands),
    but it moves the check to the last possible moment before the order
    actually goes out - the same defensive placement the sell-side clamp
    already uses - so a branch never knowingly asks Coinbase for more
    than genuinely exists at that instant, and a spend that's fully
    covered up to some real amount fills for that amount instead of
    getting rejected outright."""
    real_usd, _ = await get_usd_balance(session)
    if real_usd is not None and real_usd < usd_amount:
        log.info(f"[BTC-COMPOUND] {product_id}: clamping buy ${usd_amount:.2f} -> real USD balance ${real_usd:.2f}")
        usd_amount = real_usd
    if usd_amount <= 0:
        log.warning(f"[BTC-COMPOUND] {product_id}: nothing to spend after real-balance clamp")
        return None
    # Real gap found live: a request for real free cash that's genuinely
    # too thin to ever fill (a few residual cents of unclaimed dust, not
    # actual spendable money) survived the clamp above (still > 0) and
    # went on to hit Coinbase anyway, which correctly rejected it with a
    # real INSUFFICIENT_FUND every single time - the same identical,
    # non-resolving rejection repeating every cycle (confirmed live:
    # crypto_btc_compound reinforcement retrying forever against a real
    # account with virtually all its cash already claimed by other
    # branches' own allocated_usd). This never loses real money (the seed
    # is refunded either way) but it's a pointless real API call and a
    # confusing raw Coinbase error where an honest "not enough real cash
    # to even try" is clearer. MIN_TRADE_USD is the same real practical
    # floor the stranded-dust sweep already uses for "too small to ever
    # trade."
    if usd_amount < MIN_TRADE_USD:
        log.warning(
            f"[BTC-COMPOUND] {product_id}: only ${usd_amount:.2f} real free cash after clamp - below the "
            f"${MIN_TRADE_USD:.2f} minimum trade size, skipping without hitting Coinbase"
        )
        _last_order_error[product_id] = (
            f"INSUFFICIENT_FUND: only ${usd_amount:.2f} real free cash right now - below the "
            f"${MIN_TRADE_USD:.2f} minimum trade size"
        )
        return None

    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}},
    }
    return await _place_and_confirm(session, path, order)


async def place_market_sell(session, qty: float, product_id: str = PRODUCT_ID):
    """Sells qty of product_id at market. Returns (filled_qty, filled_price) or None.

    Before placing, clamps qty to the real held balance and rounds it down
    to the product's allowed decimal precision. Both guard against a rejected
    order that would otherwise retry forever with the exact same bad size:
    the tracked position qty can drift above the real balance (fees taken in
    the asset itself, dust from an old bot, rounding on an adopted position),
    which Coinbase rejects as INSUFFICIENT_FUND; and different assets allow
    different size precision (BTC-USD allows 8 decimals, others fewer), which
    Coinbase rejects as INVALID_SIZE_PRECISION if exceeded.
    """
    base_currency = product_id.split("-")[0]
    real_balance, _ = await get_asset_balance(session, base_currency)
    if real_balance is not None and real_balance < qty:
        log.info(f"[BTC-COMPOUND] {product_id}: clamping sell qty {qty:.8f} -> real held balance {real_balance:.8f}")
        qty = real_balance

    decimals = await get_product_size_decimals(session, product_id)
    factor = 10 ** decimals
    qty = math.floor(qty * factor) / factor

    if qty <= 0:
        log.warning(f"[BTC-COMPOUND] {product_id}: nothing sellable after balance/precision clamp (qty was {qty})")
        # Tags this as a distinct, recognizable reason (not a generic
        # rejection) so a caller like _branch_sell_and_settle can tell
        # "there was genuinely nothing left to sell" apart from a real,
        # possibly-transient order rejection - see the real cross-branch
        # balance drift on shared coins this was built to catch.
        _last_order_error[product_id] = "NOTHING_TO_SELL: real balance is effectively 0 - tracked position no longer matches reality"
        return None

    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": f"{qty:.{decimals}f}"}},
    }
    return await _place_and_confirm(session, path, order)


_last_order_error = {}


def _describe_order_rejection(resp: dict) -> str:
    """Pulls the real reason out of a Coinbase order-rejection response.
    The useful part (error code, message) lives nested under
    error_response - logging the raw resp dict directly (the old
    behavior) got the important part cut off by Railway's log-line
    truncation on mobile, e.g. "{'error': 'INVALID_ARG..." with the
    actual code and message never visible. This flattens it into one
    short line so it survives truncation, and gets persisted to
    _last_order_error either way for the dashboard to show directly."""
    err = resp.get("error_response") if isinstance(resp, dict) else None
    if isinstance(err, dict):
        code = err.get("error") or resp.get("failure_reason") or "UNKNOWN"
        message = err.get("message") or err.get("error_details") or ""
        return f"{code}: {message}" if message else str(code)
    if isinstance(resp, dict) and resp.get("failure_reason"):
        return str(resp["failure_reason"])
    return str(resp)


# Real, confirmed-live rejection patterns that can NEVER succeed on retry -
# a fixed account-level permission ("PERMISSION_DENIED", confirmed on
# RNDR-USD), a dead/never-listed pair ("Invalid product_id", confirmed
# on MATIC-USD before its POL-USD migration, and on JUP-USD), or an
# order-structure mismatch the product itself will never accept
# ("UNSUPPORTED_ORDER_CONFIGURATION", confirmed live: crypto_btc_compound's
# reinforcement buy into a POL-USD branch failed with this exact code on
# every single retry across many consecutive real cycles with zero
# variation - the market_market_ioc/quote_size configuration this bot
# always sends is apparently incompatible with how that specific product
# is configured, which retrying the identical order can never fix) - as
# opposed to something that might resolve on its own (insufficient funds,
# a rate limit, a network hiccup). Deliberately narrow: only patterns
# actually observed in real production rejections, not a guess at every
# possible Coinbase error code.
_PERMANENT_REJECTION_PATTERNS = ("PERMISSION_DENIED", "Invalid product_id", "UNSUPPORTED_ORDER_CONFIGURATION")


def _is_permanent_order_rejection(reason: str) -> bool:
    """True if a real order-rejection reason (see _describe_order_rejection)
    means this exact product_id can never fill for this account, no matter
    how many times the same order is retried - used by
    crypto_family_tree_bot.py to stop a flat branch from retrying a
    doomed buy forever and switch to a different coin instead."""
    if not reason:
        return False
    return any(pattern in reason for pattern in _PERMANENT_REJECTION_PATTERNS)


async def _place_and_confirm(session, path: str, order: dict):
    product_id = order.get("product_id")
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path), json=order, timeout=15) as r:
            resp = await r.json()
            if r.status not in (200, 201) or not resp.get("success"):
                reason = _describe_order_rejection(resp)
                log.warning(f"[BTC-COMPOUND] Order not accepted ({product_id}, {order.get('side')}): {reason}")
                if product_id:
                    _last_order_error[product_id] = reason
                return None
            order_id = resp["success_response"]["order_id"]
            if product_id:
                _last_order_error.pop(product_id, None)
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Order placement failed: {e}")
        if product_id:
            _last_order_error[product_id] = f"{type(e).__name__}: {e}"
        return None

    # Poll briefly for the fill to settle so we record a real fill price -
    # never assume the requested price/qty is what actually executed.
    detail_path = f"/api/v3/brokerage/orders/historical/{order_id}"
    for _ in range(10):
        await asyncio.sleep(1)
        try:
            async with session.get(COINBASE_BASE_URL + detail_path, headers=_auth_headers("GET", detail_path), timeout=15) as r:
                if r.status != 200:
                    continue
                detail = (await r.json()).get("order", {})
                if detail.get("status") in ("FILLED", "DONE"):
                    filled_size = float(detail.get("filled_size", 0) or 0)
                    filled_value = float(detail.get("filled_value", 0) or 0)
                    if filled_size <= 0:
                        return None
                    return filled_size, filled_value / filled_size
        except Exception:
            continue
    log.warning(f"[BTC-COMPOUND] Order {order_id} placed but fill not confirmed within 10s")
    return None


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
                log.info(f"[BTC-COMPOUND] 🪜 Reloaded equity floor from DB: ${equity_floor:,.2f}")
    except Exception as e:
        log.error(f"[BTC-COMPOUND] Failed to reload equity floor from DB: {e}")


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
        log.error(f"[BTC-COMPOUND] Failed to persist equity floor: {e}")


async def load_position():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        return result.scalar_one_or_none()


async def save_position(entry_price: float, qty: float, target_price: float, stop_price: float):
    async with AsyncSessionLocal() as session:
        session.add(BotPosition(
            bot=BOT_NAME, symbol=SYMBOL, side="long",
            entry_price=entry_price, qty=qty,
            target_price=target_price, stop_price=stop_price,
            opened_at=datetime.utcnow(),
        ))
        await session.commit()


async def _raise_stop_to_breakeven(entry_price: float):
    """Only ever moves the open position's stop UP to its own entry price -
    never down, never past entry. See BREAKEVEN_TRIGGER_PCT."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        pos = result.scalar_one_or_none()
        if pos and pos.stop_price is not None and pos.stop_price < entry_price:
            pos.stop_price = entry_price
            await session.commit()


async def clear_position():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        pos = result.scalar_one_or_none()
        if pos:
            await session.delete(pos)
            await session.commit()


async def _sell_and_settle(session, position, reason: str):
    """Shared by both the normal target/stop exit and a floor-breach forced
    exit: place the market sell, confirm the real fill, record P&L, clear
    the position. Returns True if it actually sold, False if it should be
    retried next cycle."""
    global daily_pnl
    fill = await place_market_sell(session, position.qty)
    if not fill:
        log.warning(f"[BTC-COMPOUND] {reason} but sell did not fill - will retry next cycle")
        return False
    filled_qty, filled_price = fill
    gross_pnl = (filled_price - position.entry_price) * filled_qty
    fees = (position.entry_price * position.qty + filled_price * filled_qty) * (ROUND_TRIP_FEE_RATE / 2)
    net_pnl = gross_pnl - fees
    daily_pnl += net_pnl
    await clear_position()
    log.info(
        f"[BTC-COMPOUND] SOLD {filled_qty:.8f} BTC @ ${filled_price:,.2f} ({reason}) | "
        f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
        f"P&L: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f} after est. fees"
    )
    return True


async def run_cycle():
    global last_cycle_at, equity_floor
    last_cycle_at = datetime.now(timezone.utc)

    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        log.error("[BTC-COMPOUND] COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set - cannot trade")
        return

    async with aiohttp.ClientSession() as session:
        position = await load_position()
        balance, balance_err = await get_usd_balance(session)
        price, atr_pct = await get_price_and_volatility(session)

        # Total account value right now: cash + open position marked to
        # market. Skipped (not treated as zero) when either leg is
        # unavailable, so a transient API hiccup can't falsely ratchet the
        # floor down or falsely trigger a breach - it just waits for the
        # next cycle when both are available again.
        equity = None
        if balance is not None:
            equity = balance + (position.qty * price if position is not None and price is not None else 0.0)

        if equity is not None and equity >= EQUITY_FLOOR_TIER:
            candidate_floor = math.floor(equity / EQUITY_FLOOR_TIER) * EQUITY_FLOOR_TIER
            if candidate_floor > equity_floor:
                equity_floor = candidate_floor
                await save_equity_floor(equity_floor)
                log.info(f"[BTC-COMPOUND] 🪜 EQUITY FLOOR RAISED to ${equity_floor:,.2f} — will not trade below this again")

        breached = equity is not None and equity < equity_floor

        if breached:
            if position is not None:
                log.warning(
                    f"[BTC-COMPOUND] 🛑 EQUITY FLOOR BREACH: ${equity:.2f} < locked floor ${equity_floor:,.2f} "
                    f"— force-selling open position, pausing new entries"
                )
                if price is None:
                    log.warning("[BTC-COMPOUND] No price available to force-sell - will retry next cycle")
                    return
                await _sell_and_settle(session, position, "EQUITY FLOOR BREACH - forced exit")
            else:
                log.info(f"[BTC-COMPOUND] 🛑 Equity ${equity:.2f} below locked floor ${equity_floor:,.2f} — new entries paused until it recovers")
            return

        if position is None:
            if balance is None:
                log.warning(f"[BTC-COMPOUND] Balance unavailable ({balance_err}) - skipping this cycle")
                return
            if balance < MIN_TRADE_USD:
                log.info(f"[BTC-COMPOUND] Balance ${balance:.2f} below minimum trade size ${MIN_TRADE_USD:.2f} - waiting")
                return
            if price is None:
                log.warning("[BTC-COMPOUND] Could not fetch BTC price/volatility - skipping this cycle")
                return

            target_pct = max(pick_target_pct(atr_pct), min_profit_target_pct(balance, atr_pct))
            fill = await place_market_buy(session, balance)
            if not fill:
                log.warning("[BTC-COMPOUND] Buy did not fill - will retry next cycle")
                return
            filled_qty, filled_price = fill
            target_price = filled_price * (1 + target_pct)
            stop_price = filled_price * (1 - STOP_LOSS_PCT)
            await save_position(filled_price, filled_qty, target_price, stop_price)
            log.info(
                f"[BTC-COMPOUND] BOUGHT {filled_qty:.8f} BTC @ ${filled_price:,.2f} (${balance:.2f} deployed) | "
                f"ATR volatility: {atr_pct*100:.2f}% -> target +{target_pct*100:.2f}% (${target_price:,.2f}, min ${pick_min_profit_usd(atr_pct):.2f} net) | "
                f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f}) | floor ${equity_floor:,.2f}"
            )
            return

        # Position open, not breached - check for target/stop, otherwise report status.
        if price is None:
            log.warning("[BTC-COMPOUND] Could not fetch current price - holding, will re-check next cycle")
            return

        unrealized_pct = (price / position.entry_price - 1) * 100
        if price >= position.target_price:
            await _sell_and_settle(session, position, "TARGET HIT")
        elif price <= position.stop_price:
            await _sell_and_settle(session, position, "STOP HIT")
        else:
            if (position.stop_price is not None and position.stop_price < position.entry_price
                    and price >= position.entry_price * (1 + BREAKEVEN_TRIGGER_PCT)):
                await _raise_stop_to_breakeven(position.entry_price)
                position.stop_price = position.entry_price
                log.info(
                    f"[BTC-COMPOUND] 🔒 stop raised to breakeven ${position.entry_price:,.2f} "
                    f"(up {unrealized_pct:+.2f}%) - can no longer close below (about) even from here"
                )
            log.info(
                f"[BTC-COMPOUND] HOLDING {position.qty:.8f} BTC | entry ${position.entry_price:,.2f} | "
                f"now ${price:,.2f} ({unrealized_pct:+.2f}%) | target ${position.target_price:,.2f} | "
                f"stop ${position.stop_price:,.2f} | equity ${equity:.2f} | floor ${equity_floor:,.2f}"
            )


def run():
    log.info("=" * 60)
    log.info("BTC COMPOUNDING LOOP BOT — single-position, adaptive target")
    log.info(f"Stop-loss: -{STOP_LOSS_PCT*100:.1f}% | Targets: {TARGET_LOW_PCT*100:.1f}%/{TARGET_MED_PCT*100:.1f}%/{TARGET_HIGH_PCT*100:.1f}% "
              f"(quiet/normal/volatile, by ATR%) | Min trade: ${MIN_TRADE_USD:.2f} | Cycle: {CYCLE_SECONDS}s")
    log.info(f"Equity floor ratchet: locks in every ${EQUITY_FLOOR_TIER:,.0f} milestone, force-sells + pauses new "
              f"entries if total value drops below the current floor (starts at ${EQUITY_FLOOR_BASE:,.2f})")
    log.info("=" * 60)

    # One persistent event loop for this thread's whole lifetime, not a new
    # one per cycle - see prop_bot.py's run() for why (repeated asyncio.run()
    # under uvicorn's process-wide uvloop policy corrupts cross-cycle state).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(load_equity_floor())

    while True:
        try:
            loop.run_until_complete(run_cycle())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[BTC-COMPOUND] Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"[BTC-COMPOUND] Cycle error: {e}")
                log.error(f"Traceback: {traceback.format_exc()}")
        except Exception as e:
            log.error(f"[BTC-COMPOUND] Cycle error: {e}")
            log.error(f"Traceback: {traceback.format_exc()}")
        time.sleep(CYCLE_SECONDS)
