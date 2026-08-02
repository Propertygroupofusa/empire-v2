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
from models import BotPosition, ClosedTrade, TradingBotState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_coinbase_bot")

COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"

CRYPTO_PAIRS = ["BTC/USD", "ETH/USD"]


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
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None), "ES256"
    decoded = base64.b64decode(raw)
    return Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


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


# Narrowed from 45 to 25 on measurement. An entry-signal sweep over 30
# days of real 5-minute candles compared each signal's forward return
# against the UNCONDITIONAL forward return over the same bars - so the
# result is independent of whatever target/stop is chosen, and cannot be
# tuned into existence by adjusting exits.
#
# 24h-horizon edge over a random entry in the same window:
#
#     signal        BTC        ETH
#     rsi < 25    +0.229%    +0.455%
#     rsi < 30    +0.183%    +0.258%
#     rsi < 35    +0.090%    +0.122%
#     rsi < 40    +0.091%    +0.066%
#     rsi < 45    +0.050%    +0.043%   <- the old setting
#
# Monotonic on both pairs independently, which is what a structural
# effect looks like and what noise does not. Every momentum signal
# (rsi > 55/60/65/70, price above SMAs, breakouts) came out NEGATIVE on
# both pairs, so the mean-reversion direction is right and the threshold
# was simply far too loose.
#
# The trade-count difference is most of the point: rsi < 45 fired 1,838
# times in the sample against 309 for rsi < 25 - six times the fee drag
# for a fifth of the per-trade edge. On an edge this size the fee is the
# adversary, so trading less is the improvement.
#
# HONEST LIMIT: the absolute return of rsi < 25 was still around zero
# after fees (BTC mean -0.213%, ETH +0.220%), and out-of-sample the ETH
# edge decayed to roughly nil. This is a real but small effect, roughly
# the same size as trading costs - a reason to trade more selectively,
# not evidence of a profitable system.
RSI_BUY_BELOW  = float(os.getenv("CRYPTO_RSI_BUY_BELOW", "25"))
RSI_SELL_ABOVE = float(os.getenv("CRYPTO_RSI_SELL_ABOVE", "50"))

MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", str(len(CRYPTO_PAIRS))))
# Unset by default - no ceiling, so the full account balance (principal +
# compounded profit) is always in play. Set CRYPTO_MAX_ALLOCATION to cap
# it at a fixed dollar amount instead, if ever wanted.
_max_allocation_env = os.getenv("CRYPTO_MAX_ALLOCATION", "")
MAX_ALLOCATION = float(_max_allocation_env) if _max_allocation_env else None
MIN_POSITION_NOTIONAL = float(os.getenv("CRYPTO_MIN_POSITION_NOTIONAL", "5"))

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

# Coinbase's real Advanced Trade taker fee for this account's volume tier -
# used only to size the profit target sensibly, not charged/simulated here
# (the real fee is already reflected in Coinbase's fill price/balance).
# 0.40%, the account's real taker tier. Was 0.006, which is simply wrong
# and made every fee-derived number downstream wrong with it - including
# the "Round-trip fee: 1.20%" printed every cycle in Railway, when the
# true cost is 0.80%.
TAKER_FEE_RATE = float(os.getenv("CRYPTO_TAKER_FEE_RATE", "0.004"))
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2

