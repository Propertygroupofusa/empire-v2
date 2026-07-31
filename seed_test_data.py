#!/usr/bin/env python3
"""Seed test data for bot earnings system - adds workers, clients, jobs, and payments."""

import asyncio
from datetime import datetime, timedelta
from database import AsyncSessionLocal
from models import Worker, Client, Job, Payment
import uuid

async def seed_test_data():
    """Add test workers, clients, jobs, and payments to the database."""
    async with AsyncSessionLocal() as session:
        try:
            # Use timestamp to make emails unique across runs
            ts = datetime.utcnow().timestamp()

            # 1. Create test worker with unique email
            worker = Worker(
                email=f"bot-worker-{int(ts)}@example.com",
                name="Test Bot Worker",
                phone="+1-555-0100",
                status="active",
                w9_submitted=True,
                w9_legal_name="Test Worker",
                w9_tax_classification="individual",
                w9_tin_last4="1234",
                credentials_submitted=True,
                credentials_verified=True,
                stripe_account_id="acct_test_" + uuid.uuid4().hex[:16],
            )
            session.add(worker)
            await session.flush()
            worker_id = worker.id
            print(f"✅ Created test worker (ID: {worker_id}, email: {worker.email})")

            # 2. Create test client with unique email
            client = Client(
                email=f"test-client-{int(ts)}@example.com",
                name="Test Client",
                company="Test Company",
                phone="+1-555-0200",
                status="active",
            )
            session.add(client)
            await session.flush()
            client_id = client.id
            print(f"✅ Created test client (ID: {client_id})")

            # 3. Create completed jobs with payments
            job_count = 5
            for i in range(job_count):
                job = Job(
                    job_type="notarization",
                    client_id=client_id,
                    worker_id=worker_id,
                    state="CA",
                    description=f"Test notarization job {i+1}",
                    status="completed",
                    service_tier="standard",
                    price=150.00,
                    paid=True,
                    stripe_session_id=f"cs_test_{uuid.uuid4().hex[:8]}",
                    created_at=datetime.utcnow() - timedelta(days=i),
                    completed_at=datetime.utcnow() - timedelta(days=i-1),
                )
                session.add(job)
                await session.flush()

                # Create payment for this job
                gross = 150.00
                platform_fee = 30.00  # 20% platform fee
                worker_earnings = gross - platform_fee

                payment = Payment(
                    id=f"pay_{uuid.uuid4().hex[:16]}",
                    job_id=str(job.id),
                    worker_id=str(worker_id),
                    client_id=str(client_id),
                    gross_amount=gross,
                    worker_amount=worker_earnings,
                    platform_amount=platform_fee,
                    payout_status="paid",
                    stripe_payout_id=f"po_test_{uuid.uuid4().hex[:8]}",
                    stripe_transfer_id=f"tr_test_{uuid.uuid4().hex[:8]}",
                    created_at=datetime.utcnow() - timedelta(days=i),
                    paid_at=datetime.utcnow() - timedelta(days=i-1),
                )
                session.add(payment)
                print(f"✅ Created job {i+1} with payment (${worker_earnings:.2f} to worker)")

            await session.commit()
            print(f"\n🎉 Successfully seeded {job_count} test jobs with payments!")
            print(f"Total worker earnings: ${job_count * (gross - platform_fee):.2f}")
            print(f"Total platform fees: ${job_count * platform_fee:.2f}")

        except Exception as e:
            await session.rollback()
            print(f"❌ Error seeding data: {e}")
            raise

if __name__ == "__main__":
    asyncio.run(seed_test_data())
