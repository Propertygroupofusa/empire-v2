#!/usr/bin/env python3
"""
Real Alerts for Revenue Issues
================================
Detects problems as they occur (not just on time-based checks):
- Blocked payment flow (orders stuck)
- Missing/delayed payouts
- Failed video generation
- New revenue events (earns or orders)
- Conversion rate drops
"""

import asyncio
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import AsyncSessionLocal
from models import VideoQuoteOrder, Payment
from sqlalchemy import select, func

ET = ZoneInfo("America/New_York")

# Alert thresholds
MIN_PAYOUT_TIME_HOURS = 24  # Alert if payment not paid within 24h
VIDEO_GEN_TIMEOUT_HOURS = 4  # Alert if video not generated within 4h
CONVERSION_DROP_PCT = 30  # Alert if conversion rate drops >30%
REVENUE_CHECK_INTERVAL_SEC = 60  # Check every 60 seconds


class EarningsAlertSystem:
    """Detects issues that block revenue flow"""

    def __init__(self):
        self.last_state = {
            "total_earned": 0,
            "completed_jobs": 0,
            "paid_orders": 0,
            "videos_ready": 0,
            "pending_payouts": 0,
            "last_check": datetime.now(ET)
        }
        self.alerts_issued = []

    async def get_current_state(self):
        """Fetch current system state"""
        try:
            # Get bot earnings
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("http://localhost:8000/payments/bot/earnings")
                if resp.status_code != 200:
                    return None

                earnings = resp.json()

            # Get order metrics from database
            async with AsyncSessionLocal() as session:
                # Paid orders
                paid_result = await session.execute(
                    select(func.count(VideoQuoteOrder.id)).where(VideoQuoteOrder.paid == True)
                )
                paid_orders = paid_result.scalar() or 0

                # Videos ready
                video_result = await session.execute(
                    select(func.count(VideoQuoteOrder.id)).where(
                        VideoQuoteOrder.video_generation_status == "completed"
                    )
                )
                videos_ready = video_result.scalar() or 0

                # Pending payouts (not yet paid)
                pending_result = await session.execute(
                    select(func.count(Payment.id)).where(
                        Payment.payout_status.in_(["pending", "processing"])
                    )
                )
                pending_payouts = pending_result.scalar() or 0

                # Check for stuck orders (paid but no video for >4 hours)
                stuck_result = await session.execute(
                    select(func.count(VideoQuoteOrder.id)).where(
                        VideoQuoteOrder.paid == True,
                        VideoQuoteOrder.video_generation_status.in_(["pending", "generating", "failed"]),
                        VideoQuoteOrder.requested_at < datetime.utcnow() - timedelta(hours=4)
                    )
                )
                stuck_orders = stuck_result.scalar() or 0

                return {
                    "total_earned": earnings.get("total_earned", 0),
                    "completed_jobs": earnings.get("payment_count", 0),
                    "paid_orders": paid_orders,
                    "videos_ready": videos_ready,
                    "pending_payouts": pending_payouts,
                    "stuck_orders": stuck_orders,
                    "timestamp": datetime.now(ET),
                    "worker": earnings.get("worker")
                }

        except Exception as e:
            print(f"❌ Failed to get current state: {e}")
            return None

    async def check_for_alerts(self, current_state):
        """Detect issues that block revenue"""
        if not current_state:
            return

        alerts = []

        # ALERT 1: New Earnings Detected ✅
        if current_state["completed_jobs"] > self.last_state["completed_jobs"]:
            new_jobs = current_state["completed_jobs"] - self.last_state["completed_jobs"]
            earnings_gained = current_state["total_earned"] - self.last_state["total_earned"]
            alerts.append({
                "severity": "SUCCESS",
                "type": "new_revenue",
                "message": f"🎉 NEW REVENUE: {new_jobs} job(s) completed, earned ${earnings_gained:.2f}",
                "data": {
                    "jobs": new_jobs,
                    "amount": earnings_gained,
                    "total_earned": current_state["total_earned"]
                }
            })

        # ALERT 2: Stuck Payouts ❌
        if current_state["pending_payouts"] > 0:
            async with AsyncSessionLocal() as session:
                stuck_result = await session.execute(
                    select(Payment).where(
                        Payment.payout_status == "pending",
                        Payment.created_at < datetime.utcnow() - timedelta(hours=MIN_PAYOUT_TIME_HOURS)
                    )
                )
                stuck_payments = stuck_result.scalars().all()

                if stuck_payments:
                    for payment in stuck_payments:
                        hours_stuck = (datetime.utcnow() - payment.created_at).total_seconds() / 3600
                        alerts.append({
                            "severity": "CRITICAL",
                            "type": "stuck_payout",
                            "message": f"❌ PAYOUT BLOCKED: Payment ${payment.worker_amount:.2f} stuck for {hours_stuck:.1f}h",
                            "data": {
                                "payment_id": payment.id,
                                "amount": payment.worker_amount,
                                "hours_stuck": hours_stuck,
                                "status": payment.payout_status
                            }
                        })

        # ALERT 3: Failed Video Generation ❌
        if current_state["paid_orders"] > current_state["videos_ready"]:
            async with AsyncSessionLocal() as session:
                failed_result = await session.execute(
                    select(VideoQuoteOrder).where(
                        VideoQuoteOrder.paid == True,
                        VideoQuoteOrder.video_generation_status == "failed"
                    )
                )
                failed_videos = failed_result.scalars().all()

                if failed_videos:
                    for order in failed_videos:
                        alerts.append({
                            "severity": "WARNING",
                            "type": "video_generation_failed",
                            "message": f"⚠️  VIDEO FAILED: Order #{order.id} - customer not receiving video",
                            "data": {
                                "order_id": order.id,
                                "customer_email": order.customer_email,
                                "status": order.video_generation_status
                            }
                        })

        # ALERT 4: Stuck Video Generation (paid but no video after 4 hours) ⏳
        if current_state["stuck_orders"] > 0:
            alerts.append({
                "severity": "WARNING",
                "type": "stuck_video_generation",
                "message": f"⏳ TIMEOUT: {current_state['stuck_orders']} order(s) waiting for video generation >4h",
                "data": {
                    "stuck_orders": current_state["stuck_orders"],
                    "action_needed": "Check HeyGen API status"
                }
            })

        # ALERT 5: Conversion Rate Dropped ❌
        if self.last_state["paid_orders"] > 0:
            last_conversion = self.last_state["paid_orders"]
            current_conversion = current_state["paid_orders"]
            if current_conversion < last_conversion:
                drop_pct = ((last_conversion - current_conversion) / last_conversion) * 100
                alerts.append({
                    "severity": "WARNING",
                    "type": "conversion_rate_drop",
                    "message": f"📉 CONVERSION DROP: {drop_pct:.1f}% decrease in paid orders",
                    "data": {
                        "drop_percentage": drop_pct,
                        "previous": last_conversion,
                        "current": current_conversion
                    }
                })

        # ALERT 6: No Activity (no new earnings in last 2 hours) ⚠️
        if current_state["completed_jobs"] == self.last_state["completed_jobs"]:
            time_since_last = (datetime.now(ET) - self.last_state["last_check"]).total_seconds() / 3600
            if time_since_last > 2:
                alerts.append({
                    "severity": "INFO",
                    "type": "no_activity",
                    "message": f"ℹ️  NO RECENT ACTIVITY: {time_since_last:.1f}h since last revenue event",
                    "data": {
                        "hours_idle": time_since_last,
                        "last_job_count": self.last_state["completed_jobs"]
                    }
                })

        return alerts

    async def issue_alerts(self, alerts):
        """Display and log alerts"""
        if not alerts:
            return

        print("\n" + "🚨" * 40)
        print(f"ALERT CHECK - {len(alerts)} ISSUE(S) DETECTED")
        print("🚨" * 40)

        for alert in alerts:
            severity = alert["severity"]
            msg = alert["message"]

            if severity == "CRITICAL":
                print(f"\n❌ CRITICAL: {msg}")
                print(f"   Data: {alert['data']}")
                print(f"   ACTION REQUIRED: Fix immediately to resume revenue")

            elif severity == "WARNING":
                print(f"\n⚠️  WARNING: {msg}")
                print(f"   Data: {alert['data']}")

            elif severity == "SUCCESS":
                print(f"\n✅ {msg}")
                print(f"   Data: {alert['data']}")

            elif severity == "INFO":
                print(f"\nℹ️  {msg}")

            self.alerts_issued.append({
                "timestamp": datetime.now(ET).isoformat(),
                "severity": severity,
                "type": alert["type"],
                "message": msg
            })

        print("\n" + "🚨" * 40 + "\n")

    def update_state(self, current_state):
        """Update baseline for next cycle"""
        self.last_state = {
            "total_earned": current_state["total_earned"],
            "completed_jobs": current_state["completed_jobs"],
            "paid_orders": current_state["paid_orders"],
            "videos_ready": current_state["videos_ready"],
            "pending_payouts": current_state["pending_payouts"],
            "last_check": datetime.now(ET)
        }

    async def run(self):
        """Main monitoring loop - run continuously"""
        print("🔔 EARNINGS ALERT SYSTEM STARTED")
        print("   Monitoring: blocked payments, failed videos, new revenue")
        print("   Alert interval: every 60 seconds\n")

        while True:
            try:
                current_state = await self.get_current_state()
                if current_state:
                    alerts = await self.check_for_alerts(current_state)
                    if alerts:
                        await self.issue_alerts(alerts)
                    self.update_state(current_state)
                else:
                    print("⚠️  Could not fetch current state - retrying in 60s")

                # Wait for next check
                await asyncio.sleep(REVENUE_CHECK_INTERVAL_SEC)

            except KeyboardInterrupt:
                print("\n⏹️  Alert system stopped")
                break
            except Exception as e:
                print(f"❌ Alert loop error: {e}")
                await asyncio.sleep(REVENUE_CHECK_INTERVAL_SEC)


async def main():
    system = EarningsAlertSystem()
    await system.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAlert system stopped")