# A flat dollar profit target (the original design) doesn't scale with
# position size, so on a small position it can be - and was, at $0.50 on a
# ~$100 position - smaller than the ~1.2% round-trip fee itself, meaning
# every "successful" exit still lost money net of fees. The target is a
# percentage of the position's entry value instead, with a floor that
# guarantees it clears the round-trip fee with real profit left over.
# NOTE - correcting TAKER_FEE_RATE moves this, and the move is intended
# rather than an oversight. The floor is ROUND_TRIP_FEE_PCT * 1.5, so:
#
#     old (fee 0.006):  max(1.50%, 1.80%) = 1.80%   <- floor was binding
#     new (fee 0.004):  max(1.50%, 1.20%) = 1.50%   <- floor no longer binds
#
# The target was 1.80% only because the fee constant was overstated. With
# the real 0.80% round trip, a 1.50% target still clears costs with 0.70%
# left over, which is exactly what the floor exists to guarantee. The
# formula is doing its job; it was being fed a wrong number.
#
# Deliberately NOT tuned beyond that. The entry sweep measured forward
# returns, which says nothing about where to place a target, and the
# earlier geometry sweep found no target/stop pair that survived
# out-of-sample. Changing the entry threshold and the exit geometry in
# the same commit would also make the next measurement unattributable.
PROFIT_TARGET_PCT = max(
    float(os.getenv("CRYPTO_PROFIT_TARGET_PCT", "0.015")),
    ROUND_TRIP_FEE_PCT * 1.5,
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


open_crypto_positions = {}
daily_pnl = 0.0
latest_signals = {}
last_cycle_at = None

BOT_NAME = "crypto_coinbase"


async def load_open_positions():
    """Reload open_crypto_positions from the DB once at startup, before
    the first cycle runs - otherwise a Railway restart wipes this dict
    while the position is still open for real on Coinbase, and the bot
    can never take profit or cut losses on it again (see BotPosition)."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
            rows = result.scalars().all()
            for row in rows:
                open_crypto_positions[row.symbol] = {"entry": row.entry_price, "qty": row.qty}
            if rows:
                log.info(f"[CRYPTO] Reloaded {len(rows)} open position(s) from DB: {list(open_crypto_positions.keys())}")
    except Exception as e:
        log.error(f"[CRYPTO] Failed to reload open positions from DB: {e}")


async def _db_save_open(symbol: str, side: str, entry: float, qty: float, rsi=None):
    """Persist the open position AND the RSI that triggered it.

    This bot computes only price and RSI, so RSI is the single feature it
    can contribute - but a feature recorded is worth more than three
    discarded into a log line."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(BotPosition(bot=BOT_NAME, symbol=symbol, side=side,
                               entry_price=entry, qty=qty, entry_rsi=rsi))
            await db.commit()
    except Exception as e:
        log.error(f"[CRYPTO] Failed to persist opened position {symbol}: {e}")


async def _db_record_closed(symbol: str, entry: float, qty: float, exit_price: float,
                            reason: str, pnl: float, pnl_pct: float):
    """Append the completed round trip, carrying the entry snapshot across.

    Runs BEFORE _db_delete_open, which removes the row holding entry_rsi.
    Best-effort: a lost training row matters far less than an exception on
    the close path leaving the DB believing a closed position is open."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BotPosition).where(BotPosition.bot == BOT_NAME,
                                          BotPosition.symbol == symbol))
            row = result.scalars().first()
            opened_at = row.opened_at if row else None
            hold_hours = ((datetime.utcnow() - opened_at).total_seconds() / 3600) if opened_at else 0.0
            db.add(ClosedTrade(
                bot=BOT_NAME, symbol=symbol, side="long",
                entry_price=entry, qty=qty,
                entry_rsi=row.entry_rsi if row else None,
                exit_price=exit_price, exit_reason=reason,
                pnl=pnl, pnl_pct=pnl_pct, hold_hours=hold_hours,
                opened_at=opened_at,
            ))
            await db.commit()
    except Exception as e:
        log.warning(f"[CRYPTO] Could not record closed trade {symbol}: {e}")


async def _db_delete_open(symbol: str):
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME, BotPosition.symbol == symbol))
            for row in result.scalars().all():
                await db.delete(row)
            await db.commit()
    except Exception as e:
        log.error(f"[CRYPTO] Failed to remove closed position {symbol} from DB: {e}")


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
    """Price+RSI from Coinbase's public candles endpoint - the same
    exchange orders execute on, so there's no cross-exchange price drift."""
    try:
        closes = await _fetch_coinbase_closes(session, symbol)
    except Exception as e:
        log.debug(f"coinbase price fetch failed for {symbol}: {e}")
        closes = None

    if closes and len(closes) >= 14:
        rsi = _compute_rsi(closes)
        price = closes[-1]
        return {"price": price, "rsi": rsi}

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


