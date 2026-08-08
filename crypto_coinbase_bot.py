"""
CRYPTO TRADING BOT — Coinbase Advanced Trade, runs 24/7 (no market-hours gate)
================================================================================
Replaces crypto_alpaca_bot.py. Alpaca crypto trading turned out to be
blocked for this account: Alpaca only allows crypto orders for accounts
whose state/jurisdiction is on their approved list (confirmed directly
with Alpaca support - "crypto orders not allowed for account" was never
a bug, it's a regulatory gate with no code-side workaround), and this
account's state isn't on it. Coinbase has no such state restriction for
US accounts, so this bot places real orders there instead.

Trades BTC-USD and ETH-USD on a SEPARATE Coinbase account/balance (not
the Alpaca account prop_bot.py and daily_brief.py use for stocks) -
funding this bot means depositing USD into Coinbase directly.

Long-only, same reasoning as crypto_alpaca_bot.py had: Coinbase spot
trading has no shorting, so there's no short side to manage here either.

Compounds by default: every cycle sizes new positions off the account's
actual current USD balance (principal + whatever profit has accumulated),
not a fixed slice - a winning trade grows the pool available to the next
one automatically. There's no sibling bot sharing this Coinbase account
(unlike the old Alpaca version, which capped itself so it wouldn't compete
with prop_bot.py's stock entries for the same cash), so there's nothing
left to protect by holding part of the balance back. CRYPTO_MAX_ALLOCATION
remains available as an optional hard ceiling if a cap is ever wanted
again, but compounds fully by default when unset. The bot never
transfers/withdraws money on its own under any circumstance - funds only
ever leave this account when withdrawn manually.

Auth uses Coinbase Developer Platform (CDP) API keys - the current
Advanced Trade API auth method, replacing the older HMAC/passphrase
scheme Coinbase retired. Each request is signed as a short-lived JWT
(Coinbase's documented format: sub/iss/nbf/exp/uri claims, kid+nonce
headers), not a static signature. CDP issues two different key types
for this, and the portal doesn't ask which one you want - it just gives
you whichever it defaults to:
  - ECDSA: a PEM block ("-----BEGIN EC PRIVATE KEY-----...") -> ES256
  - Ed25519: a bare base64 string -> EdDSA
Both are handled here (see _load_signing_key) since which one gets
issued isn't something this code controls.
"""
import base64
import os
import asyncio
import json
import logging
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
import aiohttp
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from database import AsyncSessionLocal
from models import BotPosition, TradingBotState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_coinbase_bot")

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"

CRYPTO_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "AVAX/USD", "DOGE/USD", "SHIB/USD", "LINK/USD"]


def _to_product_id(symbol: str) -> str:
    """BTC/USD -> BTC-USD (Coinbase's product ID format)."""
    return symbol.replace("/", "-")


def _load_signing_key():
    """Returns (key_object, jwt_algorithm). A PEM block is an ECDSA key
    (ES256); anything else is treated as an Ed25519 key (EdDSA) - CDP's
    Ed25519 secret is a base64 string decoding to 64 bytes (a 32-byte
    seed followed by the 32-byte public key), of which only the seed is
    the actual private key."""
    raw = COINBASE_API_PRIVATE_KEY.strip()
    try:
        if raw.startswith("-----BEGIN"):
            return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
        decoded = base64.b64decode(raw)
        if len(decoded) != 64:
            log.error(f"Coinbase private key decoded to {len(decoded)} bytes, expected 64 - key may be corrupted")
        return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"
    except Exception as e:
        log.error(f"Failed to load Coinbase signing key: {type(e).__name__}: {e} - key={raw[:40]}...")
        raise


def _build_jwt(method: str, path: str) -> str:
    """Coinbase CDP-style JWT, valid ~2 minutes, scoped to one method+path -
    a fresh one is required per request, unlike a static API signature."""
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


# Optimized for 24/7 volume: slightly loosened thresholds (32/68 vs 30/70)
# to catch more entry signals without sacrificing quality. Still conservative
# enough to avoid false breakout noise, but flexible enough for volatile crypto.
# 30/70 catches only extreme oversold/overbought; 32/68 catches 2% wider moves.
RSI_BUY_BELOW  = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "32"))      # Entry threshold for LONG positions
RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "68"))    # Exit threshold for LONG / Entry for SHORT
RSI_SHORT_ABOVE = float(os.getenv("CRYPTO_RSI_SHORT_ABOVE", "68"))  # Entry threshold for SHORT positions
RSI_SHORT_BELOW = float(os.getenv("CRYPTO_RSI_SHORT_BELOW", "32"))  # Exit threshold for SHORT positions

MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "8"))  # Allow up to 8 concurrent positions (4 long + 4 short, or 8 long, or 8 short)
# Unset by default - no ceiling, so the full account balance (principal +
# compounded profit) is always in play. Set CRYPTO_MAX_ALLOCATION to cap
# it at a fixed dollar amount instead, if ever wanted.
_max_allocation_env = os.getenv("CRYPTO_MAX_ALLOCATION", "")
MAX_ALLOCATION = float(_max_allocation_env) if _max_allocation_env else None
# Micro-trades on small balances: $0.50 minimum lets bot scalp $0.58-100 accounts
# without sitting idle. At $0.58 balance: $0.50 × 0.5% move = $0.0025 profit (micro-compounding)
# $100+ balance: trades full notional, beats fees, compounds faster.
MIN_POSITION_NOTIONAL = float(os.getenv("CRYPTO_MIN_POSITION_NOTIONAL", "0.50"))

# Minimum trade size guard: skip trading if cash pool is too small to be meaningful
MIN_CRYPTO_TRADE_USD = float(os.getenv("MIN_CRYPTO_TRADE_USD", "5.00"))

# Staged capital release, requested after watching the account get drawn
# down to single-digit cents trading with 100% of the balance every cycle:
# instead of always trading the full balance, only ever risk it in fixed
# $100 steps. Below the first $100 the bot places no new entries at all
# (existing open positions still get managed normally, below in Pass 1 -
# this only gates Pass 2's new entries). Once the real balance crosses a
# tier, that whole tier becomes tradable; anything above the current tier
# sits untouched until the balance actually grows past the next one, so a
# losing streak can't eat into gains that already "graduated" to the next
# tier. Set CRYPTO_TIER_SIZE=0 to disable and go back to trading 100% of
# the balance (or whatever CRYPTO_MAX_ALLOCATION caps it at) every cycle.
TIER_SIZE = float(os.getenv("CRYPTO_TIER_SIZE", "100"))


def get_unlocked_tier(balance: float) -> float:
    """The highest whole multiple of TIER_SIZE that `balance` has actually
    reached, or the full balance if tiering is off. This is the tier the
    CURRENT balance qualifies for - see get_tier_highwater()/
    set_tier_highwater() for the persisted version that survives a
    withdrawal dropping the balance back down."""
    if TIER_SIZE <= 0:
        return balance
    return (balance // TIER_SIZE) * TIER_SIZE


# Once a tier is unlocked it stays unlocked permanently, even if a
# withdrawal drops the real balance back below it - a withdrawal is you
# taking profit out, not the bot losing, so it shouldn't re-lock trading
# privilege that was already earned. Reuses the same TradingBotState
# table trading_dashboard.py already tracks base-capital baselines in,
# under a dedicated bot_name key, rather than adding a new table.
TIER_STATE_KEY = "crypto_coinbase_tier_highwater"


async def get_tier_highwater() -> float:
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == TIER_STATE_KEY))
            row = result.scalar_one_or_none()
            return row.base_capital if row else 0.0
    except Exception as e:
        log.error(f"[CRYPTO] Failed to read tier high-water mark: {e}")
        return 0.0


async def set_tier_highwater(value: float):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradingBotState).where(TradingBotState.bot_name == TIER_STATE_KEY))
            row = result.scalar_one_or_none()
            if row:
                row.base_capital = value
            else:
                db.add(TradingBotState(bot_name=TIER_STATE_KEY, base_capital=value))
            await db.commit()
    except Exception as e:
        log.error(f"[CRYPTO] Failed to persist tier high-water mark: {e}")

# Coinbase trading cost: 0.40% total round-trip assumption = 0.20% entry + 0.20% exit
# This is used only to size the profit target sensibly, not charged/simulated here
# (the real fee is already reflected in Coinbase's fill price/balance).
CRYPTO_ROUND_TRIP_FEE_RATE = float(os.getenv("CRYPTO_ROUND_TRIP_FEE_RATE", "0.004"))

