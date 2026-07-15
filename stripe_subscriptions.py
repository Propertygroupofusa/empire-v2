"""
Stripe Subscription Integration
Handles recurring billing for subscription tiers
"""

import stripe
import logging
import os
from typing import Optional
from datetime import datetime

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


def setup_stripe_products():
    """
    Create or fetch Stripe products and prices for subscription tiers
    Run this once on startup to ensure products exist
    """
    global stripe_price_ids

    if not stripe.api_key:
        log.warning("STRIPE_SECRET_KEY not configured - subscription billing disabled")
        return False

    try:
        for tier_id, tier_info in STRIPE_PRODUCTS.items():
            if tier_id == "free":
                # Skip free tier - no Stripe product needed
                continue

            # Create product if it doesn't exist
            try:
                product = stripe.Product.create(
                    name=tier_info["name"],
                    description=tier_info["description"],
                    metadata={"tier_id": tier_id},
                )
                log.info(f"Created Stripe product for {tier_id}: {product.id}")
            except stripe.error.InvalidRequestError:
                # Product might already exist, try to fetch by metadata
                products = stripe.Product.list(limit=100)
                product = next(
                    (p for p in products if p.metadata.get("tier_id") == tier_id),
                    None,
                )
                if not product:
                    log.error(f"Failed to create/find product for {tier_id}")
                    continue

            # Create price for the product
            try:
                price = stripe.Price.create(
                    product=product.id,
                    unit_amount=tier_info["price_cents"],
                    currency="usd",
                    recurring={"interval": "month"},
                    metadata={"tier_id": tier_id},
                )
                stripe_price_ids[tier_id] = price.id
                log.info(f"Created Stripe price for {tier_id}: {price.id}")
            except stripe.error.InvalidRequestError as e:
                # Price might already exist
                prices = stripe.Price.list(product=product.id, limit=10)
                price = next(
                    (p for p in prices if p.metadata.get("tier_id") == tier_id),
                    None,
                )
                if price:
                    stripe_price_ids[tier_id] = price.id
                    log.info(f"Found existing Stripe price for {tier_id}: {price.id}")
                else:
                    log.error(f"Failed to create/find price for {tier_id}: {e}")

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


def handle_subscription_event(event: dict) -> bool:
    """
    Handle Stripe subscription webhook events
    Returns True if event was handled successfully
    """
    event_type = event.get("type")

    if event_type == "checkout.session.completed":
        return handle_checkout_completed(event)
    elif event_type == "customer.subscription.updated":
        return handle_subscription_updated(event)
    elif event_type == "customer.subscription.deleted":
        return handle_subscription_deleted(event)
    elif event_type == "invoice.payment_failed":
        return handle_payment_failed(event)
    elif event_type == "invoice.payment_succeeded":
        return handle_payment_succeeded(event)

    return True  # Ignore unhandled event types


def handle_checkout_completed(event: dict) -> bool:
    """Handle checkout.session.completed - subscription activated"""
    try:
        session = event["data"]["object"]
        customer_email = session["metadata"].get("customer_email")
        tier_id = session["metadata"].get("tier_id")
        subscription_id = session["subscription"]

        if not all([customer_email, tier_id, subscription_id]):
            log.warning(f"Incomplete checkout data: {session}")
            return False

        log.info(f"Subscription activated: {customer_email} -> {tier_id} (sub: {subscription_id})")

        # Import here to avoid circular imports
        from subscription_tiers import customer_subscriptions

        if customer_email in customer_subscriptions:
            customer_subscriptions[customer_email]["stripe_subscription_id"] = subscription_id
            customer_subscriptions[customer_email]["stripe_customer_id"] = session["customer"]
            customer_subscriptions[customer_email]["status"] = "active"
            customer_subscriptions[customer_email]["payment_status"] = "active"

        return True

    except Exception as e:
        log.error(f"Failed to handle checkout completion: {e}")
        return False


def handle_subscription_updated(event: dict) -> bool:
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


def handle_subscription_deleted(event: dict) -> bool:
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


def handle_payment_failed(event: dict) -> bool:
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


def handle_payment_succeeded(event: dict) -> bool:
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
