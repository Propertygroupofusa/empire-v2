#!/usr/bin/env python3
"""
Coinbase Market Buy Order Executor
Places a $50 BTC market order immediately via Coinbase Advanced Trade API
Usage: python3 coinbase_execute_trade.py [amount_usd]
Default: $50
"""

import os
import sys
import json
import base64
from datetime import datetime, timezone, timedelta
import uuid
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("coinbase_trade")

# Try httpx first, fall back to requests
try:
    import httpx
    HTTP_CLIENT = "httpx"
except ImportError:
    try:
        import requests
        HTTP_CLIENT = "requests"
    except ImportError:
        log.error("❌ Neither httpx nor requests installed - cannot execute trade")
        sys.exit(1)

def load_credentials():
    """Load and validate Coinbase API credentials from environment"""
    api_key_name = os.getenv("COINBASE_API_KEY_NAME", "").strip()
    api_private_key = os.getenv("COINBASE_API_PRIVATE_KEY", "").strip()

    if not api_key_name or not api_private_key:
        log.error("❌ Coinbase credentials not configured")
        log.error("   Required environment variables:")
        log.error("   - COINBASE_API_KEY_NAME")
        log.error("   - COINBASE_API_PRIVATE_KEY")
        return None, None

    return api_key_name, api_private_key

def build_jwt(api_key_name, api_private_key):
    """Build JWT token for Coinbase CDP API v1 with Ed25519 signature"""
    try:
        # Decode the private key (base64 encoded Ed25519)
        key_bytes = base64.b64decode(api_private_key)
        if len(key_bytes) != 64:
            log.error(f"❌ Invalid Ed25519 key size: {len(key_bytes)} bytes (expected 64)")
            return None

        # Load Ed25519 private key
        from cryptography.hazmat.primitives.asymmetric import ed25519
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])

        # Build JWT payload
        now = datetime.now(timezone.utc)
        exp = now + timedelta(minutes=1)

        payload = {
            "sub": api_key_name,
            "iss": "coinbase-cloud",
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iat": int(now.timestamp()),
        }

        header = {
            "alg": "EdDSA",
            "kid": api_key_name,
            "typ": "JWT"
        }

        # Manually build JWT
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        msg = f"{header_b64}.{payload_b64}".encode()

        # Sign with Ed25519
        signature = private_key.sign(msg)
        sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")

        jwt_token = f"{header_b64}.{payload_b64}.{sig_b64}"
        log.info(f"✅ JWT token built successfully")
        return jwt_token

    except Exception as e:
        log.error(f"❌ Failed to build JWT: {e}")
        import traceback
        traceback.print_exc()
        return None

def execute_market_buy(amount_usd=50.0):
    """Execute a market buy order for Bitcoin"""

    log.info("=" * 60)
    log.info("COINBASE MARKET BUY ORDER EXECUTOR")
    log.info("=" * 60)

    # Load credentials
    api_key_name, api_private_key = load_credentials()
    if not api_key_name:
        return False

    # Build JWT
    jwt_token = build_jwt(api_key_name, api_private_key)
    if not jwt_token:
        return False

    # Prepare order
    order_id = str(uuid.uuid4())
    order_payload = {
        "client_order_id": order_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "order_type": "MARKET",
        "market_market_ioc": {
            "quote_size": str(amount_usd)
        }
    }

    log.info(f"\n📝 ORDER DETAILS:")
    log.info(f"   Product: BTC-USD")
    log.info(f"   Side: BUY")
    log.info(f"   Amount: ${amount_usd:.2f} USD (market order)")
    log.info(f"   Order ID: {order_id}\n")

    log.info("⏳ Placing order on Coinbase Advanced Trade API...")

    try:
        if HTTP_CLIENT == "httpx":
            with httpx.Client() as client:
                response = client.post(
                    "https://api.coinbase.com/api/v1/brokerage/orders",
                    headers={
                        "Authorization": f"Bearer {jwt_token}",
                        "Content-Type": "application/json"
                    },
                    json=order_payload,
                    timeout=15
                )
                status = response.status_code
                result = response.json()
        else:
            import requests
            response = requests.post(
                "https://api.coinbase.com/api/v1/brokerage/orders",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Content-Type": "application/json"
                },
                json=order_payload,
                timeout=15
            )
            status = response.status_code
            result = response.json()

        # Handle response
        if status in (200, 201):
            log.info(f"\n✅ ORDER EXECUTED SUCCESSFULLY\n")
            log.info(f"📊 TRANSACTION DETAILS:")
            log.info(f"   Order ID: {result.get('order_id', 'N/A')}")
            log.info(f"   Product: {result.get('product_id', 'N/A')}")
            log.info(f"   Side: {result.get('side', 'N/A')}")
            log.info(f"   Status: {result.get('status', 'N/A')}")
            log.info(f"   Created: {result.get('created_at', 'N/A')}")

            if result.get('filled_size'):
                log.info(f"   BTC Received: {result['filled_size']}")
            if result.get('filled_value'):
                log.info(f"   USD Spent: ${float(result['filled_value']):.2f}")

            log.info(f"\n💰 SUCCESS: ${amount_usd:.2f} converted to Bitcoin")
            log.info(f"🔄 Ready for 10% profit cycle: Buy → Hold → Sell → Repeat")

            return True
        else:
            log.error(f"\n❌ ORDER FAILED (HTTP {status})")
            log.error(f"Response: {json.dumps(result, indent=2)}")
            return False

    except Exception as e:
        log.error(f"\n❌ Trade execution error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    amount = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0
    success = execute_market_buy(amount)
    sys.exit(0 if success else 1)
