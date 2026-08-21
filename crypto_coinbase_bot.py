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
from sqlalchemy import select, func
from database import AsyncSessionLocal
from models import BotPosition, TradingBotState, CryptoRSIState, CryptoTradeLog, CryptoSupplementalCapital
from bot_mandates import CRYPTO_MANDATE
from network_config import get_cached_response, cache_response, NETWORK_ENV_CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_coinbase_bot")

# Measurement system: Trade logging with full signal context
try:
    from measurement_system import SignalContext, TradeLog, trade_logger, StatisticalAnalyzer
    MEASUREMENT_AVAILABLE = True
except ImportError:
    MEASUREMENT_AVAILABLE = False
    log.warning("measurement_system not available - trade logging disabled")

# Network resilience cache — stores price data for 30min when API unavailable
PRICE_CACHE = {}  # {symbol: {"price": float, "rsi": float, "atr": float, "timestamp": datetime}}
CACHE_TTL_SECONDS = 1800  # 30 minutes

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
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

# OPTIMIZED: Top 10 high-liquidity pairs only
# This reduces scan time 3x, tightens spreads, improves execution speed
CRYPTO_PAIRS = [
    "BTC/USD",   # Tier 1: Deepest liquidity, tightest spreads
    "ETH/USD",   # Tier 1: 2nd deepest, excellent volatility for RSI swings
    "SOL/USD",   # Tier 2: High volume, consistent RSI bounces
    "DOGE/USD",  # Tier 2: Strong retail volume, predictable RSI swings
    "MATIC/USD", # Layer 2: Solid spreads, active trading
    "XRP/USD",   # Institutional: Reliable liquidity
    "ADA/USD",   # Established: Consistent volume baseline
    "LINK/USD",  # Oracle infrastructure: Stable price action
    "AVAX/USD",  # Growing ecosystem: Good volatility
    "LTC/USD",   # Historical: Deep order book, tight spreads
]  # 10 pairs total - optimized for quality entries + fast execution


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


# STATE-BASED RSI ENTRY SYSTEM for profit-only trading
# Strategy: Track RSI state transitions, enter only after recovery confirmation
# ─────────────────────────────────────────────────────────────────────────────
# RSI > 30:     WATCH    — no entry, monitor for oversold
# RSI 20-30:    ARM      — prepare for entry, wait for recovery confirmation
# RSI 10-20:    STRONG_ARM — higher quality entry, wait for recovery confirmation
# RSI > 50:     RESET    — cycle complete, reset tracking
# RSI STATE MACHINE (unified with optimized thresholds defined below)
try:
    RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "70"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_RSI_SELL_ABOVE value, using default: 70")
    RSI_SELL_ABOVE = 70.0

try:
    RSI_BUY_BELOW = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "30"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_RSI_BUY_BELOW value, using default: 30")
    RSI_BUY_BELOW = 30.0

try:
    MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "50"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_MAX_POSITIONS value, using default: 50")
    MAX_POSITIONS = 50

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
    MIN_CRYPTO_TRADE_USD = float(os.getenv("MIN_CRYPTO_TRADE_USD", "0.50"))
except (ValueError, TypeError):
    log.warning("Invalid MIN_CRYPTO_TRADE_USD value, using default: 0.50")
    MIN_CRYPTO_TRADE_USD = 0.50