# Maker vs taker. Coinbase charges 0.40% taker and 0.25% maker on this
# tier, so routing both legs as maker takes the round trip from 0.80% to
# 0.50%. On a measured edge of roughly 0.2-0.4% that 0.30% is not a
# refinement - it is most of the difference between negative and
# marginally positive.
#
# Off by default. A maker order is a resting limit that may simply never
# fill, and that failure mode is not symmetric between the two sides:
# see maker_allowed() below.
ORDER_MODE = os.getenv("CRYPTO_ORDER_MODE", "taker").strip().lower()
MAKER_FILL_TIMEOUT_S = float(os.getenv("CRYPTO_MAKER_FILL_TIMEOUT", "60"))
# Clamped above zero deliberately. The fill loop advances its own clock by
# this value, so a zero or negative interval would never reach the timeout -
# it would spin forever, polling Coinbase as fast as the event loop allows
# until the rate limiter cut it off. Found by setting it to 0 in a test and
# watching the process hang.
MAKER_POLL_INTERVAL_S = max(0.5, float(os.getenv("CRYPTO_MAKER_POLL_SECONDS", "5")))


def maker_allowed(side: str, reason: str = "") -> bool:
    """Whether this specific order may rest as a maker.

    An unfilled BUY costs nothing - no position is opened, no risk is
    taken, and the bot simply tries again next cycle. Skipping an entry
    is free.

    An unfilled SELL is a different animal: the position stays open and
    the exit did not happen. For a profit-target or RSI exit that is
    merely annoying - the position is in profit and can be sold next
    cycle. For a STOP LOSS it is unacceptable. A stop that might not fill
    is not a stop, and posting one behind the market while the price runs
    away from it is precisely how a small loss becomes a large one. Stops
    always cross the spread and pay the taker fee, which is what the fee
    is for.
    """
    if ORDER_MODE != "maker":
        return False
    if side == "sell" and reason == "STOP LOSS":
        return False
    return True


async def get_best_quote(session, symbol):
    """(best_bid, best_ask) from the level-1 book, or (None, None)."""
    product_id = _to_product_id(symbol)
    path = f"/api/v3/brokerage/product_book?product_id={product_id}&limit=1"
    try:
        async with session.get(COINBASE_BASE_URL + path,
                               headers=_auth_headers("GET", path)) as r:
            if r.status != 200:
                return None, None
            book = (await r.json()).get("pricebook", {})
            bids, asks = book.get("bids") or [], book.get("asks") or []
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            return bid, ask
    except Exception as e:
        log.warning(f"[CRYPTO] Could not read book for {symbol}: {e}")
        return None, None


async def _order_filled_size(session, order_id):
    """(status, filled_base_size) for an order, or (None, 0.0)."""
    path = f"/api/v3/brokerage/orders/historical/{order_id}"
    try:
        async with session.get(COINBASE_BASE_URL + path,
                               headers=_auth_headers("GET", path)) as r:
            if r.status != 200:
                return None, 0.0
            o = (await r.json()).get("order", {})
            return o.get("status"), float(o.get("filled_size") or 0.0)
    except Exception as e:
        log.warning(f"[CRYPTO] Could not read order {order_id}: {e}")
        return None, 0.0


async def _cancel_order(session, order_id):
    path = "/api/v3/brokerage/orders/batch_cancel"
    try:
        async with session.post(COINBASE_BASE_URL + path,
                                headers=_auth_headers("POST", path),
                                json={"order_ids": [order_id]}) as r:
            return r.status in (200, 201)
    except Exception as e:
        log.warning(f"[CRYPTO] Could not cancel order {order_id}: {e}")
        return False


