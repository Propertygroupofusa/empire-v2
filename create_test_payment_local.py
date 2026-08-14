#!/usr/bin/env python3
"""Create test payment records to simulate Stripe charges"""

import asyncio
import uuid
from datetime import datetime
from database import AsyncSessionLocal
from models import Payment, Worker, Job
from sqlalchemy import select

async def create_test_payment():
    """Create test payment records in database"""

    async with AsyncSessionLocal() as session:
        # Get or create bot worker
        result = await session.execute(select(Worker).where(Worker.email == "bot@pgusa.local"))
        bot_worker = result.scalar_one_or_none()

        if not bot_worker:
            print("❌ Bot worker not found. Run create_test_jobs_new.py first")
            return False

        # Get latest job for this worker
        result = await session.execute(
            select(Job).where(Job.worker_id == bot_worker.id).order_by(Job.id.desc()).limit(1)
        )
        latest_job = result.scalar_one_or_none()

        if not latest_job:
            print("❌ No jobs found for bot worker")
            return False

        # Create test payment
        payment_id = str(uuid.uuid4())
        worker_amount = 83.25  # $83.25
        platform_amount = 16.75  # $16.75
        total_amount = 100.00

        payment = Payment(
            id=payment_id,
            job_id=latest_job.id,
            worker_id=str(bot_worker.id),
            client_id="test_stripe_payment",
            worker_amount=worker_amount,
            platform_amount=platform_amount,
            gross_amount=total_amount,
            payout_status="pending",
            stripe_payout_id=None,
            created_at=datetime.utcnow()
        )

        session.add(payment)
        await session.commit()

        print(f"\n✅ Test payment created!")
        print(f"   Payment ID: {payment_id}")
        print(f"   Job ID: {latest_job.id}")
        print(f"   Worker Amount: ${worker_amount:.2f}")
        print(f"   Platform Amount: ${platform_amount:.2f}")
        print(f"   Total: ${total_amount:.2f}")
        print(f"   Status: pending")
        print(f"\n💰 Run monitor_bot_earnings.py to verify payment recorded")

        return True

if __name__ == "__main__":
    success = asyncio.run(create_test_payment())
    exit(0 if success else 1)
