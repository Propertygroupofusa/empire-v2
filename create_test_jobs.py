#!/usr/bin/env python3
"""
Create test jobs for live payout processing verification on Railway

Usage:
  python3 create_test_jobs.py

This script:
1. Creates a test client
2. Creates 5 completed jobs linked to that client
3. Generates pending payments for each job
4. Ready for /process-pending-payouts endpoint testing
"""

import asyncio
from datetime import datetime
from database import AsyncSessionLocal
from models import Client, Job, Payment
import uuid


async def create_test_jobs_for_payout():
    """Create new jobs, mark them complete, and generate pending payments for live payout testing"""

    print("📋 CREATING TEST JOBS FOR LIVE PAYOUT TESTING")
    print("=" * 80)

    async with AsyncSessionLocal() as session:
        # Create a test client
        test_client = Client(
            email="test_client@payouts.local",
            name="Test Client Payouts",
            company="Test Company",
            status="active",
        )
        session.add(test_client)
        await session.commit()

        print(f"\n✅ Test Client Created: {test_client.email} (ID: {test_client.id})")

        # Create 5 new jobs
        jobs_created = []
        for i in range(1, 6):
            job = Job(
                job_type="notarization",
                client_id=test_client.id,
                worker_id=1,  # bot worker
                state="CA",
                description=f"Live payout test job #{i}",
                status="completed",  # Mark as completed immediately
                service_tier="standard",
                price=50.0 + (i * 5),  # $50, $55, $60, $65, $70
                paid=True,
                completed_at=datetime.utcnow(),
            )
            session.add(job)
            jobs_created.append(job)

        await session.commit()

        print(f"\n✅ Created {len(jobs_created)} new jobs (marked completed)")

        # Generate payments for each completed job
        payments_created = []
        for i, job in enumerate(jobs_created, 1):
            price = job.price
            worker_amount = price * 0.83  # Worker gets 83%
            platform_amount = price * 0.17  # Platform gets 17%

            payment = Payment(
                id=str(uuid.uuid4()),
                job_id=str(job.id),
                worker_id="1",  # bot worker
                client_id=str(test_client.id),
                worker_amount=worker_amount,
                platform_amount=platform_amount,
                gross_amount=price,
                payout_status="pending",  # NEW - waiting for payout
                created_at=datetime.utcnow(),
            )
            session.add(payment)
            payments_created.append(payment)

            print(f"\nJob {i}: ${price:.2f}")
            print(f"  Payment ID: {payment.id[:8]}...")
            print(f"  Worker Amount: ${worker_amount:.2f}")
            print(f"  Status: PENDING (ready for Stripe processing)")

        await session.commit()

        print("\n" + "=" * 80)
        print(f"✅ Created {len(payments_created)} NEW PENDING PAYMENTS")
        print("=" * 80)

        # Summary
        total_pending = sum(p.worker_amount for p in payments_created)
        print(f"\n💰 New Revenue for Testing:")
        print(f"   Total Pending: ${total_pending:.2f}")
        print(f"   Payments: {len(payments_created)}")
        print(f"\n🚀 Next Step: Test payout processing")
        print(f"   curl -X POST https://empire-v2-production.up.railway.app/process-pending-payouts")
        print(f"\n📊 Monitor progress:")
        print(f"   python3 monitor_bot_earnings.py")


if __name__ == "__main__":
    asyncio.run(create_test_jobs_for_payout())
