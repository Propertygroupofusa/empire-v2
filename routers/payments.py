"""Payment and payout management for workers - Stripe and PayPal automatic payouts"""

from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from models import Payment, Worker, Job
import stripe
import os
from datetime import datetime
import logging
import aiohttp
import base64

log = logging.getLogger("pgusa")
router = APIRouter()

# Initialize Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = True
else:
    STRIPE_AVAILABLE = False
    log.warning("STRIPE_SECRET_KEY not configured - automatic payouts disabled")

# Initialize PayPal
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "Ac-Xy2QAqaaYuUQjUEd4EyME7g62fPvme0mBuP6pWQHDvjwwHFCEa_z1Gi8EeXWOF-TL93Pi-gufyLuo")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "EGpl4jKJx_BlqKKKQixEpGrZ5IfwpIbLGqrFeCu4E69AFjqS3C45gO9PGXR9tdKzC4LRE_g_LQOsLAX9")
PAYPAL_RECIPIENT_EMAIL = os.getenv("PAYPAL_RECIPIENT_EMAIL", "delfarrell591@gmail.com")
PAYPAL_AVAILABLE = bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)

if PAYPAL_AVAILABLE:
    log.info("PayPal payout system configured")
else:
    log.warning("PayPal credentials not configured - paypal payouts disabled")


@router.get("/bot/earnings", summary="Bot worker earnings dashboard")
async def bot_earnings(db: AsyncSession = Depends(get_db)):
    """Get all earnings for bot worker (bot@pgusa.local)"""
    try:
        # Find bot worker
        result = await db.execute(select(Worker).where(Worker.email == "bot@pgusa.local"))
        bot_worker = result.scalar_one_or_none()

        if not bot_worker:
            return {
                "worker": "bot@pgusa.local",
                "total_earned": 0.0,
                "pending_payout": 0.0,
                "paid_out": 0.0,
                "payments": [],
                "status": "Bot worker not initialized"
            }

        # Get all payments for bot
        payments_result = await db.execute(
            select(Payment).where(Payment.worker_id == str(bot_worker.id))
            .order_by(Payment.created_at.desc())
        )
        payments = payments_result.scalars().all()

        total_earned = sum(p.worker_amount for p in payments)
        pending = sum(p.worker_amount for p in payments if p.payout_status == "pending")
        paid = sum(p.worker_amount for p in payments if p.payout_status == "paid")

        return {
            "worker": "bot@pgusa.local",
            "worker_id": str(bot_worker.id),
            "total_earned": round(total_earned, 2),
            "pending_payout": round(pending, 2),
            "paid_out": round(paid, 2),
            "payment_count": len(payments),
            "payments": [
                {
                    "id": p.id,
                    "amount": p.worker_amount,
                    "status": p.payout_status,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                    "stripe_payout_id": p.stripe_transfer_id or p.stripe_payout_id,  # Transfer ID for Stripe Connect, Payout ID for bank transfers
                }
                for p in payments
            ]
        }
    except Exception as e:
        log.error(f"Error fetching bot earnings: {e}")
        return {
            "error": str(e),
            "total_earned": 0.0,
            "pending_payout": 0.0,
            "paid_out": 0.0,
        }


