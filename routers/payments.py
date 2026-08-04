"""Payment and payout management for workers - Stripe automatic payouts"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from models import Payment, Worker, Job
import stripe
import os
from datetime import datetime
import logging

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
                    "stripe_payout_id": p.stripe_payout_id,
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


@router.post("/process-pending-payouts", summary="Process pending payouts via Stripe")
async def process_pending_payouts(db: AsyncSession = Depends(get_db)):
    """Process all pending payouts - can be called manually or by scheduler"""
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

        for payment in pending_payments:
            try:
                # Get worker details for bank account
                worker_result = await db.execute(
                    select(Worker).where(Worker.id == int(payment.worker_id))
                )
                worker = worker_result.scalar_one_or_none()

                if not worker:
                    log.warning(f"Payment {payment.id}: Worker not found")
                    continue

                # Create Stripe payout
                # Note: This uses direct API call; for production with Stripe Connect,
                # you'd use connected account IDs instead of bank transfer
                payout = stripe.Payout.create(
                    amount=int(payment.worker_amount * 100),  # Convert to cents
                    currency="usd",
                    description=f"Payout for job {payment.job_id}",
                    metadata={
                        "payment_id": payment.id,
                        "worker_id": payment.worker_id,
                        "worker_email": worker.email,
                    }
                )

                # Update payment record
                payment.payout_status = "processing"
                payment.stripe_payout_id = payout.id
                await db.commit()
                processed += 1
                log.info(f"Payment {payment.id}: Payout created {payout.id}")

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
            "message": f"Processed {processed} payouts, {failed} failed"
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