# A flat dollar profit target (the original design) doesn't scale with
# position size, so on a small position it can be - and was, at $0.50 on a
# ~$100 position - smaller than the round-trip fee itself, meaning
# every "successful" exit still lost money net of fees. The target is a
# percentage of the position's entry value instead, with a floor that
# guarantees it clears the round-trip fee with real profit left over.
# Increased from 1.5% to 3% to let winners run and match crypto volatility.
# BTC/ETH see 1-5% daily moves - capture those instead of closing at first tick.
PROFIT_TARGET_PCT = max(
    float(os.getenv("CRYPTO_PROFIT_TARGET_PCT", "0.05")),  # Increased from 3% to 5%
    CRYPTO_ROUND_TRIP_FEE_RATE * 1.5,
)

# Previously there was no stop-loss at all - the only exits were the
# profit target and "RSI recovered to neutral," so a position that never
# saw RSI recover again would just sit open indefinitely with the fee
# already sunk.
#
# Backtested against 30 days of real BTC/ETH 5-min candles with real fees
# applied: a tight stop (2%) performs WORSE than a wide one here, because
# this is a mean-reversion signal on volatile 5-min bars - normal noise
# trips a tight stop before the RSI thesis has time to play out. Results
# by stop width (this target, both symbols, same 30-day window):
#   2% stop: -$35 / -$45      5% stop: -$3  / -$11
#   4% stop: -$12 / -$19      6% stop: -$3  / -$6 (near breakeven)
# Widening further (8-10%) barely improves on 6% and does so on very few
# trades (10-16/month) - not enough to trust as a real edge, and it starts
# giving up real protection against an actual sharp move. 5% is chosen as
# the point that captures most of the realistic improvement without
# relying on an extreme, thinly-tested width.
#
# IMPORTANT: this is a fee-survival fix, not a proven profitable edge -
# every configuration tested landed at "roughly breakeven to slightly
# negative," never a clear, robust win. Start with MAX_ALLOCATION kept
# low and watch real results before trusting this with more capital.
STOP_LOSS_PCT = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.05"))

# Professional tiered exit levels for crypto - secure profits at milestones
# Tier 1: Exit 1/3 at 1.5% (half profit target, lock early gain)
# Tier 2: Exit 1/3 at 3% (full profit target)
# Tier 3: Exit final 1/3 at 5% (let winners run 2x the target)
CRYPTO_TIER_LEVELS = [0.02, 0.05, 0.10]  # OPTIMIZED: Exit at 2%, 5%, 10% for max profit

open_crypto_positions = {}  # Long positions: {symbol: {"entry": price, "qty": qty}}
open_crypto_shorts = {}      # Short positions: {symbol: {"entry": price, "qty": qty}}
daily_pnl = 0.0
daily_usd_balance_start = None  # For daily 2% loss limit
latest_signals = {}
last_cycle_at = None

BOT_NAME = "crypto_coinbase"


async def load_open_positions():
    """Reload open_crypto_positions and open_crypto_shorts from the DB once at startup, before
    the first cycle runs - otherwise a Railway restart wipes these dicts
    while the positions are still open for real on Coinbase, and the bot
    can never take profit or cut losses on them again (see BotPosition)."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
            rows = result.scalars().all()
            for row in rows:
                position_data = {"entry": row.entry_price, "qty": row.qty}
                if row.side == "short":
                    open_crypto_shorts[row.symbol] = position_data
                else:
                    open_crypto_positions[row.symbol] = position_data
            total = len(open_crypto_positions) + len(open_crypto_shorts)
            if rows:
                log.info(f"[CRYPTO] Reloaded {total} open position(s) from DB: {len(open_crypto_positions)} long, {len(open_crypto_shorts)} short")
    except Exception as e:
        log.error(f"[CRYPTO] Failed to reload open positions from DB: {e}")


async def _db_save_open(symbol: str, side: str, entry: float, qty: float):
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=symbol, side=side, entry_price=entry, qty=qty))
            await db.commit()
    except Exception as e:
        log.error(f"[CRYPTO] Failed to persist opened position {symbol}: {e}")


async def _db_delete_open(symbol: str, side: str = None):
    try:
        async with AsyncSessionLocal() as db:
            query = select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.symbol == symbol)
            if side:
                query = query.where(BotPosition.side == side)
            result = await db.execute(query)
            for row in result.scalars().all():
                await db.delete(row)
            await db.commit()
    except Exception as e:
        log.error(f"[CRYPTO] Failed to remove closed position {symbol} {side or ''} from DB: {e}")


TRADE_ALERT_EMAIL = os.getenv("TRADE_ALERT_EMAIL", "")


def send_trade_alert(subject: str, body: str):
    """Same GMAIL_EMAIL/GMAIL_PASSWORD SMTP pattern used elsewhere -
    no-ops quietly if creds aren't set."""
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    alert_email = TRADE_ALERT_EMAIL

    if not sender_email or not sender_password:
        log.debug(f"(trade alert email skipped - GMAIL_EMAIL/GMAIL_PASSWORD not set) {subject}")
        return

    if not alert_email:
        log.debug(f"(trade alert email skipped - TRADE_ALERT_EMAIL not set) {subject}")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = alert_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, alert_email, msg.as_string())
        log.info(f"📧 Trade alert emailed to {alert_email}")
    except Exception as e:
        log.warning(f"Trade alert email failed: {e}")


