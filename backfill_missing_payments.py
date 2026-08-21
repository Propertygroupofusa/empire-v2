#!/usr/bin/env python3
"""
Backfill missing payment records for completed jobs.

This script ensures that all completed jobs that should have payment records
(paid + worker assigned + price > 0) get their Payment entries created in
the database. This is a recovery/maintenance script for cases where job
completion happened through a code path that bypassed the automatic
payment creation logic.

Usage:
  python3 backfill_missing_payments.py

This is safe to run multiple times - it only creates payment records for
jobs that don't already have one.
"""

import asyncio
import uuid
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


async def backfill_missing_payments():
    """Find completed jobs without payment records and create them."""
    engine = create_async_engine("sqlite+aiosqlite:///empire.db")
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as session:
            from models import Job, Payment

            print(f"\n{'='*70}")
            print(f"🔄 BACKFILL MISSING PAYMENTS")
            print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"{'='*70}\n")

            # Find all completed jobs
            result = await session.execute(select(Job).where(Job.status == "completed"))
            completed_jobs = result.scalars().all()
            print(f"📊 Scanning {len(completed_jobs)} completed jobs...")
            print()

            # Filter: paid jobs with worker assigned and price > 0
            eligible_jobs = [
                j for j in completed_jobs
                if j.paid and j.worker_id and j.price and j.price > 0
            ]
            print(f"✓ Eligible for payment: {len(eligible_jobs)} jobs")
            print()

            # For each eligible job, check if it has a payment record
            created_count = 0
            skipped_count = 0

            for job in eligible_jobs:
                result = await session.execute(
                    select(func.count(Payment.id)).where(Payment.job_id == str(job.id))
                )
                existing_payment = result.scalar() or 0

                if existing_payment == 0:
                    # Create payment record
                    payment = Payment(
                        id=str(uuid.uuid4()),
                        job_id=str(job.id),
                        worker_id=str(job.worker_id),
                        client_id=job.client_id,
                        gross_amount=float(job.price * 1.2),
                        worker_amount=float(job.price),
                        platform_amount=float(job.price * 0.2),
                        payout_status="pending",
                    )
                    session.add(payment)
                    created_count += 1
                    print(f"  ✓ Created payment for job {job.id}: ${job.price:.2f}")
                else:
                    skipped_count += 1

            # Commit if any were created
            if created_count > 0:
                await session.commit()
                print()
                print(f"✅ CREATED: {created_count} payment records")

            if skipped_count > 0:
                print(f"⏭️  SKIPPED: {skipped_count} (already have payments)")

            # Report final totals
            print()
            print(f"📊 FINAL STATUS:")

            result = await session.execute(select(func.count(Payment.id)))
            total_payments = result.scalar() or 0

            result = await session.execute(select(func.sum(Payment.worker_amount)))
            total_earnings = (result.scalar() or 0)

            result = await session.execute(
                select(func.count(Payment.id)).where(Payment.payout_status == 'pending')
            )
            pending = result.scalar() or 0

            result = await session.execute(
                select(func.sum(Payment.worker_amount)).where(Payment.payout_status == 'pending')
            )
            pending_amount = (result.scalar() or 0)

            print(f"  Total payment records: {total_payments}")
            print(f"  Total earnings: ${total_earnings:.2f}")
            print(f"  Pending payouts: {pending} (${pending_amount:.2f})")
            print()
            print(f"{'='*70}\n")

    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(backfill_missing_payments())
