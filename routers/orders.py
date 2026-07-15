"""
Customer Order & Quote Management Router
Handles video request submissions, quote generation, payments
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy import text
from datetime import datetime
from typing import Optional
import json
import logging
import os
import stripe
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# Pydantic models for request bodies
class RequestQuoteData(BaseModel):
    customer_name: str
    customer_email: str
    customer_company: str = ""
    video_type: str
    script_or_topic: str
    target_audience: str = ""
    avatar: str
    language: str
    delivery_days: int = 2
    reference_url: Optional[str] = None
    phone: Optional[str] = None

# Try to import HeyGen integration, but don't crash if it fails
try:
    from heygan_integration import generate_video, get_video_url
    HEYGAN_AVAILABLE = True
except Exception as e:
    log.warning(f"HeyGen integration not available: {e}")
    HEYGAN_AVAILABLE = False
    generate_video = None
    get_video_url = None

# Import subscription management
try:
    from subscription_tiers import (
        get_subscription,
        can_create_video,
        use_video_quota,
        get_pricing_for_customer,
    )
    SUBSCRIPTIONS_AVAILABLE = True
except Exception as e:
    log.warning(f"Subscription integration not available: {e}")
    SUBSCRIPTIONS_AVAILABLE = False

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

@router.get("/stripe-key")
async def get_stripe_key():
    """Get Stripe publishable key for frontend"""
    return {
        "publishable_key": stripe_publishable_key,
    }


# ============================================================
# POST /request-quote - Customer submits video request
# ============================================================

@router.post("/request-quote")
async def request_quote(data: RequestQuoteData, background_tasks: BackgroundTasks):
    """
    Customer submits a video request with script, avatar, and language.
    Automatically uses subscription quota if available, otherwise creates quote for payment.
    """

    if not data.customer_name or not data.customer_email or not data.video_type or not data.avatar or not data.language:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # Check subscription status and quota
    subscription_status = None
    pricing_info = None
    quota_deducted = False

    if SUBSCRIPTIONS_AVAILABLE:
        # Check if customer can create video (has quota)
        can_create, reason = can_create_video(data.customer_email)
        if not can_create:
            raise HTTPException(status_code=429, detail=f"Video quota exceeded: {reason}")

        # Get subscription and pricing info
        subscription_status = get_subscription(data.customer_email)
        pricing_info = get_pricing_for_customer(
            data.customer_email,
            data.video_type,
            data.delivery_days
        )

        # If subscription exists and video is included, mark quota as used
        if subscription_status and subscription_status.get("active", False):
            quota_deducted = use_video_quota(data.customer_email)
            if not quota_deducted:
                raise HTTPException(status_code=429, detail="Could not deduct from quota - quota exceeded")

    # Create order object
    order = {
        "id": len(pending_orders) + 1,
        "status": "quote_requested",
        "customer_name": data.customer_name,
        "customer_email": data.customer_email,
        "customer_company": data.customer_company,
        "phone": data.phone,
        "video_type": data.video_type,
        "script_or_topic": data.script_or_topic,
        "target_audience": data.target_audience,
        "avatar": data.avatar,
        "language": data.language,
        "delivery_days": data.delivery_days,
        "reference_url": data.reference_url,
        "requested_at": datetime.now().isoformat(),
        "quote_sent_at": None,
        "quote_price": None,
        "paid": False,
        "payment_link": None,
        "stripe_session_id": None,
        "completed_at": None,
        "video_url": None,
        "video_generation_status": "pending",
        "subscription_type": None,
        "pricing_type": None,
    }

    # Calculate pricing
    if pricing_info:
        order["quote_price"] = pricing_info.get("total", 0)
        order["pricing_type"] = pricing_info.get("type", "one_off")
        order["subscription_type"] = subscription_status.get("tier_id") if subscription_status else None

        # Mark as paid if included in subscription
        if pricing_info.get("type") == "subscription":
            order["status"] = "payment_received"
            order["paid"] = True
            # Auto-trigger video generation for subscription videos
            background_tasks.add_task(generate_video_for_order, order["id"], order)
            order["video_generation_status"] = "generating"
    else:
        # Fall back to regular quote price calculation
        quote_price = calculate_quote_price(data.video_type, data.delivery_days)
        order["quote_price"] = quote_price
        order["pricing_type"] = "one_off"

    pending_orders.append(order)

    log.info(
        f"Video order from {data.customer_name} ({data.customer_email}): "
        f"{data.video_type}, pricing={order['pricing_type']}, price=${order['quote_price']}, "
        f"subscription={order['subscription_type'] or 'none'}"
    )

    return {
        "success": True,
        "message": f"Video order created successfully",
        "order_id": order["id"],
        "quote_price": order["quote_price"],
        "customer_email": data.customer_email,
        "pricing_type": order["pricing_type"],
        "subscription_tier": order["subscription_type"],
        "status": order["status"],
        "details": {
            "included_in_subscription": pricing_info.get("type") == "subscription" if pricing_info else False,
            "videos_remaining": (
                subscription_status.get("videos_remaining", 0)
                if subscription_status else None
            ),
        }
    }


async def send_video_ready_email(customer_email: str, customer_name: str, order_id: int, video_url: str):
    """Send email to customer when video is ready"""
    try:
        sender_email = os.getenv("GMAIL_EMAIL", "noreply@empire-v2.com")
        sender_password = os.getenv("GMAIL_PASSWORD", "")

        if not sender_password:
            log.warning("GMAIL credentials not configured, skipping email")
            return

        subject = f"Your Video is Ready! - Order #{order_id}"

        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <h2>Your Video is Ready! 🎉</h2>
                    <p>Hi {customer_name},</p>
                    <p>Great news! Your video for order <strong>#{order_id}</strong> has been completed and is ready for download.</p>

                    <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                        <p><strong>📥 Download Your Video:</strong></p>
                        <a href="{video_url}" style="display: inline-block; background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">Download Video</a>
                    </div>

                    <p>This video is also available in your customer portal at:</p>
                    <p><a href="https://empire-v2-production.up.railway.app/customer/{order_id}">{order_id}</a></p>

                    <p>Questions? Reply to this email and we'll help you right away.</p>

                    <p>Thank you for your business!<br>
                    <strong>Empire Video Production</strong></p>
                </div>
            </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = customer_email

        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, customer_email, msg.as_string())

        log.info(f"Video ready email sent to {customer_email} for order {order_id}")

    except Exception as e:
        log.error(f"Failed to send video ready email: {str(e)}")


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

@router.post("/{order_id}/create-checkout")
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

@router.post("/webhook/stripe")
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

            # Trigger automatic video generation in background (if available)
            if HEYGAN_AVAILABLE:
                asyncio.create_task(
                    generate_video_for_order(order_id, order)
                )
            else:
                log.warning(f"HeyGen not available, skipping video generation for order {order_id}")

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

                # Send email to customer
                await send_video_ready_email(
                    customer_email=order["customer_email"],
                    customer_name=order["customer_name"],
                    order_id=order_id,
                    video_url=video_url,
                )
                return

            attempt += 1

        order["video_generation_status"] = "timeout"
        log.warning(f"Video generation timeout for order {order_id}")

    except Exception as e:
        order["video_generation_status"] = "error"
        log.error(f"Video generation error for order {order_id}: {str(e)}")


# ============================================================
# LITERAL ROUTES (must come before /{order_id})
# ============================================================

@router.get("/stats")
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


@router.get("/admin-dashboard")
async def admin_dashboard():
    """Complete admin dashboard view"""

    # Count orders by video generation status
    generating = sum(1 for o in pending_orders if o.get("video_generation_status") == "generating")
    video_ready = sum(1 for o in pending_orders if o.get("video_generation_status") == "completed")
    failed = sum(1 for o in pending_orders if o.get("video_generation_status") in ["failed", "error", "timeout"])

    return {
        "pending_orders": {
            "count": len(pending_orders),
            "list": [
                {
                    "id": o["id"],
                    "customer_name": o["customer_name"],
                    "customer_email": o["customer_email"],
                    "video_type": o["video_type"],
                    "quote_price": o["quote_price"],
                    "paid": o["paid"],
                    "status": o["status"],
                    "video_generation_status": o.get("video_generation_status", "pending"),
                    "video_url": o.get("video_url"),
                    "requested_at": o["requested_at"],
                }
                for o in pending_orders
            ],
            "revenue_at_stake": sum(o["quote_price"] for o in pending_orders if o["paid"]),
        },
        "completed_orders": {
            "count": len(completed_orders),
            "list": [
                {
                    "id": o["id"],
                    "customer_name": o["customer_name"],
                    "customer_email": o["customer_email"],
                    "video_type": o["video_type"],
                    "quote_price": o["quote_price"],
                    "paid": o["paid"],
                    "video_url": o.get("video_url"),
                    "completed_at": o.get("completed_at"),
                }
                for o in completed_orders[-20:]
            ],
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
        },
        "video_generation_status": {
            "generating": generating,
            "ready": video_ready,
            "failed": failed,
        },
        "stats": {
            "total_requests": len(pending_orders) + len(completed_orders),
            "conversion_rate": len(completed_orders) / (len(pending_orders) + len(completed_orders)) if (len(pending_orders) + len(completed_orders)) > 0 else 0,
            "total_revenue": sum(o["quote_price"] for o in completed_orders if o["paid"]),
            "avg_order_value": sum(o["quote_price"] for o in completed_orders if o["paid"]) // len([o for o in completed_orders if o["paid"]]) if any(o["paid"] for o in completed_orders) else 0,
        },
    }


# ============================================================
# GET /{order_id} - Get specific order status
# ============================================================

@router.get("/{order_id}")
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

@router.post("/{order_id}/payment-received")
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
# POST /orders/{order_id}/generate-video - Manually trigger video generation
# ============================================================

@router.post("/{order_id}/generate-video")
async def manual_generate_video(order_id: int):
    """Admin endpoint to manually trigger video generation"""
    order = next((o for o in pending_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order["paid"]:
        raise HTTPException(status_code=400, detail="Order must be paid before generating video")

    if order.get("video_url"):
        return {
            "status": "already_complete",
            "message": "Video already generated",
            "video_url": order["video_url"],
        }

    # Trigger video generation
    asyncio.create_task(generate_video_for_order(order_id, order))

    return {
        "status": "generation_started",
        "order_id": order_id,
        "message": "Video generation started. Check status in a few moments.",
    }


# ============================================================
# POST /orders/{order_id}/mark-complete - Mark video as complete
# ============================================================

@router.post("/{order_id}/mark-complete")
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
# GET /orders/customer/{order_id} - Customer portal
# ============================================================

@router.get("/customer/{order_id}")
async def customer_portal(order_id: int):
    """Customer portal to track order and download video"""
    order = next((o for o in pending_orders if o["id"] == order_id), None)
    if not order:
        order = next((o for o in completed_orders if o["id"] == order_id), None)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Build response with customer-friendly status
    status_messages = {
        "quote_requested": "Quote submitted - awaiting payment",
        "payment_received": "Payment received - video is being created",
        "video_ready": "✓ Video is ready to download!",
        "delivered": "✓ Order complete",
    }

    video_generation_messages = {
        "pending": "Waiting to start...",
        "generating": "Creating your video with AI...",
        "completed": "Done!",
        "failed": "Generation failed",
        "timeout": "Generation took too long",
        "error": "An error occurred",
    }

    return {
        "order_id": order["id"],
        "customer_name": order["customer_name"],
        "customer_email": order["customer_email"],
        "status": order["status"],
        "status_message": status_messages.get(order["status"], order["status"]),
        "video_type": order["video_type"],
        "avatar": order.get("avatar", "N/A"),
        "language": order.get("language", "N/A"),
        "quote_price": order["quote_price"],
        "paid": order["paid"],
        "requested_at": order["requested_at"],
        "video_generation_status": order.get("video_generation_status", "pending"),
        "video_generation_message": video_generation_messages.get(
            order.get("video_generation_status", "pending")
        ),
        "video_url": order.get("video_url"),
        "completed_at": order.get("completed_at"),
        "can_download": order.get("video_url") is not None,
    }


# ============================================================
# GET /orders/customer/{email}/all - Get all orders for customer
# ============================================================

@router.get("/customer-email/{email}/all")
async def customer_orders_by_email(email: str):
    """Get all orders for a customer by email"""
    customer_orders = [
        o for o in (pending_orders + completed_orders)
        if o["customer_email"].lower() == email.lower()
    ]

    if not customer_orders:
        raise HTTPException(status_code=404, detail="No orders found for this email")

    return {
        "email": email,
        "total_orders": len(customer_orders),
        "orders": [
            {
                "order_id": o["id"],
                "status": o["status"],
                "quote_price": o["quote_price"],
                "paid": o["paid"],
                "video_ready": o.get("video_url") is not None,
                "requested_at": o["requested_at"],
            }
            for o in customer_orders
        ]
    }


# ============================================================
# GET /orders/stats - Quick stats for dashboard
# ============================================================

