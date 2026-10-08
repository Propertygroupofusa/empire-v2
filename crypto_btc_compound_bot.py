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
import hashlib
import hmac
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
from database import get_session_factory
from models import BotPosition

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
import execution_quantity as _eq
import dust_cooldown as _dust
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


# Dual-auth support: CDP JWT (new) or HMAC/Basic Auth (legacy)
COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME") or os.getenv("COINBASE_API_KEY_NAME_BOT") or ""
COINBASE_API_PRIVATE_KEY = (os.getenv("COINBASE_API_PRIVATE_KEY") or os.getenv("COINBASE_API_PRIVATE_KEY_BOT") or "").replace("\\n", "\n")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY") or os.getenv("COINBASE_API_KEY_BOT") or ""
COINBASE_SECRET_KEY = os.getenv("COINBASE_SECRET_KEY") or os.getenv("COINBASE_SECRET_KEY_BOT") or ""
COINBASE_PASSPHRASE = os.getenv("COINBASE_PASSPHRASE") or os.getenv("COINBASE_PASSPHRASE_BOT") or ""
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
PRODUCT_ID = "BTC-USD"
SYMBOL = "BTC/USD"
BOT_NAME = "crypto_btc_compound"

# Startup validation - check for both auth methods
cdp_configured = bool(COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY)
hmac_configured = bool(COINBASE_API_KEY and COINBASE_SECRET_KEY and COINBASE_PASSPHRASE)

if cdp_configured:
    log.info(f"✓ CDP Auth configured: COINBASE_API_KEY_NAME={COINBASE_API_KEY_NAME[:15]}...")
    if COINBASE_API_PRIVATE_KEY.startswith("-----BEGIN"):
        log.info(f"  └─ Private key format: PEM (ECDSA, {len(COINBASE_API_PRIVATE_KEY)} chars)")
    else:
        log.info(f"  └─ Private key format: base64 (Ed25519, {len(COINBASE_API_PRIVATE_KEY)} chars)")

if hmac_configured:
    log.info(f"✓ HMAC Auth configured: COINBASE_API_KEY={COINBASE_API_KEY[:15]}...")

if not (cdp_configured or hmac_configured):
    log.error("⚠️  No Coinbase API credentials configured - bot will fail to start")

CYCLE_SECONDS = _safe_int_env("BTC_COMPOUND_CYCLE_SECONDS", "30")
MIN_TRADE_USD = _safe_float_env("BTC_COMPOUND_MIN_TRADE_USD", "5.00")

# Ceiling on how much of the USD balance a single entry may use. The bot
# otherwise deploys 100% of available cash every time, so money moved into
# the account for any other purpose - a reserve held back while a strategy
# is still being measured, proceeds from liquidating other coins - gets
# swept into the next buy automatically.
#
# 0 means no cap, which is the historical behaviour and stays the default:
# setting this is opt-in and nothing changes for anyone who does not.
# Capital above the cap simply stays as cash; it is not reserved, tracked
# or spent, and it still counts toward equity for the floor ratchet, which
# is correct - it is real money at risk of nothing.
# Money held out of every entry. The bot deploys the whole balance above
# this line and never touches the line itself.
#
# This replaced a ceiling (BTC_COMPOUND_MAX_DEPLOY_USD, "deploy at most
# $X"), which protected the same dollars but froze position size: once the
# balance passed the cap every win landed in idle cash and the next entry
# still deployed the cap, so a compounding bot stopped compounding. A floor
# protects the same amount and lets everything above it grow - $500 held
# back either way, but the traded pool goes $582 -> $681 over ten wins
# instead of staying at $582 while $589 sits dead.
RESERVE_USD = _safe_float_env("BTC_COMPOUND_RESERVE_USD", "500.00")

# The retired ceiling. Reading it only to say it is being ignored, because
# an env var that silently stops applying is worse than one that never
# existed.
if os.getenv("BTC_COMPOUND_MAX_DEPLOY_USD"):
    log.warning(
        "BTC_COMPOUND_MAX_DEPLOY_USD is set but no longer used - it was a "
        "deploy CEILING and froze position size. Use BTC_COMPOUND_RESERVE_USD "
        "(currently $%.2f held back) instead, and remove the old variable.",
        RESERVE_USD,
    )


def deployable_usd(balance: float) -> float:
    """Everything above the reserve. Grows with the account, unlike a cap."""
    if RESERVE_USD <= 0:
        return balance
    return max(0.0, balance - RESERVE_USD)


# Profit skim. On a winning exit, this fraction of the REALIZED net profit
# is moved into a locked ledger and permanently excluded from deployment,
# so a gain that has already been banked cannot be handed back by a later
# losing trade. It does not make the account profitable - nothing does -
# it only stops profit that was genuinely made from being re-risked.
#
# Default 10%, set on the account owner's explicit instruction ("set the
# skim to 10%"), given after the request that realized profit must stop
# being handed back by later losing trades.
#
# This deliberately reverses an earlier instruction from the same owner,
# recorded at crypto_family_tree_bot.PROFIT_SKIM_PCT: "take away the lock
# profit, I don't want that anymore for any of my stuff, I want all my
# money to be making money." That one still governs the family tree, which
# remains at 0.0 and is untouched here. Only this bot skims. The two
# settings disagreeing is intentional, not drift - if the tree should skim
# too, TREE_PROFIT_SKIM_PCT is its own switch.
PROFIT_SKIM_PCT = _safe_float_env("BTC_COMPOUND_PROFIT_SKIM_PCT", "0.10")
LOCKED_PROFIT_STATE_KEY = "crypto_btc_compound_locked_usd"


async def get_locked_usd() -> float:
    """Profit already skimmed out of the compounding loop. 0.0 if unreadable
    - a DB hiccup must not make locked money look spendable, but it also
    must not stop the bot, so the conservative read is combined with the
    caller only ever SUBTRACTING this from what it may deploy."""
    try:
        from models import TradingBotState
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(
                    TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
            row = result.scalar_one_or_none()
            return float(row.base_capital) if row and row.base_capital else 0.0
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Could not read locked profit ({e}) - treating as $0")
        return 0.0


async def add_locked_usd(amount: float) -> None:
    """Move realized profit permanently out of the compounding loop.

    Only ever called with a positive amount after a winning exit. There is
    no automatic path back: releasing locked profit is a deliberate manual
    action, the same convention the family tree uses.
    """
    if amount <= 0:
        return
    try:
        from models import TradingBotState
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(
                    TradingBotState.bot_name == LOCKED_PROFIT_STATE_KEY))
            row = result.scalar_one_or_none()
            if row:
                row.base_capital = (row.base_capital or 0.0) + amount
            else:
                db.add(TradingBotState(bot_name=LOCKED_PROFIT_STATE_KEY,
                                       base_capital=amount, starting_capital=0.0))
            await db.commit()
    except Exception as e:
        log.error(f"[BTC-COMPOUND] Failed to lock ${amount:.2f} of profit: {e}")


def tracked_equity(balance: float, position_value):
    """The capital the equity floor should watch: what is actually at risk.

    Without a reserve this is the whole account, unchanged. With one, money
    the reserve holds back is not trading and must not drag the floor up
    behind it - otherwise a reserve makes the floor rise while the traded
    capital stays the same size, and a drawdown that only ever touched the
    traded portion trips a floor set against money that never moved.

    Flat, the trading pool is everything above the reserve. Holding a
    position, every deployable dollar is already in it, so the idle cash IS
    the reserve and the position's own market value is the pool.
    """
    pos = position_value or 0.0
    if RESERVE_USD <= 0:
        return balance + pos
    if position_value is not None:
        return pos
    return max(0.0, balance - RESERVE_USD)
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
# TARGET_LOW_PCT was 1.5% against a 2% stop - the quiet-market tier risked
# more than it stood to make, before fees. Observed live on 2026-09-24:
# entry $84,455.82, target $85,722.66 (+1.5%), stop $82,766.70 (-2%), which
# needs an 80% win rate to break even once the ~0.8% round trip is paid.
# Raised so a target is never smaller than the stop it is paired with. It
# stays the SMALLEST of the three tiers - a quiet market really does offer
# less - and when even this tier cannot clear the bar below, the honest
# answer is not to trade at all, which is what the entry gate enforces.
TARGET_LOW_PCT = _safe_float_env("BTC_COMPOUND_TARGET_LOW_PCT", "0.021")   # 2.1%
TARGET_MED_PCT = _safe_float_env("BTC_COMPOUND_TARGET_MED_PCT", "0.025")   # 2.5%
TARGET_HIGH_PCT = _safe_float_env("BTC_COMPOUND_TARGET_HIGH_PCT", "0.04")  # 4%

# The real Coinbase round trip: 0.75% per leg taker = 1.50% both ways,
# measured 2026-09-25 from Coinbase's own fill records (liquidity_indicator
# and commission per fill), not assumed. The old 0.008 was documented as
# "~0.4% each way, taker" and was roughly half the truth.
#
# crypto_family_tree_bot re-exports this as its own ROUND_TRIP_FEE_RATE and
# prices exit fees with it, and the dashboard shows a per-trade fee
# estimate from it - so every one of those understated the cost of getting
# out. Live order execution reads the OBSERVED rate via
# get_effective_round_trip_fee_rate(), which is unaffected either way.
ROUND_TRIP_FEE_RATE = _safe_float_env("BTC_COMPOUND_ROUND_TRIP_FEE_RATE", "0.015")

# The most an entry is allowed to demand of the win rate before this bot
# refuses to place it.
#
# There is no target/stop pair that cannot lose, and it is worth being
# exact about why, because the intuition that a bigger target or a tighter
# stop fixes this is wrong. For a bracket order on a driftless price, the
# chance of touching +T before -S is S/(T+S), so the expectancy works out
# to exactly -fee for EVERY choice of T and S - the wider target pays more
# per win and is hit proportionally less often, and the two cancel with
# nothing left over. Target and stop do not create edge; they only decide
# how an edge, or the absence of one, gets expressed. What they CAN do is
# be arithmetically self-defeating, which is what a target below its own
# stop is.
#
# So the enforceable version of "don't take losing trades" is not a magic
# ratio - it is declining the entries whose own arithmetic needs a win
# rate nobody here has produced. At the default 0.8% taker round trip that
# admits only the volatile tier; switching to maker orders halves the fee
# and admits the normal tier too, which is the real lever and the reason
# the fee rate is a variable rather than a constant.
MAX_BREAKEVEN_WIN_RATE = _safe_float_env("BTC_COMPOUND_MAX_BREAKEVEN_WIN_RATE", "0.55")

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

