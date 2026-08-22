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


async def get_usd_balance(session) -> tuple:
    """Real available USD balance. Returns (balance, None) or (None, reason)."""
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
                    if account.get("currency") == "USD":
                        return float(account["available_balance"]["value"]), None
                if not data.get("has_next") or not data.get("cursor"):
                    break
                cursor = data.get("cursor")
        return None, "no USD account found on this key"
    except asyncio.TimeoutError:
        return None, "Coinbase API timeout"
    except aiohttp.ClientError as e:
        return None, f"Coinbase connection failed: {type(e).__name__}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


async def get_price_and_volatility(session) -> tuple:
    """Current BTC price and ATR% (volatility as a fraction of price), from
    5-minute candles on Coinbase's public market-data endpoint (no auth
    needed - same endpoint crypto_coinbase_bot.py uses for its own ATR).
    Returns (price, atr_pct) or (None, None) on failure."""
    url = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/candles?granularity=300"
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
            if not data or len(data) < 20:
                return None, None
            # Coinbase returns newest-first: [time, low, high, open, close, volume]
            candles = list(reversed(data))
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[1]) for c in candles]
            price = closes[-1]

            period = 14
            if len(closes) < period + 1:
                return price, 0.0
            true_ranges = []
            for i in range(1, len(closes)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                true_ranges.append(tr)
            atr = sum(true_ranges[-period:]) / period
            atr_pct = atr / price if price else 0.0
            return price, atr_pct
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Price/volatility fetch failed: {e}")
        return None, None


def pick_target_pct(atr_pct: float) -> float:
    if atr_pct < VOL_LOW_THRESHOLD:
        return TARGET_LOW_PCT
    if atr_pct < VOL_HIGH_THRESHOLD:
        return TARGET_MED_PCT
    return TARGET_HIGH_PCT


async def place_market_buy(session, usd_amount: float):
    """Spends usd_amount on BTC at market. Returns (filled_qty, filled_price) or None."""
    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": PRODUCT_ID,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}},
    }
    return await _place_and_confirm(session, path, order)


async def place_market_sell(session, qty: float):
    """Sells qty BTC at market. Returns (filled_qty, filled_price) or None."""
    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": PRODUCT_ID,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": f"{qty:.8f}"}},
    }
    return await _place_and_confirm(session, path, order)


async def _place_and_confirm(session, path: str, order: dict):
    try:
        async with session.post(COINBASE_BASE_URL + path, headers=_auth_headers("POST", path), json=order, timeout=15) as r:
            resp = await r.json()
            if r.status not in (200, 201) or not resp.get("success"):
                log.warning(f"[BTC-COMPOUND] Order not accepted: {resp}")
                return None
            order_id = resp["success_response"]["order_id"]
    except Exception as e:
        log.warning(f"[BTC-COMPOUND] Order placement failed: {e}")
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


async def clear_position():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPosition).where(BotPosition.bot == BOT_NAME))
        pos = result.scalar_one_or_none()
        if pos:
            await session.delete(pos)
            await session.commit()


async def run_cycle():
    global last_cycle_at, daily_pnl
    last_cycle_at = datetime.now(timezone.utc)

    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        log.error("[BTC-COMPOUND] COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set - cannot trade")
        return

    async with aiohttp.ClientSession() as session:
        position = await load_position()

        if position is None:
            balance, err = await get_usd_balance(session)
            if balance is None:
                log.warning(f"[BTC-COMPOUND] Balance unavailable ({err}) - skipping this cycle")
                return
            if balance < MIN_TRADE_USD:
                log.info(f"[BTC-COMPOUND] Balance ${balance:.2f} below minimum trade size ${MIN_TRADE_USD:.2f} - waiting")
                return

            price, atr_pct = await get_price_and_volatility(session)
            if price is None:
                log.warning("[BTC-COMPOUND] Could not fetch BTC price/volatility - skipping this cycle")
                return

            target_pct = pick_target_pct(atr_pct)
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
                f"ATR volatility: {atr_pct*100:.2f}% -> target +{target_pct*100:.2f}% (${target_price:,.2f}) | "
                f"stop -{STOP_LOSS_PCT*100:.2f}% (${stop_price:,.2f})"
            )
            return

        # Position open - check for target/stop, otherwise just report status.
        price, _ = await get_price_and_volatility(session)
        if price is None:
            log.warning("[BTC-COMPOUND] Could not fetch current price - holding, will re-check next cycle")
            return

        unrealized_pct = (price / position.entry_price - 1) * 100
        if price >= position.target_price:
            reason = "TARGET HIT"
        elif price <= position.stop_price:
            reason = "STOP HIT"
        else:
            log.info(
                f"[BTC-COMPOUND] HOLDING {position.qty:.8f} BTC | entry ${position.entry_price:,.2f} | "
                f"now ${price:,.2f} ({unrealized_pct:+.2f}%) | target ${position.target_price:,.2f} | "
                f"stop ${position.stop_price:,.2f}"
            )
            return

        fill = await place_market_sell(session, position.qty)
        if not fill:
            log.warning(f"[BTC-COMPOUND] {reason} but sell did not fill - will retry next cycle")
            return
        filled_qty, filled_price = fill
        gross_pnl = (filled_price - position.entry_price) * filled_qty
        fees = (position.entry_price * position.qty + filled_price * filled_qty) * (ROUND_TRIP_FEE_RATE / 2)
        net_pnl = gross_pnl - fees
        daily_pnl += net_pnl
        await clear_position()
        log.info(
            f"[BTC-COMPOUND] SOLD {filled_qty:.8f} BTC @ ${filled_price:,.2f} ({reason}) | "
            f"entry ${position.entry_price:,.2f} -> exit ${filled_price:,.2f} | "
            f"P&L: {'+' if net_pnl >= 0 else ''}${net_pnl:.2f} after est. fees | "
            f"next cycle buys again with the resulting balance"
        )


def run():
    log.info("=" * 60)
    log.info("BTC COMPOUNDING LOOP BOT — single-position, adaptive target")
    log.info(f"Stop-loss: -{STOP_LOSS_PCT*100:.1f}% | Targets: {TARGET_LOW_PCT*100:.1f}%/{TARGET_MED_PCT*100:.1f}%/{TARGET_HIGH_PCT*100:.1f}% "
              f"(quiet/normal/volatile, by ATR%) | Min trade: ${MIN_TRADE_USD:.2f} | Cycle: {CYCLE_SECONDS}s")
    log.info("=" * 60)

    # One persistent event loop for this thread's whole lifetime, not a new
    # one per cycle - see prop_bot.py's run() for why (repeated asyncio.run()
    # under uvicorn's process-wide uvloop policy corrupts cross-cycle state).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

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