async def get_usd_balance(session):
    """Real Coinbase USD balance available for trading. Returns
    (balance, None) on success, or (None, reason) on any failure (auth
    issue, network hiccup, no USD wallet) - the reason travels back to
    the caller instead of only being logged here, so it shows up
    directly in the per-cycle log line instead of a separate one that's
    easy to scroll past.

    Paginates through every page of accounts (Coinbase caps each page
    at ~49 regardless of the requested limit) - this account holds
    dozens of small/dust altcoin wallets, so the USD wallet is often not
    on the first page at all. Looking at only page 1 previously made
    the bot think there was no USD wallet when there was one further in."""
    path = "/api/v3/brokerage/accounts"
    all_currencies = []
    cursor = None
    try:
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), params=params) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    return None, f"HTTP {r.status}: {body}"
                data = await r.json()
                accounts = data.get("accounts", [])
                for account in accounts:
                    if account.get("currency") == "USD":
                        return float(account["available_balance"]["value"]), None
                all_currencies.extend(a.get("currency") for a in accounts)
                if not data.get("has_next") or not data.get("cursor"):
                    break
                cursor = data.get("cursor")
        return None, f"no USD account found across {len(all_currencies)} accounts on this key: {all_currencies}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _compute_rsi(closes: list) -> float:
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return round(100 - (100 / (1 + rs)), 1)


async def _fetch_coinbase_closes(session, symbol):
    """Free, no-auth public endpoint - BTC/USD -> BTC-USD. This is the
    primary price source since it's the same exchange orders execute on."""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    async with session.get(url, headers={"Accept": "application/json"}) as r:
        if r.status != 200:
            return None
        data = await r.json()
        if not data or len(data) < 14:
            return None
        # Coinbase returns newest-first; each row is [time, low, high, open, close, volume]
        rows = list(reversed(data))[-20:]
        return [float(row[4]) for row in rows]


async def get_price_rsi(session, symbol):
    """Price+RSI+breakout from Coinbase's public candles endpoint - the same
    exchange orders execute on, so there's no cross-exchange price drift.
    Also computes 14-bar MA for breakout confirmation (only buy above MA)."""
    try:
        closes = await _fetch_coinbase_closes(session, symbol)
    except Exception as e:
        log.debug(f"coinbase price fetch failed for {symbol}: {e}")
        closes = None

    if closes and len(closes) >= 14:
        rsi = _compute_rsi(closes)
        price = closes[-1]
        ma_14 = sum(closes[-14:]) / 14  # 14-bar moving average
        above_ma = price > ma_14  # Breakout confirmation: price above MA
        return {"price": price, "rsi": rsi, "ma_14": ma_14, "above_ma": above_ma}

    log.error(f"❌ {symbol}: coinbase price fetch failed")
    return None


def size_position(cash_pool_remaining, slots_remaining, price):
    """Same dollar-based fractional sizing as crypto_alpaca_bot.py's
    size_position() - splits whatever's left in the crypto cash pool
    evenly across remaining open slots. That pool is the full account
    balance by default (compounding), so a bigger balance means bigger
    positions here automatically, with no other code change needed.

    Caps the order at a fraction of the pool (not 100% of it) - a market
    BUY's quote_size is the dollar amount handed to Coinbase, and Coinbase
    charges the taker fee on top of that from the same source account.
    Requesting the literal full balance as quote_size leaves zero room for
    that fee and Coinbase bounces the whole order as INSUFFICIENT_FUND
    (seen in production with a single open slot sizing to 100% of a $49
    balance). Reserving a couple points above the real taker fee covers
    that with margin to spare."""
    if slots_remaining <= 0 or cash_pool_remaining < MIN_POSITION_NOTIONAL:
        return None
    fee_safe_pool = cash_pool_remaining * (1 - max(TAKER_FEE_RATE * 2, 0.01))
    amount = min(max(fee_safe_pool / slots_remaining, MIN_POSITION_NOTIONAL), fee_safe_pool)
    qty = round(amount / price, 8)
    return qty if qty > 0 else None


