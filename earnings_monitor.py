#!/usr/bin/env python3
"""
Bot Earnings Monitoring System
================================
Continuous monitoring of revenue generation from video orders and Stripe payments.
Checks: bot earnings, completed jobs, Stripe payments, payout status.
"""

import asyncio
import httpx
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from database import AsyncSessionLocal
from models import VideoQuoteOrder, Payment
from sqlalchemy import select, func

ET = ZoneInfo("America/New_York")

async def check_bot_earnings():
    """Check /payments/bot/earnings endpoint"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("http://localhost:8000/payments/bot/earnings")
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        print(f"❌ Failed to fetch bot earnings: {e}")
    return None

async def check_order_metrics():
    """Query order and payment metrics from database"""
    try:
        async with AsyncSessionLocal() as session:
            # Order counts
            order_result = await session.execute(select(func.count(VideoQuoteOrder.id)))
            total_orders = order_result.scalar() or 0

            paid_result = await session.execute(
                select(func.count(VideoQuoteOrder.id)).where(VideoQuoteOrder.paid == True)
            )
            paid_orders = paid_result.scalar() or 0

            # Video completion
            video_result = await session.execute(
                select(func.count(VideoQuoteOrder.id)).where(
                    VideoQuoteOrder.video_generation_status == "completed"
                )
            )
            videos_ready = video_result.scalar() or 0

            # Revenue from orders
            revenue_result = await session.execute(
                select(func.sum(VideoQuoteOrder.quote_price)).where(VideoQuoteOrder.paid == True)
            )
            order_revenue = revenue_result.scalar() or 0

            # Payment records
            payment_result = await session.execute(select(func.count(Payment.id)))
            total_payments = payment_result.scalar() or 0

            # Get latest payment
            latest_payment_result = await session.execute(
                select(Payment).order_by(Payment.created_at.desc()).limit(1)
            )
            latest_payment = latest_payment_result.scalar_one_or_none()

            return {
                "total_orders": total_orders,
                "paid_orders": paid_orders,
                "videos_ready": videos_ready,
                "order_revenue": order_revenue / 100 if order_revenue else 0,
                "total_payments": total_payments,
                "latest_payment": {
                    "amount": latest_payment.worker_amount if latest_payment else 0,
                    "status": latest_payment.payout_status if latest_payment else None,
                    "created_at": latest_payment.created_at.isoformat() if latest_payment else None
                }
            }
    except Exception as e:
        print(f"❌ Failed to query database: {e}")
        return None

async def generate_report():
    """Generate comprehensive earnings monitoring report"""
    print("\n" + "=" * 80)
    print(f"📈 BOT EARNINGS MONITORING REPORT")
    print(f"   {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 80)

    # Check bot earnings
    earnings = await check_bot_earnings()
    if earnings:
        print("\n✅ BOT EARNINGS ENDPOINT (Real Revenue Generation)")
        print(f"   Worker: {earnings['worker']}")
        print(f"   Total Earned: ${earnings['total_earned']:.2f}")
        print(f"   Paid Out: ${earnings['paid_out']:.2f}")
        print(f"   Pending Payout: ${earnings['pending_payout']:.2f}")
        print(f"   Completed Jobs: {earnings['payment_count']}")

        if earnings['payment_count'] > 0:
            print(f"   Status: ✓ REVENUE FLOWING (avg ${earnings['total_earned']/earnings['payment_count']:.2f} per job)")
        else:
            print(f"   Status: ⚠️ NO JOBS COMPLETED YET")
    else:
        print("\n❌ BOT EARNINGS ENDPOINT")
        print("   Failed to fetch data")

    # Check order metrics
    orders = await check_order_metrics()
    if orders:
        print("\n✅ VIDEO ORDER SYSTEM (Stripe + HeyGen)")
        print(f"   Total Orders Submitted: {orders['total_orders']}")
        print(f"   Paid Orders (Stripe): {orders['paid_orders']}")
        print(f"   Videos Ready (HeyGen): {orders['videos_ready']}")
        print(f"   Revenue from Orders: ${orders['order_revenue']:.2f}")
        print(f"   Total Payment Records: {orders['total_payments']}")

        if orders['latest_payment']:
            latest = orders['latest_payment']
            print(f"   Latest Payment:")
            print(f"     - Amount: ${latest['amount']:.2f}")
            print(f"     - Status: {latest['status']}")
            print(f"     - Created: {latest['created_at']}")

        # Check payment flow status
        if orders['total_orders'] > 0:
            conversion_rate = (orders['paid_orders'] / orders['total_orders']) * 100
            print(f"\n   💰 Payment Flow:")
            print(f"     - Quote → Payment Conversion: {conversion_rate:.1f}%")

        if orders['paid_orders'] > 0 and orders['videos_ready'] == 0:
            print(f"\n   ⏳ Waiting: {orders['paid_orders']} orders waiting for video generation")
        elif orders['videos_ready'] > 0:
            print(f"\n   ✅ Ready: {orders['videos_ready']} videos ready for customer download")
    else:
        print("\n❌ ORDER SYSTEM")
        print("   Failed to fetch data")

    # Overall status
    print("\n" + "=" * 80)
    if earnings and earnings['total_earned'] > 0:
        print("✅ SYSTEM STATUS: REVENUE GENERATION ACTIVE")
        print(f"   🎯 Bot has earned ${earnings['total_earned']:.2f} from {earnings['payment_count']} completed jobs")

        if orders and orders['total_orders'] > 0:
            print(f"   🎬 Video orders: {orders['total_orders']} submitted, {orders['paid_orders']} paid")

        if earnings['pending_payout'] > 0:
            print(f"   ⚠️  Pending payout: ${earnings['pending_payout']:.2f}")
        else:
            print(f"   ✅ All earnings paid out")
    else:
        print("⚠️ SYSTEM STATUS: WAITING FOR REVENUE")
        print("   No earnings yet - monitoring for first revenue events")

    print("=" * 80 + "\n")

    return {
        "earnings": earnings,
        "orders": orders,
        "timestamp": datetime.now(ET).isoformat()
    }

async def main():
    report = await generate_report()
    return report

if __name__ == "__main__":
    asyncio.run(main())
