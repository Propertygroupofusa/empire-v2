"""
Subscription Tier Management System
Handles customer tiers, monthly quotas, and subscription pricing
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import CustomerSubscription

log = logging.getLogger("subscriptions")

# ============================================================
# TIER DEFINITIONS
# ============================================================

SUBSCRIPTION_TIERS = {
    "free": {
        "id": "free",
        "name": "Free Trial",
        "monthly_price": 0,
        "videos_per_month": 1,
        "features": ["Single avatar (Anna)", "Basic languages", "2-3 day delivery"],
        "description": "Try one video free to test the service",
    },
    "starter": {
        "id": "starter",
        "name": "Starter",
        "monthly_price": 50000,  # $500 in cents
        "videos_per_month": 2,
        "features": ["All 8 avatars", "All 22 languages", "2-3 day delivery", "Email support"],
        "description": "Perfect for trying different styles",
        "add_ons": {
            "rush_24h": 15000,  # $150
            "rush_4h": 30000,   # $300
        },
    },
    "pro": {
        "id": "pro",
        "name": "Pro",
        "monthly_price": 150000,  # $1,500 in cents
        "videos_per_month": 8,
        "features": ["All 8 avatars", "All 22 languages", "24hr express delivery", "Priority support", "Custom scripts"],
        "description": "Best for marketing teams and content creators",
        "add_ons": {
            "extra_video": 112500,  # $1,125 (20% discount from base)
            "rush_4h": 20000,       # $200 expedite
        },
    },
    "enterprise": {
        "id": "enterprise",
        "name": "Enterprise",
        "monthly_price": 350000,  # $3,500 in cents
        "videos_per_month": 25,
        "features": ["All avatars + custom avatars", "All languages + custom voices", "24hr delivery + white-label", "Dedicated account manager", "API access", "Custom scripts + AI enhancement"],
        "description": "For agencies and high-volume creators",
        "add_ons": {
            "extra_video": 87500,   # $875 (25% discount from base)
            "white_label_setup": 500000,  # $5,000 one-time
        },
    },
}


# ============================================================
# SUBSCRIPTION FUNCTIONS
# ============================================================

def get_tier(tier_id: str) -> Optional[dict]:
    """Get tier definition by ID"""
    return SUBSCRIPTION_TIERS.get(tier_id)


def get_all_tiers() -> List[dict]:
    """Get all tier definitions"""
    return list(SUBSCRIPTION_TIERS.values())


def _sub_to_dict(sub: CustomerSubscription) -> dict:
    return {
        "customer_email": sub.customer_email,
        "tier_id": sub.tier_id,
        "tier_name": SUBSCRIPTION_TIERS[sub.tier_id]["name"],
        "start_date": sub.start_date,
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
        "videos_used_this_month": sub.videos_used_this_month,
        "active": sub.active,
        "stripe_subscription_id": sub.stripe_subscription_id,
        "stripe_customer_id": sub.stripe_customer_id,
        "status": sub.status,
        "payment_status": sub.payment_status,
    }


async def _get_sub_row(db: AsyncSession, customer_email: str) -> Optional[CustomerSubscription]:
    result = await db.execute(
        select(CustomerSubscription).where(CustomerSubscription.customer_email == customer_email)
    )
    return result.scalar_one_or_none()


async def subscribe_customer(db: AsyncSession, customer_email: str, tier_id: str) -> dict:
    """Subscribe a customer to a tier (create or overwrite their record)"""
    if tier_id not in SUBSCRIPTION_TIERS:
        raise ValueError(f"Invalid tier: {tier_id}")

    now = datetime.utcnow()
    sub = await _get_sub_row(db, customer_email)

    if sub:
        sub.tier_id = tier_id
        sub.start_date = now
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=30)
        sub.videos_used_this_month = 0
        sub.active = True
    else:
        sub = CustomerSubscription(
            customer_email=customer_email,
            tier_id=tier_id,
            start_date=now,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            videos_used_this_month=0,
            active=True,
        )
        db.add(sub)

    await db.commit()
    await db.refresh(sub)

    log.info(f"Customer {customer_email} subscribed to {tier_id} tier")
    return _sub_to_dict(sub)


async def update_stripe_ids(db: AsyncSession, customer_email: str, stripe_subscription_id: str, stripe_customer_id: str) -> None:
    """Attach Stripe IDs and mark active/paid - called once a checkout webhook confirms payment"""
    sub = await _get_sub_row(db, customer_email)
    if not sub:
        log.warning(f"update_stripe_ids: no subscription record for {customer_email}")
        return

    sub.stripe_subscription_id = stripe_subscription_id
    sub.stripe_customer_id = stripe_customer_id
    sub.status = "active"
    sub.payment_status = "active"
    await db.commit()


async def get_subscription(db: AsyncSession, customer_email: str) -> Optional[dict]:
    """Get customer subscription status"""
    sub = await _get_sub_row(db, customer_email)
    if not sub:
        return None

    # Check if subscription period has ended
    if sub.active and datetime.utcnow() > sub.current_period_end:
        # Reset monthly counter and extend period
        sub.current_period_start = datetime.utcnow()
        sub.current_period_end = datetime.utcnow() + timedelta(days=30)
        sub.videos_used_this_month = 0
        await db.commit()

    tier = get_tier(sub.tier_id)
    d = _sub_to_dict(sub)
    d["tier_details"] = tier
    d["videos_remaining"] = tier["videos_per_month"] - sub.videos_used_this_month
    return d


async def get_all_subscriptions(db: AsyncSession) -> List[dict]:
    """[ADMIN] Get every customer's subscription record"""
    result = await db.execute(select(CustomerSubscription))
    return [_sub_to_dict(sub) for sub in result.scalars().all()]


