"""
Stripe Subscription Integration
Handles recurring billing for subscription tiers
"""

import stripe
import logging
import os
from typing import Optional
from datetime import datetime

from payments_pause import payments_paused

log = logging.getLogger("stripe_subscriptions")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe_publishable_key = os.getenv("STRIPE_PUBLISHABLE_KEY")

# ============================================================
# STRIPE PRODUCT/PRICE SETUP
# ============================================================

STRIPE_PRODUCTS = {
    "free": {
        "name": "Free Trial",
        "price_cents": 0,
        "description": "Free trial - 1 video per month",
    },
    "starter": {
        "name": "Starter Plan",
        "price_cents": 50000,  # $500
        "description": "2 videos per month with all avatars and languages",
    },
    "pro": {
        "name": "Pro Plan",
        "price_cents": 150000,  # $1500
        "description": "8 videos per month with priority support",
    },
    "enterprise": {
        "name": "Enterprise Plan",
        "price_cents": 350000,  # $3500
        "description": "25 videos per month with dedicated support and white-label",
    },
}

# Store Stripe product/price IDs (in production, fetch from Stripe API)
stripe_price_ids = {}


def _find_product(client, tier_id):
    """An existing active product for this tier, or None.

    Stripe does NOT reject a duplicate product name - Product.create always
    succeeds and makes another one. So the previous code's "create, and fall
    back to a lookup if it errors" never took the lookup branch, and every
    boot added three more products and three more prices to the live
    account. Two restarts on 2026-10-07 alone produced six of each.

    Look FIRST, create only if nothing matches.
    """
    starting_after = None
    for _ in range(10):                       # 10 x 100 = 1,000 products
        page = client.Product.list(limit=100, active=True,
                                   **({"starting_after": starting_after}
                                      if starting_after else {}))
        items = list(page)
        for p in items:
            if (p.get("metadata") or {}).get("tier_id") == tier_id:
                return p
        if not getattr(page, "has_more", False) or not items:
            return None
        starting_after = items[-1].id
    return None


def _find_price(client, product_id, tier_info, tier_id):
    """An existing active monthly price at this amount, or None.

    Matched on what actually defines the price - product, amount, currency
    and interval - not on metadata alone, so a price created before the
    metadata was added is still reused instead of duplicated.
    """
    page = client.Price.list(product=product_id, limit=100, active=True)
    for p in page:
        rec = p.get("recurring") or {}
        if (p.get("unit_amount") == tier_info["price_cents"]
                and (p.get("currency") or "").lower() == "usd"
                and rec.get("interval") == "month"):
            return p
    return None


def setup_stripe_products(client=None):
    """
    Ensure Stripe products and prices for subscription tiers exist.

    IDEMPOTENT. Reuses what is already in the account and creates only what
    is genuinely missing, so restarting the service no longer litters the
    live Stripe account with duplicates. `client` exists so this can be
    tested without touching Stripe.
    """
    global stripe_price_ids

    client = client or stripe
    if not client.api_key:
        log.warning("STRIPE_SECRET_KEY not configured - subscription billing disabled")
        return False

    try:
        for tier_id, tier_info in STRIPE_PRODUCTS.items():
            if tier_id == "free":
                # Skip free tier - no Stripe product needed
                continue

            product = _find_product(client, tier_id)
            if product is not None:
                log.info(f"Reusing Stripe product for {tier_id}: {product.id}")
            else:
                product = client.Product.create(
                    name=tier_info["name"],
                    description=tier_info["description"],
                    metadata={"tier_id": tier_id},
                )
                log.info(f"Created Stripe product for {tier_id}: {product.id}")

            price = _find_price(client, product.id, tier_info, tier_id)
            if price is not None:
                stripe_price_ids[tier_id] = price.id
                log.info(f"Reusing Stripe price for {tier_id}: {price.id}")
                continue

            try:
                price = client.Price.create(
                    product=product.id,
                    unit_amount=tier_info["price_cents"],
                    currency="usd",
                    recurring={"interval": "month"},
                    metadata={"tier_id": tier_id},
                )
                stripe_price_ids[tier_id] = price.id
                log.info(f"Created Stripe price for {tier_id}: {price.id}")
            except Exception as e:                        # noqa: BLE001
                log.error(f"Failed to create price for {tier_id}: {e}")

        log.info(f"Stripe products ready: {stripe_price_ids}")
        return True

    except Exception as e:
        log.error(f"Stripe setup failed: {e}")
        return False


def get_price_id(tier_id: str) -> Optional[str]:
    """Get Stripe price ID for a tier"""
    return stripe_price_ids.get(tier_id)


def create_subscription_checkout(
    customer_email: str,
    customer_name: str,
    tier_id: str,
    success_url: str = "https://empire-v2-production.up.railway.app/subscription-success",
    cancel_url: str = "https://empire-v2-production.up.railway.app/quote",
) -> Optional[dict]:
    """
    Create a Stripe checkout session for subscription
    Returns session details including checkout URL
    """
    if tier_id == "free":
        # Free tier - no Stripe checkout needed
        return {
            "session_id": "free_trial",
            "url": None,
            "tier_id": "free",
            "message": "Free trial activated",
        }

    if not stripe.api_key:
        log.error("STRIPE_SECRET_KEY not configured")
        return None

    if payments_paused():
        log.warning("Payments paused (PAYMENTS_PAUSED=true) - refusing to create subscription checkout")
        return None

    price_id = get_price_id(tier_id)
    if not price_id:
        log.error(f"No Stripe price ID found for tier {tier_id}")
        return None

    try:
        # Create or get Stripe customer
        customer = stripe.Customer.create(
            email=customer_email,
            name=customer_name,
            metadata={"tier_id": tier_id},
        )

        # Create checkout session for subscription
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "customer_email": customer_email,
                "tier_id": tier_id,
            },
            billing_address_collection="auto",
        )

        log.info(f"Subscription checkout session created for {customer_email} ({tier_id}): {session.id}")

        return {
            "session_id": session.id,
            "url": session.url,
            "customer_id": customer.id,
            "tier_id": tier_id,
        }

    except Exception as e:
        log.error(f"Subscription checkout creation failed: {e}")
        return None


