"""
Subscription Management Router
Handles tier browsing, subscriptions, and usage tracking
"""

from fastapi import APIRouter, HTTPException, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from subscription_tiers import (
    get_all_tiers,
    get_tier,
    subscribe_customer,
    get_subscription,
    get_all_subscriptions,
    get_customer_billing_summary,
    SUBSCRIPTION_TIERS,
)
from stripe_subscriptions import (
    create_subscription_checkout,
    handle_subscription_event,
    setup_stripe_products,
)
from database import get_db
import stripe
import logging
import os

log = logging.getLogger("subscriptions")
router = APIRouter()


@router.get("/tiers")
async def list_tiers():
    """
    List all available subscription tiers with pricing and features
    """
    tiers = get_all_tiers()
    return {
        "tiers": tiers,
        "total_tiers": len(tiers),
    }


@router.get("/tiers/{tier_id}")
async def get_tier_details(tier_id: str):
    """
    Get detailed information about a specific tier
    """
    tier = get_tier(tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail=f"Tier '{tier_id}' not found")

    return {
        "tier": tier,
        "yearly_savings": (
            tier["monthly_price"] * 12 * 0.15  # 15% discount for annual commitment
            if tier["monthly_price"] > 0
            else 0
        ),
    }


@router.post("/subscribe")
async def subscribe(
    customer_email: str,
    customer_name: str,
    tier_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Activate the free tier directly. Paid tiers must go through
    POST /subscriptions/checkout so payment is actually collected -
    this endpoint used to grant any tier (including $3,500/mo Enterprise)
    with no payment at all.
    """
    if tier_id not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier_id}")

    if tier_id != "free":
        raise HTTPException(
            status_code=400,
            detail="Paid tiers require checkout - use POST /subscriptions/checkout instead",
        )

    try:
        subscription = await subscribe_customer(db, customer_email, tier_id)
        tier = get_tier(tier_id)

        return {
            "success": True,
            "message": f"Successfully subscribed to {tier['name']} tier",
            "customer_email": customer_email,
            "tier": tier["name"],
            "monthly_price": tier["monthly_price"],
            "videos_per_month": tier["videos_per_month"],
            "subscription_start": subscription["start_date"].isoformat(),
        }
    except Exception as e:
        log.error(f"Subscription failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{customer_email}")
async def get_subscription_status(customer_email: str, db: AsyncSession = Depends(get_db)):
    """
    Get current subscription status and usage for a customer
    """
    subscription = await get_subscription(db, customer_email)

    if not subscription:
        return {
            "customer_email": customer_email,
            "has_subscription": False,
            "message": "No active subscription - using one-off pricing",
        }

    return {
        "customer_email": customer_email,
        "has_subscription": True,
        "tier_name": subscription["tier_name"],
        "tier_id": subscription["tier_id"],
        "tier_details": subscription["tier_details"],
        "videos_used_this_month": subscription["videos_used_this_month"],
        "videos_remaining": subscription["videos_remaining"],
        "current_period_start": subscription["current_period_start"].isoformat(),
        "current_period_end": subscription["current_period_end"].isoformat(),
        "active": subscription["active"],
    }


@router.get("/billing/{customer_email}")
async def get_billing_info(customer_email: str, db: AsyncSession = Depends(get_db)):
    """
    Get billing summary and invoice information for a customer
    """
    summary = await get_customer_billing_summary(db, customer_email)
    return summary


@router.post("/upgrade")
async def upgrade_subscription(
    customer_email: str,
    new_tier_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Move a customer to a different tier. Only free is allowed here since
    upgrading to a paid tier needs an actual Stripe charge - use
    POST /subscriptions/checkout for paid tiers instead.
    """
    if new_tier_id not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {new_tier_id}")

    if new_tier_id != "free":
        raise HTTPException(
            status_code=400,
            detail="Upgrading to a paid tier requires checkout - use POST /subscriptions/checkout instead",
        )

    old_subscription = await get_subscription(db, customer_email)
    old_tier = old_subscription["tier_name"] if old_subscription else "None"

    # Subscribe to new tier (overwrites old subscription)
    await subscribe_customer(db, customer_email, new_tier_id)
    new_tier = get_tier(new_tier_id)

    return {
        "success": True,
        "message": f"Upgraded from {old_tier} to {new_tier['name']}",
        "previous_tier": old_tier,
        "new_tier": new_tier["name"],
        "monthly_price": new_tier["monthly_price"],
        "effective_date": "Immediate",
    }


@router.get("/admin/subscriptions")
async def admin_list_subscriptions(db: AsyncSession = Depends(get_db)):
    """
    [ADMIN] List all active subscriptions with usage
    """
    subscriptions = []
    for sub in await get_all_subscriptions(db):
        tier = get_tier(sub["tier_id"])
        subscriptions.append(
            {
                "customer_email": sub["customer_email"],
                "tier": tier["name"],
                "monthly_price": tier["monthly_price"],
                "videos_per_month": tier["videos_per_month"],
                "videos_used": sub["videos_used_this_month"],
                "started": sub["start_date"].isoformat(),
                "period_end": sub["current_period_end"].isoformat(),
            }
        )

    return {
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": sum(1 for s in subscriptions if s["monthly_price"] > 0),
        "subscriptions": subscriptions,
        "total_mrr": sum(s["monthly_price"] for s in subscriptions),  # Monthly recurring revenue
    }


@router.get("/admin/subscriptions/{customer_email}")
async def admin_get_customer_subscriptions(customer_email: str, db: AsyncSession = Depends(get_db)):
    """
    [ADMIN] Get detailed subscription info for a customer
    """
    subscription = await get_subscription(db, customer_email)

    if not subscription:
        return {
            "customer_email": customer_email,
            "status": "no_subscription",
        }

    return {
        "customer_email": customer_email,
        "subscription": subscription,
        "billing_summary": await get_customer_billing_summary(db, customer_email),
    }


# ============================================================
# STRIPE SUBSCRIPTION CHECKOUT
# ============================================================


@router.post("/checkout")
async def create_subscription_checkout_session(
    customer_email: str,
    customer_name: str,
    tier_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe checkout session for subscription signup
    Returns redirect URL to Stripe Checkout
    """
    if tier_id not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier_id}")

    if tier_id == "free":
        # Free tier - no payment needed
        await subscribe_customer(db, customer_email, "free")
        return {
            "success": True,
            "message": "Free trial activated",
            "tier": SUBSCRIPTION_TIERS["free"]["name"],
            "redirect_url": None,
        }

    # For paid tiers, create Stripe checkout
    checkout = create_subscription_checkout(
        customer_email=customer_email,
        customer_name=customer_name,
        tier_id=tier_id,
    )

    if not checkout:
        raise HTTPException(status_code=500, detail="Failed to create checkout session")

    if not checkout.get("url"):
        raise HTTPException(status_code=500, detail="No checkout URL generated")

    return {
        "success": True,
        "message": "Checkout session created",
        "session_id": checkout["session_id"],
        "redirect_url": checkout["url"],
        "tier": SUBSCRIPTION_TIERS[tier_id]["name"],
        "monthly_price": SUBSCRIPTION_TIERS[tier_id]["monthly_price"],
    }


# ============================================================
# STRIPE WEBHOOK - Subscription Events
# ============================================================


@router.post("/webhook/stripe")
async def subscription_stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle Stripe webhook events for subscriptions
    Events: checkout.session.completed, customer.subscription.updated, invoice.payment_failed, etc.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    # Falls back to the shared STRIPE_WEBHOOK_SECRET so this keeps working if
    # only one Stripe webhook endpoint is registered; set the dedicated var
    # once a separate endpoint (with its own signing secret) exists for /subscriptions.
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET_SUBSCRIPTIONS") or os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET_SUBSCRIPTIONS (or STRIPE_WEBHOOK_SECRET) not configured")
        return {"status": "success"}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle subscription events
    success = await handle_subscription_event(db, event)

    if success:
        log.info(f"Subscription webhook processed: {event['type']}")
        return {"status": "success"}
    else:
        log.warning(f"Subscription webhook failed: {event['type']}")
        return {"status": "error", "message": "Failed to process webhook"}


# ============================================================
# SUCCESS/CANCEL PAGES
# ============================================================


@router.get("/subscription-success")
async def subscription_success(session_id: str = None):
    """Subscription payment success page"""
    return {
        "status": "success",
        "message": "Subscription activated! Your videos are ready to create.",
        "session_id": session_id,
        "next_step": "Visit /quote to start creating videos",
    }


@router.get("/subscription-cancel")
async def subscription_cancel():
    """Subscription payment cancelled page"""
    return {
        "status": "cancelled",
        "message": "Subscription setup cancelled",
        "next_step": "Feel free to try again or contact support",
    }
