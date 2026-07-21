"""
Customer Order & Quote Management Router
Handles video request submissions, quote generation, payments
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional
import logging
import os
import stripe
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote

from database import get_db, AsyncSessionLocal
from admin_auth import require_admin_key
from models import VideoQuoteOrder
from payments_pause import payments_paused, PAUSE_MESSAGE

log = logging.getLogger("orders")

# Try to import HeyGen integration, but don't crash if it fails
try:
    from heygan_integration import generate_video, get_video_url
    HEYGAN_AVAILABLE = True
except Exception as e:
    log.warning(f"HeyGen integration not available: {e}")
    HEYGAN_AVAILABLE = False
    generate_video = None
    get_video_url = None

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe_publishable_key = os.getenv("STRIPE_PUBLISHABLE_KEY")

router = APIRouter()

# ============================================================
# GET /stripe-key - Get Stripe publishable key
# ============================================================

@router.get("/stripe-key")
async def get_stripe_key():
    """Get Stripe publishable key for frontend"""
    return {
        "publishable_key": stripe_publishable_key,
    }


class QuoteRequest(BaseModel):
    customer_name: str
    customer_email: str
    customer_company: str
    video_type: str
    script_or_topic: str
    target_audience: str
    avatar: str
    language: str
    delivery_days: int = 2
    reference_url: Optional[str] = None
    phone: Optional[str] = None


# ============================================================
# POST /request-quote - Customer submits video request
# ============================================================

@router.post("/request-quote")
async def request_quote(quote: QuoteRequest, db: AsyncSession = Depends(get_db)):
    """
    Customer submits a video request with script, avatar, and language.
    Returns order ID and quote price for payment processing.
    """
    if not quote.customer_name or not quote.customer_email or not quote.video_type or not quote.avatar or not quote.language:
        raise HTTPException(status_code=400, detail="Missing required fields")

    quote_price = calculate_quote_price(quote.video_type, quote.delivery_days)

    order = VideoQuoteOrder(
        status="quote_requested",
        customer_name=quote.customer_name,
        customer_email=quote.customer_email,
        customer_company=quote.customer_company,
        phone=quote.phone,
        video_type=quote.video_type,
        script_or_topic=quote.script_or_topic,
        target_audience=quote.target_audience,
        avatar=quote.avatar,
        language=quote.language,
        delivery_days=quote.delivery_days,
        reference_url=quote.reference_url,
        quote_price=quote_price,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    log.info(f"New quote request from {quote.customer_name} ({quote.customer_email}): {quote.video_type}, ${quote_price}")

    return {
        "success": True,
        "message": "Quote created successfully",
        "order_id": order.id,
        "quote_price": quote_price,
        "customer_email": quote.customer_email,
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
                    <p><a href="https://empire-v2-production.up.railway.app/orders/customer/{order_id}?email={quote(customer_email)}">Order #{order_id}</a></p>

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
async def create_checkout_session(order_id: int, db: AsyncSession = Depends(get_db)):
    """Create Stripe checkout session for order"""

    order = await db.get(VideoQuoteOrder, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.paid:
        raise HTTPException(status_code=400, detail="Order already paid")

    if payments_paused():
        raise HTTPException(status_code=503, detail=PAUSE_MESSAGE)

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Video Production: {order.video_type.replace('_', ' ').title()}",
                            "description": f"Video for {order.customer_company}",
                        },
                        "unit_amount": order.quote_price * 100,
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url="https://empire-v2-production.up.railway.app/order-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://empire-v2-production.up.railway.app/quote",
            customer_email=order.customer_email,
            metadata={
                "order_id": order_id,
                "customer_email": order.customer_email,
                "customer_name": order.customer_name,
            }
        )

        order.stripe_session_id = checkout_session.id
        await db.commit()
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
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events"""

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    # Falls back to the shared STRIPE_WEBHOOK_SECRET so this keeps working if
    # only one Stripe webhook endpoint is registered; set the dedicated var
    # once a separate endpoint (with its own signing secret) exists for /orders.
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET_ORDERS") or os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET_ORDERS (or STRIPE_WEBHOOK_SECRET) not configured")
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

        order = await db.get(VideoQuoteOrder, order_id)
        if order:
            # Stripe can and does redeliver the same webhook event more than
            # once - without this check a redelivery would re-trigger a
            # second video-generation job and a second "video ready" email
            # for an order that was already paid.
            if order.paid:
                log.info(f"Order {order_id} already marked paid, ignoring duplicate webhook")
                return {"status": "success"}

            order.paid = True
            order.status = "payment_received"
            order.transaction_id = session["id"]
            await db.commit()
            log.info(f"Payment received for order {order_id} via Stripe")

            # Trigger automatic video generation in background (if available)
            if HEYGAN_AVAILABLE:
                asyncio.create_task(generate_video_for_order(order_id))
            else:
                log.warning(f"HeyGen not available, skipping video generation for order {order_id}")

    return {"status": "success"}