# A fixed $50 tier does not scale, and at a small account it strangles the
# strategy it is meant to protect. On $582 the floor lands at $550, leaving
# $32 of room - three stop-outs. Worse, two winning trades ratchet it to
# $600 against equity of $602, leaving $2.31: the very next loss breaches
# and halts the bot. The same $50 on a $10,000 account is 0.5% and barely
# felt. The tier was sized for the larger account.
#
# So the floor never sits closer to equity than EQUITY_FLOOR_MIN_HEADROOM_PCT
# of it. Below that the tier is widened proportionally, which keeps roughly
# the same number of stop-outs of room at every account size instead of
# tightening as the account grows. The ratchet is unchanged: it is still
# computed from real equity and still only ever moves up.
EQUITY_FLOOR_MIN_HEADROOM_PCT = _safe_float_env(
    "BTC_COMPOUND_EQUITY_FLOOR_MIN_HEADROOM_PCT", "0.10")


def compute_equity_floor(equity: float) -> float:
    """Floor a fixed percentage below equity, rounded down to a tier.

    Taking the headroom first and rounding second is what guarantees the
    room actually exists. The old form rounded equity down to a $50 tier and
    took whatever was left, which is zero whenever equity lands on a clean
    multiple - at exactly $650, $1,000 or $10,000 the floor equalled equity
    and the next tick of any size halted the bot.

    Rounding to EQUITY_FLOOR_TIER afterwards keeps the floor a tidy number
    to read in the logs, and only ever adds headroom, never removes it.
    Returns a candidate; the caller still only ever raises the stored floor.
    """
    if equity <= 0:
        return 0.0
    target = equity * (1.0 - EQUITY_FLOOR_MIN_HEADROOM_PCT)
    return max(0.0, math.floor(target / EQUITY_FLOOR_TIER) * EQUITY_FLOOR_TIER)
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
        log.error("COINBASE_API_PRIVATE_KEY not set or empty - check Railway environment variables")
        raise ValueError("COINBASE_API_PRIVATE_KEY not set")
    if raw.startswith("-----BEGIN"):
        log.debug(f"Using PEM format private key (ES256 algorithm), key starts with: {raw[:50]}...")
        return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
    log.debug(f"Using base64 format Ed25519 key (EdDSA algorithm), key starts with: {raw[:50]}...")
    decoded = base64.b64decode(raw, validate=True)
    if len(decoded) != 64:
        raise ValueError(f"Ed25519 key must be 64 bytes decoded, got {len(decoded)}")
    return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


def _build_jwt(method: str, path: str) -> str:
    if not COINBASE_API_KEY_NAME:
        log.error("COINBASE_API_KEY_NAME not set - check Railway environment variables")
        raise ValueError("COINBASE_API_KEY_NAME not set")

    private_key, algorithm = _load_signing_key()
    now = int(time.time())
    payload = {
        "sub": COINBASE_API_KEY_NAME,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        # The URI claim must NOT carry the query string. Coinbase signs
        # "GET host/api/v3/brokerage/product_book", not
        # "GET host/api/v3/brokerage/product_book?product_id=BTC-USD&limit=1",
        # so including it makes every parameterised request fail the
        # signature check and return 401.
        #
        # What that cost, live: get_best_bid_ask() passes
        # "...product_book?product_id=X&limit=1". It 401'd on every call and
        # returned (None, None) - and place_maker_buy/place_maker_sell open
        # with "if bid is None: return None", so they fell straight through
        # to place_market_buy/sell. Not one maker order was ever placed.
        # Every fill paid the 1.50% taker round trip instead of 0.70%,
        # which pinned the fee floor at 1.70%, which pinned the grid step at
        # 2.00%, which is why the fleet trades a few times a week.
        # get_recent_market_trades() and the order-reconciliation fill
        # lookup were failing the same way, silently.
        "uri": f"{method} {COINBASE_HOST}{path.split('?', 1)[0]}",
    }
    headers = {"kid": COINBASE_API_KEY_NAME, "nonce": secrets.token_hex(16)}
    jwt_token = pyjwt.encode(payload, private_key, algorithm=algorithm, headers=headers)
    log.debug(f"Built JWT for {method} {path} using algorithm {algorithm}, sub={COINBASE_API_KEY_NAME}")
    return jwt_token


