"""
Customer Order & Quote Management Router
Handles video request submissions, quote generation, payments
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from sqlalchemy import text
from datetime import datetime
from typing import Optional
import json
import logging
import os
import stripe
import asyncio
from heygan_integration import generate_video, get_video_url

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe_publishable_key = os.getenv("STRIPE_PUBLISHABLE_KEY")

log = logging.getLogger("orders")
router = APIRouter()

# ============================================================
# IN-MEMORY STORAGE (FOR NOW - move to DB in production)
# ============================================================
pending_orders = []
completed_orders = []

# ============================================================
# GET /orders/stripe-key - Get Stripe publishable key
# ============================================================

@router.get("/orders/stripe-key")
async def get_stripe_key():
    """Get Stripe publishable key for frontend"""
    return {
        "publishable_key": stripe_publishable_key,
    }


# ============================================================
# POST /orders/request-quote - Customer submits video request
# ============================================================

@router.post("/orders/request-quote")
async def request_quote(
    customer_name: str,
    customer_email: str,
    customer_company: str,
    video_type: str,
    script_or_topic: str,
    target_audience: str,
    avatar: str,
    language: str,
    delivery_days: int = 2,
    reference_url: Optional[str] = None,
    phone: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    """
    Customer submits a video request with script, avatar, and language.
    Returns order ID and quote price for payment processing.
    """

    if not customer_name or not customer_email or not video_type or not avatar or not language:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # Create order object
    order = {
        "id": len(pending_orders) + 1,
        "status": "quote_requested",
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_company": customer_company,
        "phone": phone,
        "video_type": video_type,
        "script_or_topic": script_or_topic,
        "target_audience": target_audience,
        "avatar": avatar,
        "language": language,
        "delivery_days": delivery_days,
        "reference_url": reference_url,
        "requested_at": datetime.now().isoformat(),
        "quote_sent_at": None,
        "quote_price": None,
        "paid": False,
        "payment_link": None,
        "stripe_session_id": None,
        "completed_at": None,
        "video_url": None,
        "video_generation_status": "pending",
    }

    pending_orders.append(order)

    # Calculate quote price based on video type and delivery speed
    quote_price = calculate_quote_price(video_type, delivery_days)
    order["quote_price"] = quote_price

    log.info(f"New quote request from {customer_name} ({customer_email}): {video_type}, ${quote_price}")

    return {
        "success": True,
        "message": f"Quote created successfully",
        "order_id": order["id"],
        "quote_price": quote_price,
        "customer_email": customer_email,
    }


def calculate_quote_price(video_type: str, delivery_days: int) -> int:
    """Calculate quote price based on video type and delivery speed"""

    base_prices = {
        "youtube": 750,
        "social": 500,
        "testimonial": 600,
        "product_demo": 800,
        "course": 900,
        "custom": 1000,
    }

    base_price = base_prices.get(video_type, 750)

    # Rush delivery premium (less than 24 hours = +$250)
    if delivery_days < 1:
        base_price += 250

    return base_price


# ============================================================
# POST /orders/{order_id}/create-checkout - Create Stripe session
# ============================================================

@router.post("/orders/{order_id}/create-checkout")
async def create_checkout_session(order_id: int):
    """Create Stripe checkout session for order"""

    order = next((o for o in pending_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["paid"]:
        raise HTTPException(status_code=400, detail="Order already paid")

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Video Production: {order['video_type'].replace('_', ' ').title()}",
                            "description": f"Video for {order['customer_company']}",
                        },
                        "unit_amount": order["quote_price"] * 100,
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url="https://empire-v2-production.up.railway.app/order-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://empire-v2-production.up.railway.app/quote",
            customer_email=order["customer_email"],
            metadata={
                "order_id": order_id,
                "customer_email": order["customer_email"],
                "customer_name": order["customer_name"],
            }
        )

        order["stripe_session_id"] = checkout_session.id
        log.info(f"Stripe checkout session created for order {order_id}")

        return {
            "success": True,
            "session_id": checkout_session.id,
            "publishable_key": stripe_publishable_key,
        }

    except Exception as e:
        log.error(f"Stripe checkout creation failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Payment processing error")


# ============================================================
# POST /orders/webhook/stripe - Stripe webhook handler
# ============================================================

@router.post("/orders/webhook/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET not configured")
        return {"status": "success"}

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = int(session["metadata"]["order_id"])

        order = next((o for o in pending_orders if o["id"] == order_id), None)
        if order:
            order["paid"] = True
            order["status"] = "payment_received"
            order["transaction_id"] = session["id"]
            log.info(f"Payment received for order {order_id} via Stripe")

            # Trigger automatic video generation in background
            asyncio.create_task(
                generate_video_for_order(order_id, order)
            )

    return {"status": "success"}


async def generate_video_for_order(order_id: int, order: dict):
    """Background task to generate video after payment"""
    try:
        log.info(f"Starting video generation for order {order_id}")
        order["video_generation_status"] = "generating"

        # Call HeyGen to generate video
        video_id = await generate_video(
            order_id=order_id,
            script=order["script_or_topic"],
            avatar=order["avatar"],
            language=order["language"],
            video_type=order["video_type"],
        )

        if not video_id:
            order["video_generation_status"] = "failed"
            log.error(f"Video generation failed for order {order_id}")
            return

        # Poll for video completion (max 10 minutes)
        max_attempts = 60
        attempt = 0
        while attempt < max_attempts:
            await asyncio.sleep(10)  # Check every 10 seconds
            video_url = await get_video_url(video_id)

            if video_url:
                order["video_url"] = video_url
                order["status"] = "video_ready"
                order["video_generation_status"] = "completed"
                log.info(f"Video completed for order {order_id}: {video_url}")
                return

            attempt += 1

        order["video_generation_status"] = "timeout"
        log.warning(f"Video generation timeout for order {order_id}")

    except Exception as e:
        order["video_generation_status"] = "error"
        log.error(f"Video generation error for order {order_id}: {str(e)}")


# ============================================================
# GET /orders/{order_id} - Get specific order status
# ============================================================

@router.get("/orders/{order_id}")
async def get_order_status(order_id: int):
    """Get status of specific order"""
    order = next((o for o in pending_orders if o["id"] == order_id), None)
    if not order:
        order = next((o for o in completed_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "order": order,
        "status": order["status"],
        "paid": order["paid"],
        "completed": order["status"] == "delivered",
    }


# ============================================================
# POST /orders/{order_id}/payment-received - Mark order as paid
# ============================================================

@router.post("/orders/{order_id}/payment-received")
async def mark_payment_received(order_id: int, transaction_id: str = ""):
    """
    Mark order as paid
    """
    order = next((o for o in pending_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order["paid"] = True
    order["status"] = "payment_received"
    order["transaction_id"] = transaction_id

    log.info(f"Payment received for order {order_id}: ${order['quote_price']}")

    return {
        "success": True,
        "order_id": order_id,
        "status": "payment_received",
        "message": f"Payment confirmed. Video creation starting now.",
    }


# ============================================================
# POST /orders/{order_id}/mark-complete - Mark video as complete
# ============================================================

@router.post("/orders/{order_id}/mark-complete")
async def mark_order_complete(
    order_id: int,
    video_url: str,
    video_download_link: str,
):
    """
    Admin marks order complete and provides video link
    """
    order = next((o for o in pending_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order["paid"]:
        raise HTTPException(status_code=400, detail="Order not paid yet")

    # Mark as complete
    order["status"] = "delivered"
    order["completed_at"] = datetime.now().isoformat()
    order["video_url"] = video_url
    order["video_download_link"] = video_download_link

    # Move to completed
    pending_orders.remove(order)
    completed_orders.append(order)

    log.info(f"Order {order_id} marked complete. Video: {video_url}")

    return {
        "success": True,
        "order_id": order_id,
        "status": "delivered",
        "customer_email": order["customer_email"],
        "message": f"Order marked complete.",
    }


# ============================================================
# GET /orders/stats - Quick stats for dashboard
# ============================================================

@router.get("/orders/stats")
async def get_order_stats():
    """Get order statistics"""
    total_revenue = sum(o["quote_price"] for o in completed_orders if o["paid"])
    pending_revenue = sum(o["quote_price"] for o in pending_orders if o["status"] != "quote_requested")

    return {
        "total_quote_requests": len(pending_orders),
        "total_paid_orders": len([o for o in completed_orders if o["paid"]]),
        "total_delivered": len(completed_orders),
        "revenue_completed": total_revenue,
        "revenue_pending": pending_revenue,
        "total_potential_revenue": total_revenue + pending_revenue,
        "average_order_value": total_revenue // len(completed_orders) if completed_orders else 0,
    }


# ============================================================
# GET /orders/admin-dashboard - Full admin view
# ============================================================

@router.get("/orders/admin-dashboard")
async def admin_dashboard():
    """Complete admin dashboard view"""
    return {
        "pending_orders": {
            "count": len(pending_orders),
            "list": pending_orders,
            "revenue_at_stake": sum(o["quote_price"] for o in pending_orders if o["paid"]),
        },
        "completed_orders": {
            "count": len(completed_orders),
            "list": completed_orders[-20:],
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
        },
        "stats": {
            "total_requests": len(pending_orders) + len(completed_orders),
            "conversion_rate": len(completed_orders) / (len(pending_orders) + len(completed_orders)) if (len(pending_orders) + len(completed_orders)) > 0 else 0,
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
        },
    }
