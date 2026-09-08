"""
Crypto Trading API Router
==========================
Handles cryptocurrency trading operations via Coinbase Advanced Trade API
including account management, order placement, and emergency withdrawals.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Dict, Any
import os
import hmac
import hashlib
import base64
import time
import uuid
import logging
import httpx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/crypto", tags=["crypto"])

# Coinbase API Configuration
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET_KEY = os.getenv("COINBASE_SECRET_KEY")
COINBASE_PASSPHRASE = os.getenv("COINBASE_PASSPHRASE")

COINBASE_API_URL = "https://api.coinbase.com"


class OrderRequest(BaseModel):
    """Request model for placing orders"""
    product_id: str
    size: str
    order_type: str = "market"
    side: str  # "buy" or "sell"


class WithdrawRequest(BaseModel):
    """Request model for emergency BTC withdrawal"""
    product_id: str = "BTC-USD"


def generate_auth_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    """Generate Coinbase Advanced Trade API authentication headers"""
    if not all([COINBASE_API_KEY, COINBASE_SECRET_KEY, COINBASE_PASSPHRASE]):
        raise HTTPException(
            status_code=401,
            detail="Coinbase API keys not configured"
        )

    timestamp = str(time.time())
    message = timestamp + method + path + body

    signature = base64.b64encode(
        hmac.new(
            COINBASE_SECRET_KEY.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()

    return {
        "CB-ACCESS-KEY": COINBASE_API_KEY,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "CB-ACCESS-PASSPHRASE": COINBASE_PASSPHRASE,
        "Content-Type": "application/json"
    }


@router.get("/accounts")
async def get_accounts():
    """Fetch all crypto accounts from Coinbase"""
    try:
        headers = generate_auth_headers("GET", "/api/v3/brokerage/accounts")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{COINBASE_API_URL}/api/v3/brokerage/accounts",
                headers=headers,
                timeout=10.0
            )

            if response.status_code != 200:
                logger.error(f"Coinbase API error: {response.status_code}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to fetch accounts from Coinbase"
                )

            data = response.json()
            accounts = data.get("accounts", [])

            # Transform accounts into asset list
            assets = []
            for account in accounts:
                try:
                    currency = account.get("currency", "")
                    available = float(account.get("available_balance", {}).get("value", 0))

                    if available > 0:
                        # Get current price for this asset
                        price_pair = f"{currency}-USD"
                        assets.append({
                            "currency": currency,
                            "amount": available,
                            "current_price": 0,  # Will be fetched separately
                            "value": 0,
                            "pnl": 0,
                            "pnl_pct": 0
                        })
                except (ValueError, KeyError):
                    continue

            return assets

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching accounts: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch accounts: {str(e)}"
        )


@router.post("/order")
async def place_order(order: OrderRequest):
    """Place a cryptocurrency order"""
    try:
        if not order.product_id or not order.size or not order.side:
            raise HTTPException(
                status_code=400,
                detail="Missing required order parameters"
            )

        # Build order payload
        order_payload = {
            "client_order_id": str(uuid.uuid4()),
            "product_id": order.product_id,
            "side": order.side,
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": str(float(order.size))
                }
            }
        }

        body = str(order_payload).replace("'", '"')
        headers = generate_auth_headers("POST", "/api/v3/brokerage/orders", body)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{COINBASE_API_URL}/api/v3/brokerage/orders",
                json=order_payload,
                headers=headers,
                timeout=10.0
            )

            if response.status_code not in [200, 201]:
                error_detail = response.text
                logger.error(f"Order failed: {error_detail}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Order failed: {error_detail}"
                )

            return response.json()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to place order: {str(e)}"
        )


@router.post("/withdraw")
async def withdraw_all_btc(withdraw_req: WithdrawRequest):
    """
    Emergency withdrawal: Sell all BTC at market price immediately

    Flow:
    1. Fetch BTC account balance
    2. Get current BTC-USD price
    3. Create market sell order for full amount
    4. Return transaction details
    """
    try:
        # Step 1: Fetch BTC account balance
        headers = generate_auth_headers("GET", "/api/v3/brokerage/accounts")

        async with httpx.AsyncClient() as client:
            # Get accounts
            response = await client.get(
                f"{COINBASE_API_URL}/api/v3/brokerage/accounts",
                headers=headers,
                timeout=10.0
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to fetch accounts"
                )

            accounts_data = response.json()
            btc_account = None

            for account in accounts_data.get("accounts", []):
                if account.get("currency") == "BTC":
                    btc_account = account
                    break

            if not btc_account:
                raise HTTPException(
                    status_code=404,
                    detail="No BTC account found"
                )

            btc_amount = float(btc_account.get("available_balance", {}).get("value", 0))

            if btc_amount <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="No BTC available to withdraw"
                )

            # Step 2: Get current BTC price
            try:
                price_response = await client.get(
                    "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                    timeout=5.0
                )

                if price_response.status_code == 200:
                    price_data = price_response.json()
                    btc_price = float(price_data.get("data", {}).get("amount", 0))
                else:
                    btc_price = 0
            except Exception as e:
                logger.warning(f"Failed to get BTC price: {e}")
                btc_price = 0

            if btc_price <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Could not fetch BTC price"
                )

            # Step 3: Create market sell order
            order_payload = {
                "client_order_id": str(uuid.uuid4()),
                "product_id": "BTC-USD",
                "side": "sell",
                "order_configuration": {
                    "market_market_ioc": {
                        "base_size": str(btc_amount)
                    }
                }
            }

            body = str(order_payload).replace("'", '"')
            order_headers = generate_auth_headers("POST", "/api/v3/brokerage/orders", body)

            order_response = await client.post(
                f"{COINBASE_API_URL}/api/v3/brokerage/orders",
                json=order_payload,
                headers=order_headers,
                timeout=10.0
            )

            if order_response.status_code not in [200, 201]:
                error_text = order_response.text
                logger.error(f"Order failed: {error_text}")
                raise HTTPException(
                    status_code=order_response.status_code,
                    detail=f"Order failed: {error_text}"
                )

            order_data = order_response.json()

            return {
                "success": True,
                "btc_amount": btc_amount,
                "btc_price": btc_price,
                "estimated_proceeds": btc_amount * btc_price,
                "order_id": order_data.get("order_id"),
                "status": order_data.get("order_status", "pending")
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during withdrawal: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to complete withdrawal: {str(e)}"
        )


@router.get("/price/{pair}")
async def get_price(pair: str):
    """Get current price for a crypto pair (e.g., BTC-USD)"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.coinbase.com/v2/prices/{pair}/spot",
                timeout=5.0
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "pair": pair,
                    "price": float(data.get("data", {}).get("amount", 0))
                }
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Price not found for {pair}"
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching price: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch price: {str(e)}"
        )
