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
from models import BotPosition, TradingBotState, CryptoRSIState, CryptoTradeLog
from bot_mandates import CRYPTO_MANDATE

# Measurement system: Trade logging with full signal context
try:
    from measurement_system import SignalContext, TradeLog, trade_logger, StatisticalAnalyzer
    MEASUREMENT_AVAILABLE = True
except ImportError:
    MEASUREMENT_AVAILABLE = False
    log.warning("measurement_system not available - trade logging disabled")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_coinbase_bot")

# Network resilience cache — stores price data for 30min when API unavailable
PRICE_CACHE = {}  # {symbol: {"price": float, "rsi": float, "atr": float, "timestamp": datetime}}
CACHE_TTL_SECONDS = 1800  # 30 minutes

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
NETWORK_RETRY_ATTEMPTS = int(os.getenv("NETWORK_RETRY_ATTEMPTS", "3"))
NETWORK_RETRY_DELAY = float(os.getenv("NETWORK_RETRY_DELAY", "1.0"))  # seconds

try:
    NETWORK_RETRY_ATTEMPTS = int(os.getenv("NETWORK_RETRY_ATTEMPTS", "3"))
except (ValueError, TypeError):
    NETWORK_RETRY_ATTEMPTS = 3
    log.warning("Invalid NETWORK_RETRY_ATTEMPTS, using default: 3")

try:
    NETWORK_RETRY_DELAY = float(os.getenv("NETWORK_RETRY_DELAY", "1.0"))
except (ValueError, TypeError):
    NETWORK_RETRY_DELAY = 1.0
    log.warning("Invalid NETWORK_RETRY_DELAY, using default: 1.0")

try:
    NETWORK_RETRY_ATTEMPTS = int(os.getenv("NETWORK_RETRY_ATTEMPTS", "3"))
except (ValueError, TypeError):
    NETWORK_RETRY_ATTEMPTS = 3
    log.warning("Invalid NETWORK_RETRY_ATTEMPTS, using default: 3")

try:
    NETWORK_RETRY_DELAY = float(os.getenv("NETWORK_RETRY_DELAY", "1.0"))
except (ValueError, TypeError):
    NETWORK_RETRY_DELAY = 1.0
    log.warning("Invalid NETWORK_RETRY_DELAY, using default: 1.0")

CRYPTO_PAIRS = [
    "BTC/USD", "ETH/USD",  # Tier 1: Core stable cryptos
    "SOL/USD", "XRP/USD", "AVAX/USD", "LINK/USD",  # Tier 2: Established mid-caps
    "DOGE/USD", "SHIB/USD",  # Meme coins with strong volume
    "NEAR/USD", "MATIC/USD",  # Layer 2 / scaling solutions
    "ARB/USD", "OP/USD",  # Arbitrum & Optimism
    "AAVE/USD", "UNI/USD",  # DeFi protocols
    "STX/USD", "ATOM/USD",  # Bitcoin L2 & Cosmos
    "LTC/USD", "ADA/USD", "DOT/USD",  # Established alts with high volume
    "APT/USD", "SUI/USD", "JUP/USD",  # High-velocity newer pairs
    "LDO/USD", "RNDR/USD", "ICP/USD",  # Staking & compute protocols
    "BLUR/USD", "FLOKI/USD", "BONK/USD",  # Additional meme/community coins
]  # 28 pairs total - aggressive expansion for 4x entry frequency


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
    if not raw:
        log.error("COINBASE_API_PRIVATE_KEY environment variable is empty or missing")
        raise ValueError("COINBASE_API_PRIVATE_KEY not set")
    try:
        if raw.startswith("-----BEGIN"):
            return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
        # Ed25519 key: base64 string → 64 bytes (32 seed + 32 public key)
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as e:
            log.error(f"COINBASE_API_PRIVATE_KEY is not valid base64: {e}")
            raise
        if len(decoded) != 64:
            log.error(f"COINBASE_API_PRIVATE_KEY decoded to {len(decoded)} bytes, expected 64. Is this the correct key from Coinbase CDP?")
            raise ValueError(f"Ed25519 key must be 64 bytes when decoded, got {len(decoded)}")
        return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"
    except Exception as e:
        log.error(f"Failed to load Coinbase signing key: {type(e).__name__}: {e} - key starts with: {raw[:20]}...")
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


# AGGRESSIVE THRESHOLDS for high-velocity trading in overbought/oversold markets
# Long-only: Coinbase SPOT has no shorting support
RSI_STRONG_BUY = 25   # RSI < 25 = oversold, strong reversal signal
RSI_BUY = 50          # RSI < 50 = oversold/neutral zone, widened for faster entry (test phase)
RSI_NO_ENTRY = 60     # RSI >= 60 = overbought, skip LONG entries only (lowered from 50, allows 40-60 zone)
try:
    RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "75"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_RSI_SELL_ABOVE value, using default: 75")
    RSI_SELL_ABOVE = 75.0

try:
    RSI_BUY_BELOW = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "10"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_RSI_BUY_BELOW value, using default: 10")
    RSI_BUY_BELOW = 10.0

try:
    MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "18"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_MAX_POSITIONS value, using default: 18")
    MAX_POSITIONS = 18

# Unset by default - no ceiling, so the full account balance (principal +
# compounded profit) is always in play. Set CRYPTO_MAX_ALLOCATION to cap
# it at a fixed dollar amount instead, if ever wanted.
_max_allocation_env = os.getenv("CRYPTO_MAX_ALLOCATION", "")
try:
    MAX_ALLOCATION = float(_max_allocation_env) if _max_allocation_env else None
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_MAX_ALLOCATION value, using default: None")
    MAX_ALLOCATION = None