async def handle_subscription_event(db, event: dict) -> bool:
    """
    Handle Stripe subscription webhook events
    Returns True if event was handled successfully
    """
    event_type = event.get("type")

    if event_type == "checkout.session.completed":
        return await handle_checkout_completed(db, event)
    elif event_type == "customer.subscription.updated":
        return await handle_subscription_updated(db, event)
    elif event_type == "customer.subscription.deleted":
        return await handle_subscription_deleted(db, event)
    elif event_type == "invoice.payment_failed":
        return await handle_payment_failed(db, event)
    elif event_type == "invoice.payment_succeeded":
        return await handle_payment_succeeded(db, event)

    return True  # Ignore unhandled event types


async def handle_checkout_completed(db, event: dict) -> bool:
    """Handle checkout.session.completed - subscription activated.

    This is the only place a paid tier's subscription record gets created:
    routers/subscriptions.py's /checkout endpoint never calls
    subscribe_customer() for paid tiers (it just starts the Stripe
    session), so a subscription row only used to get patched here IF one
    already existed - which for a real paid signup it never did. That
    meant a customer could pay Stripe successfully and still have
    get_subscription() return None forever. Create the record here
    instead of conditionally patching one.
    """
    try:
        session = event["data"]["object"]
        customer_email = session["metadata"].get("customer_email")
        tier_id = session["metadata"].get("tier_id")
        subscription_id = session["subscription"]

        if not all([customer_email, tier_id, subscription_id]):
            log.warning(f"Incomplete checkout data: {session}")
            return False

        # Import here to avoid circular imports
        from subscription_tiers import subscribe_customer, update_stripe_ids

        await subscribe_customer(db, customer_email, tier_id)
        await update_stripe_ids(db, customer_email, subscription_id, session["customer"])

        log.info(f"Subscription activated: {customer_email} -> {tier_id} (sub: {subscription_id})")
        return True

    except Exception as e:
        log.error(f"Failed to handle checkout completion: {e}")
        return False


async def handle_subscription_updated(db, event: dict) -> bool:
    """Handle customer.subscription.updated"""
    try:
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]
        status = subscription["status"]

        log.info(f"Subscription updated: {customer_id} -> {status}")

        # In production, fetch customer email from Stripe
        # For now, status is tracked by Stripe subscription ID

        return True

    except Exception as e:
        log.error(f"Failed to handle subscription update: {e}")
        return False


async def handle_subscription_deleted(db, event: dict) -> bool:
    """Handle customer.subscription.deleted - subscription cancelled"""
    try:
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]

        log.warning(f"Subscription cancelled: {customer_id}")

        # In production, mark subscription as inactive
        return True

    except Exception as e:
        log.error(f"Failed to handle subscription deletion: {e}")
        return False


async def handle_payment_failed(db, event: dict) -> bool:
    """Handle invoice.payment_failed - payment failed"""
    try:
        invoice = event["data"]["object"]
        customer_id = invoice["customer"]
        amount = invoice["amount_due"]

        log.warning(f"Payment failed: {customer_id} - ${amount/100:.2f}")

        # In production: send email, mark subscription as past_due
        return True

    except Exception as e:
        log.error(f"Failed to handle payment failure: {e}")
        return False


async def handle_payment_succeeded(db, event: dict) -> bool:
    """Handle invoice.payment_succeeded - payment processed"""
    try:
        invoice = event["data"]["object"]
        customer_id = invoice["customer"]
        amount = invoice["amount_paid"]

        log.info(f"Payment succeeded: {customer_id} - ${amount/100:.2f}")

        # In production: log payment, reset past_due status
        return True

    except Exception as e:
        log.error(f"Failed to handle payment success: {e}")
        return False


def get_subscription_details(subscription_id: str) -> Optional[dict]:
    """Get subscription details from Stripe"""
    if not stripe.api_key:
        return None

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        return {
            "id": subscription.id,
            "customer_id": subscription.customer,
            "status": subscription.status,
            "current_period_start": datetime.fromtimestamp(subscription.current_period_start),
            "current_period_end": datetime.fromtimestamp(subscription.current_period_end),
            "cancel_at_period_end": subscription.cancel_at_period_end,
            "price": subscription.items.data[0].price.unit_amount if subscription.items.data else 0,
        }
    except Exception as e:
        log.error(f"Failed to get subscription details: {e}")
        return None


def cancel_subscription(subscription_id: str, immediately: bool = False) -> bool:
    """Cancel a subscription"""
    if not stripe.api_key:
        return False

    try:
        if immediately:
            stripe.Subscription.delete(subscription_id)
            log.info(f"Subscription cancelled immediately: {subscription_id}")
        else:
            stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
            log.info(f"Subscription marked for cancellation at period end: {subscription_id}")

        return True
    except Exception as e:
        log.error(f"Failed to cancel subscription {subscription_id}: {e}")
        return False
