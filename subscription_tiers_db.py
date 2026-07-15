"""
Subscription Tier Management System - Database-backed
Handles customer tiers, monthly quotas, and subscription pricing with persistent storage
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import CustomerSubscription
from subscription_tiers import SUBSCRIPTION_TIERS, get_tier

log = logging.getLogger("subscriptions")


async def subscribe_customer(session: AsyncSession, customer_email: str, tier_id: str) -> dict:
    """Subscribe a customer to a tier (database-backed)"""
    if tier_id not in SUBSCRIPTION_TIERS:
        raise ValueError(f"Invalid tier: {tier_id}")

    now = datetime.utcnow()

    # Check if subscription already exists
    result = await session.execute(
        select(CustomerSubscription).where(CustomerSubscription.customer_email == customer_email)
    )
    existing = result.scalars().first()

    if existing:
        # Update existing subscription
        existing.tier_id = tier_id
        existing.tier_name = SUBSCRIPTION_TIERS[tier_id]["name"]
        existing.current_period_start = now
        existing.current_period_end = now + timedelta(days=30)
        existing.videos_used_this_month = 0
        existing.active = True
        existing.updated_at = now
    else:
        # Create new subscription
        sub = CustomerSubscription(
            customer_email=customer_email,
            tier_id=tier_id,
            tier_name=SUBSCRIPTION_TIERS[tier_id]["name"],
            start_date=now,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            videos_used_this_month=0,
            active=True,
        )
        session.add(sub)

    await session.commit()
    log.info(f"Customer {customer_email} subscribed to {tier_id} tier")

    # Return subscription as dict for compatibility
    result = await session.execute(
        select(CustomerSubscription).where(CustomerSubscription.customer_email == customer_email)
    )
    subscription = result.scalars().first()
    return subscription_to_dict(subscription)


async def get_subscription(session: AsyncSession, customer_email: str) -> Optional[dict]:
    """Get customer subscription status (database-backed)"""
    result = await session.execute(
        select(CustomerSubscription).where(CustomerSubscription.customer_email == customer_email)
    )
    sub = result.scalars().first()

    if not sub:
        return None

    # Check if subscription period has ended
    if sub.active and datetime.utcnow() > sub.current_period_end:
        # Reset monthly counter and extend period
        sub.current_period_start = datetime.utcnow()
        sub.current_period_end = datetime.utcnow() + timedelta(days=30)
        sub.videos_used_this_month = 0
        await session.commit()

    tier = get_tier(sub.tier_id)
    result_dict = subscription_to_dict(sub)
    result_dict["tier_details"] = tier
    result_dict["videos_remaining"] = tier["videos_per_month"] - sub.videos_used_this_month

    return result_dict


async def can_create_video(session: AsyncSession, customer_email: str) -> Tuple[bool, str]:
    """Check if customer can create a video (within quota) - database-backed"""
    sub = await get_subscription(session, customer_email)

    # If no subscription, allow one-off purchase
    if not sub:
        return True, "one_off"

    # Check if subscription is active
    if not sub.get("active", False):
        return True, "one_off (subscription inactive)"

    # Check quota
    tier = get_tier(sub["tier_id"])
    remaining = tier["videos_per_month"] - sub["videos_used_this_month"]

    if remaining > 0:
        return True, f"subscription ({remaining} remaining)"
    else:
        return False, "monthly quota exceeded"


async def use_video_quota(session: AsyncSession, customer_email: str) -> bool:
    """Deduct one video from customer quota (database-backed)"""
    sub = await get_subscription(session, customer_email)

    if not sub or not sub.get("active", False):
        return True

    # Get the subscription record from database
    result = await session.execute(
        select(CustomerSubscription).where(CustomerSubscription.customer_email == customer_email)
    )
    subscription_record = result.scalars().first()

    tier = get_tier(sub["tier_id"])
    if subscription_record.videos_used_this_month < tier["videos_per_month"]:
        subscription_record.videos_used_this_month += 1
        subscription_record.updated_at = datetime.utcnow()
        await session.commit()
        log.info(f"Video quota used for {customer_email}: {subscription_record.videos_used_this_month}/{tier['videos_per_month']}")
        return True

    return False


async def get_pricing_for_customer(session: AsyncSession, customer_email: str, video_type: str, delivery_days: int) -> dict:
    """Calculate video pricing based on customer tier (database-backed)"""
    base_prices = {
        "explainer": 50000,
        "testimonial": 50000,
        "product_demo": 60000,
        "social_media": 40000,
        "educational": 55000,
        "promotional": 65000,
    }

    base_price = base_prices.get(video_type, 50000)
    sub = await get_subscription(session, customer_email)

    if not sub or not sub.get("active", False):
        # One-off pricing
        rush_fee = 0
        if delivery_days == 1:
            rush_fee = 30000
        elif delivery_days == 2:
            rush_fee = 15000

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
        return {
            "type": "free_tier",
            "base_price": base_price,
            "rush_fee": 0,
            "total": base_price,
            "description": "Video included in free trial",
        }
    else:
        return {
            "type": "subscription",
            "tier_name": tier["name"],
            "monthly_price": tier["monthly_price"],
            "base_price": 0,
            "rush_fee": 0,
            "total": 0,
            "description": f"Included in {tier['name']} subscription",
        }


async def get_customer_billing_summary(session: AsyncSession, customer_email: str) -> dict:
    """Get billing summary for a customer (database-backed)"""
    sub = await get_subscription(session, customer_email)

    if not sub:
        return {
            "customer_email": customer_email,
            "status": "no_subscription",
            "message": "Not subscribed - using one-off pricing",
        }

    tier = get_tier(sub["tier_id"])

    return {
        "customer_email": customer_email,
        "status": "active" if sub.get("active", False) else "inactive",
        "tier": tier["name"],
        "monthly_price": tier["monthly_price"],
        "videos_per_month": tier["videos_per_month"],
        "videos_used_this_month": sub["videos_used_this_month"],
        "videos_remaining": tier["videos_per_month"] - sub["videos_used_this_month"],
        "current_period_start": sub["current_period_start"],
        "current_period_end": sub["current_period_end"],
        "days_remaining": max(0, (sub["current_period_end"] - datetime.utcnow()).days),
    }


def subscription_to_dict(subscription: CustomerSubscription) -> dict:
    """Convert CustomerSubscription ORM object to dict"""
    return {
        "id": subscription.id,
        "customer_email": subscription.customer_email,
        "tier_id": subscription.tier_id,
        "tier_name": subscription.tier_name,
        "start_date": subscription.start_date,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "videos_used_this_month": subscription.videos_used_this_month,
        "active": subscription.active,
        "stripe_subscription_id": subscription.stripe_subscription_id,
        "stripe_customer_id": subscription.stripe_customer_id,
        "payment_status": subscription.payment_status,
    }