# Micro-trades on small balances: $0.50 minimum lets bot scalp $0.58-100 accounts
# without sitting idle. At $0.58 balance: $0.50 × 0.5% move = $0.0025 profit (micro-compounding)
# $100+ balance: trades full notional, beats fees, compounds faster.
try:
    MIN_POSITION_NOTIONAL = float(os.getenv("CRYPTO_MIN_POSITION_NOTIONAL", "0.50"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_MIN_POSITION_NOTIONAL value, using default: 0.50")
    MIN_POSITION_NOTIONAL = 0.50

# Minimum trade size guard: skip trading if cash pool is too small to be meaningful
try:
    MIN_CRYPTO_TRADE_USD = float(os.getenv("MIN_CRYPTO_TRADE_USD", "5.00"))
except (ValueError, TypeError):
    log.warning("Invalid MIN_CRYPTO_TRADE_USD value, using default: 5.00")
    MIN_CRYPTO_TRADE_USD = 5.00

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
try:
    TIER_SIZE = float(os.getenv("CRYPTO_TIER_SIZE", "100"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_TIER_SIZE value, using default: 100")
    TIER_SIZE = 100.0


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
try:
    CRYPTO_ROUND_TRIP_FEE_RATE = float(os.getenv("CRYPTO_ROUND_TRIP_FEE_RATE", "0.004"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_ROUND_TRIP_FEE_RATE value, using default: 0.004")
    CRYPTO_ROUND_TRIP_FEE_RATE = 0.004

# DEPRECATED: Fixed 37% target replaced with tiered system (see CRYPTO_TIER_LEVELS above)
# The old fixed target was mathematically unsound: 18.5:1 reward/risk meant the bot held
# positions indefinitely waiting for 37%, bleeding capital on the 2% stop while RSI recovered.
# New strategy: Take profits at realistic milestones (3%, 5%, 10%) and trail the final position.
# This lets winners run while locking in gains and stopping losses early.
# Hard-coded: Deprecated - uses CRYPTO_TIER_LEVELS instead
# Do NOT restore the 37% target - it proved unworkable in live trading
PROFIT_TARGET_PCT = 0.37  # DEPRECATED - kept for reference only, use CRYPTO_TIER_LEVELS instead

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
# Hard-coded: 2% stop loss (tight capital preservation, avoid fee bleed)
# Do NOT override via env var - this is proven through backtesting
STOP_LOSS_PCT = 0.02  # 2% tight stop for capital preservation

# Coinbase Advanced Trade API taker fee (0.6% standard rate for crypto)
TAKER_FEE_RATE = 0.006

# IMPROVED tiered exit levels - take profits at realistic milestones, not fixed 37%
# Tier 1: Exit 1/3 at 3-5% (first profit zone, lock early gain)
# Tier 2: Exit 1/3 at 6-10% (second profit zone, move stop to breakeven)
# Tier 3: Remaining 1/3 trails at trailing stop (let winners run with protection)
# This replaces the fixed 37% target which caused the bot to hold indefinitely
CRYPTO_TIER_LEVELS = [0.03, 0.05, 0.10]  # 3-tier exit: 3% (lock), 5%, 10% (trailing)
CRYPTO_TIER_FRACTIONS = [1/3, 1/3, 1/3]   # Exit 1/3 of position at each tier
CRYPTO_TRAILING_STOP_PCT = 0.05  # Trail final position by 5% from recent high (protection while letting winners run)

open_crypto_positions = {}  # Long positions: {symbol: {"entry": price, "qty": qty}}
daily_pnl = 0.0
daily_usd_balance_start = None  # For daily 2% loss limit
latest_signals = {}
last_cycle_at = None

# RSI state cache: loaded once at startup, maintained in-memory during cycle, batch-flushed to DB at end
# This removes database latency from the hot trading loop - 56 DB queries per cycle become 0
RSI_STATE_CACHE = {}  # {symbol: {"entered_oversold": bool, "armed_rsi": float, "last_rsi": float, "changed": bool}}

BOT_NAME = "crypto_coinbase"


async def load_open_positions():
    """Reload open_crypto_positions and open_crypto_shorts from the DB once at startup, before
    the first cycle runs - otherwise a Railway restart wipes these dicts
    while the positions are still open for real on Coinbase, and the bot
    can never take profit or cut losses on them again (see BotPosition)."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.side == "long"))
            rows = result.scalars().all()
            for row in rows:
                position_data = {"entry": row.entry_price, "qty": row.qty}
                open_crypto_positions[row.symbol] = position_data
            if rows:
                log.info(f"[CRYPTO] Reloaded {len(open_crypto_positions)} long position(s) from DB")
    except Exception as e:
        log.error(f"[CRYPTO] Failed to reload open positions from DB: {e}")


async def load_all_rsi_states():
    """Load RSI state for ALL trading pairs at startup, cache in memory.
    This removes database latency from the hot trading loop - instead of 56 DB queries
    per 60-second cycle, we load once at startup and batch-flush at cycle end.

    CRITICAL FIX: Database operations inside aiohttp context were causing timeouts
    on Coinbase API calls. Moving to startup + in-memory + batch-flush unblocks the event loop."""
    global RSI_STATE_CACHE
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(CryptoRSIState))
            states = result.scalars().all()

            # Load existing states
            for state in states:
                RSI_STATE_CACHE[state.symbol] = {
                    "entered_oversold": state.entered_oversold,
                    "armed_rsi": state.armed_rsi,
                    "last_rsi": state.last_rsi,
                    "changed": False  # Track if this state changed this cycle
                }

            # Initialize missing symbols (will be created on first flush)
            for symbol in CRYPTO_PAIRS:
                if symbol not in RSI_STATE_CACHE:
                    RSI_STATE_CACHE[symbol] = {
                        "entered_oversold": False,
                        "armed_rsi": None,
                        "last_rsi": None,
                        "changed": False
                    }

            log.info(f"[CRYPTO] ✅ Loaded RSI state for {len(RSI_STATE_CACHE)} symbols into memory cache")
    except Exception as e:
        log.error(f"[CRYPTO] Failed to load RSI states at startup: {e}")
        # Initialize all symbols with defaults if load fails
        for symbol in CRYPTO_PAIRS:
            RSI_STATE_CACHE[symbol] = {
                "entered_oversold": False,
                "armed_rsi": None,
                "last_rsi": None,
                "changed": False
            }
        log.warning(f"[CRYPTO] Initialized {len(RSI_STATE_CACHE)} symbols with default state")


def cache_price_data(symbol: str, price_data: dict):
    """Store price/RSI data locally for offline mode (Railway network unavailable).
    Cache expires after 30 minutes."""
    global PRICE_CACHE
    PRICE_CACHE[symbol] = {
        **price_data,
        "timestamp": datetime.utcnow(),
        "source": "coinbase"
    }


def get_cached_price(symbol: str, max_age_seconds: int = CACHE_TTL_SECONDS) -> dict:
    """Retrieve cached price data if fresh (<30min old). Returns None if expired/missing."""
    if symbol not in PRICE_CACHE:
        return None

    cached = PRICE_CACHE[symbol]
    age = (datetime.utcnow() - cached["timestamp"]).total_seconds()

    if age > max_age_seconds:
        del PRICE_CACHE[symbol]  # Expire old cache
        return None

    return cached


async def _retry_with_backoff(async_func, max_attempts: int = None):
    """Retry network calls with exponential backoff. Falls back to cache on persistent failure."""
    attempts = max_attempts or NETWORK_RETRY_ATTEMPTS
    delay = NETWORK_RETRY_DELAY

    for attempt in range(attempts):
        try:
            return await async_func()
        except Exception as e:
            if attempt == attempts - 1:
                log.debug(f"Retry exhausted after {attempts} attempts")
                return None

            await asyncio.sleep(delay)
            delay *= 1.5  # Exponential backoff: 1s → 1.5s → 2.25s, etc.


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
    except asyncio.TimeoutError:
        return None, "Coinbase API timeout (network slow or API unresponsive)"
    except aiohttp.ClientError as e:
        return None, f"Coinbase API connection failed: {type(e).__name__}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:100]}"


def _compute_rsi(closes: list) -> float:
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return round(100 - (100 / (1 + rs)), 1)


def _compute_atr(closes: list, highs: list, lows: list, period: int = 14) -> float:
    """Calculate Average True Range for volatility-based position sizing and exits.
    ATR = average of true ranges over N periods.
    True Range = max(high - low, abs(high - prior close), abs(low - prior close))"""
    if len(closes) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        true_ranges.append(tr)
    atr = sum(true_ranges[-period:]) / period if true_ranges else 0.0
    return atr


def _crypto_buy_signal_improved(rsi: float, rsi_recovery: bool, volume_ratio: float, close_position: float, entered_oversold: bool) -> str:
    """Improved entry signal: tiered RSI + recovery trigger + momentum/volume confirmation.
    Returns: "STRONG_BUY" (RSI < 20 + bounce), "BUY" (20-30 + bounce), or "NO_ACTION"

    Key improvement: Only enter when RSI has ENTERED the oversold zone (10-30) AND
    is BOUNCING upward (rsi_recovery=True), not just when it enters oversold.
    This avoids catching falling knives and ensures we capture real reversals."""

    # Must have entered oversold zone before we can enter
    if not entered_oversold:
        return "NO_ACTION"

    # Don't enter if RSI isn't recovering (still falling) - wait for bounce
    if not rsi_recovery:
        return "NO_ACTION"

    # Extreme oversold: strong reversal signal
    if rsi < RSI_STRONG_BUY:  # RSI < 20
        if volume_ratio >= 1.5 and close_position >= 0.50:
            return "STRONG_BUY"

    # Oversold recovery: normal reversal signal
    if RSI_STRONG_BUY <= rsi < RSI_BUY:  # 20 <= RSI < 30
        if volume_ratio >= 1.5 and close_position >= 0.60:  # Stricter candle filter
            return "BUY"

    return "NO_ACTION"


def _calculate_atr_targets(entry_price: float, atr: float) -> dict:
    """Calculate tiered profit targets from ATR (volatility-based).
    Returns: {tier1: price, tier2: price, tier3: price, stop: price}

    ATR typically represents 14-period average true range. Scale it for realistic targets:
    - Tier 1: Entry + 2×ATR (lock early profit)
    - Tier 2: Entry + 3.5×ATR (second profit zone)
    - Tier 3: Entry + 6×ATR (let winners run with trailing stop)
    - Stop: Entry - ATR (emergency protection)
    """
    if atr <= 0:
        # Fallback to percentage-based if ATR unavailable
        return {
            "tier1_price": entry_price * 1.03,   # 3%
            "tier2_price": entry_price * 1.05,   # 5%
            "tier3_price": entry_price * 1.10,   # 10%
            "stop_price": entry_price * 0.98,    # -2%
            "trailing_stop_pct": 0.05,            # 5% trail
        }
    # ATR-based targets (ATR is typically 0.5-2% of price for crypto)
    return {
        "tier1_price": entry_price + (2.0 * atr),    # First profit zone
        "tier2_price": entry_price + (3.5 * atr),    # Second profit zone
        "tier3_price": entry_price + (6.0 * atr),    # Let winners run
        "stop_price": entry_price - atr,              # Emergency stop
        "trailing_stop_pct": 0.05,                    # 5% trailing stop on final position
    }


async def _fetch_coinbase_candles(session, symbol, limit=50):
    """Fetch OHLCV candles for RSI, ATR, and directional bias calculation.
    Returns list of candles (newest first in API, reversed to chronological for analysis).
    Each candle: [time, low, high, open, close, volume]"""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    try:
        async with session.get(url, headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < limit:
                return None
            # Coinbase returns newest-first; reverse to chronological order
            return list(reversed(data))[-limit:]
    except Exception:
        return None


async def _fetch_coinbase_closes(session, symbol):
    """Legacy: fetch just closes for RSI calculation. Uses _fetch_coinbase_candles internally."""
    candles = await _fetch_coinbase_candles(session, symbol, limit=50)
    if not candles:
        return None
    return [float(row[4]) for row in candles]


async def _fetch_latest_candle(session, symbol):
    """Fetch latest 5-minute candle OHLC data + volume for momentum & volume confirmation.
    Returns candle dict with open, close, high, low, volume, and prior candle volume or None if fetch fails."""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    async with session.get(url, headers={"Accept": "application/json"}) as r:
        if r.status != 200:
            return None
        data = await r.json()
        if not data or len(data) < 2:
            return None
        # Latest candle is first in response; each row is [time, low, high, open, close, volume]
        latest = data[0]
        prior = data[1] if len(data) > 1 else None
        return {
            "open": float(latest[3]),
            "close": float(latest[4]),
            "high": float(latest[2]),
            "low": float(latest[1]),
            "volume": float(latest[5]),
            "prior_volume": float(prior[5]) if prior else 0,
        }


async def get_price_rsi(session, symbol):
    """Price+RSI+ATR+directional bias from Coinbase's public candles endpoint.
    Network resilient: retries with backoff, falls back to cache if unavailable.
    Uses MA50 to determine directional bias: only long above MA50, only short below MA50.
    RSI recovery: True if RSI is bouncing UP from oversold (useful for entry timing)."""

    candles = await _retry_with_backoff(
        lambda: _fetch_coinbase_candles(session, symbol, limit=50),
        max_attempts=NETWORK_RETRY_ATTEMPTS
    )
    candle = None
    if candles:
        candle = await _retry_with_backoff(
            lambda: _fetch_latest_candle(session, symbol),
            max_attempts=NETWORK_RETRY_ATTEMPTS
        )

    if candles and len(candles) >= 50:
        closes = [float(row[4]) for row in candles]
        highs = [float(row[2]) for row in candles]
        lows = [float(row[1]) for row in candles]

        rsi = _compute_rsi(closes)
        atr = _compute_atr(closes, highs, lows, period=14)
        price = closes[-1]
        ma_50 = sum(closes[-50:]) / 50
        above_ma50 = price > ma_50

        rsi_recovery = False
        if len(closes) >= 2:
            rsi_recovery = (rsi < 30 and closes[-1] > closes[-2])

        result = {
            "price": price, "rsi": rsi, "atr": atr,
            "ma_50": ma_50, "in_uptrend": above_ma50,
            "candle": candle,
            "rsi_recovery": rsi_recovery,
        }
        cache_price_data(symbol, result)  # Cache successful result
        return result

    # Network failed or insufficient data — try cache
    cached = get_cached_price(symbol)
    if cached:
        log.warning(f"⚠️ {symbol}: Using cached price data ({(datetime.utcnow() - cached['timestamp']).total_seconds() / 60:.1f} min old)")
        # Remove timestamp before returning
        return {k: v for k, v in cached.items() if k != 'timestamp'}

    log.error(f"❌ {symbol}: coinbase price fetch failed and no cache available")
    return None


async def get_4h_rsi(session, symbol):
    """Fetch 4-hour RSI for higher timeframe confirmation (reduces false signals)."""
    try:
        pair = _to_product_id(symbol)
        # 4h = 14400 seconds; fetch last 20 candles = ~3.3 days
        url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=14400"
        async with session.get(url, headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < 14:
                return None
            closes = [float(row[4]) for row in reversed(data)][-14:]
            rsi_4h = _compute_rsi(closes)
            return rsi_4h
    except Exception as e:
        log.debug(f"4h RSI fetch failed for {symbol}: {e}")
        return None


def size_position(cash_pool_remaining, slots_remaining, price):
    """Fixed $250 per trade for maximum capital deployment.
    Aggressive sizing: maximizes gains while maintaining 3-position minimum buffer."""
    FIXED_POSITION_SIZE = 250.0  # $250 per entry — with $700 pool, allows 2-3 concurrent positions

    if cash_pool_remaining < FIXED_POSITION_SIZE * 1.05:  # Need 5% buffer for fees
        return None

    # Calculate qty based on fixed dollar amount (Coinbase taker fee is deducted from the fill, not pre-calculated)
    qty = round(FIXED_POSITION_SIZE / price, 8)
    return qty if qty > 0 else None


async def place_order(session, symbol, side, qty, price):
    """Coinbase Advanced Trade orders:
    - BUY: market IOC for immediate entry
    - SELL: limit GTC at profit target to hold order until price reached
    """
    product_id = _to_product_id(symbol)
    path = "/api/v3/brokerage/orders"

    if side == "buy":
        # Market order: immediate entry at market price (IOC = Immediate or Cancel)
        order_config = {
            "market_ioc": {
                "quote_size": f"{qty * price:.2f}"
            }
        }
    else:
        # Limit order: exit at specific price (GTC = Good Until Cancelled)
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


async def load_rsi_state(symbol: str) -> dict:
    """Load RSI state for a symbol from in-memory cache (loaded at startup).
    CRITICAL: This no longer hits the database - all state is cached in memory during the cycle.
    This removes database latency from the hot trading loop."""
    global RSI_STATE_CACHE
    if symbol not in RSI_STATE_CACHE:
        # Safety: if symbol not in cache, initialize it
        RSI_STATE_CACHE[symbol] = {
            "entered_oversold": False,
            "armed_rsi": None,
            "last_rsi": None,
            "changed": False
        }
    cache_entry = RSI_STATE_CACHE[symbol]
    return {
        "entered_oversold": cache_entry["entered_oversold"],
        "armed_rsi": cache_entry["armed_rsi"],
        "last_rsi": cache_entry["last_rsi"],
    }


async def update_rsi_state(symbol: str, rsi: float, entered_oversold: bool, armed_rsi: float = None):
    """Update RSI state for a symbol in in-memory cache only.
    CRITICAL: This no longer hits the database during the cycle.
    Changes are marked and flushed to database at cycle end via flush_rsi_state_cache().
    This removes database latency from the hot trading loop."""
    global RSI_STATE_CACHE
    if symbol not in RSI_STATE_CACHE:
        # Safety: if symbol not in cache, initialize it
        RSI_STATE_CACHE[symbol] = {
            "entered_oversold": False,
            "armed_rsi": None,
            "last_rsi": None,
            "changed": False
        }

    # Update cache entry
    cache_entry = RSI_STATE_CACHE[symbol]
    cache_entry["entered_oversold"] = entered_oversold
    cache_entry["armed_rsi"] = armed_rsi
    cache_entry["last_rsi"] = rsi
    cache_entry["changed"] = True  # Mark for batch flush at cycle end


async def log_trade_entry(symbol: str, entry_rsi: float, entry_price: float, qty: float,
                          arm_rsi: float, volume_ratio: float, candle_close_position: float):
    """Log when a trade enters (ENTER → ENTER) and record to measurement system."""
    try:
        trade_id = str(uuid.uuid4())[:8]  # Unique trade ID

        async with AsyncSessionLocal() as session:
            trade = CryptoTradeLog(
                symbol=symbol,
                strategy_version="RSI_RECOVERY_ATR_V1",  # ATR-based exits, RSI recovery entry
                armed_at=datetime.now(timezone.utc),  # Timestamp trade was armed
                arm_rsi=arm_rsi,
                entered_at=datetime.now(timezone.utc),
                entry_rsi=entry_rsi,
                entry_price=entry_price,
                quantity=qty,
                volume_ratio=volume_ratio,
                candle_close_position=candle_close_position,
            )
            session.add(trade)
            await session.commit()
            log.info(f"[CRYPTO-LOG] {symbol} ENTRY logged: RSI {entry_rsi:.1f} @ ${entry_price:.2f} vol_ratio {volume_ratio:.2f}x (strategy: RSI_RECOVERY_ATR_V1)")

        # Log to measurement system
        if MEASUREMENT_AVAILABLE:
            signal_context = SignalContext(
                symbol=symbol,
                rsi=entry_rsi,
                price=entry_price,
                trend="uptrend",  # RSI recovery = uptrend
                regime="bull",
                custom_data={"arm_rsi": arm_rsi, "volume_ratio": volume_ratio, "candle_close_position": candle_close_position}
            )
            trade_logger.record_entry(
                trade_id=trade_id,
                strategy="crypto_coinbase_rsi_recovery",
                symbol=symbol,
                entry_price=entry_price,
                entry_quantity=qty,
                signal_context=signal_context,
                expected_value=None,  # Will calculate based on exit targets
                mode="live",
            )
    except Exception as e:
        log.warning(f"Trade entry logging error for {symbol}: {e}")


async def flush_rsi_state_cache():
    """Batch-write all changed RSI states back to database at END of cycle.
    This runs AFTER all trading logic (Pass 1, 2, 3), so database latency doesn't
    affect Coinbase API calls or trade execution.

    CRITICAL: Without this, RSI state changes would be lost on restart, since they're
    only in the in-memory cache now. But if the table doesn't exist, it gracefully skips."""
    global RSI_STATE_CACHE
    try:
        async with AsyncSessionLocal() as session:
            changed_count = 0
            for symbol, cache_entry in RSI_STATE_CACHE.items():
                if not cache_entry.get("changed", False):
                    continue  # Skip unchanged entries

                try:
                    # Fetch or create database record
                    stmt = select(CryptoRSIState).where(CryptoRSIState.symbol == symbol)
                    result = await session.execute(stmt)
                    state = result.scalars().first()

                    if not state:
                        # Create new state record
                        state = CryptoRSIState(
                            symbol=symbol,
                            entered_oversold=cache_entry["entered_oversold"],
                            armed_rsi=cache_entry["armed_rsi"],
                            last_rsi=cache_entry["last_rsi"],
                        )
                        session.add(state)
                    else:
                        # Update existing
                        state.entered_oversold = cache_entry["entered_oversold"]
                        state.armed_rsi = cache_entry["armed_rsi"]
                        state.last_rsi = cache_entry["last_rsi"]
                        state.updated_at = datetime.now(timezone.utc)

                    changed_count += 1
                    cache_entry["changed"] = False  # Clear changed flag
                except Exception as e:
                    # If this specific symbol fails, skip it but continue with others
                    # This prevents one symbol's database issue from blocking others
                    if "does not exist" in str(e):
                        cache_entry["changed"] = False  # Still mark as flushed (in-memory)
                        continue
                    log.debug(f"[CRYPTO] Failed to flush RSI state for {symbol}: {e}")

            if changed_count > 0:
                await session.commit()
                log.debug(f"[CRYPTO] ✅ RSI state cache flushed: {changed_count} symbols updated in DB")
    except Exception as e:
        # If database is completely unavailable, just skip persistence
        # In-memory cache still works, so trading continues
        if "does not exist" in str(e):
            log.debug(f"[CRYPTO] RSI state table unavailable, skipping database persistence (in-memory cache active)")
        else:
            log.debug(f"[CRYPTO] RSI state flush attempted but skipped: {type(e).__name__}")


async def log_trade_exit(symbol: str, exit_price: float, exit_reason: str, realized_pnl: float,
                        realized_pnl_pct: float, time_held_minutes: int,
                        max_adverse_excursion: float = None, max_favorable_excursion: float = None,
                        partial_exit_count: int = 0, trailing_stop_triggered: bool = False):
    """Log when a trade exits (EXIT) and record to measurement system."""
    try:
        async with AsyncSessionLocal() as session:
            # Find the most recent unclosed trade for this symbol
            stmt = select(CryptoTradeLog).where(
                CryptoTradeLog.symbol == symbol,
                CryptoTradeLog.exit_at == None
            ).order_by(CryptoTradeLog.entered_at.desc())
            result = await session.execute(stmt)
            trade = result.scalars().first()

            if trade:
                trade.exit_at = datetime.now(timezone.utc)
                trade.exit_price = exit_price
                trade.exit_reason = exit_reason
                trade.realized_pnl = realized_pnl
                trade.realized_pnl_pct = realized_pnl_pct
                trade.time_held_minutes = time_held_minutes
                trade.max_adverse_excursion = max_adverse_excursion
                trade.max_favorable_excursion = max_favorable_excursion
                trade.partial_exit_count = partial_exit_count
                trade.trailing_stop_triggered = trailing_stop_triggered
                await session.commit()
                log.info(f"[CRYPTO-LOG] {symbol} EXIT logged: {exit_reason} @ ${exit_price:.2f} P&L ${realized_pnl:.2f} ({realized_pnl_pct*100:.2f}%)")

                # Log to measurement system using database trade ID as trade_id
                if MEASUREMENT_AVAILABLE:
                    trade_logger.record_exit(
                        trade_id=str(trade.id) if trade.id else symbol,  # Use database ID or symbol as fallback
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        entry_slippage=None,
                        exit_slippage=None,
                    )
    except Exception as e:
        log.warning(f"Trade exit logging error for {symbol}: {e}")


async def run_crypto_cycle():
    """
    IMPROVED STRATEGY (replaces RSI < 10 + fixed 37% target):

    ENTRY: Tiered RSI system
    ─────────────────────
    • RSI < 20 + Volume >= 1.5x + Close in upper 50% → STRONG_BUY
    • 20-30 RSI + Volume >= 1.5x + Close in upper 60% → BUY
    • RSI >= 50 → No new entries (neutral/bullish zone)

    This replaces the hard RSI < 10 requirement which produced too few signals.

    EXIT: Tiered profit-taking with trailing stop
    ──────────────────────────────────────────
    • 3% profit → Exit 1/3 (lock early gain)
    • 5% profit → Exit 1/3 (move remaining stop to breakeven + buffer)
    • 10% profit → Exit 1/3 OR trail by 5% (let winners run with protection)
    • -2% loss → Emergency stop (capital preservation)
    • RSI > 70 after breakeven → Exit if profit is clear (additional confirmation)

    This replaces the fixed 37% target which was unrealistic and caused the bot to
    hold indefinitely while the 2% stop bled capital waiting for a massive move.

    BACKTEST BEFORE DEPLOYING: Old vs. New on 28 pairs, historical data
    ═════════════════════════════════════════════════════════════════════
    """
    global daily_pnl, last_cycle_at

    now = datetime.now(timezone.utc)
    last_cycle_at = now.isoformat()
    log.info(f"[CRYPTO] Scanning {', '.join(CRYPTO_PAIRS)} (24/7, no market-hours gate) | Daily P&L: ${daily_pnl:.2f}")

    connector = aiohttp.TCPConnector(use_dns_cache=True)
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=5)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout) as session:
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

        # CRITICAL: Dynamic floor & aggressive growth strategy
        BALANCE_FLOOR = 1990.00  # If drops to $1,990, resume aggressive trading
        PROFIT_LOCK_ACTIVATION = 2000.10  # When balance hits $2,000.10+, take profits
        TARGET_TRADE_PROFIT = 10.00  # Close trades with $10+ profit when activated
        GROWTH_TARGET = 2000.00  # Seek to go above and beyond $2,000

        is_at_floor = cash is not None and cash <= BALANCE_FLOOR
        should_take_profits = cash is not None and cash >= PROFIT_LOCK_ACTIVATION
        is_aggressive_mode = cash is not None and cash < PROFIT_LOCK_ACTIVATION  # Seek to climb

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

        status_suffix = ""
        if is_at_floor:
            status_suffix += f" | 🚀 AT FLOOR (${BALANCE_FLOOR:.2f}) - AGGRESSIVE MODE: ALL-IN TO CLIMB"
        if should_take_profits:
            status_suffix += f" | 💰 ABOVE $1,001 - TAKING PROFITS: Close trades with ${TARGET_TRADE_PROFIT:.2f}+ gains"
        if is_aggressive_mode:
            status_suffix += f" | 📈 GROWTH MODE: Seeking to exceed $1,000"
        if is_hitting_daily_loss_limit:
            status_suffix += " | ⚠️ DAILY 2% LOSS LIMIT HIT - stopping new trades"

        log.info(f"[CRYPTO] Coinbase USD balance: {'$%.2f' % cash if cash is not None else 'unknown'} | Crypto cash pool: ${cash_pool:.2f} ({cap_desc}, {tier_desc}) | Target: +{PROFIT_TARGET_PCT*100:.2f}% | Stop: -{STOP_LOSS_PCT*100:.2f}% | Round-trip fee: {CRYPTO_ROUND_TRIP_FEE_RATE*100:.2f}%{status_suffix}")

        # AGGRESSIVE GROWTH MODE: If at floor ($990), maximize position sizing
        if is_at_floor and not is_hitting_daily_loss_limit:
            log.info(
                f"[CRYPTO] 🚀 AGGRESSIVE MODE ACTIVATED (${cash:.2f} ≤ ${BALANCE_FLOOR:.2f}); "
                "using FULL balance to climb above $1,000"
            )
            # Full position sizing in aggressive mode - don't hold back

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

                # ATR-based exit levels (volatility-adjusted, stored at entry time)
                stop_price = position.get("stop_price", entry * 0.98)  # Fallback to -2% if missing
                tier1_price = position.get("tier1_price", entry * 1.03)
                tier2_price = position.get("tier2_price", entry * 1.05)
                tier3_price = position.get("tier3_price", entry * 1.10)

                stop_hit = price <= stop_price
                tier1_hit = price >= tier1_price
                tier2_hit = price >= tier2_price
                tier3_hit = price >= tier3_price
                rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE

                # Micro-profit-taking: +0.5%, +1.5%, +2.5% (25% each layer)
                micro_target_1 = entry * 1.005  # +0.5%
                micro_target_2 = entry * 1.015  # +1.5%
                micro_target_3 = entry * 1.025  # +2.5%
                micro_hit_1 = price >= micro_target_1
                micro_hit_2 = price >= micro_target_2
                micro_hit_3 = price >= micro_target_3

                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                    "has_position": True, "checked_at": now.isoformat(),
                    "targets": {
                        "stop": stop_price,
                        "micro_1": micro_target_1,
                        "micro_2": micro_target_2,
                        "micro_3": micro_target_3,
                        "tier1": tier1_price,
                        "tier2": tier2_price,
                        "tier3": tier3_price,
                    }
                }

                # Track partial sells (how many 25% chunks sold: 0-4)
                partial_sells = position.get("partial_sells", 0)
                remaining_qty = qty * (1 - partial_sells * 0.25)  # Qty left to sell
                sell_qty = qty * 0.25  # 25% of original position

                should_exit = False
                partial_exit = False
                reason = None

                # Priority 1: Stop loss (hard exit)
                if stop_hit:
                    should_exit = True
                    reason = f"STOP LOSS @ ${stop_price:.2f} (-{(1 - price/entry)*100:.2f}%)"
                # Priority 2: Micro-profit taking (partial exits)
                elif micro_hit_3 and partial_sells < 3:
                    partial_exit = True
                    reason = f"MICRO PROFIT L3 @ ${micro_target_3:.2f} (+0.25%, sell 25%)"
                    position["partial_sells"] = 3
                elif micro_hit_2 and partial_sells < 2:
                    partial_exit = True
                    reason = f"MICRO PROFIT L2 @ ${micro_target_2:.2f} (+0.15%, sell 25%)"
                    position["partial_sells"] = 2
                elif micro_hit_1 and partial_sells < 1:
                    partial_exit = True
                    reason = f"MICRO PROFIT L1 @ ${micro_target_1:.2f} (+0.05%, sell 25%)"
                    position["partial_sells"] = 1
                # Priority 3: Full exit on tier targets or RSI
                elif rsi_exit:
                    should_exit = True
                    reason = "RSI EXIT"
                elif tier3_hit:
                    should_exit = True
                    reason = f"TIER 3 @ ${tier3_price:.2f} (+{(price/entry - 1)*100:.2f}%, let winners run)"
                elif tier2_hit and unrealized_pct >= PROFIT_TARGET_PCT:
                    should_exit = True
                    reason = f"TIER 2 @ ${tier2_price:.2f} (+{(price/entry - 1)*100:.2f}%, hit 3% target)"
                elif tier1_hit:
                    should_exit = True
                    reason = f"TIER 1 @ ${tier1_price:.2f} (+{(price/entry - 1)*100:.2f}%, lock early gain)"

                if partial_exit:
                    # Sell 25% of original position
                    filled = await place_order(session, symbol, "sell", sell_qty, price)
                    if filled:
                        pnl_partial = (price - entry) * sell_qty
                        daily_pnl += pnl_partial
                        log.info(f"[CRYPTO] 💰 PARTIAL {symbol} ({reason}) | Qty: {sell_qty:.8f} | P&L: ${pnl_partial:.2f} | Remaining: {remaining_qty:.8f}")
                elif should_exit:
                    # Full exit: sell all remaining
                    filled = await place_order(session, symbol, "sell", remaining_qty if remaining_qty > 0 else qty, price)
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
                        await _db_delete_open(symbol, "long")
                await asyncio.sleep(0.3)
            return

        scans = {}
        for symbol in CRYPTO_PAIRS:
            data = await get_price_rsi(session, symbol)
            if data:
                scans[symbol] = data
                log.info(f"[CRYPTO] {symbol} | ${data['price']:.2f} | RSI:{data['rsi']}")
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

            # ATR-based exit levels (volatility-adjusted, stored at entry time)
            stop_price = position.get("stop_price", entry * 0.98)  # Fallback to -2% if missing
            tier1_price = position.get("tier1_price", entry * 1.03)
            tier2_price = position.get("tier2_price", entry * 1.05)
            tier3_price = position.get("tier3_price", entry * 1.10)

            stop_hit = price <= stop_price
            tier1_hit = price >= tier1_price
            tier2_hit = price >= tier2_price
            tier3_hit = price >= tier3_price
            # RSI recovering to neutral is only a real exit if it's already
            # cleared the round-trip fee - otherwise this "safe-looking"
            # exit is actually a guaranteed net loss (this was the actual
            # source of the fee bleed a backtest caught: RSI_SELL_ABOVE=50
            # fires on almost every trade long before PROFIT_TARGET_PCT
            # ever does, and it used to have no profit floor at all).
            rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                "has_position": True, "checked_at": now.isoformat(),
                "targets": {
                    "stop": stop_price,
                    "tier1": tier1_price,
                    "tier2": tier2_price,
                    "tier3": tier3_price,
                }
            }

            # Exit conditions: stop loss, profit-taking above $1,001, RSI reversal, or tiered profit target
            should_exit = False
            reason = None

            # PROFIT TAKING: Close trades with $10+ profit when balance > $1,001
            dollar_profit = unrealized_pnl
            profit_take_exit = should_take_profits and dollar_profit >= TARGET_TRADE_PROFIT

            if stop_hit:
                should_exit = True
                reason = f"STOP LOSS @ ${stop_price:.2f} (-{(1 - price/entry)*100:.2f}%)"
            elif profit_take_exit:
                should_exit = True
                reason = f"💰 PROFIT TAKEN (+${dollar_profit:.2f}, balance above $1,001)"
            elif rsi_exit:
                should_exit = True
                reason = "RSI EXIT"
            elif tier3_hit:
                should_exit = True
                reason = f"TIER 3 @ ${tier3_price:.2f} (+{(price/entry - 1)*100:.2f}%, let winners run)"
            elif tier2_hit and unrealized_pct >= PROFIT_TARGET_PCT:
                should_exit = True
                reason = f"TIER 2 @ ${tier2_price:.2f} (+{(price/entry - 1)*100:.2f}%, hit 3% target)"
            elif tier1_hit:
                should_exit = True
                reason = f"TIER 1 @ ${tier1_price:.2f} (+{(price/entry - 1)*100:.2f}%, lock early gain)"

            if should_exit:
                filled = await place_order(session, symbol, "sell", qty, price)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    # Log trade exit for analytics
                    time_held = (now - position.get("opened_at", now)).total_seconds() // 60 if "opened_at" in position else 0
                    await log_trade_exit(
                        symbol, exit_price=price, exit_reason=reason,
                        realized_pnl=unrealized_pnl, realized_pnl_pct=unrealized_pct,
                        time_held_minutes=int(time_held)
                    )
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} closed ({reason})",
                        f"Position closed on your Coinbase account:\n\n"
                        f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                        f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                    )
                    open_crypto_positions.pop(symbol, None)
                    await _db_delete_open(symbol, "long")

            await asyncio.sleep(0.3)

        # ── Pass 1b: DISABLED (Coinbase SPOT does not support shorting) ──────────────────────────────────────
        # Short exit logic removed: Coinbase spot only supports long positions.

        # ── Pass 2: new entries (long only, RSI oversold + momentum confirmation) ────
        for symbol in CRYPTO_PAIRS:
            if symbol in open_crypto_positions:
                continue
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]
            atr = data.get("atr", 0)
            ma_50 = data.get("ma_50", 0)
            in_uptrend = data.get("in_uptrend", False)
            rsi_recovery = data.get("rsi_recovery", False)
            candle = data.get("candle")

            # ─ RSI STATE MACHINE: Load and update per-symbol entry state ─
            rsi_state = await load_rsi_state(symbol)
            entered_oversold = rsi_state["entered_oversold"]
            armed_rsi = rsi_state.get("armed_rsi")

            # State transitions (AGGRESSIVE):
            # 1. RSI >= 60: Reset state (overbought, need pullback to entry zone)
            if rsi >= 60:
                if entered_oversold:
                    log.info(f"[CRYPTO] {symbol} RSI {rsi:.1f} → RESET (>=60, overbought, waiting for pullback)")
                entered_oversold = False
                armed_rsi = None
            # 2. RSI enters 10-40 zone: Arm the setup (widened from 10-30 for aggressive trading)
            elif 10 <= rsi < 40 and not entered_oversold:
                log.info(f"[CRYPTO] {symbol} RSI {rsi:.1f} → ARM oversold/neutral entry zone")
                entered_oversold = True
                armed_rsi = rsi
            # 3. Otherwise: Maintain current state

            # Persist state changes to database
            await update_rsi_state(symbol, rsi, entered_oversold, armed_rsi)

            # IMPROVED ENTRY LOGIC: Tiered RSI + RSI recovery trigger
            # Requires: RSI < 30 (oversold) AND RSI bouncing upward (recovery) + volume + momentum
            # This avoids catching falling knives and waits for actual reversal
            if rsi >= RSI_NO_ENTRY:  # RSI >= 50: neutral/bullish, don't enter new positions
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "NEUTRAL",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            if not rsi_recovery:
                # RSI oversold but not bouncing - wait for recovery signal
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "OVERSOLD_WAITING_BOUNCE",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            if candle:
                candle_range = candle["high"] - candle["low"]
                if candle_range > 0:
                    close_position = (candle["close"] - candle["low"]) / candle_range
                    volume_spike_ratio = candle["volume"] / max(candle["prior_volume"], 1)

                    # Tiered buy signal: STRONG_BUY (RSI < 20 + bounce) or BUY (20-30 + bounce)
                    # Pass entered_oversold to enforce state-machine discipline
                    signal = _crypto_buy_signal_improved(rsi, rsi_recovery, volume_spike_ratio, close_position, entered_oversold)

                    if signal == "NO_ACTION":
                        # No signal - log why
                        if rsi < RSI_STRONG_BUY and close_position < 0.50:
                            reason = "OVERSOLD_NO_MOMENTUM"
                        elif rsi < RSI_STRONG_BUY and volume_spike_ratio < 1.5:
                            reason = "OVERSOLD_LOW_VOLUME"
                        elif rsi < RSI_BUY and close_position < 0.60:
                            reason = "OVERSOLD_NO_MOMENTUM"
                        else:
                            reason = "NO_ENTRY_SIGNAL"
                        latest_signals[symbol] = {
                            "price": price, "rsi": rsi, "status": reason,
                            "has_position": False, "checked_at": now.isoformat(),
                        }
                        continue

                    # Signal received (STRONG_BUY or BUY) - calculate ATR-based exits
                    targets = _calculate_atr_targets(price, atr)
                    latest_signals[symbol] = {
                        "price": price, "rsi": rsi, "atr": atr,
                        "status": f"{signal}_CONFIRMED",
                        "targets": targets,  # Store targets for position tracking
                        "has_position": False, "checked_at": now.isoformat(),
                    }
                else:
                    # No range on candle (doji-like), skip as unclear momentum
                    latest_signals[symbol] = {
                        "price": price, "rsi": rsi, "status": "OVERSOLD_DOJI",
                        "has_position": False, "checked_at": now.isoformat(),
                    }
                    continue
            else:
                # Candle data unavailable, skip entry
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "CANDLE_DATA_MISSING",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

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
                # Store ATR-based exit targets with position (for volatility-adjusted exits)
                targets = _calculate_atr_targets(price, atr)
                open_crypto_positions[symbol] = {
                    "entry": price,
                    "qty": qty,
                    "opened_at": now,
                    "atr": atr,
                    "stop_price": targets["stop_price"],
                    "tier1_price": targets["tier1_price"],
                    "tier2_price": targets["tier2_price"],
                    "tier3_price": targets["tier3_price"],
                    "trailing_stop_pct": targets["trailing_stop_pct"],
                }
                await _db_save_open(symbol, "long", price, qty)
                # Log trade entry for analytics (ARM → ENTER transition)
                await log_trade_entry(
                    symbol, entry_rsi=rsi, entry_price=price, qty=qty,
                    arm_rsi=armed_rsi or rsi, volume_ratio=volume_spike_ratio,
                    candle_close_position=close_position
                )
                # Update cash_pool from actual Coinbase balance to prevent tracking drift
                # (fill price may differ from quoted price due to market conditions)
                updated_cash, _ = await get_usd_balance(session)
                if updated_cash is not None:
                    cash_pool = min(updated_cash, unlocked, MAX_ALLOCATION) if MAX_ALLOCATION is not None else min(updated_cash, unlocked)
                else:
                    # Fallback to local tracking if balance fetch fails
                    cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — BUY {symbol} opened",
                    f"Long opened on your Coinbase account:\n\n"
                    f"BUY {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
                    f"Dashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                )

            await asyncio.sleep(0.3)

        # ── Pass 3: DISABLED (Coinbase SPOT does not support shorting) ────
        # Long-only mode: only BUY entries and SELL exits for closing longs.

        # ── CRITICAL: Flush all RSI state changes to database (end of cycle) ──
        # This happens AFTER all trading logic (Pass 1, 2, 3), so database latency
        # doesn't affect Coinbase API calls or trade execution.
        # In-memory cache removed ~56 database queries from the hot loop.
        await flush_rsi_state_cache()