def _build_hmac_signature(method: str, path: str, body: str = "") -> tuple:
    """Coinbase HMAC/Basic Auth signature (legacy method)."""
    timestamp = str(time.time())
    message = timestamp + method + path + body
    signature = base64.b64encode(
        hmac.new(
            COINBASE_SECRET_KEY.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()
    return signature, timestamp


def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """Return auth headers - try CDP JWT first, fall back to HMAC if JWT unavailable."""
    # Try CDP JWT authentication first (new method)
    if COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY:
        try:
            return {"Authorization": f"Bearer {_build_jwt(method, path)}", "Content-Type": "application/json"}
        except Exception as e:
            log.warning(f"CDP JWT auth failed, trying HMAC: {e}")

    # Fall back to HMAC/Basic Auth (legacy method)
    if COINBASE_API_KEY and COINBASE_SECRET_KEY and COINBASE_PASSPHRASE:
        signature, timestamp = _build_hmac_signature(method, path, body)
        return {
            "CB-ACCESS-KEY": COINBASE_API_KEY,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "CB-ACCESS-PASSPHRASE": COINBASE_PASSPHRASE,
            "Content-Type": "application/json"
        }

    # No credentials available - error
    raise ValueError("No Coinbase API credentials configured. Set either CDP (COINBASE_API_KEY_NAME + COINBASE_API_PRIVATE_KEY) or HMAC (COINBASE_API_KEY + COINBASE_SECRET_KEY + COINBASE_PASSPHRASE)")


# ONE ACCOUNT PULL SERVES EVERY CURRENCY IN IT.
#
# get_asset_balance reads /api/v3/brokerage/accounts - the WHOLE account
# list, up to 250 rows a page - and then scans it for ONE currency. Reading
# USD and USDC was therefore two complete account pulls for two numbers that
# arrive in the same response. There are 38 call sites across 7 files, and
# place_maker_sell calls it per branch per cycle: 23 branches on a 30s cycle
# is 23 full account listings a minute from that path alone, before the
# census, the dashboard and the other bots.
#
# That is where the rate limiting came from. Measured 2026-10-01 05:21Z:
#
#     WARNING HTTP 429 fetching USD
#     WARNING HTTP 429 fetching USDC
#     WARNING [GRID] real fee-tier lookup failed (HTTP 429)
#     [GRID] account book unreadable - concentration not checked this cycle
#     [DEPLOY] UNKNOWN: free cash unreadable - a gap is not a zero
#
# Every one of those is downstream of the same thing: too many identical
# calls. The response already contains every currency, so one pull now
# answers all of them for a short window.
#
# THE TTL IS SHORT ON PURPOSE. place_maker_sell sizes REAL ORDERS against
# this number. Ten seconds collapses one cycle's reads into one call while
# staying current inside that cycle; anything longer starts sizing orders
# against a balance that a fill may already have changed.
_BALANCE_TTL_SECONDS = float(os.getenv("COINBASE_BALANCE_TTL_SECONDS", "10"))
_BALANCES_CACHE = {"at": 0.0, "by_currency": None}


def invalidate_balance_cache(reason: str = "") -> None:
    """Drop the cached balances. Call after anything that moves money.

    A fill changes a balance, and an order sized against the balance from
    before it is an order sized against money that is already gone. Cheaper
    to re-read than to reason about which currencies a fill touched.
    """
    _BALANCES_CACHE["by_currency"] = None
    _BALANCES_CACHE["at"] = 0.0
    if reason:
        log.debug(f"[balances] cache dropped: {reason}")


async def get_asset_balance(session, currency: str) -> tuple:
    """Real available balance of a given asset currency (e.g. 'USD', 'DOT',
    'LDO'). Returns (balance, None) or (None, reason).

    Served from a short-lived cache of the whole account listing - see
    _BALANCES_CACHE above. A FAILED read is never cached and never served:
    an error returns an error, so a rate limit still reads as a gap rather
    than as a stale number wearing a current one's clothes.
    """
    cached = _BALANCES_CACHE.get("by_currency")
    if cached is not None and (time.time() - _BALANCES_CACHE["at"]) < _BALANCE_TTL_SECONDS:
        if currency in cached:
            return cached[currency], None
        # A currency absent from a COMPLETE listing genuinely has no account
        # on this key. That is the same answer the uncached path gives.
        return None, f"no {currency} account found on this key"

    path = "/api/v3/brokerage/accounts"
    cursor = None
    _all = {}
    _walked_to_end = False
    try:
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            try:
                headers = _auth_headers("GET", path)
            except ValueError as e:
                log.error(f"Failed to build auth headers for {currency}: {e}")
                return None, f"Auth header build failed: {str(e)}"

            async with session.get(COINBASE_BASE_URL + path, headers=headers, params=params, timeout=15) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    if r.status == 401:
                        log.error(f"HTTP 401 Unauthorized fetching {currency}: {body}. API key name: {COINBASE_API_KEY_NAME[:10] if COINBASE_API_KEY_NAME else 'NOT SET'}...")
                    else:
                        log.warning(f"HTTP {r.status} fetching {currency}: {body}")
                    return None, f"HTTP {r.status}: {body}"
                data = await r.json()
                for account in data.get("accounts", []):
                    # Keep EVERY currency this page carried, not just the one
                    # asked for. The rows are already here; throwing them away
                    # is what made the next caller fetch them again.
                    cur = account.get("currency")
                    if not cur:
                        continue
                    try:
                        _all[cur] = float(account["available_balance"]["value"])
                    except (TypeError, ValueError, KeyError):
                        continue      # an unreadable row is skipped, never zeroed
                if not data.get("has_next"):
                    _walked_to_end = True      # genuinely the last page
                    break
                if not data.get("cursor"):
                    # has_next says there IS more and the venue gave us no way
                    # to ask for it. The listing is INCOMPLETE, and the
                    # difference matters: a complete listing can say "this key
                    # has no DOGE account", an incomplete one cannot.
                    break
                cursor = data.get("cursor")
        # Cache only a listing walked to its end. Caching a partial one would
        # make a currency on an unfetched page look like it has no account at
        # all - a gap reported as a fact, which is the bug this codebase keeps
        # relearning.
        if _walked_to_end:
            _BALANCES_CACHE["by_currency"] = _all
            _BALANCES_CACHE["at"] = time.time()
        if currency in _all:
            return _all[currency], None
        if not _walked_to_end:
            return None, (f"{currency} not found, but the account listing was "
                          f"incomplete - this is UNKNOWN, not an absent account")
        return None, f"no {currency} account found on this key"
    except asyncio.TimeoutError:
        return None, "Coinbase API timeout"
    except aiohttp.ClientError as e:
        return None, f"Coinbase connection failed: {type(e).__name__}"
    except Exception as e:
        log.exception(f"Exception in get_asset_balance for {currency}")
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
        try:
            headers = _auth_headers("GET", path)
        except ValueError as e:
            log.error(f"Failed to build auth headers for get_all_asset_balances: {e}")
            return None, f"Auth header build failed: {str(e)}"

        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            async with session.get(COINBASE_BASE_URL + path, headers=headers, params=params, timeout=15) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    if r.status == 401:
                        log.error(f"HTTP 401 Unauthorized fetching all balances: {body}. API key name: {COINBASE_API_KEY_NAME[:10] if COINBASE_API_KEY_NAME else 'NOT SET'}...")
                    else:
                        log.warning(f"HTTP {r.status} fetching all balances: {body}")
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
        log.exception(f"Exception in get_all_asset_balances")
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


# Product rules are static per market, so they are fetched once and kept.
# Only SUCCESSES are cached: caching a failure would let one blip disable a
# product until restart, and the whole point of failing closed is that it
# recovers as soon as the venue answers again.
_PRODUCT_RULES_CACHE = {}


async def get_product_rules(session, product_id: str):
    """Every size rule the venue publishes for this market, or None.

    None means UNKNOWN and the caller must refuse. That is the difference
    between this and get_product_size_decimals, which returns 8 on any
    failure - the most permissive value on the venue - and so turns an
    unreadable product into an order sized against a guess. ALGO-USD's real
    base_increment is 0.1; sized at 8 decimals the venue rejects it.

    base_min_size and quote_min_size may legitimately be absent: the public
    Exchange API publishes neither while this brokerage API publishes both.
    Absent means the rule is not asserted, never that it is satisfied, and
    the increment floor applies either way.
    """
    cached = _PRODUCT_RULES_CACHE.get(product_id)
    if cached is not None:
        return cached
    path = f"/api/v3/brokerage/products/{product_id}"
    try:
        async with session.get(COINBASE_BASE_URL + path,
                               headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                log.warning(f"[GRID] {product_id}: product rules unreadable "
                            f"(HTTP {r.status}). No order will be sized against "
                            f"a guess.")
                return None
            data = await r.json()
    except Exception as e:
        log.warning(f"[GRID] {product_id}: product rules fetch failed "
                    f"({type(e).__name__}: {e}). No order will be sized "
                    f"against a guess.")
        return None
    inc = data.get("base_increment")
    if not inc:
        # The one field with no safe default. Without it there is no way to
        # know what sizes this market accepts.
        log.warning(f"[GRID] {product_id}: the venue returned no "
                    f"base_increment. Refusing rather than assuming one.")
        return None
    rules = {
        "base_increment": inc,
        "base_min_size": data.get("base_min_size") or None,
        "quote_min_size": data.get("quote_min_size") or None,
        # The PRICE tick, as distinct from base_increment's SIZE step. Free
        # here - it is in the same response - and needed by slice_target to
        # round a sell target UP to a price the venue will actually accept.
        # Rounding a sell target DOWN would give away the edge it was
        # computed to earn, which is why slice_target has its own rounding
        # rather than reusing resting_stops.round_price.
        #
        # None when absent, never a guessed tick: a target rounded to an
        # invented increment is a price the venue may refuse.
        "quote_increment": data.get("quote_increment") or None,
        "product_id": product_id,
    }
    _PRODUCT_RULES_CACHE[product_id] = rules
    return rules


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


def breakeven_win_rate(target_pct: float, stop_pct: float = None,
                       fee_rate: float = None) -> float:
    """The win rate this target/stop/fee combination needs just to break even.

    A win nets target minus the round trip; a loss costs the stop PLUS the
    same round trip, because the fee is paid either way. Solving
    p*net_win == (1-p)*net_loss gives the rate below which the setup loses
    money however well it is executed.

    Returns 1.0 - unachievable, refuse it - when the target does not clear
    the fee at all. That case is not a near miss: a "winning" trade that
    nets zero or less has no win rate that rescues it, and expressing it as
    a ratio would understate it.
    """
    stop = STOP_LOSS_PCT if stop_pct is None else stop_pct
    fee = ROUND_TRIP_FEE_RATE if fee_rate is None else fee_rate
    net_win = target_pct - fee
    net_loss = stop + fee
    if net_win <= 0:
        return 1.0
    return net_loss / (net_win + net_loss)


async def place_market_buy(session, usd_amount: float, product_id: str = PRODUCT_ID,
                           source: str = None):
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
    return await _place_and_confirm(session, path, order, source=source)


async def place_market_sell(session, qty: float, product_id: str = PRODUCT_ID,
                            source: str = None,
                            allow_unverified_balance: bool = False):
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
    real_balance, _bal_err = await get_asset_balance(session, base_currency)
    if real_balance is None:
        # AN UNKNOWN BALANCE MUST NEVER GENERATE AN ORDER.
        #
        # This used to fall through. The clamp read "if the balance is not
        # None AND it is smaller, clamp", so a FAILED read skipped the clamp
        # entirely and the order went out at the TRACKED quantity - the one
        # number already known to drift away from the wallet. That is how a
        # branch sends an order for coin it does not hold, and the reason was
        # discarded into `_`, so nothing ever said why.
        #
        # A forced exit is the one caller that may proceed: refusing there
        # leaves a live position unprotected, which is the worse failure for
        # a protection path. It must ask explicitly and it is logged as the
        # risk it is.
        if not allow_unverified_balance:
            log.warning(
                f"[BTC-COMPOUND] {product_id}: REFUSING to sell - the real "
                f"{base_currency} balance could not be read ({_bal_err}). An "
                f"unknown balance is not a known one; no order placed, will "
                f"retry next cycle.")
            _last_order_error[product_id] = f"balance unreadable: {_bal_err}"
            return None
        log.warning(
            f"[BTC-COMPOUND] {product_id}: balance unreadable ({_bal_err}) but "
            f"the caller is a FORCED EXIT, so selling the tracked qty "
            f"{qty:.8f} unverified. If this over-sends the venue rejects it; "
            f"leaving the position open was judged the larger risk.")
    elif real_balance < qty:
        log.info(f"[BTC-COMPOUND] {product_id}: clamping sell qty {qty:.8f} -> real held balance {real_balance:.8f}")
        qty = real_balance

    # A PROTECTION PATH, SIZED BY THE VENUE'S REAL RULES.
    #
    # The increment and any minimum SIZE are enforced. The minimum order
    # VALUE deliberately is not: evaluating it needs a live price, and this
    # function is the forced-exit path - the same one whose comment above
    # says that leaving a position open was judged the larger risk than
    # selling unverified. Adding a book read here would give a protection a
    # new way to fail, and a sale under the venue's notional floor is
    # rejected loudly rather than lost quietly.
    #
    # Unreadable rules still refuse. There is no safe guess for what sizes a
    # market accepts, and the old 8-decimal fallback produced orders the
    # venue rejects outright on any product with a coarser increment.
    _rules = await get_product_rules(session, product_id)
    if _rules is None:
        log.warning(
            f"[BTC-COMPOUND] {product_id}: NOT SELLING - the product's size "
            f"rules could not be read. Refusing to size an order against a "
            f"guess.")
        _last_order_error[product_id] = (
            "product rules unreadable: refusing to size an order against a guess")
        return None
    _plan = _eq.plan_order_quantity(
        requested_quantity=qty, available_quantity=qty, price=None,
        base_increment=_rules["base_increment"],
        base_min_size=_rules["base_min_size"], quote_min_size=None)
    qty = float(_plan.executable_quantity)

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
        # The planner's own string, so the format cannot disagree with the floor.
        "order_configuration": {"market_market_ioc": {"base_size": _plan.order_size_string}},
    }
    return await _place_and_confirm(session, path, order, source=source)


async def get_best_bid_ask(session, product_id: str = PRODUCT_ID):
    """Real current top of book for product_id. Returns (bid, ask) or
    (None, None) on a real failure - never a fabricated price.

    Needed for maker (post-only) orders: to earn the maker rate an order
    has to REST on the book rather than cross it, so it must be priced at
    or behind the current best bid (buying) / best ask (selling). A market
    order crosses by definition and always pays the taker rate."""
    path = f"/api/v3/brokerage/product_book?product_id={product_id}&limit=1"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                return None, None
            book = (await r.json()).get("pricebook", {})
            bids, asks = book.get("bids") or [], book.get("asks") or []
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            return bid, ask
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] {product_id}: real order-book fetch failed: {type(e).__name__}: {e}")
        return None, None


async def get_mid_prices(session, product_ids, chunk: int = 50) -> dict:
    """Real mid price (bid+ask)/2 for many products in ONE request per chunk.

    Returns {product_id: price_or_None}. A product the venue did not price
    comes back None - never a fabricated or stale number.

    WHY THIS EXISTS. Valuing the grid's holdings used to download a full
    candle history per coin (get_price_and_volatility) one after another -
    ~23 sequential requests on every dashboard poll, on the same API key the
    trading loop is using. Any one timeout or 429 blanked the whole Coinbase
    net-worth figure, because that total is (correctly) all-or-nothing. A
    price needs one top-of-book read, not 300 candles, and one request can
    carry every product.
    """
    ids = [p for p in dict.fromkeys(product_ids or ()) if p]
    out = {p: None for p in ids}
    path = "/api/v3/brokerage/best_bid_ask"
    for i in range(0, len(ids), max(1, chunk)):
        part = ids[i:i + chunk]
        try:
            async with session.get(COINBASE_BASE_URL + path,
                                   headers=_auth_headers("GET", path),
                                   params=[("product_ids", p) for p in part],
                                   timeout=15) as r:
                if r.status != 200:
                    log.warning(f"[PRICES] best_bid_ask HTTP {r.status} for {len(part)} product(s)")
                    continue
                books = (await r.json()).get("pricebooks") or []
        except Exception as e:
            log.warning(f"[PRICES] best_bid_ask failed: {type(e).__name__}: {e}")
            continue
        for b in books:
            pid = b.get("product_id")
            if pid not in out:
                continue
            try:
                bid = float((b.get("bids") or [{}])[0].get("price"))
                ask = float((b.get("asks") or [{}])[0].get("price"))
            except (TypeError, ValueError, IndexError):
                continue
            if bid > 0 and ask > 0:
                out[pid] = (bid + ask) / 2
    return out


async def get_book_top_and_depth(session, product_id: str = PRODUCT_ID, levels: int = 5):
    """Top of book plus cumulative USD depth on each side.

    get_best_bid_ask() above fetches limit=1, which answers "what price"
    but not "how much is there" - and those are different questions. An
    order sized at or above the visible depth does not trade AT the top of
    book, it trades THROUGH it, and its exit then finds nothing to sell
    into. Sizing needs the second number.

    Depth is summed as price x size over the first `levels` entries on each
    side, which is the quantity a marketable order would actually consume.

    Returns (bid, ask, bid_depth_usd, ask_depth_usd), any element None on a
    real failure - never a fabricated number, so a caller can fail closed
    on a book it could not read rather than sizing against a guess.
    """
    path = f"/api/v3/brokerage/product_book?product_id={product_id}&limit={max(1, levels)}"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                return None, None, None, None
            book = (await r.json()).get("pricebook", {})
            bids, asks = book.get("bids") or [], book.get("asks") or []

            def _depth(side):
                total = 0.0
                for entry in side[:levels]:
                    try:
                        total += float(entry["price"]) * float(entry["size"])
                    except (KeyError, TypeError, ValueError):
                        # One malformed level is not a reason to discard the
                        # rest; it just does not count toward the total,
                        # which errs toward reporting LESS depth than exists.
                        continue
                return total

            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            return bid, ask, (_depth(bids) if bids else None), (_depth(asks) if asks else None)
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] {product_id}: real book depth fetch failed: {type(e).__name__}: {e}")
        return None, None, None, None