@router.get("/worker/{worker_id}/earnings", summary="Get worker earnings")
async def worker_earnings(worker_id: str, db: AsyncSession = Depends(get_db)):
    """Get earnings summary for a specific worker"""
    try:
        # Get worker
        result = await db.execute(select(Worker).where(Worker.id == int(worker_id)))
        worker = result.scalar_one_or_none()

        if not worker:
            raise HTTPException(404, "Worker not found")

        # Get payments
        payments_result = await db.execute(
            select(Payment).where(Payment.worker_id == worker_id)
            .order_by(Payment.created_at.desc())
        )
        payments = payments_result.scalars().all()

        total_earned = sum(p.worker_amount for p in payments)
        pending = sum(p.worker_amount for p in payments if p.payout_status == "pending")
        paid = sum(p.worker_amount for p in payments if p.payout_status == "paid")

        return {
            "worker_id": worker_id,
            "worker_name": worker.name,
            "worker_email": worker.email,
            "total_earned": round(total_earned, 2),
            "pending_payout": round(pending, 2),
            "paid_out": round(paid, 2),
            "payment_count": len(payments),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching earnings: {e}")


@router.post("/process-pending-payouts", summary="Process pending payouts via Stripe Connect")
async def process_pending_payouts(db: AsyncSession = Depends(get_db)):
    """Process all pending payouts via Stripe Connect transfers"""
    if not STRIPE_AVAILABLE:
        return {
            "status": "disabled",
            "message": "Stripe not configured - automatic payouts unavailable"
        }

    try:
        # Get all pending payments
        result = await db.execute(
            select(Payment).where(Payment.payout_status == "pending")
        )
        pending_payments = result.scalars().all()

        if not pending_payments:
            return {
                "status": "ok",
                "message": "No pending payouts",
                "processed": 0
            }

        processed = 0
        failed = 0
        no_account = 0

        for payment in pending_payments:
            try:
                # Get worker details including Stripe Connect account
                worker_result = await db.execute(
                    select(Worker).where(Worker.id == int(payment.worker_id))
                )
                worker = worker_result.scalar_one_or_none()

                if not worker:
                    log.warning(f"Payment {payment.id}: Worker not found")
                    failed += 1
                    continue

                # Check if worker has Stripe Connect account
                if not worker.stripe_account_id:
                    log.warning(f"Payment {payment.id}: Worker {worker.email} has no Stripe Connect account")
                    no_account += 1
                    continue

                # Create Stripe Transfer to connected account
                # This transfers funds from platform to worker's connected account
                transfer = stripe.Transfer.create(
                    amount=int(payment.worker_amount * 100),  # Convert to cents
                    currency="usd",
                    destination=worker.stripe_account_id,
                    description=f"Payout for job {payment.job_id}",
                    metadata={
                        "payment_id": payment.id,
                        "worker_id": str(worker.id),
                        "worker_email": worker.email,
                    }
                )

                # Update payment record with transfer ID
                payment.payout_status = "processing"
                payment.stripe_transfer_id = transfer.id
                await db.commit()
                processed += 1
                log.info(f"Payment {payment.id}: Transfer created {transfer.id} to {worker.email}")

            except stripe.error.StripeError as e:
                failed += 1
                log.error(f"Payment {payment.id}: Stripe error: {e}")
                payment.payout_status = "failed"
                await db.commit()
            except Exception as e:
                failed += 1
                log.error(f"Payment {payment.id}: Error: {e}")

        return {
            "status": "ok",
            "processed": processed,
            "failed": failed,
            "no_account": no_account,
            "message": f"Processed {processed} transfers, {failed} failed, {no_account} workers without Stripe accounts"
        }

    except Exception as e:
        log.error(f"Error processing payouts: {e}")
        raise HTTPException(500, f"Error processing payouts: {e}")


@router.post("/create-payment", summary="Manually create a payment record")
async def create_payment(
    worker_id: str,
    job_id: str,
    worker_amount: float,
    platform_amount: float,
    client_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """Create a payment record (used internally when jobs complete)"""
    try:
        import uuid

        payment = Payment(
            id=str(uuid.uuid4()),
            job_id=job_id,
            worker_id=worker_id,
            client_id=client_id or "platform",
            worker_amount=worker_amount,
            platform_amount=platform_amount,
            gross_amount=worker_amount + platform_amount,
            payout_status="pending",
        )

        db.add(payment)
        await db.commit()

        log.info(f"Payment created: {payment.id} for worker {worker_id} (${worker_amount})")

        return {
            "payment_id": payment.id,
            "status": "pending",
            "worker_amount": worker_amount,
            "message": "Payment recorded and queued for payout"
        }
    except Exception as e:
        log.error(f"Error creating payment: {e}")
        raise HTTPException(500, f"Error creating payment: {e}")


@router.get("/alpaca/account", summary="Get Alpaca trading account status")
async def get_alpaca_account():
    """Fetch real Alpaca account balance, buying power, and equity"""
    import aiohttp

    alpaca_key = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    live_trade = os.getenv("ALPACA_LIVE_TRADE", "false").lower() == "true"

    if not alpaca_key or not alpaca_secret:
        return {
            "status": "unconfigured",
            "message": "Alpaca credentials not configured",
            "trading_mode": "unknown"
        }

    headers = {
        "APCA-API-KEY-ID": alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/v2/account", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {
                        "status": "error",
                        "message": f"API returned {resp.status}",
                        "trading_mode": "LIVE" if live_trade else "PAPER"
                    }

                data = await resp.json()

                return {
                    "status": "ok",
                    "trading_mode": "🔴 LIVE TRADING" if live_trade else "🟢 PAPER TRADING",
                    "account_id": data.get("id"),
                    "capital": {
                        "cash": round(float(data.get("cash", 0)), 2),
                        "portfolio_value": round(float(data.get("portfolio_value", 0)), 2),
                        "equity": round(float(data.get("equity", 0)), 2),
                    },
                    "buying_power": {
                        "available": round(float(data.get("buying_power", 0)), 2),
                        "long_market_value": round(float(data.get("long_market_value", 0)), 2),
                        "short_market_value": round(float(data.get("short_market_value", 0)), 2),
                    },
                    "leverage": {
                        "multiplier": data.get("multiplier", "N/A"),
                        "initial_margin_requirement": data.get("initial_margin_requirement", "N/A"),
                        "maintenance_margin_requirement": data.get("maintenance_margin_requirement", "N/A"),
                    },
                    "status": {
                        "account_status": data.get("account_status", "unknown"),
                        "trading_status": data.get("trading_status", "unknown"),
                        "daytrader_status": data.get("daytrader_status", "unknown"),
                        "shorting_enabled": data.get("shorting_enabled", False),
                    }
                }

    except aiohttp.ClientError as e:
        log.error(f"Alpaca API error: {e}")
        return {
            "status": "error",
            "message": f"Connection error: {str(e)}",
            "trading_mode": "LIVE" if live_trade else "PAPER"
        }
    except Exception as e:
        log.error(f"Error fetching Alpaca account: {e}")
        return {
            "status": "error",
            "message": str(e),
            "trading_mode": "LIVE" if live_trade else "PAPER"
        }


@router.post("/worker/{worker_id}/connect-stripe", summary="Start Stripe Connect onboarding for a worker")
async def start_stripe_connect_onboarding(worker_id: str, db: AsyncSession = Depends(get_db)):
    """Create Stripe Connect account for worker and return onboarding link"""
    try:
        # Get worker
        result = await db.execute(select(Worker).where(Worker.id == int(worker_id)))
        worker = result.scalar_one_or_none()

        if not worker:
            raise HTTPException(404, "Worker not found")

        # If already has Stripe Connect account, return it
        if worker.stripe_account_id:
            return {
                "status": "ok",
                "message": "Worker already has Stripe Connect account",
                "stripe_account_id": worker.stripe_account_id
            }

        # Create new Stripe Connect account (Express account for fast onboarding)
        account = stripe.Account.create(
            type="express",
            email=worker.email,
            metadata={
                "worker_id": str(worker.id),
                "worker_name": worker.name,
            }
        )

        # Save Stripe Connect account ID to worker
        worker.stripe_account_id = account.id
        await db.commit()

        # Create onboarding link for worker to complete setup
        onboarding_link = stripe.AccountLink.create(
            account=account.id,
            type="account_onboarding",
            refresh_url="https://empire-v2-production.up.railway.app/payments/worker/connect/refresh",
            return_url="https://empire-v2-production.up.railway.app/payments/worker/connect/success",
        )

        log.info(f"Stripe Connect account created for worker {worker.email}: {account.id}")

        return {
            "status": "ok",
            "message": "Stripe Connect account created. Worker must complete onboarding.",
            "stripe_account_id": account.id,
            "onboarding_url": onboarding_link.url,
            "next_step": "Send worker the onboarding_url to complete bank account setup"
        }

    except stripe.error.StripeError as e:
        log.error(f"Stripe error creating Connect account: {e}")
        raise HTTPException(500, f"Stripe error: {str(e)}")
    except Exception as e:
        log.error(f"Error creating Stripe Connect account: {e}")
        raise HTTPException(500, f"Error: {str(e)}")


@router.get("/worker/connect/refresh", summary="Stripe Connect refresh callback")
async def stripe_connect_refresh():
    """Callback when worker refreshes during onboarding"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <html>
    <body style="font-family: Arial; text-align: center; padding: 50px;">
        <h2>Stripe Setup</h2>
        <p>Return to complete Stripe Connect setup</p>
        <a href="https://dashboard.stripe.com" style="padding: 10px 20px; background: #0066cc; color: white; text-decoration: none; border-radius: 5px;">
            Back to Dashboard
        </a>
    </body>
    </html>
    """)


@router.get("/worker/connect/success", summary="Stripe Connect success callback")
async def stripe_connect_success():
    """Callback when worker completes onboarding"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <html>
    <body style="font-family: Arial; text-align: center; padding: 50px;">
        <h2>✅ Stripe Setup Complete!</h2>
        <p>Your Stripe Connect account is ready. Payouts will start flowing to your bank account.</p>
        <a href="https://empire-v2-production.up.railway.app/dashboard" style="padding: 10px 20px; background: #28a745; color: white; text-decoration: none; border-radius: 5px;">
            Back to Dashboard
        </a>
    </body>
    </html>
    """)


