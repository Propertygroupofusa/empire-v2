"""
BTC PROFIT LOCK BOT v1
Simple 10% profit cycling strategy for rapid compounding
Target: $50 → $8,000+ via repeated 10% wins

Strategy:
1. Buy Bitcoin at market price
2. Hold until +10% profit (auto-sell)
3. Reinvest 100% of proceeds into next cycle
4. Stop-loss: -3% (cut losses immediately)
5. Max hold: 24 hours (free capital if stalled)
6. Repeat indefinitely
"""

import os
import sys
import json
import uuid
import asyncio
import logging
import base64
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# Database
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Payment

# HTTP
try:
    import httpx
except ImportError:
    print("❌ httpx required: pip install httpx")
    sys.exit(1)

# Crypto signing
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("❌ cryptography required: pip install cryptography")
    sys.exit(1)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("btc_profit_lock")

# Coinbase API
COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "").strip()
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").strip()

# Strategy parameters
BTC_PROFIT_TARGET_PCT = 0.10  # 10% profit target
BTC_STOP_LOSS_PCT = 0.03  # -3% stop loss
BTC_MAX_HOLD_HOURS = 24  # Max 24 hour hold
BTC_MIN_ORDER_USD = 150.0  # TRIPLED: $50 → $150 per cycle (3x speed)

# Tracking
open_btc_position = None  # Current position: {order_id, entry_price, qty, entry_time, entry_usd}
cycle_count = 0
total_profit = 0.0


def build_jwt():
    """Build JWT for Coinbase API"""
    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        log.error("❌ Coinbase credentials not configured")
        return None

    try:
        key_bytes = base64.b64decode(COINBASE_API_PRIVATE_KEY)
        if len(key_bytes) != 64:
            log.error(f"❌ Invalid key size: {len(key_bytes)} bytes")
            return None

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])

        now = datetime.now(timezone.utc)
        exp = now + timedelta(minutes=1)

        payload = {
            "sub": COINBASE_API_KEY_NAME,
            "iss": "coinbase-cloud",
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iat": int(now.timestamp()),
        }

        header = {"alg": "EdDSA", "kid": COINBASE_API_KEY_NAME, "typ": "JWT"}

        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        msg = f"{header_b64}.{payload_b64}".encode()

        signature = private_key.sign(msg)
        sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")

        return f"{header_b64}.{payload_b64}.{sig_b64}"

    except Exception as e:
        log.error(f"❌ JWT build failed: {e}")
        return None


async def get_btc_price():
    """Fetch current BTC/USD price"""
    try:
        jwt = build_jwt()
        if not jwt:
            return None

        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.coinbase.com/api/v1/brokerage/market/products/BTC-USD",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                price = float(data.get("price", 0))
                return price if price > 0 else None
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
    return None


async def buy_btc(usd_amount):
    """Place market buy order for Bitcoin"""
    global open_btc_position, cycle_count

    log.info(f"\n🚀 CYCLE #{cycle_count + 1}: BUY $${usd_amount:.2f} BTC")

    jwt = build_jwt()
    if not jwt:
        log.error("❌ Cannot build JWT")
        return False

    # Get current price first
    price = await get_btc_price()
    if not price:
        log.error("❌ Cannot get BTC price")
        return False

    order_id = str(uuid.uuid4())

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.coinbase.com/api/v1/brokerage/orders",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json"
                },
                json={
                    "client_order_id": order_id,
                    "product_id": "BTC-USD",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "market_market_ioc": {"quote_size": str(usd_amount)}
                },
                timeout=15
            )

            if r.status_code not in (200, 201):
                log.error(f"❌ Buy order failed: {r.text}")
                return False

            result = r.json()
            filled_qty = float(result.get("filled_size", 0))
            filled_value = float(result.get("filled_value", 0))

            if filled_qty <= 0:
                log.error(f"❌ No fill received")
                return False

            # Calculate actual entry price
            entry_price = filled_value / filled_qty if filled_qty > 0 else price

            open_btc_position = {
                "order_id": result.get("order_id", order_id),
                "entry_price": entry_price,
                "qty": filled_qty,
                "entry_time": datetime.now(timezone.utc),
                "entry_usd": filled_value,
            }

            cycle_count += 1

            log.info(f"✅ BUY FILLED")
            log.info(f"   Order ID: {open_btc_position['order_id']}")
            log.info(f"   BTC Received: {filled_qty:.8f}")
            log.info(f"   Entry Price: ${entry_price:.2f}")
            log.info(f"   USD Spent: ${filled_value:.2f}")
            log.info(f"   Target Sell: ${entry_price * 1.10:.2f} (+10%)")
            log.info(f"   Stop Loss: ${entry_price * 0.97:.2f} (-3%)")

            return True

    except Exception as e:
        log.error(f"❌ Buy execution failed: {e}")
        return False