async def place_order(session, symbol, side, qty, price):
    """Coinbase Advanced Trade orders:
    - BUY: market IOC for immediate entry
    - SELL: limit GTC at profit target to hold order until price reached
    """
    product_id = _to_product_id(symbol)
    path = "/api/v3/brokerage/orders"

    if side == "buy":
        # Use IOC market for immediate entry
        order_config = {"market_market_ioc": {"quote_size": f"{qty * price:.2f}"}}
    else:
        # Use GTC limit for profit-taking - order persists until target price hit
        order_config = {
            "limit_limit_gtc": {
                "base_size": f"{qty:.8f}",
                "limit_price": f"{price:.2f}"
            }
        }

    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": order_config,
    }
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path), json=order) as r:
            result = await r.json()
            if r.status in (200, 201) and result.get("success", True):
                log.info(f"✅ CRYPTO TRADE | {side.upper()} {qty} {symbol}")
                return True
            log.error(f"❌ Crypto order failed (requested {order_config}): {result.get('error_response', result)}")
            return False
    except Exception as e:
        log.error(f"Crypto order error: {e}")
        return False


async def run_crypto_cycle():
    global daily_pnl, last_cycle_at

    now = datetime.now(timezone.utc)
    last_cycle_at = now.isoformat()
    log.info(f"[CRYPTO] Scanning {', '.join(CRYPTO_PAIRS)} (24/7, no market-hours gate) | Daily P&L: ${daily_pnl:.2f}")

    connector = aiohttp.TCPConnector(use_dns_cache=True)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        cash, balance_error = await get_usd_balance(session)
        unlocked = 0.0
        if cash is None:
            log.warning(f"[CRYPTO] Could not read Coinbase USD balance - {balance_error} - skipping entries this cycle (exits below still run on open positions)")
            cash_pool = 0.0
        elif TIER_SIZE <= 0:
            unlocked = cash
            cash_pool = min(unlocked, MAX_ALLOCATION) if MAX_ALLOCATION is not None else unlocked
        else:
            raw_tier = get_unlocked_tier(cash)
            highwater = await get_tier_highwater()
            if raw_tier > highwater:
                await set_tier_highwater(raw_tier)
                log.info(f"[CRYPTO] 🎉 Tier up — ${raw_tier:.2f} unlocked for trading (was ${highwater:.2f}). This stays unlocked permanently, even if you withdraw and the balance drops back down.")
                highwater = raw_tier
            unlocked = highwater
            # Tier is a permanent permission, not a promise there's still
            # cash sitting there - can never trade more than what's real.
            cash_pool = min(cash, unlocked, MAX_ALLOCATION) if MAX_ALLOCATION is not None else min(cash, unlocked)

        # Professional daily loss limit: stop new entries after losing 2% of account in a day
        global daily_usd_balance_start
        if daily_usd_balance_start is None and cash is not None:
            daily_usd_balance_start = cash

        daily_loss_limit_pct = 0.02  # 2% daily loss limit
        daily_loss_limit = (daily_usd_balance_start * daily_loss_limit_pct) if daily_usd_balance_start else None
        is_hitting_daily_loss_limit = (
            daily_loss_limit and cash is not None and
            (daily_usd_balance_start - cash) >= daily_loss_limit
        )

        cap_desc = f"capped at ${MAX_ALLOCATION:.2f}" if MAX_ALLOCATION is not None else "full balance, compounding"
        if TIER_SIZE > 0:
            tier_desc = f"tier unlocked (permanent): ${unlocked:.2f} | tradable now: ${cash_pool:.2f}"
        else:
            tier_desc = "tiering off"
        status_suffix = " | ⚠️ DAILY 2% LOSS LIMIT HIT - stopping new trades" if is_hitting_daily_loss_limit else ""
        log.info(f"[CRYPTO] Coinbase USD balance: {'$%.2f' % cash if cash is not None else 'unknown'} | Crypto cash pool: ${cash_pool:.2f} ({cap_desc}, {tier_desc}) | Target: +{PROFIT_TARGET_PCT*100:.2f}% | Stop: -{STOP_LOSS_PCT*100:.2f}% | Round-trip fee: {CRYPTO_ROUND_TRIP_FEE_RATE*100:.2f}%{status_suffix}")

        # Skip new entries entirely if cash pool is below meaningful trade size
        if cash_pool < MIN_CRYPTO_TRADE_USD:
            log.info(
                f"[CRYPTO] Cash pool ${cash_pool:.2f} below minimum trade size ${MIN_CRYPTO_TRADE_USD:.2f}; "
                "skipping new entries (exits on open positions still run)"
            )
            # Still manage exits on existing positions, then return
            scans = {}
            for symbol in CRYPTO_PAIRS:
                data = await get_price_rsi(session, symbol)
                if data:
                    scans[symbol] = data
                await asyncio.sleep(0.3)
            # Run Pass 1 (exits) only
            for symbol, position in list(open_crypto_positions.items()):
                data = scans.get(symbol)
                if not data:
                    continue
                price, rsi = data["price"], data["rsi"]
                entry, qty = position["entry"], position["qty"]
                unrealized_pnl = (price - entry) * qty
                unrealized_pct = (price - entry) / entry
                stop_hit = unrealized_pct <= -STOP_LOSS_PCT
                rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE
                tier1_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[0]
                tier2_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[1]
                tier3_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[2]
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                    "has_position": True, "checked_at": now.isoformat(),
                }
                should_exit = False
                reason = None
                if stop_hit:
                    should_exit = True
                    reason = f"STOP LOSS (-{unrealized_pct*100:.2f}%)"
                elif rsi_exit:
                    should_exit = True
                    reason = "RSI EXIT"
                elif tier3_hit:
                    should_exit = True
                    reason = f"TIER 3 (+{unrealized_pct*100:.2f}%, let winners run)"
                elif tier2_hit and unrealized_pct >= PROFIT_TARGET_PCT:
                    should_exit = True
                    reason = f"TIER 2 (+{unrealized_pct*100:.2f}%, hit 3% target)"
                elif tier1_hit:
                    should_exit = True
                    reason = f"TIER 1 (+{unrealized_pct*100:.2f}%, lock early gain)"
                if should_exit:
                    filled = await place_order(session, symbol, "sell", qty, price)
                    if filled:
                        daily_pnl += unrealized_pnl
                        log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                        send_trade_alert(
                            f"🤖 Crypto bot — {symbol} closed ({reason})",
                            f"Position closed on your Coinbase account:\n\n"
                            f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                            f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                        )
                        open_crypto_positions.pop(symbol, None)
                        await _db_delete_open(symbol)
                await asyncio.sleep(0.3)
            return

        scans = {}
        for symbol in CRYPTO_PAIRS:
            data = await get_price_rsi(session, symbol)
            if data:
                scans[symbol] = data
                ma = data.get('ma_14', 0)
                above_ma = "✓" if data.get('above_ma') else "✗"
                log.info(f"[CRYPTO] {symbol} | ${data['price']:.2f} | RSI:{data['rsi']} | MA14:${ma:.2f} {above_ma}")
            await asyncio.sleep(0.3)

        # ── Pass 1: manage exits (long only) ──────────────────────────
        for symbol, position in list(open_crypto_positions.items()):
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            entry, qty = position["entry"], position["qty"]
            unrealized_pnl = (price - entry) * qty
            unrealized_pct = (price - entry) / entry
            stop_hit = unrealized_pct <= -STOP_LOSS_PCT
            # RSI recovering to neutral is only a real exit if it's already
            # cleared the round-trip fee - otherwise this "safe-looking"
            # exit is actually a guaranteed net loss (this was the actual
            # source of the fee bleed a backtest caught: RSI_SELL_ABOVE=50
            # fires on almost every trade long before PROFIT_TARGET_PCT
            # ever does, and it used to have no profit floor at all).
            rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE

            # Professional tiered profit-taking: lock in gains at milestones
            tier1_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[0]  # 1.5%
            tier2_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[1]  # 3%
            tier3_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[2]  # 5%

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                "has_position": True, "checked_at": now.isoformat(),
            }

            # Exit conditions: stop loss, RSI reversal, or tiered profit target
            should_exit = False
            reason = None

            if stop_hit:
                should_exit = True
                reason = f"STOP LOSS (-{unrealized_pct*100:.2f}%)"
            elif rsi_exit:
                should_exit = True
                reason = "RSI EXIT"
            elif tier3_hit:
                should_exit = True
                reason = f"TIER 3 (+{unrealized_pct*100:.2f}%, let winners run)"
            elif tier2_hit and unrealized_pct >= PROFIT_TARGET_PCT:
                should_exit = True
                reason = f"TIER 2 (+{unrealized_pct*100:.2f}%, hit 3% target)"
            elif tier1_hit:
                should_exit = True
                reason = f"TIER 1 (+{unrealized_pct*100:.2f}%, lock early gain)"

            if should_exit:
                filled = await place_order(session, symbol, "sell", qty, price)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} closed ({reason})",
                        f"Position closed on your Coinbase account:\n\n"
                        f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                        f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                    )
                    open_crypto_positions.pop(symbol, None)
                    await _db_delete_open(symbol)

            await asyncio.sleep(0.3)

        # ── Pass 1b: manage short exits ──────────────────────────────────────
        for symbol, position in list(open_crypto_shorts.items()):
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            entry, qty = position["entry"], position["qty"]
            # For shorts: profit when price drops (entry > price)
            unrealized_pnl = (entry - price) * qty
            unrealized_pct = (entry - price) / entry
            # Short stop loss: when price goes too high (entry - price becomes negative)
            stop_hit = unrealized_pct <= -STOP_LOSS_PCT
            # Short exit on RSI recovery to oversold: when RSI drops back below 32
            rsi_exit = rsi < RSI_SHORT_BELOW and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE

            # Tiered profit-taking
            tier1_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[0]
            tier2_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[1]
            tier3_hit = unrealized_pct >= CRYPTO_TIER_LEVELS[2]

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "HOLDING_SHORT",
                "has_position": True, "checked_at": now.isoformat(),
            }

            should_exit = False
            reason = None

            if stop_hit:
                should_exit = True
                reason = f"STOP LOSS (-{unrealized_pct*100:.2f}%)"
            elif rsi_exit:
                should_exit = True
                reason = "RSI EXIT (SHORT)"
            elif tier3_hit:
                should_exit = True
                reason = f"TIER 3 (+{unrealized_pct*100:.2f}%, let winners run)"
            elif tier2_hit and unrealized_pct >= PROFIT_TARGET_PCT:
                should_exit = True
                reason = f"TIER 2 (+{unrealized_pct*100:.2f}%, hit 3% target)"
            elif tier1_hit:
                should_exit = True
                reason = f"TIER 1 (+{unrealized_pct*100:.2f}%, lock early gain)"

            if should_exit:
                # Buy to close short position at current price
                filled = await place_order(session, symbol, "buy", qty, price)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE SHORT {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} SHORT closed ({reason})",
                        f"Short position closed on your Coinbase account:\n\n"
                        f"SELL {qty} {symbol} @ ${entry:.2f} → BUY @ ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                        f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                    )
                    open_crypto_shorts.pop(symbol, None)
                    await _db_delete_open(symbol, "short")

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries (long only, RSI oversold + breakout) ─────────────
        for symbol in CRYPTO_PAIRS:
            if symbol in open_crypto_positions:
                continue
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            ma_14 = data.get("ma_14", 0)
            above_ma = data.get("above_ma", False)

            if rsi >= RSI_BUY_BELOW:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "ma_14": ma_14, "status": "NEUTRAL",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            # Entry confirmed only if: (1) RSI oversold AND (2) price above 14-bar MA
            if not above_ma:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "ma_14": ma_14, "status": "OVERSOLD_BUT_BELOW_MA",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "ma_14": ma_14, "status": "BUY_ZONE",
                "has_position": False, "checked_at": now.isoformat(),
            }

            # Professional risk management: stop new entries if daily 2% loss limit hit
            if is_hitting_daily_loss_limit:
                log.info(f"[CRYPTO] 🛑 {symbol} blocked — daily 2% loss limit reached, no new entries")
                continue

            if len(open_crypto_positions) >= MAX_POSITIONS:
                log.info(f"[CRYPTO] At max positions ({MAX_POSITIONS}) - {symbol} BUY signal held, not entering")
                continue

            slots_remaining = MAX_POSITIONS - len(open_crypto_positions)
            qty = size_position(cash_pool, slots_remaining, price)
            if qty is None:
                log.info(f"[CRYPTO] Skipping {symbol} entry — not enough allocated cash (${cash_pool:.2f})")
                continue

            log.info(f"[CRYPTO] 📡 BUY {symbol} — RSI:{rsi}")
            filled = await place_order(session, symbol, "buy", qty, price)
            if filled:
                open_crypto_positions[symbol] = {"entry": price, "qty": qty}
                await _db_save_open(symbol, "long", price, qty)
                cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — BUY {symbol} opened",
                    f"Long opened on your Coinbase account:\n\n"
                    f"BUY {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                )

            await asyncio.sleep(0.3)

        # ── Pass 3: new short entries (RSI overbought + below MA for confirmation) ────
        total_positions = len(open_crypto_positions) + len(open_crypto_shorts)
        for symbol in CRYPTO_PAIRS:
            # Don't short if already long on this pair
            if symbol in open_crypto_positions:
                continue
            # Don't open multiple shorts on same pair
            if symbol in open_crypto_shorts:
                continue

            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            ma_14 = data.get("ma_14", 0)
            above_ma = data.get("above_ma", False)

            if rsi <= RSI_SHORT_ABOVE:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "ma_14": ma_14, "status": "NEUTRAL",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            # Short entry confirmed only if: (1) RSI overbought AND (2) price below 14-bar MA
            if above_ma:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "ma_14": ma_14, "status": "OVERBOUGHT_ABOVE_MA",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "ma_14": ma_14, "status": "SHORT_ZONE",
                "has_position": False, "checked_at": now.isoformat(),
            }

            # Stop new entries if daily 2% loss limit hit
            if is_hitting_daily_loss_limit:
                log.info(f"[CRYPTO] 🛑 {symbol} SHORT blocked — daily 2% loss limit reached, no new entries")
                continue

            if total_positions >= MAX_POSITIONS:
                log.info(f"[CRYPTO] At max positions ({MAX_POSITIONS}) - {symbol} SHORT signal held, not entering")
                continue

            slots_remaining = MAX_POSITIONS - total_positions
            qty = size_position(cash_pool, slots_remaining, price)
            if qty is None:
                log.info(f"[CRYPTO] Skipping {symbol} SHORT entry — not enough allocated cash (${cash_pool:.2f})")
                continue

            log.info(f"[CRYPTO] 📡 SHORT {symbol} — RSI:{rsi}")
            filled = await place_order(session, symbol, "sell", qty, price)
            if filled:
                open_crypto_shorts[symbol] = {"entry": price, "qty": qty}
                await _db_save_open(symbol, "short", price, qty)
                cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — SHORT {symbol} opened",
                    f"Short opened on your Coinbase account:\n\n"
                    f"SELL {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                )

            await asyncio.sleep(0.3)