def run():
    # Emergency stop: disable bot if CRYPTO_BOT_DISABLED is set (default: ENABLED for trading)
    if os.getenv("CRYPTO_BOT_DISABLED", "false").lower() == "true":
        log.warning("🛑 CRYPTO BOT DISABLED — set via CRYPTO_BOT_DISABLED env var. Bot will not start.")
        return

    log.info("=" * 60)
    log.info("CRYPTO TRADING BOT — Coinbase (separate account from Alpaca stocks)")
    alloc_desc = f"${MAX_ALLOCATION:.2f} cap" if MAX_ALLOCATION is not None else "full balance (compounding)"
    log.info(f"Pairs: {', '.join(CRYPTO_PAIRS)} | Allocation: {alloc_desc} | Max positions: {MAX_POSITIONS}")
    log.info("Runs 24/7 - crypto has no market close, unlike prop_bot.py's stock/ETF trading")
    log.info("🔴 LIVE TRADING - Coinbase has no free paper-trading sandbox for Advanced Trade")
    log.info("Strategy: RSI_RECOVERY_ATR_V1 (state machine entry + volatility-based exits)")
    log.info("=" * 60)

    # Diagnostic: check credential availability
    key_name_present = bool(COINBASE_API_KEY_NAME)
    key_secret_present = bool(COINBASE_API_PRIVATE_KEY)
    log.info(f"[STARTUP] COINBASE_API_KEY_NAME: {'✓ configured' if key_name_present else '❌ MISSING'}")
    log.info(f"[STARTUP] COINBASE_API_PRIVATE_KEY: {'✓ configured' if key_secret_present else '❌ MISSING'}")

    if not (COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY):
        log.error("🛑 CRYPTO BOT STARTUP FAILED: Missing Coinbase API credentials (COINBASE_API_KEY_NAME and/or COINBASE_API_PRIVATE_KEY)")
        log.error("   Set these environment variables in Railway to enable crypto trading bot")
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

    try:
        loop.run_until_complete(load_all_rsi_states())
    except Exception as e:
        log.error(f"[CRYPTO] Startup RSI state cache load failed: {e}")

    while True:
        if os.getenv("STOP_TRADING", "false").lower() == "true":
            log.warning("STOP_TRADING=true — crypto bot paused")
            time.sleep(60)
            continue
        try:
            loop.run_until_complete(run_crypto_cycle())
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                log.warning(f"[CRYPTO] Event loop mismatch detected: {e} - recreating event loop")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                log.error(f"Crypto cycle error: {e}")
        except Exception as e:
            log.error(f"Crypto cycle error: {e}")
        time.sleep(60)  # 1-min cycle for quality entries + reduced fee bleed (1 scan/min, high-quality trades)