async def place_maker_order(session, symbol, side, qty):
    """Rest a post-only limit at the near touch and wait for it to fill.

    Returns True ONLY on a confirmed fill. This matters more than it
    looks: every caller treats a True return as "the position changed",
    recording an open position or booking a realised P&L. A maker order
    that is merely ACCEPTED has not traded anything, so returning True on
    acceptance would have the bot record a position it does not own and
    later try to sell coins that were never bought.

    post_only makes Coinbase reject the order outright rather than cross
    the spread, which is what guarantees the maker fee - the order can
    fail to be maker, but it can never silently become a taker.

    Bounded wait, then cancel. Partial fills are treated as no fill and
    cancelled: the remainder would otherwise sit on the book unmanaged,
    and the caller's accounting assumes all-or-nothing on `qty`.
    """
    product_id = _to_product_id(symbol)
    bid, ask = await get_best_quote(session, symbol)
    # BUY rests at the bid, SELL rests at the ask - at the near touch, so
    # it is first in the queue without crossing.
    limit_price = bid if side == "buy" else ask
    if not limit_price:
        log.warning(f"[CRYPTO] No book for {symbol}, falling back to taker")
        return None            # None = caller should retry as taker

    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {"limit_limit_gtc": {
            "base_size": f"{qty:.8f}",
            "limit_price": f"{limit_price:.2f}",
            "post_only": True,
        }},
    }
    try:
        async with session.post(COINBASE_BASE_URL + path,
                                headers=_auth_headers("POST", path),
                                json=order) as r:
            result = await r.json()
            if r.status not in (200, 201) or not result.get("success", True):
                log.warning(f"[CRYPTO] Maker {side} {symbol} rejected "
                            f"(post_only would have crossed?): "
                            f"{result.get('error_response', result)}")
                return None    # let the caller decide about taker
            order_id = (result.get("success_response") or {}).get("order_id")
    except Exception as e:
        log.error(f"[CRYPTO] Maker order error: {e}")
        return None

    if not order_id:
        return None

    log.info(f"[CRYPTO] Maker {side.upper()} {qty} {symbol} resting @ "
             f"${limit_price:.2f} - waiting up to {MAKER_FILL_TIMEOUT_S:.0f}s")
    waited = 0.0
    while waited < MAKER_FILL_TIMEOUT_S:
        await asyncio.sleep(MAKER_POLL_INTERVAL_S)
        waited += MAKER_POLL_INTERVAL_S
        status, filled = await _order_filled_size(session, order_id)
        if status == "FILLED" and filled > 0:
            log.info(f"✅ CRYPTO TRADE (maker) | {side.upper()} {filled} {symbol} "
                     f"@ ${limit_price:.2f} - saved the taker spread")
            return True
        if status in ("CANCELLED", "EXPIRED", "FAILED"):
            log.info(f"[CRYPTO] Maker {side} {symbol} ended {status} without filling")
            return False

    await _cancel_order(session, order_id)
    log.info(f"[CRYPTO] Maker {side} {symbol} did not fill in "
             f"{MAKER_FILL_TIMEOUT_S:.0f}s - cancelled")
    return False