async def generate_video_for_order(order_id: int):
    """Background task to generate video after payment.

    Runs as a detached asyncio task outside any request, so it opens its
    own DB session rather than reusing a request-scoped one that would
    already be closed.
    """
    async with AsyncSessionLocal() as db:
        order = await db.get(VideoQuoteOrder, order_id)
        if not order:
            log.error(f"generate_video_for_order: order {order_id} not found")
            return

        try:
            log.info(f"Starting video generation for order {order_id}")
            order.video_generation_status = "generating"
            await db.commit()

            # Call HeyGen to generate video
            video_id = await generate_video(
                order_id=order_id,
                script=order.script_or_topic,
                avatar=order.avatar,
                language=order.language,
                video_type=order.video_type,
            )

            if not video_id:
                order.video_generation_status = "failed"
                await db.commit()
                log.error(f"Video generation failed for order {order_id}")
                return

            # Poll for video completion (max 10 minutes)
            max_attempts = 60
            attempt = 0
            while attempt < max_attempts:
                await asyncio.sleep(10)  # Check every 10 seconds
                video_url = await get_video_url(video_id)

                if video_url:
                    order.video_url = video_url
                    order.status = "video_ready"
                    order.video_generation_status = "completed"
                    await db.commit()
                    log.info(f"Video completed for order {order_id}: {video_url}")

                    # Send email to customer
                    await send_video_ready_email(
                        customer_email=order.customer_email,
                        customer_name=order.customer_name,
                        order_id=order_id,
                        video_url=video_url,
                    )
                    return

                attempt += 1

            order.video_generation_status = "timeout"
            await db.commit()
            log.warning(f"Video generation timeout for order {order_id}")

        except Exception as e:
            order.video_generation_status = "error"
            await db.commit()
            log.error(f"Video generation error for order {order_id}: {str(e)}")


# ============================================================
# GET /orders/stats - Quick stats for dashboard
# ============================================================
# NOTE: these two literal-path routes must stay registered before
# GET /orders/{order_id} below - FastAPI matches routes in registration
# order, and {order_id}: int would otherwise try (and fail) to parse
# "stats"/"admin-dashboard" as an integer, 422ing before ever reaching
# the routes actually meant to handle those paths.

@router.get("/stats", dependencies=[Depends(require_admin_key)])
async def get_order_stats(db: AsyncSession = Depends(get_db)):
    """Get order statistics"""
    result = await db.execute(select(VideoQuoteOrder))
    all_orders = result.scalars().all()
    pending_orders = [o for o in all_orders if o.status != "delivered"]
    completed_orders = [o for o in all_orders if o.status == "delivered"]

    total_revenue = sum(o.quote_price for o in completed_orders if o.paid)
    pending_revenue = sum(o.quote_price for o in pending_orders if o.status != "quote_requested")

    return {
        "total_quote_requests": len(pending_orders),
        "total_paid_orders": len([o for o in completed_orders if o.paid]),
        "total_delivered": len(completed_orders),
        "revenue_completed": total_revenue,
        "revenue_pending": pending_revenue,
        "total_potential_revenue": total_revenue + pending_revenue,
        "average_order_value": total_revenue // len(completed_orders) if completed_orders else 0,
    }