async def get_recent_market_trades(session, product_id: str = PRODUCT_ID, limit: int = 50):
    """The last `limit` REAL trades printed on this product, newest first.

    The book says what is WAITING; this says what actually traded, and each
    print carries the side that crossed the spread. That is the difference
    between resting intent - which can be pulled the instant an order comes
    for it - and committed flow, which cannot.

    Returns a list of trade dicts, or None on any failure. None rather than
    [] deliberately: an empty tape and an unreachable endpoint mean
    opposite things to a caller weighing pressure, and [] would quietly
    report the market as balanced.
    """
    path = f"/api/v3/brokerage/products/{product_id}/ticker?limit={max(1, limit)}"
    try:
        async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), timeout=15) as r:
            if r.status != 200:
                return None
            trades = (await r.json()).get("trades")
            return trades if isinstance(trades, list) else None
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] {product_id}: real market trades fetch failed: {type(e).__name__}: {e}")
        return None


async def cancel_order(session, order_id: str) -> bool:
    """Cancel a real resting order. Returns True if Coinbase accepted the
    cancel. Used when a maker order hasn't filled inside its wait window -
    it MUST be cancelled before any fallback order is placed, or the
    account could end up holding both."""
    path = "/api/v3/brokerage/orders/batch_cancel"
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path),
                                json={"order_ids": [order_id]}, timeout=15) as r:
            if r.status not in (200, 201):
                return False
            results = (await r.json()).get("results") or []
            return bool(results and results[0].get("success"))
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] cancel of order {order_id} failed: {type(e).__name__}: {e}")
        return False


# Every post-only maker order this module places carries this prefix.
#
# WHY. A maker order is placed, waited on for up to 45s, then cancelled -
# all inside one call. If the process dies inside that window (a Railway
# redeploy sends SIGTERM), nothing cancels it, and because it is GTC it rests
# on Coinbase indefinitely: owned by no process, invisible to the ledger,
# holding cash or coin, and able to fill hours later with no slice recording
# it. With a bare uuid4 client_order_id it could not even be told apart from
# an order the account owner placed by hand. The prefix is what lets a
# restarted process find and cancel exactly these, and nothing else.
# (The FILLS feed carries no client_order_id - see test_order_attribution -
# but the OPEN-orders list does; resting_stops_worker relies on the same.)
GRID_MAKER_COID_PREFIX = "gmk-"


def select_orphan_maker_orders(orders):
    """The OPEN orders that are this module's own maker orders.

    Pure, so it can be tested without a venue. Anything without the prefix -
    a resting stop ("rstop-"), an order placed by hand - is never selected.
    """
    out = []
    for o in orders or ():
        if not isinstance(o, dict):
            continue
        if not str(o.get("client_order_id") or "").startswith(GRID_MAKER_COID_PREFIX):
            continue
        if str(o.get("status") or "OPEN").upper() not in ("OPEN", "PENDING", "QUEUED"):
            continue
        try:
            filled = float(o.get("filled_size") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        out.append({"order_id": o.get("order_id"),
                    "product_id": o.get("product_id"),
                    "side": o.get("side"),
                    "filled_size": filled,
                    "created_time": o.get("created_time")})
    return out


async def sweep_orphan_maker_orders(session):
    """Cancel every OPEN maker order a previous process left behind.

    Call ONCE, when a process first takes the loop, before it places any
    order of its own - at that point every OPEN "gmk-" order belongs to a
    process that is gone. Returns None if the open orders could not be read
    (a gap is not a zero: "could not look" must never read as "none left").

    A partial fill on an orphan is REPORTED, not booked. Coin or cash moved
    with no slice behind it; the existing reconciliation (slice_reconcile,
    reconcile.py, adoption) is where that is corrected, and inventing a
    slice here would be a guess written where a measurement belongs.
    """
    path = "/api/v3/brokerage/orders/historical/batch"
    try:
        async with session.get(f"{COINBASE_BASE_URL}{path}?order_status=OPEN&limit=250",
                               headers=_auth_headers("GET", path), timeout=25) as r:
            if r.status != 200:
                log.warning(f"[ORPHANS] open orders HTTP {r.status} - sweep not run")
                return None
            body = await r.json()
    except Exception as e:
        log.warning(f"[ORPHANS] open orders unreadable: {type(e).__name__}: {e}")
        return None

    orphans = select_orphan_maker_orders(body.get("orders"))
    cancelled, failed = [], []
    for o in orphans:
        (cancelled if await cancel_order(session, o["order_id"]) else failed).append(o)
    return {"found": len(orphans), "cancelled": cancelled, "failed": failed,
            "partially_filled": [o for o in orphans if o["filled_size"] > 0]}


async def _await_fill(session, order_id: str, wait_seconds: int):
    """Poll a real resting order for up to wait_seconds. Returns
    (filled_qty, avg_price) on a real fill, or None if it is still
    unfilled when the window closes. A PARTIAL fill is returned as the
    real partial - never rounded up to the full requested size."""
    detail_path = f"/api/v3/brokerage/orders/historical/{order_id}"
    for _ in range(max(1, wait_seconds)):
        await asyncio.sleep(1)
        try:
            async with session.get(COINBASE_BASE_URL + detail_path,
                                   headers=_auth_headers("GET", detail_path), timeout=15) as r:
                if r.status != 200:
                    continue
                detail = (await r.json()).get("order", {})
                filled_size = float(detail.get("filled_size", 0) or 0)
                filled_value = float(detail.get("filled_value", 0) or 0)
                if detail.get("status") in ("FILLED", "DONE") and filled_size > 0:
                    return filled_size, filled_value / filled_size
                if detail.get("status") in ("CANCELLED", "EXPIRED", "FAILED"):
                    return (filled_size, filled_value / filled_size) if filled_size > 0 else None
        except Exception:
            continue
    return None


async def _place_maker_order(session, order: dict, wait_seconds: int):
    """Place a real post-only limit order and wait for it to fill.

    post_only=true tells Coinbase to REJECT the order outright rather than
    let it cross the book - that is what guarantees the maker rate. A
    rejection here is therefore normal and expected (the price moved into
    us between reading the book and placing), not an error worth retrying
    blind; the caller falls back to a market order.

    Returns (filled_qty, avg_price) on a real fill, else None. Always
    cancels a real unfilled order before returning, so no resting order is
    ever left behind for a caller's fallback to double up on."""
    path = "/api/v3/brokerage/orders"
    product_id = order.get("product_id")
    if product_id:
        # Clear first. A verdict left over from a previous cycle read back as
        # this call's fact is the same error as a stale balance: it is not
        # this order's answer, so absent (UNKNOWN) is the correct state until
        # one of the returns below sets it.
        _last_order_rested.pop(product_id, None)
        # Same lifecycle for the venue's order id: absent is UNKNOWN, and a
        # previous cycle's id read as this order's would be a false join.
        _last_order_id.pop(product_id, None)
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path),
                                json=order, timeout=15) as r:
            resp = await r.json()
            if r.status not in (200, 201) or not resp.get("success"):
                reason = _describe_order_rejection(resp)
                log.info(f"[BTC-COMPOUND] {product_id}: maker order not accepted ({reason}) - caller will fall back")
                if product_id:
                    # Refused outright. Nothing rested; post_only rejection is
                    # the venue declining to create the order at all.
                    _last_order_rested[product_id] = False
                return None
            order_id = resp["success_response"]["order_id"]
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] {product_id}: maker order placement failed: {type(e).__name__}: {e}")
        # Deliberately NOT False. The POST may have reached Coinbase and
        # created an order before the connection broke - a gap is not a zero,
        # and claiming "nothing rested" here could be flatly untrue. Left
        # absent so it reads as UNKNOWN.
        return None

    # THE WAIT THAT FROZE THE FLEET, NOW VISIBLE WHILE IT HAPPENS.
    #
    # This await is in-line and the grid loop is sequential over branches, so
    # for as long as it runs no other branch is looked at. On 2026-10-08 it
    # ran 64 minutes on one XLM-USD buy (budget 3,600s, maker-only), the
    # order filled and paid - and for that hour /grid-status reported
    # `heartbeat.alive: false`, which reads as a dead fleet. It took fourteen
    # hand-polls to establish otherwise.
    #
    # The order behaviour below is UNCHANGED: same budget, same cancel, same
    # late-fill re-check. All that is added is a marker saying what is being
    # waited on, cleared in a finally so the exception path cannot leak it.
    # loop_wait never raises and reaches nothing; the import is guarded
    # anyway, because instrumentation must never be what breaks an order.
    try:
        import loop_wait
        _wait_token = loop_wait.mark(product_id, order.get("side", "").lower(),
                                     wait_seconds)
    except Exception:
        loop_wait, _wait_token = None, None
    try:
        fill = await _await_fill(session, order_id, wait_seconds)
    finally:
        if loop_wait is not None and _wait_token is not None:
            loop_wait.clear(_wait_token)
    if fill is None:
        # Cancel FIRST, then re-check: a fill can land in the same instant
        # the cancel does, and silently dropping it would leave the account
        # holding real coin this code thinks it never bought.
        await cancel_order(session, order_id)
        late = await _await_fill(session, order_id, 2)
        if late is not None:
            log.info(f"[BTC-COMPOUND] {product_id}: maker order filled as it was being cancelled - keeping the real fill")
            return late
        log.info(f"[BTC-COMPOUND] {product_id}: maker order did not fill in {wait_seconds}s - cancelled, falling back")
        if product_id:
            # THE ONLY CASE THE EXPIRY STUDY IS ABOUT. A real order sat on the
            # book for its whole window and no counterparty crossed it, so
            # "would it have filled had we waited longer?" is a question the
            # price history can actually answer.
            _last_order_rested[product_id] = True
        return None
    if product_id:
        _last_order_rested[product_id] = True
    return fill