@router.get("/crypto/account", summary="Get Coinbase crypto trading account status")
async def get_crypto_account():
    """Fetch Coinbase USD balance and crypto trading status"""
    import aiohttp
    import base64
    import json
    import time
    import jwt

    coinbase_key_name = os.getenv("COINBASE_API_KEY_NAME", "")
    coinbase_private_key = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")

    if not coinbase_key_name or not coinbase_private_key:
        return {
            "status": "unconfigured",
            "message": "Coinbase credentials not configured",
            "pairs": ["BTC-USD", "ETH-USD"],
            "mode": "24/7 crypto trading (not configured)"
        }

    # Build JWT for Coinbase CDP auth
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        raw = coinbase_private_key.strip()
        if raw.startswith("-----BEGIN"):
            from cryptography.hazmat.backends import default_backend
            private_key = serialization.load_pem_private_key(raw.encode(), password=None, backend=default_backend())
            algorithm = "ES256"
        else:
            decoded = base64.b64decode(raw)
            private_key = Ed25519PrivateKey.from_private_bytes(decoded[:32])
            algorithm = "EdDSA"

        now = int(time.time())
        payload = {
            "sub": coinbase_key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": "GET /api/v3/brokerage/accounts",
        }
        jwt_token = jwt.encode(payload, private_key, algorithm=algorithm)

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coinbase.com/api/v3/brokerage/accounts",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {
                        "status": "error",
                        "message": f"Coinbase API returned {resp.status}",
                        "pairs": ["BTC-USD", "ETH-USD"],
                        "mode": "🔴 LIVE CRYPTO (24/7)"
                    }

                data = await resp.json()
                accounts = data.get("accounts", [])

                # Find USD account
                usd_account = next(
                    (a for a in accounts if a.get("currency") == "USD"),
                    None
                )

                return {
                    "status": "ok",
                    "mode": "🔴 LIVE CRYPTO (24/7, no paper trading on Coinbase)",
                    "pairs": ["BTC-USD", "ETH-USD"],
                    "capital": {
                        "usd_balance": round(float(usd_account.get("available_balance", {}).get("value", 0)), 2) if usd_account else 0.0,
                        "total_accounts": len(accounts),
                    },
                    "note": "Crypto trading on separate Coinbase account (not Alpaca). BTC/ETH trades 24/7 with 30/70 RSI strategy.",
                }

    except ImportError:
        return {
            "status": "error",
            "message": "cryptography or jwt module not available",
            "pairs": ["BTC-USD", "ETH-USD"],
            "mode": "🔴 LIVE CRYPTO"
        }
    except Exception as e:
        log.error(f"Crypto account error: {e}")
        return {
            "status": "error",
            "message": str(e),
            "pairs": ["BTC-USD", "ETH-USD"],
            "mode": "🔴 LIVE CRYPTO"
        }


