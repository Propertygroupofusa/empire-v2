"""Payment and payout management for workers - Stripe automatic payouts"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update as sa_update
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

# Same gate as the background payout loop in main.py, and for the same
# reason. This endpoint reaches Stripe by a different call
# (Payout.create rather than Transfer.create) but has the identical
# unguarded shape: money moves first, then the row is updated, and a
# failure between the two leaves the payment "pending" so the next
# invocation pays it again. Gating only the background loop would leave a
# public POST able to do the thing the flag exists to prevent.
PAYOUTS_ENABLED = os.getenv("PAYOUTS_ENABLED", "false").strip().lower() == "true"


async def _mark_failed(db: AsyncSession, payment_id: str):
    """Record a payout failure without trusting the current session state.

    A failed Stripe call can leave the session holding a failed flush, so
    mutating the ORM object and committing would raise PendingRollbackError
    and take down every remaining payment in the request. Roll back first,
    then write through a standalone UPDATE.

    The stripe_payout_id IS NULL condition is the important part: if Stripe
    accepted the payout and the failure happened afterwards, the money has
    already left, and marking that row "failed" would invite someone to pay
    it a second time by hand.
    """
    await db.rollback()
    try:
        await db.execute(
            sa_update(Payment)
            .where(Payment.id == payment_id, Payment.stripe_payout_id.is_(None))
            .values(payout_status="failed")
        )
        await db.commit()
    except Exception as e:
        log.error(f"Could not mark payment {payment_id} failed: {e}")
        await db.rollback()


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
    if not PAYOUTS_ENABLED:
        return {
            "status": "disabled",
            "message": ("Payouts are disabled. Set PAYOUTS_ENABLED=true to arm. "
                        "Pending idempotency-key and per-payment-commit fixes - "
                        "without them a failure between Stripe and the database "
                        "leaves the payment pending and it is paid again.")
        }

    if not STRIPE_AVAILABLE:
        return {
            "status": "disabled",
            "message": "Stripe not configured - automatic payouts unavailable"
        }

    try:
        # stripe_payout_id IS NULL is the backstop for Stripe's 24h
        # idempotency-key expiry: past that window the same key is treated
        # as new, but a payment already carrying a payout id has provably
        # been paid and must never be a candidate again.
        result = await db.execute(
            select(Payment).where(
                Payment.payout_status == "pending",
                Payment.stripe_payout_id.is_(None),
            )
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
                # Stable across retries and restarts, so replaying this
                # call returns the ORIGINAL payout rather than issuing a
                # second one. Namespaced apart from the background loop's
                # "payout-" keys: these are different Stripe operations on
                # the same payment, and sharing a key would make whichever
                # ran second silently return the other's object.
                payout = stripe.Payout.create(
                    amount=int(payment.worker_amount * 100),  # Convert to cents
                    currency="usd",
                    description=f"Payout for job {payment.job_id}",
                    metadata={
                        "payment_id": payment.id,
                        "worker_id": payment.worker_id,
                        "worker_email": worker.email,
                    },
                    idempotency_key=f"manual-payout-{payment.id}",
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
                # Roll back before marking, then write through a fresh
                # UPDATE - the session may be carrying a failed flush, and
                # without the rollback every remaining payment in this
                # request dies on PendingRollbackError. Skips rows that
                # already have a payout id: if Stripe accepted the payout
                # and the failure came later, the money is gone and
                # "failed" would invite a manual re-pay.
                await _mark_failed(db, payment.id)
            except Exception as e:
                failed += 1
                log.error(f"Payment {payment.id}: Error: {e}")
                await _mark_failed(db, payment.id)

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
