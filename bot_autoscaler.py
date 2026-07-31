"""Auto-scaling bot manager - creates new bots based on job demand"""
import asyncio
import logging
from datetime import datetime
from database import AsyncSessionLocal
from models import Worker, Job, Payment
from sqlalchemy import select, func
from passlib.context import CryptContext

log = logging.getLogger("bot_autoscaler")


async def get_bot_metrics():
    """Get current bot metrics to determine if scaling is needed"""
    async with AsyncSessionLocal() as session:
        # Get pending jobs
        jobs_result = await session.execute(
            select(func.count(Job.id)).where(Job.status == "requested")
        )
        pending_jobs = jobs_result.scalar() or 0

        # Get active bots
        bots_result = await session.execute(
            select(Worker).where(
                Worker.email.like("%bot%pgusa.local"), Worker.status == "active"
            )
        )
        active_bots = bots_result.scalars().all()

        # Calculate bot efficiency (jobs completed per bot)
        if active_bots:
            bot_ids = [str(b.id) for b in active_bots]
            payments_result = await session.execute(
                select(func.count(Payment.id)).where(
                    Payment.worker_id.in_(bot_ids)
                )
            )
            total_jobs_done = payments_result.scalar() or 0
            jobs_per_bot = total_jobs_done / len(active_bots)
        else:
            jobs_per_bot = 0

        return {
            "pending_jobs": pending_jobs,
            "active_bots": len(active_bots),
            "jobs_per_bot": jobs_per_bot,
            "total_completed": total_jobs_done if active_bots else 0,
        }


async def create_bot(bot_number: int) -> Worker:
    """Create a new bot worker"""
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    async with AsyncSessionLocal() as session:
        bot_email = f"bot{bot_number}@pgusa.local"

        # Check if bot already exists
        existing = await session.execute(
            select(Worker).where(Worker.email == bot_email)
        )
        if existing.scalar_one_or_none():
            return None

        bot = Worker(
            email=bot_email,
            name=f"Job Bot {bot_number}",
            status="active",
            password_hash=pwd_context.hash("auto_bot_password_123"),
        )
        session.add(bot)
        await session.commit()
        log.info(f"✨ New bot created: {bot_email}")
        return bot


async def auto_scale_bots():
    """Monitor job queue and auto-scale bots"""
    log.info("🚀 Bot auto-scaler started")

    while True:
        try:
            metrics = await get_bot_metrics()
            pending = metrics["pending_jobs"]
            active = metrics["active_bots"]
            jobs_per_bot = metrics["jobs_per_bot"]

            log.info(
                f"📊 Metrics - Pending: {pending}, Active Bots: {active}, Avg/Bot: {jobs_per_bot:.1f}"
            )

            # Scaling logic
            # If more than 5 jobs per bot, create a new bot
            if jobs_per_bot > 5 and pending > active * 3:
                next_bot_num = active + 1
                await create_bot(next_bot_num)
                log.info(
                    f"📈 SCALED UP: Created bot{next_bot_num}@pgusa.local (demand: {pending} jobs, {active} bots)"
                )

            # If queue is building up fast, create multiple bots
            elif pending > active * 5:
                for i in range(2):
                    next_bot_num = active + i + 1
                    await create_bot(next_bot_num)
                    log.info(
                        f"🚀 RAPID SCALE: Created bot{next_bot_num}@pgusa.local (high demand: {pending} jobs)"
                    )

            # Scale down if bots are idle (queue empty for 2+ cycles)
            elif pending == 0 and active > 2:
                log.info(
                    f"📉 Queue empty, maintaining {active} bots for quick response"
                )

            # Log performance
            if metrics["total_completed"] > 0:
                avg_earnings_per_bot = metrics["total_completed"] * 100 / active
                log.info(f"💰 Total jobs completed: {metrics['total_completed']}")
                log.info(f"💵 Avg jobs per bot: {jobs_per_bot:.1f}")

            await asyncio.sleep(30)  # Check every 30 seconds

        except Exception as e:
            log.error(f"Auto-scaler error: {e}")
            await asyncio.sleep(30)


# Scaling triggers:
# - 5+ jobs per bot → create 1 new bot
# - 5x+ jobs than bots → create 2 new bots
# - Empty queue → maintain current count (don't waste resources)
# - High demand → rapid scaling