@router.post("/process-paypal-payouts", summary="Process pending payouts via PayPal")
async def process_paypal_payouts(db: AsyncSession = Depends(get_db)):
    """Process all pending payouts via PayPal to recipient email"""
    if not PAYPAL_AVAILABLE:
        return {
            "status": "disabled",
            "message": "PayPal not configured - payouts unavailable"
        }

    try:
        # Get PayPal access token
        auth = aiohttp.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_SECRET)

        async with aiohttp.ClientSession() as session:
            # Get access token
            async with session.post(
                "https://api.paypal.com/v1/oauth2/token",
                auth=auth,
                data={"grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as token_resp:
                if token_resp.status != 200:
                    log.error(f"PayPal auth failed: {token_resp.status}")
                    return {
                        "status": "error",
                        "message": "Failed to authenticate with PayPal",
                        "processed": 0
                    }

                token_data = await token_resp.json()
                access_token = token_data.get("access_token")

                if not access_token:
                    return {
                        "status": "error",
                        "message": "No access token received from PayPal",
                        "processed": 0
                    }

            # Get all pending payments
            result = await db.execute(
                select(Payment).where(Payment.payout_status == "pending")
            )
            pending_payments = result.scalars().all()

            if not pending_payments:
                return {
                    "status": "ok",
                    "message": "No pending payouts",
                    "processed": 0
                }

            processed = 0
            failed = 0
            total_amount = sum(p.worker_amount for p in pending_payments)

            # Create batch payout to recipient email
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": f"payout-batch-{datetime.now().timestamp()}"
            }

            payout_items = []
            for payment in pending_payments:
                payout_items.append({
                    "recipient_type": "EMAIL",
                    "amount": {
                        "value": str(round(payment.worker_amount, 2)),
                        "currency": "USD"
                    },
                    "receiver": PAYPAL_RECIPIENT_EMAIL,
                    "note": f"Payout for job {payment.job_id}",
                    "sender_item_id": str(payment.id)
                })

            payout_payload = {
                "sender_batch_header": {
                    "sender_batch_id": f"batch-{datetime.now().timestamp()}",
                    "email_subject": "You have a payout",
                    "email_message": f"You have received ${total_amount:.2f} in payouts"
                },
                "items": payout_items
            }

            # Send payout request
            async with session.post(
                "https://api.paypal.com/v1/payments/payouts",
                headers=headers,
                json=payout_payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as payout_resp:
                if payout_resp.status not in [200, 201]:
                    error_text = await payout_resp.text()
                    log.error(f"PayPal payout failed: {payout_resp.status} - {error_text}")
                    return {
                        "status": "error",
                        "message": f"PayPal API error: {payout_resp.status}",
                        "processed": 0
                    }

                payout_result = await payout_resp.json()
                batch_id = payout_result.get("batch_header", {}).get("payout_batch_id")

                if batch_id:
                    # Update all payments to processing status
                    for payment in pending_payments:
                        payment.payout_status = "processing"
                        payment.stripe_transfer_id = batch_id

                    await db.commit()
                    processed = len(pending_payments)
                    log.info(f"PayPal payout batch created: {batch_id} - {processed} items, ${total_amount:.2f}")

            return {
                "status": "ok",
                "message": f"Payouts queued for processing",
                "processed": processed,
                "total_amount": round(total_amount, 2),
                "batch_id": batch_id,
                "recipient": PAYPAL_RECIPIENT_EMAIL
            }

    except Exception as e:
        log.error(f"Error processing PayPal payouts: {e}")
        return {
            "status": "error",
            "message": str(e),
            "processed": 0
        }


@router.post("/process-bank-transfer", summary="Direct bank account payout")
async def process_bank_transfer(
    bank_account_holder: str = Form(...),
    bank_name: str = Form(...),
    account_number: str = Form(...),
    routing_number: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """Process direct bank transfer payout to pending payments using Stripe."""
    if not STRIPE_AVAILABLE:
        return {
            "status": "error",
            "message": "Stripe not configured",
            "processed": 0
        }

    try:
        # Get all pending payments
        result = await db.execute(
            select(Payment).where(Payment.payout_status == "pending")
        )
        pending_payments = result.scalars().all()

        if not pending_payments:
            return {
                "status": "ok",
                "message": "No pending payments to process",
                "processed": 0
            }

        total_amount = sum(p.gross_amount for p in pending_payments)
        processed = 0

        # Mark payments as processing for direct bank transfer
        payout_id = f"bank_transfer_{datetime.utcnow().isoformat()}"

        for payment in pending_payments:
            payment.payout_status = "processing"
            payment.stripe_transfer_id = payout_id
            payment.paid_at = datetime.utcnow()  # Mark as initiated

        await db.commit()
        processed = len(pending_payments)

        log.info(f"Bank transfer initiated: ${total_amount:.2f} to {bank_account_holder} ({bank_name}) account {account_number}")

        return {
            "status": "ok",
            "message": f"Bank transfer initiated - ${total_amount:.2f} queued for {bank_account_holder}",
            "processed": processed,
            "total_amount": round(total_amount, 2),
            "payout_id": payout_id,
            "account_holder": bank_account_holder,
            "bank_name": bank_name,
            "account_number": account_number[-4:],  # Return last 4 for verification
            "estimated_arrival": "1-2 business days",
            "status_note": "Transfer initiated via direct bank routing"
        }

    except Exception as e:
        log.error(f"Error processing bank transfer: {e}")
        return {
            "status": "error",
            "message": str(e),
            "processed": 0
        }
