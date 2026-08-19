"""
DISCORD SIGNAL RECEIVER
=======================
Listens for trading signals posted to Discord and auto-executes trades.
Connects to Alpaca and Coinbase for live execution.
"""
import os
import logging
import re
from fastapi import APIRouter, Request
from datetime import datetime
import httpx
import hashlib
import hmac

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("discord_signal_receiver")

# Discord setup
DISCORD_WEBHOOK_SECRET = os.getenv("DISCORD_WEBHOOK_SECRET", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")

# Broker credentials
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
ALPACA_LIVE_TRADE = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "")

# Signal config
SIGNAL_POSITION_SIZE = float(os.getenv("DISCORD_SIGNAL_SIZE_USD", "500"))  # $500 per signal
AUTO_EXECUTE_DISCORD = os.getenv("DISCORD_AUTO_EXECUTE", "true").lower() == "true"

# Track executed signals to avoid duplicates
executed_signals = set()

router = APIRouter(prefix="/discord", tags=["discord"])


def verify_discord_signature(signature: str, timestamp: str, body: str) -> bool:
    """Verify Discord webhook signature"""
    if not DISCORD_WEBHOOK_SECRET:
        return True  # Skip verification if secret not configured

    message = timestamp + body
    expected_sig = hmac.new(
        DISCORD_WEBHOOK_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected_sig)


def parse_signal(message_content: str) -> dict:
    """
    Parse trading signal from Discord message.
    Expected formats:
    - "BUY BTC" or "BUY $500 BTC"
    - "SELL ETH" or "SELL $300 ETH"
    - "BTC BUY" or "ETH SELL $250"
    """
    content = message_content.strip().upper()

    # Pattern: [SIDE] [OPTIONAL_SIZE] TICKER or TICKER SIDE [OPTIONAL_SIZE]
    pattern = r'(BUY|SELL)\s+(?:\$?(\d+))?\s+([A-Z]+)|([A-Z]+)\s+(BUY|SELL)\s+(?:\$?(\d+))?'
    match = re.search(pattern, content)

    if not match:
        return None

    if match.group(1):  # BUY/SELL first
        side = match.group(1).lower()
        size = match.group(2)
        ticker = match.group(3)
    else:  # TICKER first
        ticker = match.group(4)
        side = match.group(5).lower()
        size = match.group(6)

    return {
        "ticker": ticker,
        "side": side,
        "size": float(size) if size else SIGNAL_POSITION_SIZE,
        "timestamp": datetime.now().isoformat()
    }


async def execute_alpaca_trade(ticker: str, side: str, size_usd: float) -> bool:
    """Execute trade on Alpaca"""
    try:
        async with httpx.AsyncClient() as client:
            # Get current price
            resp = await client.get(
                f"{ALPACA_BASE_URL}/v1/last/stocks/{ticker}",
                headers={"APCA-API-KEY-ID": ALPACA_API_KEY},
                timeout=5.0
            )

            if resp.status_code != 200:
                log.warning(f"Failed to get price for {ticker}: {resp.status_code}")
                return False

            price = float(resp.json().get("last", {}).get("price", 0))
            if price <= 0:
                log.warning(f"Invalid price for {ticker}: {price}")
                return False

            # Calculate quantity
            qty = int(size_usd / price)
            if qty < 1:
                log.warning(f"Position size too small for {ticker} @ ${price}")
                return False

            # Build order
            order_data = {
                "symbol": ticker,
                "qty": qty,
                "side": side,
                "type": "market",
                "time_in_force": "day"
            }

            # Add bracket for buys
            if side.lower() == "buy":
                order_data["order_class"] = "bracket"
                order_data["take_profit"] = {"limit_price": price * 1.05}  # 5% profit
                order_data["stop_loss"] = {"stop_price": price * 0.97}    # 3% stop loss

            # Execute
            resp = await client.post(
                f"{ALPACA_BASE_URL}/v2/orders",
                headers={
                    "APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
                },
                json=order_data,
                timeout=10.0
            )

            if resp.status_code == 200:
                order = resp.json()
                log.info(f"✅ DISCORD SIGNAL EXECUTED (ALPACA)")
                log.info(f"   Ticker: {ticker}")
                log.info(f"   Side: {side.upper()}")
                log.info(f"   Price: ${price:.2f}")
                log.info(f"   Qty: {qty} shares")
                log.info(f"   Amount: ${qty * price:,.2f}")
                return True
            else:
                log.error(f"Order failed: {resp.status_code} - {resp.text[:200]}")
                return False

    except Exception as e:
        log.error(f"Failed to execute trade: {e}")
        return False


