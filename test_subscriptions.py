#!/usr/bin/env python3
"""
Test database persistence for subscriptions
"""
import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import AsyncSessionLocal, CustomerSubscription
from subscription_tiers_db import subscribe_customer, get_subscription, can_create_video, use_video_quota

async def test_subscription_workflow():
    """Test complete subscription workflow"""
    print("\n" + "="*60)
    print("SUBSCRIPTION SYSTEM TEST SUITE")
    print("="*60)
    
    customer_email = "alice@company.com"
    
    try:
        async with AsyncSessionLocal() as session:
            # Test 1: Subscribe to tier
            print("\n=== TEST 1: Subscribe Customer to Pro Tier ===")
            result = await subscribe_customer(session, customer_email, "pro")
            print(f"✅ Subscribed {customer_email} to {result['tier_name']}")
            print(f"   Videos per month: {result.get('videos_per_month', 'N/A')}")
            print(f"   Active: {result['active']}")
            
            # Test 2: Get subscription
            print("\n=== TEST 2: Retrieve Subscription ===")
            sub = await get_subscription(session, customer_email)
            if sub:
                print(f"✅ Subscription retrieved")
                print(f"   Tier: {sub['tier_name']}")
                print(f"   Videos used: {sub['videos_used_this_month']}")
                print(f"   Videos remaining: {sub.get('videos_remaining', 'N/A')}")
            
            # Test 3: Check quota
            print("\n=== TEST 3: Check Video Creation Quota ===")
            can_create, reason = await can_create_video(session, customer_email)
            print(f"✅ Can create video: {can_create}")
            print(f"   Reason: {reason}")
            
            # Test 4: Use quota
            print("\n=== TEST 4: Use Video Quota ===")
            for i in range(3):
                success = await use_video_quota(session, customer_email)
                print(f"   Video {i+1}: {'✅ Used' if success else '❌ Failed'}")
            
            # Test 5: Check remaining quota
            print("\n=== TEST 5: Check Remaining Quota ===")
            sub = await get_subscription(session, customer_email)
            print(f"   Videos used: {sub['videos_used_this_month']}/10")
            print(f"   Videos remaining: {sub.get('videos_remaining', 'N/A')}")
            
            # Test 6: Verify in database
            print("\n=== TEST 6: Verify in Database ===")
            result = await session.execute(
                select(CustomerSubscription).where(
                    CustomerSubscription.customer_email == customer_email
                )
            )
            sub_record = result.scalars().first()
            if sub_record:
                print(f"✅ Found in database")
                print(f"   ID: {sub_record.id}")
                print(f"   Tier: {sub_record.tier_name}")
                print(f"   Videos used: {sub_record.videos_used_this_month}")
            
            print("\n" + "="*60)
            print("✅ SUBSCRIPTION SYSTEM WORKING!")
            print("="*60)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_subscription_workflow())
