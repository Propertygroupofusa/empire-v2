"""
Customer Order & Quote Management Router
Handles video request submissions, quote generation, payments
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from sqlalchemy import text
from datetime import datetime
from typing import Optional
import json
import logging

log = logging.getLogger("orders")
router = APIRouter()

# ============================================================
# IN-MEMORY STORAGE (FOR NOW - move to DB in production)
# ============================================================
pending_orders = []
completed_orders = []

# ============================================================
# POST /orders/request-quote - Customer submits video request
# ============================================================

@router.post("/orders/request-quote")
async def request_quote(
    customer_name: str,
    customer_email: str,
    customer_company: str,
    video_type: str,  # "youtube", "social", "testimonial", "product_demo", "course", "custom"
    script_or_topic: str,
    target_audience: str,
    delivery_days: int = 2,  # Default 2 days (48 hours)
    reference_url: Optional[str] = None,
    phone: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    """
    Customer submits a video request.
    We generate a quote and send via email.
    """

    if not customer_name or not customer_email or not video_type:
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
        "delivery_days": delivery_days,
        "reference_url": reference_url,
        "requested_at": datetime.now().isoformat(),
        "quote_sent_at": None,
        "quote_price": None,
        "paid": False,
        "payment_link": None,
        "completed_at": None,
        "video_url": None,
    }

    pending_orders.append(order)

    # Calculate quote price based on video type and delivery speed
    quote_price = calculate_quote_price(video_type, delivery_days)
    order["quote_price"] = quote_price

    log.info(f"New quote request from {customer_name} ({customer_email}): {video_type}, ${quote_price}")

    # Send confirmation email to customer
    if background_tasks:
        background_tasks.add_task(
            send_quote_email,
            order_id=order["id"],
            customer_name=customer_name,
            customer_email=customer_email,
            video_type=video_type,
            quote_price=quote_price,
            delivery_days=delivery_days,
        )

    return {
        "success": True,
        "message": f"Quote request received. We'll send a quote to {customer_email} shortly.",
        "order_id": order["id"],
        "expected_quote_price": quote_price,
        "next_step": "Check your email for quote and payment link",
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


async def send_quote_email(
    order_id: int,
    customer_name: str,
    customer_email: str,
    video_type: str,
    quote_price: int,
    delivery_days: int,
):
    """Send quote email to customer (template for manual sending)"""

    email_template = f"""
Subject: Your Video Production Quote - ${quote_price}

Hi {customer_name},

Thanks for your video request!

Here's your quote:

==================================================
VIDEO PRODUCTION QUOTE
==================================================
Project: {video_type.upper()}
Quote Price: ${quote_price} (one-time fee, no subscription)
Delivery Timeline: {delivery_days} day(s)
Quality: Professional HD with AI voiceover
Revisions: Unlimited until satisfied

==================================================
NEXT STEPS
==================================================

1. Review the quote above
2. Pay via this link: [STRIPE PAYMENT LINK]
3. We'll create your video within {delivery_days} day(s)
4. You'll receive HD video file via email
5. Unlimited revisions available

Ready to move forward? Reply to this email or use the payment link above.

Questions? Reply with any details about your video needs.

Thanks,
Video Production Team
https://empire-v2-production.up.railway.app

===================================================
"""

    log.info(f"Quote email would be sent to {customer_email}:\n{email_template}")
    # In production: integrate actual email sending here


# ============================================================
# GET /orders/pending - Admin view of pending orders
# ============================================================

@router.get("/orders/pending")
async def get_pending_orders(limit: int = 50):
    """Get all pending orders (for admin dashboard)"""
    return {
        "total_pending": len(pending_orders),
        "orders": pending_orders[-limit:],
    }


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
    Mark order as paid (called by Stripe webhook)
    Admin calls this when payment is received
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
    Sends delivery email to customer
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

    # Send delivery email (template)
    delivery_email = f"""
Subject: Your Video is Ready! - Download Here

Hi {order['customer_name']},

Your video is ready to download!

Download Link: {video_download_link}

Video Details:
- Type: {order['video_type']}
- Format: MP4 (HD 1080p)
- Duration: Check file
- Script: {order['script_or_topic'][:100]}...

Questions or need revisions? Reply to this email.

Thanks for choosing us!
Video Production Team
"""

    log.info(f"Delivery email would be sent to {order['customer_email']}:\n{delivery_email}")

    return {
        "success": True,
        "order_id": order_id,
        "status": "delivered",
        "customer_email": order["customer_email"],
        "message": f"Order marked complete. Delivery email sent to {order['customer_email']}",
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
            "list": completed_orders[-20:],  # Last 20
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
        },
        "stats": {
            "total_requests": len(pending_orders) + len(completed_orders),
            "conversion_rate": len(completed_orders) / (len(pending_orders) + len(completed_orders)) if (len(pending_orders) + len(completed_orders)) > 0 else 0,
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
        },
    }