async def place_order(session, symbol, side, qty, price, reason=""):
    """Coinbase Advanced Trade market orders: BUY sizes by dollar amount
    (quote_size), SELL sizes by coin amount (base_size) - unlike Alpaca,
    which takes qty for both sides."""
    if maker_allowed(side, reason):
        maker_result = await place_maker_order(session, symbol, side, qty)
        if maker_result is True:
            return True
        if maker_result is False:
            # Rested and did not fill. For a BUY that is a free miss - do
            # NOT chase with a taker order, because paying 0.40% to force
            # an entry undoes the entire reason for being here. For a
            # non-stop SELL the position is in profit and can wait for the
            # next cycle.
            return False
        # maker_result is None: could not even place (no book, or
        # post_only rejected). Fall through to taker rather than silently
        # skipping the order.
        log.info(f"[CRYPTO] Falling back to taker for {side} {symbol}")

    product_id = _to_product_id(symbol)
    path = "/api/v3/brokerage/orders"
    order_config = (
        {"market_market_ioc": {"quote_size": f"{qty * price:.2f}"}}
        if side == "buy"
        else {"market_market_ioc": {"base_size": f"{qty:.8f}"}}
    )
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

    async with aiohttp.ClientSession() as session:
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

        cap_desc = f"capped at ${MAX_ALLOCATION:.2f}" if MAX_ALLOCATION is not None else "full balance, compounding"
        if TIER_SIZE > 0:
            tier_desc = f"tier unlocked (permanent): ${unlocked:.2f} | tradable now: ${cash_pool:.2f}"
        else:
            tier_desc = "tiering off"
        log.info(f"[CRYPTO] Coinbase USD balance: {'$%.2f' % cash if cash is not None else 'unknown'} | Crypto cash pool: ${cash_pool:.2f} ({cap_desc}, {tier_desc}) | Target: +{PROFIT_TARGET_PCT*100:.2f}% | Stop: -{STOP_LOSS_PCT*100:.2f}% | Round-trip fee: {ROUND_TRIP_FEE_PCT*100:.2f}%")

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
            target_hit = unrealized_pct >= PROFIT_TARGET_PCT
            stop_hit = unrealized_pct <= -STOP_LOSS_PCT
            # RSI recovering to neutral is only a real exit if it's already
            # cleared the round-trip fee - otherwise this "safe-looking"
            # exit is actually a guaranteed net loss (this was the actual
            # source of the fee bleed a backtest caught: RSI_SELL_ABOVE=50
            # fires on almost every trade long before PROFIT_TARGET_PCT
            # ever does, and it used to have no profit floor at all).
            rsi_exit = rsi > RSI_SELL_ABOVE and unrealized_pct > ROUND_TRIP_FEE_PCT

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "HOLDING_LONG",
                "has_position": True, "checked_at": now.isoformat(),
            }

            if target_hit or rsi_exit or stop_hit:
                reason = "PROFIT TARGET" if target_hit else ("STOP LOSS" if stop_hit else "RSI")
                filled = await place_order(session, symbol, "sell", qty, price, reason=reason)
                if filled:
                    daily_pnl += unrealized_pnl
                    log.info(f"[CRYPTO] 📤 CLOSE {symbol} ({reason}) | Entry: ${entry:.2f} Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}")
                    send_trade_alert(
                        f"🤖 Crypto bot — {symbol} closed ({reason})",
                        f"Position closed on your Coinbase account:\n\n"
                        f"{symbol} | Entry: ${entry:.2f} | Exit: ${price:.2f} | P&L: ${unrealized_pnl:.2f}\n"
                        f"Reason: {reason}\n\nDashboard: https://empire-v2-production.up.railway.app/trading-dashboard",
                    )
                    # Before the delete - that row holds entry_rsi.
                    await _db_record_closed(symbol, entry, qty, price, reason,
                                            unrealized_pnl, unrealized_pct * 100)
                    open_crypto_positions.pop(symbol, None)
                    await _db_delete_open(symbol)

            await asyncio.sleep(0.3)

        # ── Pass 2: new entries (long only, RSI oversold) ─────────────
        for symbol in CRYPTO_PAIRS:
            if symbol in open_crypto_positions:
                continue
            data = scans.get(symbol)
            if not data:
                continue
            price, rsi = data["price"], data["rsi"]

            if rsi >= RSI_BUY_BELOW:
                latest_signals[symbol] = {
                    "price": price, "rsi": rsi, "status": "NEUTRAL",
                    "has_position": False, "checked_at": now.isoformat(),
                }
                continue

            latest_signals[symbol] = {
                "price": price, "rsi": rsi, "status": "BUY_ZONE",
                "has_position": False, "checked_at": now.isoformat(),
            }
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
                await _db_save_open(symbol, "long", price, qty, rsi=rsi)
                cash_pool -= qty * price
                send_trade_alert(
                    f"🤖 Crypto bot — BUY {symbol} opened",
                    f"Long opened on your Coinbase account:\n\n"
                    f"BUY {qty} {symbol} @ ${price:.2f} | RSI: {rsi}\n\n"
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
        time.sleep(60)


if __name__ == "__main__":
    run()
