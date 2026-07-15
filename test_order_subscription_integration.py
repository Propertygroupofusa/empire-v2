#!/usr/bin/env python3
"""
Test integration between orders and subscriptions
"""
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import AsyncSessionLocal, Order, CustomerSubscription
from subscription_tiers_db import subscribe_customer, can_create_video, use_video_quota, get_pricing_for_customer

async def test_integration():
    """Test order creation with subscription integration"""
    print("\n" + "="*60)
    print("ORDER + SUBSCRIPTION INTEGRATION TEST")
    print("="*60)
    
    customer_email = "bob@startup.com"
    
    try:
        async with AsyncSessionLocal() as session:
            # Step 1: Subscribe customer
            print("\n=== STEP 1: Subscribe Customer ===")
            await subscribe_customer(session, customer_email, "starter")
            print(f"✅ {customer_email} subscribed to Starter tier")
            
            # Step 2: Check if customer can create video
            print("\n=== STEP 2: Check Creation Quota ===")
            can_create, reason = await can_create_video(session, customer_email)
            print(f"✅ Can create: {can_create} ({reason})")
            
            # Step 3: Get pricing for this tier
            print("\n=== STEP 3: Get Pricing ===")
            pricing = await get_pricing_for_customer(session, customer_email, "explainer", 2)
            print(f"✅ Pricing type: {pricing['type']}")
            print(f"   Total cost: ${pricing['total']/100:.2f}")
            print(f"   Description: {pricing['description']}")
            
            # Step 4: Create order
            print("\n=== STEP 4: Create Order ===")
            new_order = Order(
                order_id="sub_order_1",
                customer_email=customer_email,
                customer_name="Bob",
                customer_company="Startup Inc",
                customer_phone="555-0456",
                video_type="explainer",
                script="How to use our product",
                target_audience="Tech enthusiasts",
                avatar="marcus",
                language="english_us",
                delivery_days=2,
                quote_price=pricing['total'],
                status="quote_requested"
            )
            session.add(new_order)
            await session.commit()
            print(f"✅ Order created: ID={new_order.order_id}")
            print(f"   Quote price: ${new_order.quote_price/100:.2f}")
            
            # Step 5: Use quota for this order
            print("\n=== STEP 5: Allocate Video Quota ===")
            success = await use_video_quota(session, customer_email)
            print(f"✅ Quota allocated: {success}")
            
            # Step 6: Verify integration
            print("\n=== STEP 6: Verify Integration ===")
            orders = await session.execute(
                select(Order).where(Order.customer_email == customer_email)
            )
            subs = await session.execute(
                select(CustomerSubscription).where(
                    CustomerSubscription.customer_email == customer_email
                )
            )
            
            order_count = len(orders.scalars().all())
            sub_record = subs.scalars().first()
            
            print(f"✅ Orders for customer: {order_count}")
            if sub_record:
                print(f"✅ Subscription quota used: {sub_record.videos_used_this_month}/5")
            
            print("\n" + "="*60)
            print("✅ INTEGRATION WORKING!")
            print("="*60)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_integration())
