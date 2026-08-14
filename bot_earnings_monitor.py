#!/usr/bin/env python3
"""
Automated bot earnings system monitoring
Runs every 6 hours to check: bot health, jobs processed, payments, payouts
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
log = logging.getLogger("bot_earnings_monitor")

class BotEarningsMonitor:
    """Monitor bot earnings system continuously"""

    async def check_system_health(self):
        """Check overall system health"""
        try:
            async with AsyncSessionLocal() as session:
                # Check bot workers
                bot_result = await session.execute(
                    select(Worker).where(Worker.email.like("%bot%"))
                )
                bots = bot_result.scalars().all()

                # Check jobs
                jobs_result = await session.execute(
                    select(func.count(Job.id))
                )
                total_jobs = jobs_result.scalar() or 0

                completed_result = await session.execute(
                    select(func.count(Job.id)).where(Job.status == "completed")
                )
                completed_jobs = completed_result.scalar() or 0

                # Check payments
                payments_result = await session.execute(
                    select(func.count(Payment.id))
                )
                total_payments = payments_result.scalar() or 0

                total_earned = await session.execute(
                    select(func.sum(Payment.worker_amount))
                )
                total_earned_amt = total_earned.scalar() or 0

                pending = await session.execute(
                    select(func.sum(Payment.worker_amount)).where(
                        Payment.payout_status == "pending"
                    )
                )
                pending_amt = pending.scalar() or 0

                paid = await session.execute(
                    select(func.sum(Payment.worker_amount)).where(
                        Payment.payout_status == "paid"
                    )
                )
                paid_amt = paid.scalar() or 0

                # Check for real Stripe payments
                stripe_payments = await session.execute(
                    select(func.count(Payment.id)).where(
                        Payment.stripe_payout_id.isnot(None)
                    )
                )
                real_payments_count = stripe_payments.scalar() or 0

                # Log status
                log.info("=" * 60)
                log.info("🔍 BOT EARNINGS SYSTEM - MONITORING CYCLE")
                log.info("=" * 60)

                log.info(f"✅ Active Bots: {len(bots)}")
                for bot in bots:
                    log.info(f"   - {bot.email} (status: {bot.status})")

                log.info(f"📋 Jobs: {completed_jobs}/{total_jobs} completed")
                log.info(f"💰 Total Earned: ${total_earned_amt:.2f}")
                log.info(f"⏳ Pending Payout: ${pending_amt:.2f}")
                log.info(f"✅ Paid Out: ${paid_amt:.2f}")
                log.info(f"💳 Real Stripe Payments: {real_payments_count}")

                # Check for issues
                issues = []

                if total_earned_amt == 0:
                    issues.append("❌ NO EARNINGS - No jobs processed")

                if real_payments_count == 0 and total_payments > 0:
                    issues.append("⚠️  TEST MODE ONLY - No real Stripe payments")

                if len(bots) == 0:
                    issues.append("🚨 CRITICAL - No active bots")

                if total_jobs == 0:
                    issues.append("⚠️  NO JOBS - Queue empty")

                if issues:
                    log.warning("Issues detected:")
                    for issue in issues:
                        log.warning(f"  {issue}")
                else:
                    log.info("✅ No issues detected")

                log.info("=" * 60)
                log.info(f"Next check: In 6 hours")
                log.info("=" * 60)

                return {
                    "timestamp": datetime.utcnow().isoformat(),
                    "bots_active": len(bots),
                    "total_jobs": total_jobs,
                    "completed_jobs": completed_jobs,
                    "total_earned": total_earned_amt,
                    "pending": pending_amt,
                    "paid": paid_amt,
                    "real_payments": real_payments_count,
                    "issues": issues,
                    "status": "OK" if not issues else "ISSUES_DETECTED"
                }

        except Exception as e:
            log.error(f"❌ Monitoring error: {e}")
            return {"error": str(e), "status": "ERROR"}

async def run_monitoring_cycle():
    """Run a single monitoring cycle"""
    monitor = BotEarningsMonitor()
    result = await monitor.check_system_health()
    return result

def main():
    """Run monitoring"""
    log.info("🚀 Bot Earnings Monitor Started")
    log.info("Checking system every 6 hours")
    log.info("Press Ctrl+C to stop")

    try:
        while True:
            result = asyncio.run(run_monitoring_cycle())

            # Check for critical issues
            if result.get("status") == "ERROR":
                log.error("Critical error - check system")
            elif result.get("issues"):
                log.warning(f"Issues found: {len(result['issues'])}")
            else:
                log.info("All systems nominal")

            # Wait 6 hours (21600 seconds)
            log.info("Sleeping for 6 hours...")
            asyncio.run(asyncio.sleep(21600))

    except KeyboardInterrupt:
        log.info("Monitor stopped by user")

if __name__ == "__main__":
    main()