async def sell_btc(reason):
    """Sell all BTC at market price"""
    global open_btc_position, total_profit

    if not open_btc_position:
        log.warning("❌ No open position to sell")
        return False

    position = open_btc_position
    qty = position["qty"]
    entry_price = position["entry_price"]
    entry_usd = position["entry_usd"]

    log.info(f"\n💰 SELL SIGNAL: {reason}")

    jwt = build_jwt()
    if not jwt:
        return False

    try:
        # Get current price
        price = await get_btc_price()
        if not price:
            log.error("❌ Cannot fetch price for sell")
            return False

        order_id = str(uuid.uuid4())

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.coinbase.com/api/v1/brokerage/orders",
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json"
                },
                json={
                    "client_order_id": order_id,
                    "product_id": "BTC-USD",
                    "side": "SELL",
                    "order_type": "MARKET",
                    "market_market_ioc": {"base_size": str(qty)}
                },
                timeout=15
            )

            if r.status_code not in (200, 201):
                log.error(f"❌ Sell order failed: {r.text}")
                return False

            result = r.json()
            filled_qty = float(result.get("filled_size", 0))
            filled_value = float(result.get("filled_value", 0))

            if filled_qty <= 0:
                log.error(f"❌ No fill on sell")
                return False

            # Calculate P&L
            exit_price = filled_value / filled_qty
            profit_usd = filled_value - entry_usd
            profit_pct = (profit_usd / entry_usd * 100) if entry_usd > 0 else 0

            total_profit += profit_usd

            log.info(f"✅ SELL FILLED")
            log.info(f"   Order ID: {result.get('order_id', order_id)}")
            log.info(f"   BTC Sold: {filled_qty:.8f}")
            log.info(f"   Exit Price: ${exit_price:.2f}")
            log.info(f"   USD Received: ${filled_value:.2f}")
            log.info(f"   PROFIT: ${profit_usd:.2f} ({profit_pct:+.2f}%)")
            log.info(f"   Total Profit So Far: ${total_profit:.2f}")

            # Record to database
            await record_cycle_profit(profit_usd, reason, entry_price, exit_price, filled_qty)

            open_btc_position = None
            return True

    except Exception as e:
        log.error(f"❌ Sell execution failed: {e}")
        return False


async def record_cycle_profit(profit_usd, reason, entry_price, exit_price, qty):
    """Record completed cycle to database"""
    try:
        payment = Payment(
            id=f"btc_cycle_{uuid.uuid4().hex[:8]}",
            job_id=f"btc_cycle_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            worker_id="btc_profit_lock",
            client_id="bitcoin_trading",
            gross_amount=profit_usd,
            worker_amount=profit_usd * 0.90,  # 90% to worker, 10% platform
            platform_amount=profit_usd * 0.10,
            payout_status="pending",
        )

        async with AsyncSessionLocal() as session:
            session.add(payment)
            await session.commit()
            log.info(f"💾 Cycle recorded to database (ID: {payment.id})")

    except Exception as e:
        log.warning(f"Database record failed: {e}")


async def check_position():
    """Monitor open position for exit signals"""
    global open_btc_position

    if not open_btc_position:
        return

    position = open_btc_position
    entry_price = position["entry_price"]
    entry_time = position["entry_time"]

    # Get current price
    price = await get_btc_price()
    if not price:
        return

    # Calculate metrics
    profit_pct = ((price - entry_price) / entry_price * 100)
    hold_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600

    log.info(f"[CHECK] BTC ${price:.2f} | Entry ${entry_price:.2f} | P&L {profit_pct:+.2f}% | Hold {hold_hours:.1f}h")

    # Exit signal 1: Hit 10% profit target
    if profit_pct >= (BTC_PROFIT_TARGET_PCT * 100):
        await sell_btc(f"+{BTC_PROFIT_TARGET_PCT*100:.0f}% PROFIT TARGET HIT")
        return

    # Exit signal 2: Hit -3% stop loss
    if profit_pct <= -(BTC_STOP_LOSS_PCT * 100):
        await sell_btc(f"{-(BTC_STOP_LOSS_PCT*100):.0f}% STOP LOSS HIT")
        return

    # Exit signal 3: 24 hour timeout (exit at market to free capital)
    if hold_hours >= BTC_MAX_HOLD_HOURS:
        await sell_btc(f"24-HOUR TIMEOUT (P&L {profit_pct:+.2f}%)")
        return


async def run_cycle():
    """Main bot cycle"""
    log.info("\n" + "=" * 70)
    log.info("BTC PROFIT LOCK BOT — Running cycle")
    log.info("=" * 70)

    # If no open position, buy
    if not open_btc_position:
        # Get price to calculate position size
        price = await get_btc_price()
        if not price:
            log.warning("⚠️  Cannot fetch price for position sizing")
            return

        # Buy with minimum order size (can increase later)
        await buy_btc(BTC_MIN_ORDER_USD)

    # Check existing position
    await check_position()

    log.info(f"\n📊 STATUS: Cycles: {cycle_count} | Profit: ${total_profit:.2f} | Open: {bool(open_btc_position)}")


def run():
    """Main bot runner"""
    log.info("=" * 70)
    log.info("BTC PROFIT LOCK BOT v1 - TURBO MODE (3x Speed)")
    log.info(f"Strategy: Buy BTC → +10% Sell → Repeat")
    log.info(f"Stop Loss: -3% | Max Hold: 24h | Order Size: ${BTC_MIN_ORDER_USD} (3x speed)")
    log.info("=" * 70)

    # Check credentials
    if not COINBASE_API_KEY_NAME or not COINBASE_API_PRIVATE_KEY:
        log.error("❌ Coinbase credentials not configured")
        log.error("Set COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY")
        return

    # Run cycles every 10 seconds (3x faster monitoring)
    while True:
        try:
            asyncio.run(run_cycle())
        except KeyboardInterrupt:
            log.info("\n⏹️  Bot stopped by user")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")

        import time
        time.sleep(10)  # Check every 10 seconds (3x faster = catch 10% swings quicker)


if __name__ == "__main__":
    run()