async def place_maker_buy(session, usd_amount: float, product_id: str = PRODUCT_ID, wait_seconds: int = 45):
    """Real post-only limit BUY resting at the current best bid, so it
    earns the MAKER fee instead of the taker fee a market order always
    pays. Returns (filled_qty, avg_price) or None (caller falls back).

    Applies the same real-balance and minimum-size clamps place_market_buy
    already does - a maker order is still real money leaving the account."""
    # EVERY RETURN BELOW THIS LINE MEANS NO ORDER WAS CREATED, AND EACH ONE
    # NOW SAYS SO IN ITS OWN WORDS.
    #
    # The sell side carried the identical defect until the caller started
    # asking why, and the asymmetry was the tell: one path recorded its reason
    # and its mirror fell through silently. Leaving these silent stopped being
    # merely untidy once non-orders got their own ledger: the caller defaults
    # a missing reason to "the order rested at the bid and no seller crossed",
    # so a row in the NOT-PLACED table would have carried text asserting the
    # order rested. A ledger row that contradicts the table it sits in is
    # worse than no row.
    #
    # _last_order_error is cleared here too, not just _last_order_rested -
    # without that, a stale sentence from an earlier cycle is readable as this
    # call's reason.
    _last_order_rested.pop(product_id, None)
    _last_order_error.pop(product_id, None)
    _last_order_block.pop(product_id, None)
    _last_order_id.pop(product_id, None)
    _asked_usd = usd_amount
    real_usd, _ = await get_usd_balance(session)
    if real_usd is not None and real_usd < usd_amount:
        usd_amount = real_usd
    if usd_amount < MIN_TRADE_USD:
        _last_order_error[product_id] = (
            f"below the minimum trade size: asked for ${_asked_usd:.2f}, "
            f"${usd_amount:.2f} available to spend, against a "
            f"${MIN_TRADE_USD:.2f} floor")
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {"requested_qty": None,
                                         "available_units": real_usd}
        return None

    bid, ask = await get_best_bid_ask(session, product_id)
    if bid is None:
        _last_order_error[product_id] = "order book unreadable: no bid"
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {"available_units": real_usd}
        return None
    # Same rules, buy side. `available` here is the units the cash can
    # afford; the caller already decided how much cash to commit. The bid is
    # read above, so the venue's minimum order VALUE can be enforced too.
    rules = await get_product_rules(session, product_id)
    if rules is None:
        log.warning(
            f"[GRID] {product_id}: NO MAKER BUY PLACED - the product's size "
            f"rules could not be read. Refusing to size an order against a "
            f"guess; retried next cycle.")
        _last_order_error[product_id] = (
            "product rules unreadable: refusing to size an order against a guess")
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {"available_units": real_usd}
        return None
    _affordable = usd_amount / bid
    plan = _eq.plan_order_quantity(
        requested_quantity=_affordable, available_quantity=_affordable,
        price=bid, base_increment=rules["base_increment"],
        base_min_size=rules["base_min_size"],
        quote_min_size=rules["quote_min_size"])
    qty = float(plan.executable_quantity)
    if not plan.should_execute:
        log.warning("[GRID] NO MAKER BUY PLACED - " +
                    _eq.log_line(product_id, plan, maker_only=True))
        _last_order_error[product_id] = f"{plan.reason}: {plan.detail}"
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {
            "available_units": real_usd,
            "requested_qty": _affordable,
            "decision": plan.decision,
            "reason": plan.reason,
            "base_increment": str(plan.base_increment) if plan.base_increment is not None else None,
            "executable_quantity": str(plan.executable_quantity),
        }
        return None

    order = {
        "client_order_id": GRID_MAKER_COID_PREFIX + str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {"limit_limit_gtc": {
            # The planner's own string. Formatting separately is how a
            # correctly floored size becomes an invalid one - and `decimals`
            # no longer exists here at all.
            "base_size": plan.order_size_string,
            "limit_price": f"{bid:.10f}".rstrip("0").rstrip("."),
            "post_only": True,
        }},
    }
    _buy_result = await _place_maker_order(session, order, wait_seconds)
    if _buy_result:
        # Inventory on this product just grew, so a dust verdict recorded
        # against the old balance is stale. Drop it and let the next cycle
        # ask the venue properly instead of serving the stale answer.
        _dust.clear(product_id)
    return _buy_result


async def place_maker_sell(session, qty: float, product_id: str = PRODUCT_ID, wait_seconds: int = 45):
    """Real post-only limit SELL resting at the current best ask, earning
    the MAKER fee. Returns (filled_qty, avg_price) or None (caller falls
    back). Applies the same real-balance and precision clamps
    place_market_sell already does."""
    base_currency = product_id.split("-")[0]
    # Cleared at the top so no later reader can mistake a previous cycle's
    # verdict for this one's; every return below sets its own.
    _last_order_rested.pop(product_id, None)
    _last_order_block.pop(product_id, None)
    _last_order_id.pop(product_id, None)
    # The size the CALLER asked for, captured before any clamping. `qty` is
    # rebound below - clamped to the available balance, then floored to the
    # product's base_increment - so by the time the not-executable branch is
    # reached it is 0.0, and recording that as the requested size would say
    # nothing at all.
    _asked_qty = qty
    # DO NOT RE-ASK A QUESTION WHOSE ANSWER CANNOT HAVE CHANGED. The three
    # calls below - balance, product rules, order book - are all spent
    # BEFORE the dust verdict is even computed, and on a branch holding less
    # than one tradeable unit that verdict is already known. The cooldown is
    # armed only by a computed DUST decision, never by an unreadable read,
    # and a buy on this product clears it the moment inventory could grow.
    _dust_skip = _dust.skip_reason(product_id)
    if _dust_skip:
        log.debug(f"[GRID] {product_id}: no maker sell attempted - {_dust_skip}")
        _last_order_error[product_id] = f"dust cooldown: {_dust_skip}"
        _last_order_rested[product_id] = False
        # available_units deliberately absent: nothing was read this pass, so
        # there is no current figure. The last known one lives in the cooldown.
        _last_order_block[product_id] = {"requested_qty": _asked_qty,
                                         "decision": _dust.DUST,
                                         "reason": "DUST_COOLDOWN"}
        return None
    # NOTHING IS ARMED YET, AND THREE AWAITS ARE ABOUT TO HAPPEN. The check
    # above passed, the real verdict is computed at the bottom of this
    # function, and between here and there this coroutine awaits the balance,
    # the product rules and the order book. A second attempt on the same
    # product that starts inside that window passes the same check and spends
    # its own three Coinbase calls.
    #
    # Live, over 15.2 hours on a 900s cooldown, the gaps between REAL venue
    # attempts were: QNT median 310s shortest 8s, PEPE median 198s, TIA
    # median 55s. An 8-second gap is not an expiry.
    #
    # The hold is provisional and is overwritten by the real verdict moments
    # later. It asserts nothing about inventory - it only stops the SECOND
    # caller inside the gap from paying for the same answer.
    _dust.hold(product_id)
    real_balance, _bal_err = await get_asset_balance(session, base_currency)
    if real_balance is None:
        # Same rule, no exception. This is the opportunistic maker path and
        # there is always a next cycle, so there is never a reason to rest an
        # order sized off a number the wallet could not confirm.
        log.warning(
            f"[GRID] {product_id}: REFUSING to rest a maker sell - the real "
            f"{base_currency} balance could not be read ({_bal_err}). Not "
            f"placed; retried next cycle.")
        _last_order_rested[product_id] = False
        # available_units deliberately absent: the read FAILED, so there is
        # no figure to record. An absent key is UNKNOWN; a 0.0 here would be
        # the unreadable-balance-as-zero bug this path exists to prevent.
        _last_order_block[product_id] = {"requested_qty": _asked_qty}
        return None
    # THE VENUE'S OWN RULES, OR NOTHING. get_product_size_decimals returns
    # 8 on any failure, which is the most permissive value on the venue, so
    # an unreadable product used to become an order sized against a guess.
    # This refuses instead. A refusal costs one cycle; an order sized on a
    # guess is rejected by the venue at best.
    rules = await get_product_rules(session, product_id)
    if rules is None:
        log.warning(
            f"[GRID] {product_id}: NO MAKER SELL PLACED - the product's size "
            f"rules could not be read, so there is no way to know what size "
            f"this market accepts. Refusing rather than guessing; retried "
            f"next cycle.")
        _last_order_error[product_id] = (
            "product rules unreadable: refusing to size an order against a guess")
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {
            "available_units": real_balance,
            "requested_qty": _asked_qty,
        }
        return None

    # The book is read BEFORE sizing now, because the venue's minimum order
    # VALUE cannot be evaluated without a price - and the ask is the price
    # this sell would get.
    bid, ask = await get_best_bid_ask(session, product_id)
    if ask is None:
        # Not a fill failure - the book could not be read at all.
        log.warning(
            f"[GRID] {product_id}: NO MAKER SELL PLACED - the order book was "
            f"unreadable, so there is no ask to rest at. No order created.")
        _last_order_error[product_id] = "order book unreadable: no ask"
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {
            "available_units": real_balance,
            "requested_qty": _asked_qty,
        }
        return None

    plan = _eq.plan_order_quantity(
        requested_quantity=_asked_qty, available_quantity=real_balance,
        price=ask, base_increment=rules["base_increment"],
        base_min_size=rules["base_min_size"],
        quote_min_size=rules["quote_min_size"])

    # One call covers both directions: a DUST decision arms the cooldown,
    # and anything else (EXECUTE included) clears it.
    _dust.note_dust(product_id, plan.decision, available_units=real_balance,
                    reason=plan.reason)

    if not plan.should_execute:
        # DUST IS NOT A FAILED SALE. The old message here was "nothing
        # sellable ... floors to 0 at N decimals", which reads as a failure
        # and put branches at 0/3 over a holding the venue's rules simply
        # cannot express yet. The reason is now a code, the raw quantity is
        # preserved exactly, and the line carries every figure the decision
        # used.
        log.warning("[GRID] NO MAKER SELL PLACED - " +
                    _eq.log_line(product_id, plan, target_price=None,
                                 maker_only=True))
        _last_order_error[product_id] = f"{plan.reason}: {plan.detail}"
        _last_order_rested[product_id] = False
        _last_order_block[product_id] = {
            "available_units": real_balance,
            "requested_qty": _asked_qty,
            "decision": plan.decision,
            "reason": plan.reason,
            "base_increment": str(plan.base_increment) if plan.base_increment is not None else None,
            "executable_quantity": str(plan.executable_quantity),
        }
        return None

    qty = float(plan.executable_quantity)

    order = {
        "client_order_id": GRID_MAKER_COID_PREFIX + str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {"limit_limit_gtc": {
            # The planner's own string: formatting it separately is how a
            # correctly floored size becomes an invalid one.
            "base_size": plan.order_size_string,
            "limit_price": f"{ask:.10f}".rstrip("0").rstrip("."),
            "post_only": True,
        }},
    }
    return await _place_maker_order(session, order, wait_seconds)