async def can_create_video(db: AsyncSession, customer_email: str) -> tuple:
    """Check if customer can create a video (within quota)"""
    sub = await get_subscription(db, customer_email)

    # If no subscription, allow one-off purchase
    if not sub:
        return True, "one_off"

    # Check quota
    tier = get_tier(sub["tier_id"])
    remaining = tier["videos_per_month"] - sub["videos_used_this_month"]

    if remaining > 0:
        return True, f"subscription ({remaining} remaining)"
    else:
        return False, "monthly quota exceeded"


async def use_video_quota(db: AsyncSession, customer_email: str) -> bool:
    """Deduct one video from customer quota (return False if over quota)"""
    sub_dict = await get_subscription(db, customer_email)

    if not sub_dict:
        # One-off customer, no quota check needed
        return True

    sub = await _get_sub_row(db, customer_email)
    tier = get_tier(sub.tier_id)
    if sub.videos_used_this_month < tier["videos_per_month"]:
        sub.videos_used_this_month += 1
        await db.commit()
        log.info(f"Video quota used for {customer_email}: {sub.videos_used_this_month}/{tier['videos_per_month']}")
        return True

    return False


async def get_pricing_for_customer(db: AsyncSession, customer_email: str, video_type: str, delivery_days: int) -> dict:
    """
    Calculate video pricing based on customer tier
    Returns pricing breakdown
    """
    base_prices = {
        "explainer": 50000,      # $500
        "testimonial": 50000,    # $500
        "product_demo": 60000,   # $600
        "social_media": 40000,   # $400
        "educational": 55000,    # $550
        "promotional": 65000,    # $650
    }

    base_price = base_prices.get(video_type, 50000)

    # Check if customer has subscription
    sub = await get_subscription(db, customer_email)

    if not sub:
        # One-off pricing: base + rush fee
        rush_fee = 0
        if delivery_days == 1:  # Rush 4h
            rush_fee = 30000  # $300
        elif delivery_days == 2:  # Rush 24h
            rush_fee = 15000  # $150

        return {
            "type": "one_off",
            "base_price": base_price,
            "rush_fee": rush_fee,
            "total": base_price + rush_fee,
            "description": "One-time video purchase",
        }

    # Subscription pricing
    tier = get_tier(sub["tier_id"])

    if tier["monthly_price"] == 0:
        # Free tier: charge base price
        return {
            "type": "free_tier",
            "base_price": base_price,
            "rush_fee": 0,
            "total": base_price,
            "description": "Video included in free trial",
        }
    else:
        # Paid tier: included in subscription
        return {
            "type": "subscription",
            "tier_name": tier["name"],
            "monthly_price": tier["monthly_price"],
            "base_price": 0,  # Free as part of subscription
            "rush_fee": 0,  # Included in subscription
            "total": 0,
            "description": f"Included in {tier['name']} subscription",
        }


async def get_customer_billing_summary(db: AsyncSession, customer_email: str) -> dict:
    """Get billing summary for a customer"""
    sub = await get_subscription(db, customer_email)

    if not sub:
        return {
            "customer_email": customer_email,
            "status": "no_subscription",
            "message": "Not subscribed - using one-off pricing",
        }

    tier = get_tier(sub["tier_id"])

    return {
        "customer_email": customer_email,
        "status": "active" if sub["active"] else "inactive",
        "tier": tier["name"],
        "monthly_price": tier["monthly_price"],
        "videos_per_month": tier["videos_per_month"],
        "videos_used_this_month": sub["videos_used_this_month"],
        "videos_remaining": tier["videos_per_month"] - sub["videos_used_this_month"],
        "current_period_start": sub["current_period_start"].isoformat(),
        "current_period_end": sub["current_period_end"].isoformat(),
        "days_remaining": max(0, (sub["current_period_end"] - datetime.utcnow()).days),
    }
