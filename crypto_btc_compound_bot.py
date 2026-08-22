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


async def get_usd_balance(session) -> tuple:
    """Real available USD balance. Returns (balance, None) or (None, reason)."""
    return await get_asset_balance(session, "USD")


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


async def get_price_and_volatility(session, product_id: str = PRODUCT_ID) -> tuple:
    """Current price and ATR% (volatility as a fraction of price) for any
    Coinbase product, from 5-minute candles on Coinbase's public
    market-data endpoint (no auth needed - same endpoint
    crypto_coinbase_bot.py uses for its own ATR). Returns (price, atr_pct)
    or (None, None) on failure. product_id defaults to BTC-USD so this
    bot's own run_cycle doesn't need to change; crypto_family_tree_bot.py
    passes each branch's own product_id explicitly."""
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity=300"
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


async def place_market_buy(session, usd_amount: float, product_id: str = PRODUCT_ID):
    """Spends usd_amount on product_id at market. Returns (filled_qty, filled_price) or None."""
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
        return None

    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": f"{qty:.{decimals}f}"}},
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