_last_order_error = {}

# DID AN ORDER ACTUALLY REST? Keyed by product_id, set on every return path
# of the maker helpers below. True means Coinbase minted an order id and the
# order sat on the book for its whole window before being cancelled. False
# means no order was ever created. ABSENT means UNKNOWN - and absent is a
# real third answer here, not a synonym for False.
#
# This exists because GridMakerExpiry rows were being written on all three
# of place_maker_sell's None paths, two of which never place an order. The
# expiry table is the evidence base for "should a resting rung be given
# longer?", so a row where nothing rested is not a weak data point in that
# study - it is not a data point at all, and at ~2,600 such rows a day from
# ALGO and QNT alone it was on course to be the entire 5,000-row window.
#
# A separate structural flag rather than matching on _last_order_error's
# text, because a guard that reads a human-readable string is one wording
# change away from silently reclassifying every row.
_last_order_rested = {}

# WHY NO ORDER WAS CREATED, AS FIELDS RATHER THAN AS A SENTENCE.
#
# _last_order_error already carries the human-readable reason, and that stays.
# This carries the same fact structurally, because the rejection ledger has to
# be queryable: "how much was actually available, and what did it floor to?"
# is the question that separates a branch whose coin is locked from one
# holding dust, and answering it by parsing a formatted string is the exact
# pattern that has misfired repeatedly in this repo.
#
# Keys are product_id. Values are dicts with whichever of available_units,
# size_decimals and requested_qty the blocking path actually knew. Missing
# keys are UNKNOWN, never zero.
_last_order_block = {}

# THE VENUE'S OWN ORDER ID FOR THE LAST ORDER ON THIS PRODUCT.
#
# §24 step 7 is "reconcile order states", and it had nothing to reconcile
# against: an open order at the exchange could not be matched to the slice
# that placed it, because no slice recorded an id. Coinbase's fills feed
# carries order_id and NOT client_order_id - checked against a real fill,
# and the reason fills_attribution keys on it - so this is the join that
# actually works.
#
# Recorded here rather than by changing what _place_and_confirm RETURNS,
# because that return shape is consumed by four sizers and every one of
# their callers, and widening it to carry an id would put a live order
# path's signature change in the way of a diagnostic. Same shape as the
# three dicts above, which already carry per-product facts back out.
#
# IT IS LAST-WRITE-WINS, and that is a real limit, not a hidden one: if two
# orders for one product were ever in flight at once, the second would
# overwrite the first. Today one product belongs to one branch and branches
# are walked in sequence with a sleep between them, so there is no second
# writer - but an event loop beside the polling loop WOULD be one, and this
# must be revisited before §7/§8 lands.
#
# A missing key is UNKNOWN, never "no order".
_last_order_id = {}


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


async def _record_order_source(order_id: str, source: str, product_id: str, side: str):
    """Remember which subsystem asked for this order.

    Coinbase fills carry order_id and NOT client_order_id - checked
    against a real fill on 2026-09-28 before this was built, because
    tagging client_order_id was the obvious plan and would have produced
    a tag that never comes back. order_id is the only join key the fills
    feed actually offers.

    NEVER fatal and never blocking a trade: an attribution row is a
    convenience for a later audit, and losing one must not cost a fill.
    """
    if not order_id or not source:
        return
    try:
        from models import OrderAttribution
        async with get_session_factory()() as db:
            db.add(OrderAttribution(order_id=str(order_id), source=str(source)[:64],
                                    product_id=product_id, side=side))
            await db.commit()
    except Exception as e:
        log.debug(f"[BTC-COMPOUND] order attribution not recorded (non-fatal): "
                  f"{type(e).__name__}: {e}")


async def _place_and_confirm(session, path: str, order: dict, source: str = None):
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
            # THE BALANCE JUST CHANGED. Coinbase has minted an order, so the
            # coin or cash behind it is reserved from this instant - before
            # any fill. Serving the pre-order balance to the next caller is
            # how an order gets sized against money that is already spoken
            # for, so the cache is dropped here rather than waiting out its
            # TTL. Cheaper to re-read than to reason about which currencies
            # this order touched.
            invalidate_balance_cache(f"order {order_id} created")
            if product_id:
                _last_order_error.pop(product_id, None)
                # Recorded at the same instant as _record_order_source below
                # and for the same reason: this is the only moment the id is
                # certainly known, and a slow or failed fill poll must not be
                # able to lose it.
                _last_order_id[product_id] = order_id
            # Recorded the moment Coinbase mints the id, before the fill
            # poll below, so a slow or failed poll cannot lose the only
            # link between this order and whoever asked for it.
            await _record_order_source(order_id, source, product_id, order.get("side"))
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

    # An accepted market order can execute even when the order-detail endpoint
    # is briefly stale or unavailable. Reconcile by order ID before reporting
    # failure so callers never submit a duplicate order or leave a real fill
    # open in local state.
    fills_path = f"/api/v3/brokerage/orders/historical/fills?order_id={order_id}"
    try:
        async with session.get(
            COINBASE_BASE_URL + fills_path,
            headers=_auth_headers("GET", fills_path),
            timeout=15,
        ) as r:
            if r.status == 200:
                fills = (await r.json()).get("fills", [])
                matching_fills = [
                    fill for fill in fills
                    if str(fill.get("order_id", order_id)) == str(order_id)
                ]
                filled_size = sum(float(fill.get("size", 0) or 0) for fill in matching_fills)
                filled_value = sum(
                    float(fill.get("size", 0) or 0) * float(fill.get("price", 0) or 0)
                    for fill in matching_fills
                )
                if filled_size > 0:
                    if product_id:
                        _last_order_error.pop(product_id, None)
                    log.info(
                        "[BTC-COMPOUND] Reconciled accepted order %s from fill history",
                        order_id,
                    )
                    return filled_size, filled_value / filled_size
    except Exception as e:
        log.warning(
            "[BTC-COMPOUND] Fill-history reconciliation failed for order %s: %s",
            order_id,
            e,
        )

    reason = f"accepted order {order_id} not confirmed by order detail or fill history"
    if product_id:
        _last_order_error[product_id] = reason
    log.warning(f"[BTC-COMPOUND] {reason}")
    return None


async def fetch_fills_between(session, start_iso: str, end_iso: str,
                              max_pages: int = 40, page_size: int = 250) -> dict:
    """Every fill Coinbase recorded in a window. GROUND TRUTH, read-only.

    The bots' own ledgers cannot answer where money went. On 2026-09-26 an
    audit found 11 of 167 coin-history rows unable to reproduce their own
    P&L from their own columns, a real +$178.44 round trip missing from the
    grid ledger entirely, and a 16-day hole in the equity record covering
    most of a $473 decline. Every one of those is a record the account
    wrote about itself.

    This asks the exchange instead. Coinbase returns, per fill: side, size,
    price, the real `commission` charged, and `liquidity_indicator`
    (MAKER/TAKER). None of it is inferred and none of it is ours.

    Paginated with a hard page cap, because an unbounded cursor loop
    against a rate-limited endpoint is its own outage. Returns whatever it
    got plus `truncated` when the cap was hit - a partial statement that
    says it is partial beats a complete-looking one that is not.

    Never raises and never trades.
    """
    fills, cursor, pages, truncated = [], None, 0, False
    base = ("/api/v3/brokerage/orders/historical/fills"
            f"?limit={int(page_size)}"
            f"&start_sequence_timestamp={start_iso}"
            f"&end_sequence_timestamp={end_iso}")
    try:
        while pages < max_pages:
            path = base + (f"&cursor={cursor}" if cursor else "")
            async with session.get(COINBASE_BASE_URL + path,
                                   headers=_auth_headers("GET", path),
                                   timeout=30) as r:
                if r.status != 200:
                    return {"available": False,
                            "error": f"Coinbase returned {r.status}",
                            "detail": (await r.text())[:300],
                            "fills": fills, "pages_read": pages}
                body = await r.json()
            batch = body.get("fills") or []
            fills.extend(batch)
            pages += 1
            cursor = body.get("cursor") or None
            if not cursor or not batch:
                break
        else:
            truncated = True
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}",
                "fills": fills, "pages_read": pages}
    return {"available": True, "fills": fills, "pages_read": pages,
            "truncated": truncated,
            "window": {"start": start_iso, "end": end_iso}}


