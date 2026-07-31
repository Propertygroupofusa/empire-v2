#!/usr/bin/env python3
"""
Seed the database with test jobs for the bot to claim and complete.
Run this once to populate the database with work for the bot to do.
"""

import asyncio
from database import AsyncSessionLocal, engine, init_db
from models import Client, Job
from sqlalchemy import select
import os


async def seed_jobs():
    """Create test jobs and client for bot to work on"""

    # Initialize database
    await init_db()

    async with AsyncSessionLocal() as session:
        print("🌱 Seeding database with test jobs...")

        # Create test client if doesn't exist
        existing = await session.execute(
            select(Client).where(Client.email == "demo@example.com")
        )
        client = existing.scalar_one_or_none()

        if not client:
            client = Client(
                email="demo@example.com",
                name="Demo Client",
                phone="555-0000"
            )
            session.add(client)
            await session.flush()
            print(f"✅ Created client: {client.email}")
        else:
            print(f"✅ Client exists: {client.email}")

        # Create test jobs
        job_templates = [
            ("Notarization - Power of Attorney", "notarization", 100.00),
            ("Notarization - Real Estate Document", "notarization", 85.00),
            ("Notarization - Affidavit", "notarization", 75.00),
            ("Notarization - Power of Attorney (Copies)", "notarization", 95.00),
            ("Notarization - Medical Consent", "notarization", 80.00),
            ("Notarization - Vehicle Title", "notarization", 90.00),
            ("Notarization - Financial Document", "notarization", 110.00),
            ("Notarization - Legal Deposition", "notarization", 120.00),
        ]

        created_count = 0
        for desc, job_type, price in job_templates:
            existing_job = await session.execute(
                select(Job).where(
                    Job.description == desc,
                    Job.status == "requested"
                )
            )
            if not existing_job.scalar_one_or_none():
                job = Job(
                    client_id=client.id,
                    description=desc,
                    status="requested",
                    job_type=job_type,
                    price=price
                )
                session.add(job)
                created_count += 1

        await session.commit()
        print(f"✅ Created {created_count} test jobs")
        print(f"💰 Total job value: ${sum(t[2] for t in job_templates):.2f}")
        print("\n📌 The bot will now automatically claim and complete these jobs!")


if __name__ == "__main__":
    asyncio.run(seed_jobs())
