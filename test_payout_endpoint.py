#!/usr/bin/env python3
"""
Minimal test server for payout endpoint - bypasses worker_auth import issues
"""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models import Payment, Worker, Job
import stripe
import os
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_payout")

app = FastAPI()

# Initialize Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = True
else:
    STRIPE_AVAILABLE = False
    log.warning("STRIPE_SECRET_KEY not configured - automatic payouts disabled")

@app.get("/health")
async def health():
    return {"status": "ok", "stripe_configured": STRIPE_AVAILABLE}

@app.post("/process-pending-payouts")
async def process_pending_payouts():
    """Process all pending payouts - test endpoint"""

    if not STRIPE_AVAILABLE:
        return {
            "status": "disabled",
            "message": "Stripe not configured - automatic payouts unavailable"
        }

    try:
        # Import here to avoid startup issues
        from database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            # Get all pending payments
            result = await session.execute(
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
                    # Get worker details
                    worker_result = await session.execute(
                        select(Worker).where(Worker.id == int(payment.worker_id))
                    )
                    worker = worker_result.scalar_one_or_none()

                    if not worker:
                        log.warning(f"Payment {payment.id}: Worker not found")
                        continue

                    # Create Stripe payout
                    payout = stripe.Payout.create(
                        amount=int(payment.worker_amount * 100),
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
                    await session.commit()
                    processed += 1
                    log.info(f"Payment {payment.id}: Payout created {payout.id}")

                except stripe.error.StripeError as e:
                    failed += 1
                    log.error(f"Payment {payment.id}: Stripe error: {e}")
                    payment.payout_status = "failed"
                    await session.commit()
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
        return {
            "status": "error",
            "message": str(e)
        }

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Starting test payout endpoint server on http://localhost:8001")
    log.info(f"   Stripe Available: {STRIPE_AVAILABLE}")
    uvicorn.run(app, host="0.0.0.0", port=8001)