def run():
    log.info("=" * 60)
    log.info("CRYPTO TRADING BOT — Coinbase (separate account from Alpaca stocks)")
    alloc_desc = f"${MAX_ALLOCATION:.2f} cap" if MAX_ALLOCATION is not None else "full balance (compounding)"
    log.info(f"Pairs: {', '.join(CRYPTO_PAIRS)} | Allocation: {alloc_desc} | Max positions: {MAX_POSITIONS}")
    log.info("Runs 24/7 - crypto has no market close, unlike prop_bot.py's stock/ETF trading")
    log.info("🔴 LIVE TRADING - Coinbase has no free paper-trading sandbox for Advanced Trade")
    log.info("=" * 60)

    if not (COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY):
        log.warning("COINBASE_API_KEY_NAME/COINBASE_API_PRIVATE_KEY not configured - crypto_coinbase_bot will not start")
        return

    # One event loop for this thread's entire life, not a fresh one every
    # cycle. main.py's uvicorn server runs on uvloop, which installs its
    # event loop policy process-wide - calling asyncio.run() fresh each
    # cycle means tearing down and recreating a whole uvloop loop every
    # 60 seconds in this background thread, which was intermittently
    # producing "Task ... got Future ... attached to a different loop"
    # errors (seen in production logs) that made every single balance
    # fetch fail. A single persistent loop, reused via
    # run_until_complete(), removes the repeated create/destroy cycle
    # entirely.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(load_open_positions())
    except Exception as e:
        log.error(f"[CRYPTO] Startup position reload failed: {e}")

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true — crypto bot paused")
            time.sleep(60)
            continue
        try:
            loop.run_until_complete(run_crypto_cycle())
        except Exception as e:
            log.error(f"Crypto cycle error: {e}")
        time.sleep(30)  # 30-sec cycle for 24/7 responsiveness (was 60s)


if __name__ == "__main__":
    run()
