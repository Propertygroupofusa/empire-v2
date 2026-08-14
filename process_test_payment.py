#!/usr/bin/env python3
"""Process a test Stripe payment to generate Stripe balance"""

import asyncio
import stripe
import os
import requests
import json
from datetime import datetime
import uuid

# Set Stripe key
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY")
if not STRIPE_KEY:
    print("❌ STRIPE_SECRET_KEY not set")
    exit(1)

stripe.api_key = STRIPE_KEY
BASE_URL = "http://localhost:8000"  # Local testing
RAILWAY_URL = "https://empire-v2-production.up.railway.app"  # Production

def get_base_url():
    """Detect if running locally or on Railway"""
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=2)
        print(f"✓ Using local server: {BASE_URL}")
        return BASE_URL
    except:
        print(f"✓ Using Railway server: {RAILWAY_URL}")
        return RAILWAY_URL

def create_test_payment():
    """Create a test Stripe charge and process through our system"""

    base_url = get_base_url()

    # Step 1: Create order via quote request
    order_data = {
        "customer_name": "Test Customer",
        "customer_email": f"test_{uuid.uuid4().hex[:6]}@test.local",
        "customer_company": "Test Inc",
        "customer_phone": "555-0123",
        "video_type": "explainer",
        "script_or_topic": "Test video script",
        "target_audience": "business",
        "avatar": "anna",
        "language": "english_us",
        "delivery_days": 3,
        "reference_url": ""
    }

    print(f"\n📝 Creating test order...")
    print(f"   Email: {order_data['customer_email']}")

    try:
        response = requests.post(
            f"{base_url}/orders/request-quote",
            json=order_data,
            timeout=10
        )
        response.raise_for_status()
        order_response = response.json()
        order_id = order_response.get("order_id")
        quote_price = order_response.get("quote_price")

        print(f"✓ Order created: {order_id}")
        print(f"✓ Quote price: ${quote_price/100:.2f}")

    except Exception as e:
        print(f"❌ Failed to create order: {e}")
        return False

    # Step 2: Create Stripe checkout session
    print(f"\n💳 Creating Stripe checkout session...")

    try:
        response = requests.post(
            f"{base_url}/orders/{order_id}/create-checkout",
            timeout=10
        )
        response.raise_for_status()
        checkout_response = response.json()
        session_id = checkout_response.get("session_id")

        print(f"✓ Checkout session: {session_id}")

    except Exception as e:
        print(f"❌ Failed to create checkout: {e}")
        return False

    # Step 3: Process payment with Stripe test card
    print(f"\n⚡ Processing test payment with Stripe...")

    try:
        # Create a test charge
        charge = stripe.Charge.create(
            amount=quote_price,  # in cents
            currency="usd",
            source="tok_visa",  # Stripe test token
            description=f"Test payment for order {order_id}",
            metadata={
                "order_id": order_id,
                "customer_email": order_data["customer_email"]
            }
        )

        print(f"✓ Stripe charge created: {charge.id}")
        print(f"✓ Amount: ${charge.amount/100:.2f}")

    except Exception as e:
        print(f"❌ Failed to create charge: {e}")
        return False

    # Step 4: Simulate webhook callback
    print(f"\n🔗 Simulating Stripe webhook callback...")

    try:
        # Create webhook payload
        webhook_payload = {
            "type": "charge.succeeded",
            "data": {
                "object": {
                    "id": charge.id,
                    "amount": charge.amount,
                    "currency": charge.currency,
                    "metadata": charge.metadata,
                    "status": "succeeded"
                }
            }
        }

        # Call webhook endpoint
        response = requests.post(
            f"{base_url}/orders/webhook/stripe",
            json=webhook_payload,
            timeout=10
        )
        response.raise_for_status()
        webhook_response = response.json()

        print(f"✓ Webhook processed")
        print(f"  Status: {webhook_response.get('status')}")

    except Exception as e:
        print(f"❌ Webhook failed (non-critical): {e}")
        # Don't fail - payment still processed

    print(f"\n✅ Test payment complete!")
    print(f"   Order: {order_id}")
    print(f"   Stripe charge: {charge.id}")
    print(f"   Amount: ${charge.amount/100:.2f}")
    print(f"\n💰 Run monitor_bot_earnings.py to verify Stripe balance updated")

    return True

if __name__ == "__main__":
    success = create_test_payment()
    exit(0 if success else 1)