def summarise_fills(fills: list) -> dict:
    """Turn raw fills into a statement: what was bought, sold and paid.

    NET CASH FLOW is the number the whole exercise is for. Sells bring USD
    in, buys take it out, commission always goes out. Summed over a window
    it says what the account's USD balance did because of trading - which
    can then be set against what the balance ACTUALLY did, and any gap is
    something trading did not cause.
    """
    per, buy_usd, sell_usd, fees = {}, 0.0, 0.0, 0.0
    maker = taker = 0
    quote_sized = 0
    all_orders = set()
    # Split by side as well as counted. A ledger row is a CLOSE, so the
    # honest thing to compare 249 recorded round trips against is the number
    # of distinct SELL orders - not total orders halved, which assumes every
    # buy found a matching sell inside the window and that nothing was
    # scaled into or out of in pieces.
    buy_orders, sell_orders = set(), set()
    for f in fills:
        try:
            size = float(f.get("size") or 0)
            price = float(f.get("price") or 0)
            comm = float(f.get("commission") or 0)
        except (TypeError, ValueError):
            continue
        pid = f.get("product_id") or "?"
        side = (f.get("side") or "").upper()
        # SIZE IS NOT ALWAYS THE BASE QUANTITY.
        #
        # Coinbase sets size_in_quote when `size` is denominated in the QUOTE
        # currency - USD - which is what a market order placed by dollar
        # amount returns. Multiplying that by price counts the dollars once
        # and then again at the coin's own price.
        #
        # The first live call did exactly that and reported $96,544,199.94 of
        # BTC bought on a $1,000 account, from 1,175 "BTC" that were really
        # 1,175 dollars. A number that absurd is easy to catch; the same bug
        # on a $40 fill would have quietly passed for a statement.
        in_quote = bool(f.get("size_in_quote"))
        if in_quote:
            quote_sized += 1
            value = size
            qty = (size / price) if price else 0.0
        else:
            value = size * price
            qty = size
        liq = (f.get("liquidity_indicator") or "").upper()
        if liq == "MAKER":
            maker += 1
        elif liq == "TAKER":
            taker += 1
        p = per.setdefault(pid, {"product_id": pid, "fills": 0, "bought_usd": 0.0,
                                 "sold_usd": 0.0, "bought_qty": 0.0, "sold_qty": 0.0,
                                 "commission_usd": 0.0, "orders": set(),
                                 "buy_orders": set(), "sell_orders": set()})
        p["fills"] += 1
        # DISTINCT ORDERS, not fills. One order can fill in many pieces, so
        # "fills / 2 = round trips" overstates activity by however much
        # partial filling is happening - and that arithmetic was used to
        # claim three quarters of executions had gone unrecorded. An order
        # is the thing a bot places and the thing a ledger row represents.
        oid = f.get("order_id")
        if oid:
            p["orders"].add(oid)
            all_orders.add(oid)
            if side == "BUY":
                p["buy_orders"].add(oid); buy_orders.add(oid)
            elif side == "SELL":
                p["sell_orders"].add(oid); sell_orders.add(oid)
        p["commission_usd"] += comm
        fees += comm
        if side == "BUY":
            p["bought_usd"] += value; p["bought_qty"] += qty; buy_usd += value
        elif side == "SELL":
            p["sold_usd"] += value; p["sold_qty"] += qty; sell_usd += value
    rows = []
    for p in per.values():
        p["net_usd"] = round(p["sold_usd"] - p["bought_usd"] - p["commission_usd"], 2)
        p["qty_left_over"] = round(p["bought_qty"] - p["sold_qty"], 10)
        for k in ("bought_usd", "sold_usd", "commission_usd"):
            p[k] = round(p[k], 2)
        # The sets are for counting, not for serialising.
        for k in ("orders", "buy_orders", "sell_orders"):
            p[k] = len(p.pop(k, ()) or ())
        rows.append(p)
    rows.sort(key=lambda r: r["net_usd"])
    return {
        "fills": len(fills),
        # The honest denominator for "how much trading happened". A single
        # order can fill in many pieces; counting fills and halving them
        # invents activity that never occurred.
        "orders": len(all_orders),
        "buy_orders": len(buy_orders),
        "sell_orders": len(sell_orders),
        "products": rows,
        "bought_usd": round(buy_usd, 2),
        "sold_usd": round(sell_usd, 2),
        "commission_usd": round(fees, 2),
        # Sells in, buys out, commission out. What trading did to the USD
        # balance over the window, before any coin still held is marked.
        "net_cash_flow_usd": round(sell_usd - buy_usd - fees, 2),
        "maker_fills": maker,
        "taker_fills": taker,
        # Surfaced so the size_in_quote handling above is verifiable from the
        # response rather than taken on trust.
        "quote_sized_fills": quote_sized,
        "note": ("net_cash_flow_usd is CASH, not profit. Coin bought and still "
                 "held reads as cash out with nothing back; compare against the "
                 "value of what is still held before calling it a loss."),
    }


async def get_recent_fills_summary(session, limit: int = 250,
                                   want_classified: int = 40,
                                   max_pages: int = 12) -> dict:
    """What Coinbase itself says every recent fill actually cost.

    Exists to settle one question with ground truth instead of inference:
    is this account's grid getting MAKER fills or TAKER fills?

    It matters more than any other single number here. The spacing floor
    prices the taker round trip (1.50%), because a post-only order that
    does not fill inside its wait becomes a market order - so the floor is
    1.70% and no step below that can profit. At maker (0.70% round trip)
    the floor is 0.90%, and a 1.25% step nets +0.55% instead of -0.25%.
    That single fact is the difference between a fleet that trades a few
    times a week and one that trades several times a day.

    Everything else available locally is inference: P&L recorded against
    an assumed leg rate, or a fill-mix counter that only began counting
    today. Coinbase returns `liquidity_indicator` (MAKER/TAKER) and the
    real `commission` charged on every fill. That is the actual answer.

    Read-only. Returns {} on any failure rather than raising - a
    diagnostic must never be able to disturb live trading.
    """
    # PAGE UNTIL THERE ARE ENOUGH SPOT FILLS, not until 250 fills of any kind.
    #
    # This asked for one page and hoped. On this account that was the wrong
    # bet: 237 of 250 fills came back Kalshi event contracts, which the loop
    # below correctly discards - leaving 13 spot fills and a truthful but
    # useless "NOT ENOUGH EVIDENCE". The filter was never the problem. The
    # SAMPLE was: 95% of it was thrown away and nothing went back for more.
    #
    # Kalshi is not evenly spread either - 712 of 925 of its fills landed on
    # a single day, 2026-09-06 - so how much of a page survives depends
    # entirely on where in the history the page happens to fall. A fixed
    # page size cannot be sized around that; paging to a target can.
    #
    # Capped at max_pages so an account that is ALL non-spot terminates
    # instead of walking its entire history. pages_read and
    # non_spot_fills_skipped are both returned, so a starved sample says so
    # rather than looking like a quiet account.
    fills, cursor, pages = [], None, 0
    try:
        while pages < max_pages:
            path = ("/api/v3/brokerage/orders/historical/fills"
                    f"?limit={int(limit)}" + (f"&cursor={cursor}" if cursor else ""))
            async with session.get(
                COINBASE_BASE_URL + path,
                headers=_auth_headers("GET", path),
                timeout=20,
            ) as r:
                if r.status != 200:
                    if fills:
                        break      # keep what we have; a partial sample beats none
                    return {"error": f"HTTP {r.status}", "detail": (await r.text())[:300]}
                body = await r.json()
            batch = body.get("fills") or []
            fills.extend(batch)
            pages += 1
            cursor = body.get("cursor") or None
            # Count only what this function can actually use: spot fills that
            # Coinbase labelled MAKER or TAKER.
            usable = sum(
                1 for f in fills
                if (f.get("product_id") or "").endswith("-USD")
                and "KALSHI" not in (f.get("product_id") or "").upper()
                and (f.get("liquidity_indicator") or "").upper() in ("MAKER", "TAKER"))
            if usable >= want_classified or not cursor or not batch:
                break
    except Exception as e:
        if not fills:
            return {"error": f"{type(e).__name__}: {e}"}

    by_side = {}
    per_product = {}
    maker = taker = unknown = 0
    commission_total = 0.0
    maker_commission = 0.0
    maker_notional = 0.0
    taker_commission = 0.0
    taker_notional = 0.0
    notional_total = 0.0
    oldest = newest = None
    # Per-liquidity newest, so "is the fallback firing NOW?" can be
    # ANSWERED rather than handed to a human to work out. The maker_only
    # invariant read a bare taker COUNT over a 250-fill window and could
    # only shrug: a window that reaches back past the day maker-only was
    # armed cannot tell an old taker fill from a live one.
    newest_taker = newest_maker = None
    skipped_non_spot = 0
    # Counted here as well as in summarise_fills. A str.replace that matched
    # the return dict of BOTH functions added this key to this one without
    # the variable behind it - a NameError that took /fee-reality to a 500
    # and was invisible to every test, because the tests read the source and
    # the source looked right.
    quote_sized = 0

    for f in fills:
        pid_raw = f.get("product_id") or "?"
        # This endpoint returns EVERY fill on the account, and this account
        # also trades Kalshi event contracts (KXBTC15M-...-KALSHI). They are
        # a different product class with a different fee schedule - observed
        # here between 2.3% and 10.3% per leg - and they carry no
        # liquidity_indicator at all. Averaged in, they made the "real" fee
        # rate meaningless. Only spot crypto pairs answer the question this
        # function exists to answer.
        if not pid_raw.endswith("-USD") or "KALSHI" in pid_raw.upper():
            skipped_non_spot += 1
            continue

        liq = (f.get("liquidity_indicator") or "").upper()
        size = float(f.get("size") or 0)
        price = float(f.get("price") or 0)
        comm = float(f.get("commission") or 0)
        # size_in_quote means `size` is ALREADY the USD amount. Multiplying
        # it by price again reported 3 BTC-USD fills as $48,816,593 of
        # notional on an account holding $572, which dragged the computed
        # fee rate to 0.0002% and produced a confident "the floor can come
        # down" verdict from arithmetic that was pure nonsense.
        if f.get("size_in_quote"):
            quote_sized += 1
            notional = size
        else:
            notional = size * price
        ts = f.get("trade_time") or f.get("sequence_timestamp")
        if ts:
            oldest = ts if oldest is None or ts < oldest else oldest
            newest = ts if newest is None or ts > newest else newest

        if liq == "MAKER":
            maker += 1
            if ts:
                newest_maker = ts if newest_maker is None or ts > newest_maker else newest_maker
        elif liq == "TAKER":
            taker += 1
            if ts:
                newest_taker = ts if newest_taker is None or ts > newest_taker else newest_taker
        else:
            unknown += 1

        # Only a fill Coinbase actually labelled MAKER or TAKER can speak
        # to what a round trip really costs.
        if liq in ("MAKER", "TAKER"):
            commission_total += comm
            notional_total += notional
        # AND KEPT APART, because the blend answers a different question.
        # Under maker-ONLY there is no market fallback, so what a NEW round
        # trip will cost is the MAKER rate - and every coin that filled 100%
        # maker bills exactly 0.0035/leg, while the blend reads 0.006137
        # purely because pure-taker fills (XRP and XYO at 0.0075, ARB at
        # 0.0062) are mixed into it. Comparing a maker-only floor against
        # that blend flags a disagreement that is really a category error.
        if liq == "MAKER":
            maker_commission += comm
            maker_notional += notional
        elif liq == "TAKER":
            taker_commission += comm
            taker_notional += notional

        pid = pid_raw
        p = per_product.setdefault(pid, {"maker": 0, "taker": 0, "unknown": 0,
                                         "commission": 0.0, "notional": 0.0})
        p["maker" if liq == "MAKER" else ("taker" if liq == "TAKER" else "unknown")] += 1
        if liq in ("MAKER", "TAKER"):
            p["commission"] += comm
            p["notional"] += notional

        side = (f.get("side") or "?").upper()
        b = by_side.setdefault(side, {"maker": 0, "taker": 0, "unknown": 0})
        b["maker" if liq == "MAKER" else ("taker" if liq == "TAKER" else "unknown")] += 1

    classified = maker + taker
    # The rate an average LEG really paid, straight from the commission
    # Coinbase charged - not from any rate this codebase assumed.
    real_leg_rate = (commission_total / notional_total) if notional_total > 0 else None
    # None, never 0.0, when nothing of that kind filled: "no maker fills in
    # this window" is not "maker legs are free", and a caller that floors a
    # spacing on it must be able to tell the two apart.
    maker_leg_rate = (maker_commission / maker_notional) if maker_notional > 0 else None
    taker_leg_rate = (taker_commission / taker_notional) if taker_notional > 0 else None

    for pid, p in per_product.items():
        p["maker_rate"] = round(p["maker"] / (p["maker"] + p["taker"]), 4) if (p["maker"] + p["taker"]) else None
        p["real_leg_fee_rate"] = round(p["commission"] / p["notional"], 6) if p["notional"] > 0 else None
        p["commission"] = round(p["commission"], 4)
        p["notional"] = round(p["notional"], 2)

    return {
        "pages_read": pages,
        "fills_returned": len(fills),
        "fills_examined": len(fills) - skipped_non_spot,
        "non_spot_fills_skipped": skipped_non_spot,
        "maker_fills": maker,
        "taker_fills": taker,
        # Surfaced so the size_in_quote handling above is verifiable from the
        # response rather than taken on trust.
        "quote_sized_fills": quote_sized,
        "unclassified_fills": unknown,
        "maker_rate": round(maker / classified, 4) if classified else None,
        "real_leg_fee_rate": round(real_leg_rate, 6) if real_leg_rate is not None else None,
        "maker_leg_fee_rate": round(maker_leg_rate, 6) if maker_leg_rate is not None else None,
        "taker_leg_fee_rate": round(taker_leg_rate, 6) if taker_leg_rate is not None else None,
        "real_round_trip_fee_rate": round(real_leg_rate * 2, 6) if real_leg_rate is not None else None,
        "total_commission_usd": round(commission_total, 4),
        "total_notional_usd": round(notional_total, 2),
        "by_side": by_side,
        "per_product": per_product,
        "oldest_fill": oldest,
        "newest_fill": newest,
        # None means "no fill of that kind in this window" - never a
        # substitute for "none ever". The window is capped.
        "newest_taker_fill": newest_taker,
        "newest_maker_fill": newest_maker,
        "classified_fills": classified,
        "enough_to_conclude": classified >= 20,
        # So a starved sample reads as starved rather than as a quiet account.
        "sample_note": (f"{skipped_non_spot} of {len(fills)} fills across {pages} page(s) "
                        f"were non-spot (Kalshi event contracts) and were discarded - "
                        f"they carry no liquidity_indicator and a different fee schedule. "
                        f"{classified} spot fills carried a MAKER/TAKER label."),
        "note": ("liquidity_indicator and commission come from Coinbase, not from this "
                 "codebase's assumptions. real_round_trip_fee_rate is what an average round "
                 "trip ACTUALLY cost across these fills, and it is the number the spacing "
                 "floor should be priced against."),
    }


