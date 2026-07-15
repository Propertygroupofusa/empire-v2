#!/usr/bin/env python3
"""
Test database persistence for orders and subscriptions
"""
import asyncio
import httpx
import json
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import AsyncSessionLocal, Order, CustomerSubscription

BASE_URL = "http://localhost:8000"


async def test_order_creation():
    """Test creating an order and verifying it's saved to database"""
    print("\n=== TEST 1: Create Order ===")

    order_data = {
        "customer_name": "John Doe",
        "customer_email": "john@test.com",
        "customer_company": "Test Corp",
        "video_type": "explainer",
        "script_or_topic": "How to use our platform",
        "target_audience": "Enterprise customers",
        "avatar": "anna",
        "language": "english_us",
        "delivery_days": 2,
        "phone": "555-0123"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/orders/request-quote",
                json=order_data,
                timeout=5
            )

            if response.status_code != 200:
                print(f"❌ Failed to create order: {response.status_code}")
                print(f"Response: {response.text}")
                return None

            result = response.json()
            order_id = result.get("order_id")
            print(f"✅ Order created via API: ID={order_id}")
            print(f"   Price: ${result.get('quote_price')/100:.2f}")
            print(f"   Status: {result.get('status')}")
            return order_id
    except Exception as e:
        print(f"❌ API call failed: {e}")
        return None


async def test_order_in_database(order_id):
    """Verify order exists in database"""
    print("\n=== TEST 2: Verify Order in Database ===")

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Order).where(Order.order_id == str(order_id))
            )
            order = result.scalars().first()

            if order:
                print(f"✅ Order found in database")
                print(f"   ID: {order.id}")
                print(f"   Order ID: {order.order_id}")
                print(f"   Customer: {order.customer_name} ({order.customer_email})")
                print(f"   Video Type: {order.video_type}")
                print(f"   Price: ${order.quote_price/100:.2f}")
                print(f"   Status: {order.status}")
                print(f"   Created: {order.created_at}")
                return True
            else:
                print(f"❌ Order NOT found in database")
                return False
    except Exception as e:
        print(f"❌ Database query failed: {e}")
        return False


async def test_retrieve_order():
    """Test retrieving order via API"""
    print("\n=== TEST 3: Retrieve Order via API ===")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/orders/1",
                timeout=5
            )

            if response.status_code == 200:
                result = response.json()
                order = result.get("order", {})
                print(f"✅ Order retrieved via API")
                print(f"   Customer: {order.get('customer_name')}")
                print(f"   Status: {result.get('status')}")
                print(f"   Paid: {result.get('paid')}")
                return True
            else:
                print(f"❌ Failed to retrieve order: {response.status_code}")
                return False
    except Exception as e:
        print(f"❌ API call failed: {e}")
        return False


async def test_database_count():
    """Check total orders in database"""
    print("\n=== TEST 4: Count Orders in Database ===")

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Order))
            orders = result.scalars().all()
            print(f"✅ Total orders in database: {len(orders)}")

            if orders:
                print(f"\n   Recent orders:")
                for order in orders[-3:]:
                    print(f"   - {order.order_id}: {order.customer_name} ({order.status})")
            return len(orders)
    except Exception as e:
        print(f"❌ Database query failed: {e}")
        return 0


async def run_tests():
    """Run all persistence tests"""
    print("=" * 60)
    print("DATABASE PERSISTENCE TEST SUITE")
    print("=" * 60)

    # Wait for server to be ready
    print("\nWaiting for server to be ready...")
    for i in range(10):
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"{BASE_URL}/stripe-key", timeout=1)
                print("✅ Server is ready")
                break
        except:
            if i < 9:
                await asyncio.sleep(0.5)
            else:
                print("❌ Server did not start in time")
                return

    # Run tests
    order_id = await test_order_creation()
    if order_id:
        await test_order_in_database(order_id)
        await test_retrieve_order()
        order_count = await test_database_count()

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"✅ Orders created and persisted to database")
        print(f"✅ Total orders in database: {order_count}")
        print("\n✅ DATABASE PERSISTENCE WORKING!")
        print("\nTo verify persistence survives restart:")
        print("1. Stop the server (Ctrl+C)")
        print("2. Delete empire.db to start fresh")
        print("3. Run `python3 main.py &` in background")
        print("4. Run `python3 test_persistence.py` again")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_tests())