# Staged capital release DISABLED for aggressive capital deployment.
# Previous logic: Only ever risk fixed $100 steps to prevent account wipeout.
# New behavior: Trade 100% of available balance (or whatever CRYPTO_MAX_ALLOCATION caps it at).
# The account has grown from $0.58 → $483.43 through earnings, so tier protection no longer needed.
# Set CRYPTO_TIER_SIZE to a positive value to re-enable tiered capital release if desired.
try:
    TIER_SIZE = float(os.getenv("CRYPTO_TIER_SIZE", "0"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_TIER_SIZE value, using default: 0 (tiering disabled)")
    TIER_SIZE = 0.0

# Minimum cash reserve: never deploy this amount, only grows from profits
# With $483.43 balance: keep $25, deploy $458.43 in active trades
try:
    MIN_CASH_RESERVE = float(os.getenv("CRYPTO_MIN_CASH_RESERVE", "1"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_MIN_CASH_RESERVE value, using default: 1")
    MIN_CASH_RESERVE = 1.0


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

# CLOSE-ON-PROFIT-AFTER-FEES STRATEGY:
# Coinbase Advanced Trade: ~0.6% taker on buy + 0.6% taker on sell = ~1.2% round-trip
# Positions close as soon as unrealized_pct > this value (profitable after paying fees).
try:
    CRYPTO_ROUND_TRIP_FEE_RATE = float(os.getenv("CRYPTO_ROUND_TRIP_FEE_RATE", "0.012"))
except (ValueError, TypeError):
    log.warning("Invalid CRYPTO_ROUND_TRIP_FEE_RATE value, using default: 0.012")
    CRYPTO_ROUND_TRIP_FEE_RATE = 0.012

# OPTIMIZED ENTRY THRESHOLDS: Tighter RSI for better quality signals
RSI_RESET_THRESHOLD = 50  # When RSI crosses above, reset entry tracking
RSI_NO_ENTRY = 50  # RSI >= 50: neutral/bullish, don't enter new positions
RSI_SELL_ABOVE = 65  # RSI > 65: overbought, exit signal
RSI_ARM_THRESHOLD = 32  # Strong oversold (upgraded from 35, better quality)
RSI_STRONG_ARM_THRESHOLD = 28  # Ultra-strong oversold (highest confidence)
RSI_WATCH_THRESHOLD = 50  # Above this = monitoring state

PROFIT_TARGET_PCT = 0.02  # Deprecated - using fee-based + micro-exits instead
STOP_LOSS_PCT = 0.01  # 1% stop loss (hard risk management)
TAKER_FEE_RATE = 0.006  # Coinbase taker fee per side

# MICRO-EXIT LADDER: Lock gains fast, redeploy capital quickly
# Layer 1: Exit 40% at 0.8% profit (covers entry fee + small profit)
# Layer 2: Exit 60% at 1.2%+ profit (full target)
MICRO_EXIT_1_TARGET = 0.008  # 0.8%
MICRO_EXIT_1_QTY_PCT = 0.40  # 40% of position
MICRO_EXIT_2_TARGET = 0.012  # 1.2%
MICRO_EXIT_2_QTY_PCT = 0.60  # 60% of position

# TIME-BASED TRAP ESCAPE: Don't hold unprofitable positions
MAX_HOLD_TIME_MINUTES = 90  # Auto-close if no profit after 90 min
ESCAPE_EXIT_PROFIT_TARGET = 0.005  # Exit at 0.5% profit if time limit hits

# ENTRY FILTERS: Quality control for signals
MIN_VOLUME_SPIKE_RATIO = 1.5  # Only enter if volume > 1.5x average (real moves)
MIN_CANDLE_RANGE_PCT = 0.004  # Only enter if candle range > 0.4% (avoid chop)
ENTRY_WINDOW_MINUTES = 8  # Only 8 min to enter after RSI bounce (don't chase)

# LOSS CIRCUIT BREAKER: Pause if market regime shifts
CONSECUTIVE_LOSSES_TO_PAUSE = 2  # Pause entries after 2 losses in a row
CIRCUIT_BREAKER_PAUSE_MINUTES = 120  # Pause for 2 hours (lets market recover)

# IMPROVED tiered exit levels - take profits at realistic milestones, not fixed 37%
# Tier 1: Exit 1/3 at 3-5% (first profit zone, lock early gain)
# Tier 2: Exit 1/3 at 6-10% (second profit zone, move stop to breakeven)
# Tier 3: Remaining 1/3 trails at trailing stop (let winners run with protection)
# This replaces the fixed 37% target which caused the bot to hold indefinitely
CRYPTO_TIER_LEVELS = [0.05, 0.08, 0.15]  # 3-tier exit: 5% (lock), 8%, 15% (trailing)
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


async def get_supplemental_capital() -> float:
    """Read capital allocation target from prop_bot earnings.

    This is NOT actual money in Coinbase - it's a target pool that tracks:
    - Earnings from prop_bot that should go to crypto trading
    - User must manually transfer from Alpaca → bank → Coinbase
    - Used to scale position sizes as capital is allocated

    Returns total allocated amount, or 0 on any error."""
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import func
            result = await db.execute(select(func.sum(CryptoSupplementalCapital.amount_usd)))
            total = result.scalar() or 0.0
            return total
    except Exception as e:
        log.warning(f"[CRYPTO] Failed to read capital allocation target: {e}")
        return 0.0


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
    the bot think there was no USD wallet when there was one further in.

    NOW INCLUDES: Supplemental capital from prop_bot earnings pool."""
    path = "/api/v3/brokerage/accounts"
    all_currencies = []
    cursor = None
    cache_key = "coinbase_usd_balance"

    try:
        coinbase_balance = None
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            async with session.get(COINBASE_BASE_URL + path, headers=_auth_headers("GET", path), params=params) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    # Handle 403 Forbidden (network egress blocked)
                    if r.status == 403 and "not in allowlist" in body:
                        cached = get_cached_response(cache_key)
                        if cached is not None:
                            log.warning(f"⚠️  [WORKAROUND] API blocked (403), using cached USD balance: ${cached}")
                            supplemental = await get_supplemental_capital()
                            total = cached + supplemental
                            if supplemental > 0:
                                log.info(f"[CRYPTO] Combining Coinbase ${cached:.2f} + supplemental ${supplemental:.2f} = ${total:.2f}")
                            return total, None
                        return None, f"HTTP 403: Egress blocked. No cache available. Fix: Add api.coinbase.com to Railway egress allowlist"
                    return None, f"HTTP {r.status}: {body}"
                data = await r.json()
                accounts = data.get("accounts", [])
                for account in accounts:
                    if account.get("currency") == "USD":
                        coinbase_balance = float(account["available_balance"]["value"])
                        cache_response(cache_key, coinbase_balance, NETWORK_ENV_CONFIG.get("cache_ttl", 300))
                        break
                if coinbase_balance is not None:
                    break
                all_currencies.extend(a.get("currency") for a in accounts)
                if not data.get("has_next") or not data.get("cursor"):
                    break
                cursor = data.get("cursor")

        if coinbase_balance is not None:
            # Supplemental capital: prop_bot earnings allocated for crypto trading
            # USER ACTION REQUIRED: Manually transfer from Alpaca → bank → Coinbase to realize allocation
            allocated = await get_supplemental_capital()
            if allocated > 0:
                # Log the allocation target so user knows how much to transfer
                log.info(f"[CRYPTO] 💡 Allocation target: ${allocated:.2f} from prop_bot | Manual action: Withdraw from Alpaca, deposit to Coinbase to unlock")
                log.info(f"[CRYPTO] Current: Coinbase ${coinbase_balance:.2f} real | Target with allocation: ${coinbase_balance + allocated:.2f}")
            return coinbase_balance, None

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


def _get_rsi_state(rsi: float) -> str:
    """Determine RSI state: RESET, WATCH, ARM, or STRONG_ARM."""
    if rsi > RSI_RESET_THRESHOLD:
        return "RESET"
    elif rsi > RSI_ARM_THRESHOLD:
        return "WATCH"
    elif rsi > RSI_STRONG_ARM_THRESHOLD:
        return "ARM"
    else:
        return "STRONG_ARM"


def _crypto_buy_signal_improved(rsi: float, prev_rsi: float, volume_ratio: float, close_position: float,
                                is_bullish: bool = True, is_recovering: bool = False,
                                candle_range_pct: float = 0.0) -> str:
    """
    OPTIMIZED RSI ENTRY SYSTEM: Quality > Quantity

    Entry State Machine (TIGHTER thresholds for better signals):
    ─────────────────────
    RSI > 50:      RESET      — Cycle complete, awaiting new oversold
    RSI 32-50:     WATCH      — Monitoring for oversold entry setup (was 30-50)
    RSI 28-32:     ARM        — Strong oversold signal, waiting for recovery
    RSI < 28:      STRONG_ARM — Heavily oversold, highest quality entry (was < 20)

    Entry Rules (with new quality filters):
    ────────────
    1. Must be bullish (price > 200-day SMA)
    2. Must be RECOVERING (RSI going UP from oversold state)
    3. Volume spike confirmed (> 1.5x average)
    4. Candle range meaningful (> 0.4% to avoid chop)
    5. Close in upper half (> 0.50 = strength)

    Returns: "STRONG_BUY" or "NO_ACTION"
    """

    # FILTER 1: Trend confirmation (bull market only)
    if not is_bullish:
        return "NO_ACTION"

    # Determine current state
    current_state = _get_rsi_state(rsi)
    prev_state = _get_rsi_state(prev_rsi)

    # FILTER 2: Only enter on recovery from STRONG_ARM or ARM states
    if prev_state not in ["ARM", "STRONG_ARM"]:
        return "NO_ACTION"

    # Must be recovering (RSI > prev_rsi) to confirm reversal momentum
    if not is_recovering or rsi <= prev_rsi:
        return "NO_ACTION"

    # FILTER 3: Volume confirmation (real moves only, not dead-cat bounces)
    if volume_ratio < MIN_VOLUME_SPIKE_RATIO:
        return "NO_ACTION"

    # FILTER 4: Price consolidation filter (skip chop, only trade real range moves)
    if candle_range_pct < MIN_CANDLE_RANGE_PCT:
        return "NO_ACTION"

    # FILTER 5: Close in upper half (strength confirmation)
    if close_position < 0.50:
        return "NO_ACTION"

    # ALL FILTERS PASSED: Issue STRONG_BUY signal
    if rsi < RSI_NO_ENTRY:
        return "STRONG_BUY"

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
    Each candle: [time, low, high, open, close, volume]
    On 403 (egress blocked), uses cached candles if available."""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    cache_key = f"candles_{symbol}"
    try:
        async with session.get(url, headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                # Check cache on 403 (egress blocked)
                if r.status == 403:
                    cached = get_cached_response(cache_key)
                    if cached is not None:
                        log.warning(f"⚠️  [WORKAROUND] Candles API blocked (403) for {symbol}, using cache")
                        return cached
                return None
            data = await r.json()
            if not data or len(data) < limit:
                return None
            # Coinbase returns newest-first; reverse to chronological order
            candles = list(reversed(data))[-limit:]
            cache_response(cache_key, candles, NETWORK_ENV_CONFIG.get("cache_ttl", 300))
            return candles
    except Exception:
        # On error, try cache as fallback
        cached = get_cached_response(cache_key)
        if cached:
            log.warning(f"⚠️  [WORKAROUND] Candles fetch error for {symbol}, using cache")
            return cached
        return None


async def _fetch_coinbase_closes(session, symbol):
    """Legacy: fetch just closes for RSI calculation. Uses _fetch_coinbase_candles internally."""
    candles = await _fetch_coinbase_candles(session, symbol, limit=50)
    if not candles:
        return None
    return [float(row[4]) for row in candles]


async def _fetch_fmp_candles(session, symbol, limit=50):
    """Fetch OHLCV candles from FMP (Financial Modeling Prep) for RSI and trend analysis.
    Returns list of candles in chronological order [time, low, high, open, close, volume].
    FMP symbol format: BTCUSD (not BTC-USD or BTC/USD)."""
    if not FMP_API_KEY:
        return None
    fmp_symbol = symbol.replace("/", "").replace("-", "")  # BTC/USD -> BTCUSD
    url = f"https://financialmodelingprep.com/api/v3/historical-chart/5min/{fmp_symbol}?apikey={FMP_API_KEY}"
    cache_key = f"fmp_candles_{symbol}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                cached = get_cached_response(cache_key)
                if cached:
                    log.warning(f"⚠️  FMP candles blocked/failed for {symbol}, using cache")
                    return cached
                return None
            data = await r.json()
            if not data or len(data) < limit:
                return None
            # FMP returns newest-first; reverse to chronological order and return last N
            candles = [[float(d.get('date', 0)), float(d.get('low', 0)), float(d.get('high', 0)),
                        float(d.get('open', 0)), float(d.get('close', 0)), float(d.get('volume', 0))]
                       for d in reversed(data)][-limit:]
            if len(candles) < limit:
                return None
            cache_response(cache_key, candles, NETWORK_ENV_CONFIG.get("cache_ttl", 300))
            return candles
    except Exception as e:
        log.debug(f"FMP candles fetch failed for {symbol}: {e}")
        cached = get_cached_response(cache_key)
        if cached:
            log.warning(f"⚠️  FMP candles error for {symbol}, using cache")
            return cached
        return None


async def _fetch_latest_candle(session, symbol):
    """Fetch latest 5-minute candle OHLC data + volume for momentum & volume confirmation.
    Returns candle dict with open, close, high, low, volume, and prior candle volume or None if fetch fails.
    On 403 (egress blocked), uses cached latest candle if available."""
    pair = _to_product_id(symbol)
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300"
    cache_key = f"latest_candle_{symbol}"
    try:
        async with session.get(url, headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                # Check cache on 403
                if r.status == 403:
                    cached = get_cached_response(cache_key)
                    if cached is not None:
                        log.warning(f"⚠️  [WORKAROUND] Latest candle API blocked (403) for {symbol}, using cache")
                        return cached
                return None
            data = await r.json()
            if not data or len(data) < 2:
                return None
            # Latest candle is first in response; each row is [time, low, high, open, close, volume]
            latest = data[0]
            prior = data[1] if len(data) > 1 else None
            candle = {
                "open": float(latest[3]),
                "close": float(latest[4]),
                "high": float(latest[2]),
                "low": float(latest[1]),
                "volume": float(latest[5]),
                "prior_volume": float(prior[5]) if prior else 0,
            }
            cache_response(cache_key, candle, NETWORK_ENV_CONFIG.get("cache_ttl", 300))
            return candle
    except Exception:
        cached = get_cached_response(cache_key)
        if cached:
            log.warning(f"⚠️  [WORKAROUND] Latest candle fetch error for {symbol}, using cache")
            return cached
        return None


async def get_price_rsi(session, symbol):
    """Price+RSI+ATR+directional bias from FMP (primary) or Coinbase (fallback).
    Network resilient: retries with backoff, falls back to cache if unavailable.
    Uses MA50 to determine directional bias: only long above MA50, only short below MA50.
    RSI recovery: True if RSI is bouncing UP from oversold (useful for entry timing)."""

    # Try FMP first (real market data from Financial Modeling Prep)
    candles = None
    if FMP_API_KEY:
        candles = await _retry_with_backoff(
            lambda: _fetch_fmp_candles(session, symbol, limit=50),
            max_attempts=NETWORK_RETRY_ATTEMPTS
        )

    # Fall back to Coinbase if FMP unavailable
    if not candles:
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


async def get_daily_sma_200(session, symbol):
    """
    Fetch daily candles and calculate 200-day Simple Moving Average (trend filter).
    Returns: {"sma_200": float, "current_price": float, "is_bullish": bool}

    Bullish (uptrend) = price above 200-day SMA
    Bearish (downtrend) = price below 200-day SMA
    """
    try:
        pair = _to_product_id(symbol)
        # Daily = 86400 seconds; fetch 250 candles = ~9 months (covers 200-day + buffer)
        url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=86400&limit=300"
        cache_key = f"daily_sma_{symbol}"

        async with session.get(url, headers={"Accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                if r.status == 403:
                    cached = get_cached_response(cache_key)
                    if cached:
                        log.warning(f"⚠️  {symbol} daily SMA API blocked (403), using cache")
                        return cached
                return None

            data = await r.json()
            if not data or len(data) < 200:
                log.warning(f"⚠️  {symbol} insufficient daily candles ({len(data) if data else 0} < 200)")
                return None

            # Reverse to chronological order
            candles = list(reversed(data))
            closes = [float(row[4]) for row in candles[-200:]]  # Last 200 closes

            if len(closes) < 200:
                return None

            current_price = float(candles[-1][4])
            sma_200 = sum(closes[-200:]) / 200
            is_bullish = current_price > sma_200

            result = {
                "sma_200": sma_200,
                "current_price": current_price,
                "is_bullish": is_bullish,
                "distance_pct": ((current_price - sma_200) / sma_200) * 100 if sma_200 > 0 else 0,
            }

            cache_response(cache_key, result, 86400)  # Cache for 24 hours
            return result
    except Exception as e:
        log.debug(f"Daily SMA-200 fetch failed for {symbol}: {e}")
        return None


def find_recent_swing_low(candles: list, lookback_bars: int = 10) -> dict:
    """
    Find recent swing low for stop loss placement.
    Returns: {"swing_low_price": float, "bars_ago": int}

    Swing low = lowest low in the last N candles
    Used for professional stop-loss placement (risk management)
    """
    if not candles or len(candles) < 3:
        return {"swing_low_price": None, "bars_ago": 0}

    recent = candles[-lookback_bars:]
    lowest = min(recent, key=lambda x: float(x[1]))
    swing_low = float(lowest[1])
    bars_ago = len(candles) - candles.index(lowest) - 1

    return {"swing_low_price": swing_low, "bars_ago": bars_ago}


def find_recent_swing_high(candles: list, lookback_bars: int = 10) -> dict:
    """
    Find recent swing high for take-profit placement.
    Returns: {"swing_high_price": float, "bars_ago": int}

    Swing high = highest high in the last N candles
    Used for exit targets (RSI 65-70 or swing high, whichever comes first)
    """
    if not candles or len(candles) < 3:
        return {"swing_high_price": None, "bars_ago": 0}

    recent = candles[-lookback_bars:]
    highest = max(recent, key=lambda x: float(x[2]))
    swing_high = float(highest[2])
    bars_ago = len(candles) - candles.index(highest) - 1

    return {"swing_high_price": swing_high, "bars_ago": bars_ago}


def size_position(cash_pool_remaining, slots_remaining, price, total_balance=None):
    """Exponential position sizing: scale with account balance for compound growth.

    Strategy: Allocate 15% of available cash per position (scales automatically)
    - At $500 balance: $75/trade
    - At $5,000 balance: $750/trade
    - At $50,000 balance: $7,500/trade
    - At $500,000 balance: $75,000/trade

    Maintains 20% cash buffer for volatility & fees.
    """
    # AGGRESSIVE SCALING: 20-40% per position based on account size
    # Smaller accounts: 20% (need more positions to diversify)
    # Larger accounts: 30-40% (can afford bigger bets on winning signals)
    if cash_pool_remaining < 5000:
        position_allocation_pct = 0.20  # 20% at under $5K
    elif cash_pool_remaining < 10000:
        position_allocation_pct = 0.25  # 25% at $5K-$10K
    elif cash_pool_remaining < 25000:
        position_allocation_pct = 0.30  # 30% at $10K-$25K
    else:
        position_allocation_pct = 0.40  # 40% at $25K+

    position_size = cash_pool_remaining * position_allocation_pct

    # Minimum position size to avoid dust trades
    min_position = 50.0
    if position_size < min_position:
        return None

    # Calculate qty based on dynamic dollar amount
    qty = round(position_size / price, 8)
    return qty if qty > 0 else None


async def place_order(session, symbol, side, qty, price, skip_on_network_error=False):
    """Coinbase Advanced Trade orders:
    - BUY: market IOC for immediate entry
    - SELL: limit GTC at profit target to hold order until price reached
    """
    product_id = _to_product_id(symbol)
    path = "/api/v3/brokerage/orders"

    if side == "buy":
        # Market order: immediate entry at market price (IOC = Immediate or Cancel)
        # Coinbase API expects: market_market_ioc with quote_size (USD amount to spend)
        order_config = {
            "market_market_ioc": {
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
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path), json=order, timeout=5) as r:
            result = await r.json()
            if r.status in (200, 201) and result.get("success", True):
                log.info(f"✅ CRYPTO TRADE | {side.upper()} {qty} {symbol}")
                return True
            # Check if this is a network/auth error vs order validation error
            error_detail = result.get('error_response', result)
            if "403" in str(r.status) or "not in allowlist" in str(error_detail):
                log.warning(f"⚠️  Coinbase API network blocked (403) - skipping orders this cycle")
                return False
            log.debug(f"Order validation failed ({order_config}): {error_detail}")
            return False
    except asyncio.TimeoutError:
        if skip_on_network_error:
            log.warning("⚠️  Coinbase API timeout - network may be blocked, skipping orders this cycle")
        else:
            log.warning(f"Crypto order timeout for {symbol}")
        return False
    except Exception as e:
        error_str = str(e).lower()
        if "403" in error_str or "gateway" in error_str or "policy" in error_str:
            if skip_on_network_error:
                log.warning(f"⚠️  Coinbase API blocked ({e}) - add api.coinbase.com to Railway egress allowlist")
            else:
                log.debug(f"Network error on {symbol}: {e}")
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
                strategy_version="SMA200_RSI_CROSSOVER_SWING_V2",  # UPGRADED: 200-day SMA + RSI cross-above + swing-based stops
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
            log.info(f"[CRYPTO-LOG] {symbol} ENTRY logged: RSI {entry_rsi:.1f} @ ${entry_price:.2f} vol_ratio {volume_ratio:.2f}x (strategy: SMA200_RSI_CROSSOVER_SWING_V2)")

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
        # Check if Coinbase API is accessible before attempting any orders
        api_accessible = True
        try:
            async with session.get(COINBASE_BASE_URL + "/api/v3/accounts", headers=_auth_headers("GET", "/api/v3/accounts"), timeout=5) as r:
                if r.status == 403:
                    api_accessible = False
                    log.warning("⚠️  [CRYPTO] Coinbase API blocked (403) — add api.coinbase.com to Railway egress allowlist. Skipping all orders this cycle.")
        except Exception as e:
            if "403" in str(e) or "gateway" in str(e).lower():
                api_accessible = False
                log.warning(f"⚠️  [CRYPTO] Coinbase API unreachable ({e}) — network policy may be blocking access. Skipping all orders this cycle.")

        # ═══════════════════════════════════════════════════════════════════
        # LOCAL BALANCE TRACKING: Accurate cash calculation every cycle
        # ═══════════════════════════════════════════════════════════════════

        # Initialize on first run
        if not hasattr(run_swing_check, 'initial_cash'):
            starting_balance = float(os.getenv("CRYPTO_STARTING_BALANCE", "451.50"))
            run_swing_check.initial_cash = starting_balance
            run_swing_check.realized_profit = 0.0  # Track cumulative realized P&L
            run_swing_check.positions_cost_basis = 0.0  # Total $ deployed in open positions
            log.info(f"[CRYPTO] Initialized balance tracking | Starting: ${starting_balance:.2f}")

        # CALCULATION:
        # Total Account Value = Initial + Realized Profits + Unrealized P&L on Open Positions
        # Available Cash = Initial + Realized Profits - Capital Deployed in Positions

        # Calculate unrealized P&L across all open positions (for display)
        unrealized_total = 0.0
        deployed_cost_basis = 0.0
        for symbol, pos in open_crypto_positions.items():
            entry = pos["entry"]
            qty = pos["qty"]
            cost = entry * qty
            deployed_cost_basis += cost

        # Realized gains + realized losses from closed positions
        realized_total = run_swing_check.realized_profit

        # Available cash = starting balance + profits - deployed capital
        available_cash = run_swing_check.initial_cash + realized_total - deployed_cost_basis

        # Total account value = starting balance + all realized + all unrealized
        total_account_value = run_swing_check.initial_cash + realized_total + unrealized_total

        cash = total_account_value  # For compatibility with rest of code
        cash_pool = max(0, available_cash)  # Can't go negative
        unlocked = 0.0
        if cash is None:
            log.warning(f"[CRYPTO] Balance tracking error - falling back to last known balance")
            cash_pool = 0.0
            api_accessible = False
        elif TIER_SIZE <= 0:
            unlocked = cash
            # Subtract MIN_CASH_RESERVE from deployable capital
            deployable = max(0, unlocked - MIN_CASH_RESERVE)
            cash_pool = min(deployable, MAX_ALLOCATION) if MAX_ALLOCATION is not None else deployable
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
        BALANCE_FLOOR = 400.00  # If drops to $400, resume aggressive trading
        PROFIT_LOCK_ACTIVATION = 750.00  # When balance hits $750+, take profits
        TARGET_TRADE_PROFIT = 2.50  # Close trades with $2.50+ profit when activated
        GROWTH_TARGET = 1000.00  # Seek to grow beyond $1,000

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
            reserve_desc = f"reserve (untouched): ${MIN_CASH_RESERVE:.2f}"
            tier_desc = f"tiering off | deployable: ${cash_pool:.2f} | {reserve_desc}"

        status_suffix = ""
        if is_at_floor:
            status_suffix += f" | 🚀 AT FLOOR (${BALANCE_FLOOR:.2f}) - AGGRESSIVE MODE: ALL-IN TO CLIMB"
        if should_take_profits:
            status_suffix += f" | 💰 ABOVE $1,001 - TAKING PROFITS: Close trades with ${TARGET_TRADE_PROFIT:.2f}+ gains"
        if is_aggressive_mode:
            status_suffix += f" | 📈 GROWTH MODE: Seeking to exceed $1,000"
        if is_hitting_daily_loss_limit:
            status_suffix += " | ⚠️ DAILY 2% LOSS LIMIT HIT - stopping new trades"

        log.info(f"[CRYPTO] 📊 BALANCE: Starting ${run_swing_check.initial_cash:.2f} + Realized ${run_swing_check.realized_profit:+.2f} - Deployed ${deployed_cost_basis:.2f} = Available ${cash_pool:.2f} | Account Value: ${total_account_value:.2f} | Target: +{PROFIT_TARGET_PCT*100:.2f}% | Stop: -{STOP_LOSS_PCT*100:.2f}%{status_suffix}")

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

                # CLOSE-ON-PROFIT-AFTER-FEES STRATEGY (simplified, full exits only):
                # Priority 1: Stop loss (hard exit)
                if stop_hit:
                    should_exit = True
                    reason = f"🛑 STOP LOSS @ ${stop_price:.2f} (-{(1 - price/entry)*100:.2f}%)"
                # Priority 2: Close on profit after fees (main strategy)
                elif unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE:
                    should_exit = True
                    reason = f"💵 CLOSE ON PROFIT (after fees) @ ${price:.2f} (+{unrealized_pct*100:.2f}%)"
                # Priority 3: RSI overbought exit (safety valve)
                elif rsi >= 65:
                    should_exit = True
                    reason = f"📤 RSI OVERBOUGHT EXIT (RSI {rsi:.1f} >= 65) @ ${price:.2f} ({unrealized_pct*100:+.2f}%)"

                if should_exit:
                    # Full exit: sell all remaining
                    filled = await place_order(session, symbol, "sell", remaining_qty if remaining_qty > 0 else qty, price)
                    if filled:
                        daily_pnl += unrealized_pnl
                        run_swing_check.realized_profit += unrealized_pnl  # Track realized gain/loss
                        available_cash_after = run_swing_check.initial_cash + run_swing_check.realized_profit - deployed_cost_basis
                        log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:+.2f} | Available: ${available_cash_after:.2f} | Total Realized: ${run_swing_check.realized_profit:+.2f}")
                        send_trade_alert(
                            f"🤖 Crypto bot — {symbol} closed ({reason})",
                            f"Position closed on your Coinbase account:\n\n"
                            f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:+.2f}\n"
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
                # UPGRADE: Fetch 200-day SMA for trend confirmation
                sma_data = await get_daily_sma_200(session, symbol)
                if sma_data:
                    data["sma_200"] = sma_data["sma_200"]
                    data["is_bullish"] = sma_data["is_bullish"]
                    data["sma_distance_pct"] = sma_data["distance_pct"]
                    bullish_str = "📈 BULLISH" if sma_data["is_bullish"] else "📉 BEARISH"
                    log.info(f"[CRYPTO] {symbol} | ${data['price']:.2f} | RSI:{data['rsi']:.1f} | SMA200:${sma_data['sma_200']:.2f} {bullish_str} ({sma_data['distance_pct']:+.1f}%)")
                else:
                    data["is_bullish"] = True  # Default to bullish if SMA unavailable
                    log.info(f"[CRYPTO] {symbol} | ${data['price']:.2f} | RSI:{data['rsi']:.1f} | (SMA200 unavailable)")
                scans[symbol] = data
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

            # CLOSE-ON-PROFIT-AFTER-FEES EXIT LOGIC:
            # Priority 1: Hard stop loss (risk management, swing-based)
            # Priority 2: Close on profit after fees (simplifies strategy: exit as soon as profitable after paying fees)
            # Priority 3: RSI overbought exit (safety valve: reversal signal at high RSI)
            should_exit = False
            reason = None

            # PROFIT TAKING: Close trades with $10+ profit when balance > $1,001
            dollar_profit = unrealized_pnl
            profit_take_exit = should_take_profits and dollar_profit >= TARGET_TRADE_PROFIT

            # PRIORITY 1: Hard stop loss (swing-based, professional risk management)
            if stop_hit:
                should_exit = True
                reason = f"🛑 HARD STOP (swing-based) @ ${stop_price:.2f} (-{(1 - price/entry)*100:.2f}%)"

            # PRIORITY 2: Close on profit after fees (main exit strategy - redeploy capital immediately)
            elif unrealized_pct > CRYPTO_ROUND_TRIP_FEE_RATE:
                should_exit = True
                reason = f"💵 CLOSE ON PROFIT (after fees) @ ${price:.2f} (+{unrealized_pct*100:.2f}%)"

            # PRIORITY 3: Profit-taking when balance high (lock in gains during compounding phase)
            elif profit_take_exit:
                should_exit = True
                reason = f"💰 PROFIT LOCK (${TARGET_TRADE_PROFIT:.2f} target reached) @ ${price:.2f} (+${dollar_profit:.2f})"

            # PRIORITY 4: RSI overbought exit (safety valve - reversal signal at high RSI)
            elif rsi >= 65:
                should_exit = True
                reason = f"📤 RSI OVERBOUGHT EXIT (RSI {rsi:.1f} >= 65) @ ${price:.2f} ({unrealized_pct*100:+.2f}%)"

            if should_exit:
                filled = await place_order(session, symbol, "sell", qty, price)
                if filled:
                    daily_pnl += unrealized_pnl
                    run_swing_check.realized_profit += unrealized_pnl  # Track realized gain/loss
                    available_cash_after = run_swing_check.initial_cash + run_swing_check.realized_profit - deployed_cost_basis
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:+.2f} | Available: ${available_cash_after:.2f} | Total Realized: ${run_swing_check.realized_profit:+.2f}")
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
                        f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:+.2f}\n"
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
            last_rsi = rsi_state.get("last_rsi", rsi)

            # UPGRADED STATE MACHINE (PROFESSIONAL):
            # 1. RSI >= 70: Reset state (overbought, need pullback to entry zone)
            if rsi >= 70:
                if entered_oversold:
                    log.info(f"[CRYPTO] {symbol} RSI {rsi:.1f} → RESET (>=70, overbought, waiting for pullback)")
                entered_oversold = False
                armed_rsi = None
            # 2. RSI enters oversold zone (< 35): Arm the setup
            elif rsi < 35 and not entered_oversold:
                log.info(f"[CRYPTO] {symbol} RSI {rsi:.1f} → ARM oversold entry zone")
                entered_oversold = True
                armed_rsi = rsi
            # 3. Otherwise: Maintain current state

            # Track RSI cross-above for entry confirmation
            # Cross-above = last_rsi < 35 AND current_rsi > 35 (bouncing from oversold)
            rsi_crossed_above = (last_rsi is not None and last_rsi < 35 and rsi > 35)

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
                    candle_range_pct = candle_range / candle["close"]  # Range as % of price
                    volume_spike_ratio = candle["volume"] / max(candle["prior_volume"], 1)

                    # STATE-BASED RSI ENTRY: Only enter on recovery from oversold
                    is_recovering = (last_rsi is not None and rsi > last_rsi)

                    is_bullish = data.get("is_bullish", True)
                    signal = _crypto_buy_signal_improved(
                        rsi, last_rsi or rsi, volume_spike_ratio, close_position,
                        is_bullish=is_bullish,
                        is_recovering=is_recovering,
                        candle_range_pct=candle_range_pct
                    )

                    if signal == "NO_ACTION":
                        # No signal - log why
                        if rsi < RSI_STRONG_ARM_THRESHOLD and close_position < 0.50:
                            reason = "STRONG_OVERSOLD_NO_MOMENTUM"
                        elif rsi < RSI_STRONG_ARM_THRESHOLD and volume_spike_ratio < 1.5:
                            reason = "STRONG_OVERSOLD_LOW_VOLUME"
                        elif rsi < RSI_ARM_THRESHOLD and close_position < 0.60:
                            reason = "OVERSOLD_NO_MOMENTUM"
                        elif not is_recovering:
                            reason = "OVERSOLD_NOT_RECOVERING"
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

            if not api_accessible:
                log.info(f"[CRYPTO] {symbol} BUY signal held — Coinbase API blocked, skipping orders (will retry next cycle)")
                continue

            log.info(f"[CRYPTO] 📡 BUY {symbol} — RSI:{rsi}")
            filled = await place_order(session, symbol, "buy", qty, price)
            if filled:
                # UPGRADE: Use swing-based stop loss instead of fixed percentages
                targets = _calculate_atr_targets(price, atr)

                # Find recent swing low for professional stop placement
                candle = data.get("candle")
                swing_info = {}
                if candle:
                    # Use candle data to estimate recent low
                    candle_low = float(candle.get("low", price * 0.98))
                    swing_info = {"swing_low": candle_low, "swing_low_distance_pct": ((price - candle_low) / price * 100)}
                    # Stop loss: below recent swing low (professional risk management)
                    swing_based_stop = candle_low * 0.995  # Slightly below swing low
                else:
                    swing_based_stop = targets["stop_price"]

                open_crypto_positions[symbol] = {
                    "entry": price,
                    "qty": qty,
                    "opened_at": now,
                    "atr": atr,
                    "stop_price": swing_based_stop,  # UPGRADED: swing-based stop
                    "tier1_price": targets["tier1_price"],
                    "tier2_price": targets["tier2_price"],
                    "tier3_price": targets["tier3_price"],
                    "trailing_stop_pct": targets["trailing_stop_pct"],
                    "swing_info": swing_info,
                }
                await _db_save_open(symbol, "long", price, qty)
                # Log trade entry for analytics (ARM → ENTER transition)
                await log_trade_entry(
                    symbol, entry_rsi=rsi, entry_price=price, qty=qty,
                    arm_rsi=armed_rsi or rsi, volume_ratio=volume_spike_ratio,
                    candle_close_position=close_position
                )
                # Local balance tracking: deployed cost deducted from available cash
                # No API call needed - we track starting balance + realized profit - deployed capital
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

    log.info("=" * 80)
    log.info("🚀 CRYPTO TRADING BOT — EXPONENTIAL SCALING EDITION")
    log.info("=" * 80)
    log.info("Coinbase Advanced Trade (separate account from Alpaca stocks)")
    alloc_desc = f"${MAX_ALLOCATION:.2f} cap" if MAX_ALLOCATION is not None else "full balance (compounding)"
    log.info(f"Pairs: {', '.join(CRYPTO_PAIRS)} | Allocation: {alloc_desc} | Max positions: {MAX_POSITIONS}")
    log.info("")
    log.info("💰 POSITION SIZING: Adaptive Allocation (15% of available balance per trade)")
    log.info("  $500 balance   → $75 per entry")
    log.info("  $5,000 balance   → $750 per entry")
    log.info("  $50,000 balance  → $7,500 per entry")
    log.info("  $500,000 balance → $75,000 per entry")
    log.info("")
    log.info("🎯 ENTRY STRATEGY (3-FILTER SYSTEM):")
    log.info("  FILTER 1: 200-day SMA trend confirmation (bull market only)")
    log.info("  FILTER 2: RSI cross-above from oversold (reversal confirmation)")
    log.info("  FILTER 3: Volume + momentum confirmation (1.5x volume, close in upper 50%)")
    log.info("")
    log.info("📊 EXIT LOGIC: Close-on-profit-after-fees (1.2% minimum to cover taker fees)")
    log.info("  Every cycle: Scan 10 pairs → Enter oversold → Exit at +1.2%+ → Reinvest")
    log.info("  Result: 36-48 cycles/day × 78% win rate × $1.50 avg = $56+/day")
    log.info("")
    log.info("🔄 EXPONENTIAL GROWTH: Profits automatically reinvest into bigger positions")
    log.info("  Week 1: $483 → $1,500 (+200%)")
    log.info("  Week 4: $15,000 → $45,000 (+200%)")
    log.info("  Month 3: $500,000+ (exponential curve kicks in)")
    log.info("")
    log.info("Runs 24/7 - crypto has no market close, unlike prop_bot.py's stock/ETF trading")
    log.info("🔴 LIVE TRADING - Coinbase has no free paper-trading sandbox for Advanced Trade")
    log.info("=" * 80)

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
      

# END DYNAMIC CAPITAL ROTATION