async def load_equity_floor():
    """Reload the ratcheted equity floor from the DB at startup, so a
    Railway restart can't reset the ladder back down to the base level."""
    global equity_floor
    try:
        from models import TradingBotState
        async with get_session_factory()() as db:
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
        async with get_session_factory()() as db:
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
    async with get_session_factory()() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        return result.scalar_one_or_none()


async def save_position(entry_price: float, qty: float, target_price: float, stop_price: float):
    async with get_session_factory()() as session:
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
    async with get_session_factory()() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        pos = result.scalar_one_or_none()
        if pos and pos.stop_price is not None and pos.stop_price < entry_price:
            pos.stop_price = entry_price
            await session.commit()


async def clear_position():
    async with get_session_factory()() as session:
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

    # Skim only on a genuine win, and only from the realized net figure -
    # never from gross, which would lock money the fees already consumed.
    skim = round(net_pnl * PROFIT_SKIM_PCT, 2) if net_pnl > 0 else 0.0
    if skim > 0:
        await add_locked_usd(skim)

    log.info(
        f"[BTC-COMPOUND] SOLD {filled_qty:.8f} BTC @ ${filled_price:,.2f} ({reason}) | "
        f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
        f"P&L: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f} after est. fees"
        + (f" | locked ${skim:.2f} ({PROFIT_SKIM_PCT*100:.0f}%) out of the "
           f"compounding loop" if skim > 0 else "")
    )
    return True


# Waiting for capital is a normal operating state, not an incident - the
# loop never stops over it, it simply has nothing to deploy yet. Logging
# that every cycle would print 2,880 identical lines a day at a 30s cycle
# and bury the messages that do matter, so the state is announced on entry,
# repeated sparingly while it lasts, and announced again the moment capital
# returns. Nothing about the retry behaviour changes; only how loudly.
WAITING_LOG_INTERVAL_SECONDS = _safe_float_env(
    "BTC_COMPOUND_WAITING_LOG_INTERVAL_SECONDS", "900")
_waiting_for_capital_since = None
_waiting_last_logged_at = 0.0


def _note_waiting_for_capital(balance):
    """Entering or continuing the wait. Keeps cycling either way."""
    global _waiting_for_capital_since, _waiting_last_logged_at
    now = time.time()
    if _waiting_for_capital_since is None:
        _waiting_for_capital_since = now
        _waiting_last_logged_at = now
        log.info(
            f"[BTC-COMPOUND] Balance ${balance:.2f} is below the ${MIN_TRADE_USD:.2f} "
            f"minimum trade size - waiting for capital. Still checking every "
            f"{CYCLE_SECONDS}s; this will not stop the bot or need a restart."
        )
    elif now - _waiting_last_logged_at >= WAITING_LOG_INTERVAL_SECONDS:
        _waiting_last_logged_at = now
        waited = now - _waiting_for_capital_since
        log.info(
            f"[BTC-COMPOUND] Still waiting for capital after "
            f"{waited/60:.0f} min - balance ${balance:.2f}, need "
            f"${MIN_TRADE_USD:.2f}. Checking every {CYCLE_SECONDS}s."
        )


def _note_capital_available(balance):
    """Capital came back. Announced once, so the wait has a visible end."""
    global _waiting_for_capital_since, _waiting_last_logged_at
    if _waiting_for_capital_since is None:
        return
    waited = time.time() - _waiting_for_capital_since
    log.info(
        f"[BTC-COMPOUND] Capital available again: ${balance:.2f} after waiting "
        f"{waited/60:.0f} min - resuming entries."
    )
    _waiting_for_capital_since = None
    _waiting_last_logged_at = 0.0


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
            equity = tracked_equity(
                balance,
                position.qty * price if position is not None and price is not None else None,
            )

        if equity is not None and equity >= EQUITY_FLOOR_TIER:
            candidate_floor = compute_equity_floor(equity)
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
                _note_waiting_for_capital(balance)
                return
            _note_capital_available(balance)
            if price is None:
                log.warning("[BTC-COMPOUND] Could not fetch BTC price/volatility - skipping this cycle")
                return

            # Everything from here sizes off `deploy`, not `balance`. Anything
            # above the cap stays as cash and is deliberately left alone.
            # Locked profit is real USD sitting in the same account, so it
            # has to be subtracted before sizing or the skim would be
            # re-risked on the very next entry and lock nothing at all.
            locked = await get_locked_usd()
            deploy = deployable_usd(max(0.0, balance - locked))
            if locked > 0:
                log.info(f"[BTC-COMPOUND] ${locked:,.2f} of banked profit is locked and "
                         f"excluded from this entry")
            if deploy < MIN_TRADE_USD:
                log.warning(
                    f"[BTC-COMPOUND] Only ${deploy:,.2f} is deployable (${balance:,.2f} "
                    f"balance less ${RESERVE_USD:,.2f} reserve"
                    + (f" and ${locked:,.2f} locked profit" if locked > 0 else "")
                    + f"), below the ${MIN_TRADE_USD:.2f} minimum trade size - no entry "
                    f"can be placed. Lower BTC_COMPOUND_RESERVE_USD or add funds."
                )
                return
            if deploy < balance:
                log.info(f"[BTC-COMPOUND] Deploying ${deploy:,.2f} of ${balance:,.2f} available; "
                         f"${RESERVE_USD:,.2f} reserve"
                         + (f" + ${locked:,.2f} locked profit" if locked > 0 else "")
                         + " held back")

            target_pct = max(pick_target_pct(atr_pct), min_profit_target_pct(deploy, atr_pct))

            # Refuse an entry whose own arithmetic needs a win rate this
            # account has never produced. Checked against the real target
            # actually about to be used - not the tier constant - because
            # min_profit_target_pct() can raise it, and a gate that judges
            # a number the order will not use is decoration.
            #
            # Deliberately placed BEFORE place_market_buy: once filled,
            # the money is committed and the only ways out are the target,
            # the stop or a manual sale. Refusing costs one idle cycle and
            # the bot re-checks on the next one, when volatility - and so
            # the tier, and so the arithmetic - may well have changed.
            needed = breakeven_win_rate(target_pct)
            if needed > MAX_BREAKEVEN_WIN_RATE:
                log.warning(
                    f"[BTC-COMPOUND] NO ENTRY: +{target_pct*100:.2f}% target against a "
                    f"-{STOP_LOSS_PCT*100:.2f}% stop needs a {needed*100:.1f}% win rate to break "
                    f"even after the {ROUND_TRIP_FEE_RATE*100:.2f}% round trip, over the "
                    f"{MAX_BREAKEVEN_WIN_RATE*100:.1f}% limit. ATR {atr_pct*100:.2f}%. Holding cash - "
                    f"this trade loses money on average. Maker orders would halve the fee and "
                    f"may clear it; otherwise wait for volatility to widen the target."
                )
                return

            fill = await place_market_buy(session, deploy)
            if not fill:
                log.warning("[BTC-COMPOUND] Buy did not fill - will retry next cycle")
                return
            filled_qty, filled_price = fill
            target_price = filled_price * (1 + target_pct)
            stop_price = filled_price * (1 - STOP_LOSS_PCT)
            await save_position(filled_price, filled_qty, target_price, stop_price)
            log.info(
                f"[BTC-COMPOUND] BOUGHT {filled_qty:.8f} BTC @ ${filled_price:,.2f} (${deploy:.2f} deployed"
                f"{f' of ${balance:.2f}' if deploy < balance else ''}) | "
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