# ============================================================
# GET /orders/admin-dashboard - Full admin view
# ============================================================

@router.get("/admin-dashboard", dependencies=[Depends(require_admin_key)])
async def admin_dashboard(db: AsyncSession = Depends(get_db)):
    """Complete admin dashboard view"""
    result = await db.execute(select(VideoQuoteOrder))
    all_orders = result.scalars().all()
    pending_orders = [o for o in all_orders if o.status != "delivered"]
    completed_orders = [o for o in all_orders if o.status == "delivered"]

    # Count orders by video generation status
    generating = sum(1 for o in pending_orders if o.video_generation_status == "generating")
    video_ready = sum(1 for o in pending_orders if o.video_generation_status == "completed")
    failed = sum(1 for o in pending_orders if o.video_generation_status in ["failed", "error", "timeout"])

    return {
        "pending_orders": {
            "count": len(pending_orders),
            "list": [
                {
                    "id": o.id,
                    "customer_name": o.customer_name,
                    "customer_email": o.customer_email,
                    "video_type": o.video_type,
                    "quote_price": o.quote_price,
                    "paid": o.paid,
                    "status": o.status,
                    "video_generation_status": o.video_generation_status,
                    "video_url": o.video_url,
                    "requested_at": o.requested_at.isoformat() if o.requested_at else None,
                }
                for o in pending_orders
            ],
            "revenue_at_stake": sum(o.quote_price for o in pending_orders if o.paid),
        },
        "completed_orders": {
            "count": len(completed_orders),
            "list": [
                {
                    "id": o.id,
                    "customer_name": o.customer_name,
                    "customer_email": o.customer_email,
                    "video_type": o.video_type,
                    "quote_price": o.quote_price,
                    "paid": o.paid,
                    "video_url": o.video_url,
                    "completed_at": o.completed_at.isoformat() if o.completed_at else None,
                }
                for o in completed_orders[-20:]
            ],
            "total_revenue": sum(o.quote_price for o in completed_orders if o.paid),
        },
        "video_generation_status": {
            "generating": generating,
            "ready": video_ready,
            "failed": failed,
        },
        "stats": {
            "total_requests": len(pending_orders) + len(completed_orders),
            "conversion_rate": len(completed_orders) / (len(pending_orders) + len(completed_orders)) if (len(pending_orders) + len(completed_orders)) > 0 else 0,
            "total_revenue": sum(o.quote_price for o in completed_orders if o.paid),
            "avg_order_value": sum(o.quote_price for o in completed_orders if o.paid) // len([o for o in completed_orders if o.paid]) if any(o.paid for o in completed_orders) else 0,
        },
    }


# ============================================================
# GET /orders/{order_id} - Get specific order status
# ============================================================

@router.get("/{order_id}", dependencies=[Depends(require_admin_key)])
async def get_order_status(order_id: int, db: AsyncSession = Depends(get_db)):
    """Get status of specific order"""
    order = await db.get(VideoQuoteOrder, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "order": order.to_dict(),
        "status": order.status,
        "paid": order.paid,
        "completed": order.status == "delivered",
    }


# ============================================================
# POST /orders/{order_id}/payment-received - Mark order as paid
# ============================================================

