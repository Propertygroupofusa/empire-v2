#!/usr/bin/env python3
"""
Real-time bot earnings monitoring - tracks revenue from jobs, payments, and Stripe payouts
"""

import asyncio
import logging
from datetime import datetime
from database import AsyncSessionLocal
from models import Payment, Job, Worker
from sqlalchemy import select, func

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger("earnings_monitor")

async def monitor_bot_earnings():
    """Monitor bot earnings in real-time"""
    try:
        async with AsyncSessionLocal() as session:
            # Get bot worker
            result = await session.execute(
                select(Worker).where(Worker.email == "bot@pgusa.local")
            )
            bot_worker = result.scalar_one_or_none()

            if not bot_worker:
                log.warning("❌ Bot worker not found")
                return None

            # Get all payments for bot
            payments_result = await session.execute(
                select(Payment).where(Payment.worker_id == str(bot_worker.id))
            )
            payments = payments_result.scalars().all()

            # Calculate totals
            total_earned = sum(p.worker_amount for p in payments)
            pending = sum(p.worker_amount for p in payments if p.payout_status == "pending")
            processing = sum(p.worker_amount for p in payments if p.payout_status == "processing")
            paid = sum(p.worker_amount for p in payments if p.payout_status == "paid")
            failed = sum(p.worker_amount for p in payments if p.payout_status == "failed")

            # Get job stats
            jobs_result = await session.execute(select(func.count(Job.id)))
            total_jobs = jobs_result.scalar() or 0

            completed_result = await session.execute(
                select(func.count(Job.id)).where(Job.status == "completed")
            )
            completed_jobs = completed_result.scalar() or 0

            # Get Stripe payout count
            stripe_result = await session.execute(
                select(func.count(Payment.id)).where(
                    Payment.stripe_payout_id.isnot(None)
                )
            )
            real_payouts = stripe_result.scalar() or 0

            # Build report
            report = {
                "timestamp": datetime.utcnow().isoformat(),
                "bot_status": bot_worker.status,
                "payments": {
                    "total_count": len(payments),
                    "total_earned": round(total_earned, 2),
                    "pending": round(pending, 2),
                    "processing": round(processing, 2),
                    "paid": round(paid, 2),
                    "failed": round(failed, 2),
                },
                "jobs": {
                    "total": total_jobs,
                    "completed": completed_jobs,
                    "completion_rate": f"{(completed_jobs/total_jobs*100):.1f}%" if total_jobs > 0 else "0%",
                },
                "stripe": {
                    "real_payouts": real_payouts,
                    "status": "Processing" if real_payouts > 0 else "Pending",
                }
            }

            # Log report
            log.info("=" * 70)
            log.info("🤖 BOT EARNINGS MONITOR")
            log.info("=" * 70)
            log.info(f"✅ Bot Status: {report['bot_status']}")
            log.info(f"💰 Total Earned: ${report['payments']['total_earned']:.2f}")
            log.info(f"⏳ Pending: ${report['payments']['pending']:.2f}")
            log.info(f"🔄 Processing: ${report['payments']['processing']:.2f}")
            log.info(f"✨ Paid Out: ${report['payments']['paid']:.2f}")
            if report['payments']['failed'] > 0:
                log.warning(f"❌ Failed: ${report['payments']['failed']:.2f}")
            log.info(f"📋 Jobs: {report['jobs']['completed']}/{report['jobs']['total']} completed ({report['jobs']['completion_rate']})")
            log.info(f"💳 Real Stripe Payouts: {report['stripe']['real_payouts']}")

            # Check for issues
            issues = []
            if report['payments']['total_earned'] == 0:
                issues.append("⚠️  NO REVENUE - No payments recorded")
            elif report['payments']['pending'] == report['payments']['total_earned']:
                issues.append("⚠️  PENDING ONLY - No Stripe processing yet")

            if report['stripe']['real_payouts'] == 0 and report['payments']['total_count'] > 0:
                issues.append("⚠️  STRIPE IDLE - Payments pending Stripe webhook")

            if report['jobs']['completion_rate'] == "0%":
                issues.append("⚠️  NO JOB COMPLETION - Queue not processing")

            if issues:
                log.warning("\n⚠️  Issues Detected:")
                for issue in issues:
                    log.warning(f"   {issue}")
            else:
                log.info("\n✅ All systems operational - Revenue flowing")

            log.info("=" * 70)

            return report

    except Exception as e:
        log.error(f"❌ Monitor error: {e}", exc_info=True)
        return None

async def main():
    log.info("🚀 Bot Earnings Monitor Started")
    log.info("Monitoring revenue generation cycles")

    result = await monitor_bot_earnings()

    if result:
        log.info(f"Cycle complete - Next check in 5 minutes")
        log.info(f"Revenue Status: ${result['payments']['total_earned']:.2f} total")
    else:
        log.error("Monitor cycle failed")

if __name__ == "__main__":
    asyncio.run(main())