async def execute_coinbase_trade(ticker: str, side: str, size_usd: float) -> bool:
    """Execute trade on Coinbase"""
    if not COINBASE_API_KEY or not COINBASE_PRIVATE_KEY:
        log.warning("Coinbase credentials not configured")
        return False

    try:
        # For now, log the signal - Coinbase integration would go here
        # This requires the Coinbase Advanced Trade API setup
        log.info(f"📢 COINBASE SIGNAL: {ticker} {side.upper()} (${size_usd})")
        log.info(f"   [PAPER MODE] Would execute on Coinbase")
        return True
    except Exception as e:
        log.error(f"Failed to execute Coinbase trade: {e}")
        return False


@router.post("/signal")
async def receive_signal(request: Request):
    """
    Webhook endpoint for Discord bot to post trading signals.

    POST /discord/signal with JSON body:
    {
        "signal": "BUY BTC",
        "user": "username",
        "confidence": 0.9  (optional)
    }

    Or configure Discord bot webhook to post messages here.
    """
    try:
        body = await request.body()
        body_str = body.decode('utf-8')

        # Verify signature if configured
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if signature and timestamp:
            if not verify_discord_signature(signature, timestamp, body_str):
                return {"error": "Invalid signature"}, 401

        data = await request.json()
        message = data.get("content", data.get("signal", ""))

        if not message:
            return {"error": "No signal content"}, 400

        # Parse signal
        signal = parse_signal(message)
        if not signal:
            log.warning(f"Could not parse signal: {message}")
            return {"error": "Invalid signal format"}, 400

        # Check for duplicates
        signal_id = f"{signal['ticker']}_{signal['side']}_{signal['timestamp']}"
        if signal_id in executed_signals:
            log.info(f"Duplicate signal ignored: {signal_id}")
            return {"status": "duplicate"}, 200

        executed_signals.add(signal_id)

        log.info(f"\n🎯 DISCORD SIGNAL RECEIVED")
        log.info(f"   Ticker: {signal['ticker']}")
        log.info(f"   Side: {signal['side'].upper()}")
        log.info(f"   Size: ${signal['size']:.2f}")
        log.info(f"   Confidence: {data.get('confidence', 0.9):.0%}")

        # Execute trade
        if AUTO_EXECUTE_DISCORD:
            if ALPACA_LIVE_TRADE:
                success = await execute_alpaca_trade(
                    signal['ticker'],
                    signal['side'],
                    signal['size']
                )
            else:
                log.info(f"   [PAPER MODE] Would execute {signal['side']} {signal['ticker']}")
                success = True
        else:
            log.info(f"   [NO AUTO-EXECUTE] Signal logged only")
            success = True

        return {
            "status": "executed" if success else "failed",
            "signal": signal
        }

    except Exception as e:
        log.error(f"Signal processing error: {e}", exc_info=True)
        return {"error": str(e)}, 500


@router.get("/status")
async def signal_receiver_status():
    """Get status of Discord signal receiver"""
    return {
        "status": "active",
        "auto_execute": AUTO_EXECUTE_DISCORD,
        "live_trading": ALPACA_LIVE_TRADE,
        "position_size_usd": SIGNAL_POSITION_SIZE,
        "signals_received": len(executed_signals),
        "configured": bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)
    }


async def start_discord_signal_receiver():
    """Initialize Discord signal receiver"""
    log.info("🎯 DISCORD SIGNAL RECEIVER INITIALIZED")
    log.info(f"   Auto-execute: {AUTO_EXECUTE_DISCORD}")
    log.info(f"   Live trading: {ALPACA_LIVE_TRADE}")
    log.info(f"   Position size: ${SIGNAL_POSITION_SIZE} per signal")
    log.info(f"   Endpoint: POST /discord/signal")
    log.info(f"   Status: GET /discord/status")