@router.post("/{order_id}/payment-received", dependencies=[Depends(require_admin_key)])
async def mark_payment_received(order_id: int, transaction_id: str = "", db: AsyncSession = Depends(get_db)):
    """
    Mark order as paid. Admin-only: this bypasses Stripe entirely, so
    without protection anyone could call it to get a paid video for free.
    """
    order = await db.get(VideoQuoteOrder, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.paid = True
    order.status = "payment_received"
    order.transaction_id = transaction_id
    await db.commit()

    log.info(f"Payment received for order {order_id}: ${order.quote_price}")

    return {
        "success": True,
        "order_id": order_id,
        "status": "payment_received",
        "message": "Payment confirmed. Video creation starting now.",
    }


# ============================================================
# POST /orders/{order_id}/generate-video - Manually trigger video generation
# ============================================================

@router.post("/{order_id}/generate-video", dependencies=[Depends(require_admin_key)])
async def manual_generate_video(order_id: int, db: AsyncSession = Depends(get_db)):
    """Admin endpoint to manually trigger video generation"""
    order = await db.get(VideoQuoteOrder, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.paid:
        raise HTTPException(status_code=400, detail="Order must be paid before generating video")

    if order.video_url:
        return {
            "status": "already_complete",
            "message": "Video already generated",
            "video_url": order.video_url,
        }

    # Trigger video generation
    asyncio.create_task(generate_video_for_order(order_id))

    return {
        "status": "generation_started",
        "order_id": order_id,
        "message": "Video generation started. Check status in a few moments.",
    }


# ============================================================
# POST /orders/{order_id}/mark-complete - Mark video as complete
# ============================================================

@router.post("/{order_id}/mark-complete", dependencies=[Depends(require_admin_key)])
async def mark_order_complete(
    order_id: int,
    video_url: str,
    video_download_link: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin marks order complete and provides video link
    """
    order = await db.get(VideoQuoteOrder, order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.paid:
        raise HTTPException(status_code=400, detail="Order not paid yet")

    order.status = "delivered"
    order.completed_at = datetime.utcnow()
    order.video_url = video_url
    order.video_download_link = video_download_link
    await db.commit()

    log.info(f"Order {order_id} marked complete. Video: {video_url}")

    return {
        "success": True,
        "order_id": order_id,
        "status": "delivered",
        "customer_email": order.customer_email,
        "message": "Order marked complete.",
    }


# ============================================================
# GET /orders/customer/{order_id} - Customer portal
# ============================================================

@router.get("/customer/{order_id}")
async def customer_portal(order_id: int, email: str, db: AsyncSession = Depends(get_db)):
    """Customer portal to track order and download video.

    order_id is a small sequential integer with no other protection, so it's
    trivially enumerable (order 1, 2, 3...) - require the email the order was
    placed under as a second factor. Return the same 404 whether the order
    doesn't exist or the email doesn't match, so this can't be used to probe
    which order IDs are in use.
    """
    order = await db.get(VideoQuoteOrder, order_id)

    if not order or order.customer_email.lower() != email.lower():
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
        "order_id": order.id,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "status": order.status,
        "status_message": status_messages.get(order.status, order.status),
        "video_type": order.video_type,
        "avatar": order.avatar or "N/A",
        "language": order.language or "N/A",
        "quote_price": order.quote_price,
        "paid": order.paid,
        "requested_at": order.requested_at.isoformat() if order.requested_at else None,
        "video_generation_status": order.video_generation_status,
        "video_generation_message": video_generation_messages.get(order.video_generation_status),
        "video_url": order.video_url,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "can_download": order.video_url is not None,
    }


# ============================================================
# GET /orders/customer/{email}/all - Get all orders for customer
# ============================================================

@router.get("/customer-email/{email}/all")
async def customer_orders_by_email(email: str, db: AsyncSession = Depends(get_db)):
    """Get all orders for a customer by email"""
    result = await db.execute(
        select(VideoQuoteOrder).where(func.lower(VideoQuoteOrder.customer_email) == email.lower())
    )
    customer_orders = result.scalars().all()

    if not customer_orders:
        raise HTTPException(status_code=404, detail="No orders found for this email")

    return {
        "email": email,
        "total_orders": len(customer_orders),
        "orders": [
            {
                "order_id": o.id,
                "status": o.status,
                "quote_price": o.quote_price,
                "paid": o.paid,
                "video_ready": o.video_url is not None,
                "requested_at": o.requested_at.isoformat() if o.requested_at else None,
            }
            for o in customer_orders
        ]
    }
