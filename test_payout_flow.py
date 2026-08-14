#!/usr/bin/env python3
"""
Test payout processing flow - simulates Stripe payout without real API calls
Useful for testing revenue generation until Stripe keys are configured
"""

import asyncio
from datetime import datetime
from database import AsyncSessionLocal
from sqlalchemy import select
from models import Payment, Worker
import uuid

async def simulate_payout_processing():
    """Simulate processing pending payments as if Stripe payouts succeeded"""

    async with AsyncSessionLocal() as session:
        # Get bot worker
        result = await session.execute(
            select(Worker).where(Worker.email == "bot@pgusa.local")
        )
        bot_worker = result.scalar_one_or_none()

        if not bot_worker:
            print("❌ Bot worker not found")
            return

        # Get all pending payments
        result = await session.execute(
            select(Payment).where(Payment.payout_status == "pending")
            .where(Payment.worker_id == str(bot_worker.id))
        )
        pending_payments = result.scalars().all()

        if not pending_payments:
            print("✅ No pending payments to process")
            return

        print(f"📊 SIMULATING PAYOUT PROCESSING")
        print("=" * 70)
        print(f"Processing {len(pending_payments)} pending payments...\n")

        processed = 0
        for payment in pending_payments:
            # Simulate Stripe payout creation
            fake_stripe_id = f"po_{uuid.uuid4().hex[:16]}"

            print(f"Payment: ${payment.worker_amount:.2f}")
            print(f"  Status: pending → processing → paid")
            print(f"  Stripe ID: {fake_stripe_id}")

            # Update payment status (simulating successful Stripe payout)
            payment.payout_status = "paid"
            payment.stripe_payout_id = fake_stripe_id
            payment.paid_at = datetime.utcnow()

            await session.commit()
            processed += 1
            print(f"  ✅ Marked as paid\n")

        print("=" * 70)
        print(f"✅ Simulation Complete: {processed} payments processed")
        print(f"💰 Total Payout: ${sum(p.worker_amount for p in pending_payments):.2f}")

if __name__ == "__main__":
    print("⚠️  WARNING: This is a test simulation only")
    print("   In production, use real Stripe API with STRIPE_SECRET_KEY\n")

    asyncio.run(simulate_payout_processing())
