#!/usr/bin/env python3
"""
Process pending payouts through Stripe on Railway

This script:
1. Checks for pending payments
2. Calls the Railway endpoint to process payouts
3. Monitors status transitions
4. Reports final results

Usage:
  python3 process_payouts.py
"""

import asyncio
import requests
import time
from datetime import datetime
from database import AsyncSessionLocal
from sqlalchemy import select
from models import Payment, Worker


async def check_pending_payments():
    """Check how many payments are pending"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Worker).where(Worker.email == "bot@pgusa.local")
        )
        bot_worker = result.scalar_one_or_none()

        # Get pending payments
        result = await session.execute(
            select(Payment).where(Payment.payout_status == "pending")
            .where(Payment.worker_id == str(bot_worker.id))
        )
        pending_payments = result.scalars().all()

        return pending_payments


def call_payout_endpoint(railway_url: str):
    """Call the /process-pending-payouts endpoint on Railway"""
    print("\n🚀 Calling Stripe payout endpoint on Railway...")
    print(f"   URL: {railway_url}/process-pending-payouts")

    try:
        response = requests.post(
            f"{railway_url}/process-pending-payouts",
            timeout=30,
            headers={"Content-Type": "application/json"},
        )

        print(f"   Status Code: {response.status_code}")

        if response.status_code == 200:
            result = response.json()
            print(f"\n✅ PAYOUT ENDPOINT RESPONSE:")
            print(f"   Status: {result.get('status')}")
            print(f"   Processed: {result.get('processed')}")
            print(f"   Failed: {result.get('failed')}")
            print(f"   Message: {result.get('message')}")
            return result
        else:
            print(f"\n❌ ERROR: {response.status_code}")
            print(f"   Response: {response.text}")
            return None

    except requests.exceptions.ConnectionError as e:
        print(f"\n❌ CONNECTION ERROR: {e}")
        print("   Make sure Railway deployment is active")
        return None
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return None


async def monitor_payout_status(check_interval=10, max_checks=30):
    """Monitor payout status transitions"""
    print("\n📊 MONITORING PAYOUT STATUS...")
    print(f"   Checking every {check_interval} seconds (max {max_checks} checks)")

    for i in range(max_checks):
        await asyncio.sleep(check_interval)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Worker).where(Worker.email == "bot@pgusa.local")
            )
            bot_worker = result.scalar_one_or_none()

            # Get all payments
            result = await session.execute(
                select(Payment).where(Payment.worker_id == str(bot_worker.id))
            )
            all_payments = result.scalars().all()

            # Count by status
            pending_count = sum(1 for p in all_payments if p.payout_status == "pending")
            processing_count = sum(
                1 for p in all_payments if p.payout_status == "processing"
            )
            paid_count = sum(1 for p in all_payments if p.payout_status == "paid")

            pending_amt = sum(
                p.worker_amount for p in all_payments if p.payout_status == "pending"
            )
            processing_amt = sum(
                p.worker_amount for p in all_payments if p.payout_status == "processing"
            )
            paid_amt = sum(
                p.worker_amount for p in all_payments if p.payout_status == "paid"
            )

            print(
                f"\n   Check {i+1}/{max_checks} - {datetime.utcnow().isoformat()}:"
            )
            print(f"      ⏳ Pending: {pending_count} (${pending_amt:.2f})")
            print(f"      🔄 Processing: {processing_count} (${processing_amt:.2f})")
            print(f"      ✅ Paid: {paid_count} (${paid_amt:.2f})")

            # Check if all done
            if pending_count == 0 and processing_count == 0:
                print(f"\n✅ ALL PAYOUTS COMPLETE!")
                print(f"   Total Paid Out: ${paid_amt:.2f}")
                return True

    print(f"\n⏳ Monitoring timeout - payouts may still be processing")
    return False


async def main():
    """Main process flow"""
    print("=" * 80)
    print("💰 STRIPE PAYOUT PROCESSOR")
    print("=" * 80)

    # Check pending payments
    print("\n1️⃣  CHECKING PENDING PAYMENTS...")
    pending_payments = await check_pending_payments()
    print(f"   Found: {len(pending_payments)} pending payments")

    if len(pending_payments) == 0:
        print("\n✅ No pending payments to process")
        return

    total_pending = sum(p.worker_amount for p in pending_payments)
    print(f"   Total Amount: ${total_pending:.2f}")

    # Call Railway endpoint
    print("\n2️⃣  PROCESSING PAYOUTS ON RAILWAY...")
    railway_url = "https://empire-v2-production.up.railway.app"
    result = call_payout_endpoint(railway_url)

    if not result:
        print("\n❌ Failed to process payouts")
        print("   Check that:")
        print("   - Railway deployment is active")
        print("   - STRIPE_SECRET_KEY is configured")
        print("   - Network connection is available")
        return

    # Monitor status
    print("\n3️⃣  MONITORING PAYOUT TRANSITIONS...")
    success = await monitor_payout_status()

    # Final report
    print("\n" + "=" * 80)
    print("📊 FINAL REPORT")
    print("=" * 80)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Worker).where(Worker.email == "bot@pgusa.local")
        )
        bot_worker = result.scalar_one_or_none()

        result = await session.execute(
            select(Payment).where(Payment.worker_id == str(bot_worker.id))
        )
        all_payments = result.scalars().all()

        paid_amt = sum(p.worker_amount for p in all_payments if p.payout_status == "paid")
        pending_amt = sum(
            p.worker_amount for p in all_payments if p.payout_status == "pending"
        )
        processing_amt = sum(
            p.worker_amount for p in all_payments if p.payout_status == "processing"
        )

        print(f"\n💳 STRIPE ACCOUNT STATUS:")
        print(f"   ✅ Paid Out: ${paid_amt:.2f}")
        if processing_amt > 0:
            print(f"   🔄 Processing: ${processing_amt:.2f}")
        if pending_amt > 0:
            print(f"   ⏳ Still Pending: ${pending_amt:.2f}")

        print(f"\n🏦 NEXT STEPS:")
        if processing_amt > 0 or pending_amt > 0:
            print(f"   1. Wait for Stripe to confirm payouts (2-5 minutes)")
            print(f"   2. Run monitor to track status: python3 monitor_bot_earnings.py")
            print(f"   3. Funds arrive in Navy Federal within 1-2 business days")
        else:
            print(f"   ✅ All funds processed")
            print(f"   🏦 Funds will arrive in Navy Federal within 1-2 business days")

        print(f"\n" + "=" * 80)


if __name__ == "__main__":
    print("⚠️  This script processes REAL Stripe payouts")
    print("   Ensure STRIPE_SECRET_KEY is configured on Railway")
    print("")
    asyncio.run(main())