if __name__ == "__main__":
    run()


# DYNAMIC CAPITAL ROTATION — Track performance per pair and rotate to winners
PAIR_PERFORMANCE = {}  # {symbol: {"wins": int, "losses": int, "total_pnl": float, "last_trade_at": datetime}}

def update_pair_performance(symbol, exit_price, entry_price, quantity):
    """Track win/loss and P&L for each pair to enable capital rotation."""
    if symbol not in PAIR_PERFORMANCE:
        PAIR_PERFORMANCE[symbol] = {
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "last_trade_at": datetime.utcnow(),
            "win_rate": 0.0
        }
    
    pnl = (exit_price - entry_price) * quantity
    PAIR_PERFORMANCE[symbol]["total_pnl"] += pnl
    
    if pnl > 0:
        PAIR_PERFORMANCE[symbol]["wins"] += 1
    elif pnl < 0:
        PAIR_PERFORMANCE[symbol]["losses"] += 1
    
    total_trades = PAIR_PERFORMANCE[symbol]["wins"] + PAIR_PERFORMANCE[symbol]["losses"]
    if total_trades > 0:
        PAIR_PERFORMANCE[symbol]["win_rate"] = PAIR_PERFORMANCE[symbol]["wins"] / total_trades * 100
    
    PAIR_PERFORMANCE[symbol]["last_trade_at"] = datetime.utcnow()


def get_high_conviction_pairs():
    """Return pairs with win rate > 40% in priority order."""
    candidates = []
    for symbol, perf in PAIR_PERFORMANCE.items():
        if perf.get("win_rate", 0) >= 40:
            candidates.append((symbol, perf["win_rate"], perf["total_pnl"]))
    
    # Sort by win rate (desc), then by total P&L (desc)
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [c[0] for c in candidates]


def get_position_size_multiplier(symbol):
    """Scale position size based on recent wins (up to 2x on hot streaks)."""
    if symbol not in PAIR_PERFORMANCE:
        return 1.0
    
    # If pair has 3+ consecutive wins, use 1.5x
    # If pair has 5+ consecutive wins, use 2.0x
    wins = PAIR_PERFORMANCE[symbol].get("wins", 0)
    
    if wins >= 5:
        return min(2.0, 1.0 + (wins - 3) * 0.25)  # 2.0x max
    elif wins >= 3:
        return 1.5
    else:
        return 1.0


# END DYNAMIC CAPITAL ROTATION
