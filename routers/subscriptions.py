"""
Subscription Management Router
Handles tier browsing, subscriptions, and usage tracking
"""

from fastapi import APIRouter, HTTPException
from subscription_tiers import (
    get_all_tiers,
    get_tier,
    subscribe_customer,
    get_subscription,
    get_customer_billing_summary,
    SUBSCRIPTION_TIERS,
)
import logging

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


@router.get("/subscriptions/tiers/{tier_id}")
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


@router.post("/subscriptions/subscribe")
async def subscribe(
    customer_email: str,
    customer_name: str,
    tier_id: str,
):
    """
    Subscribe a customer to a tier
    """
    if tier_id not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier_id}")

    try:
        subscription = subscribe_customer(customer_email, tier_id)
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


@router.get("/subscriptions/status/{customer_email}")
async def get_subscription_status(customer_email: str):
    """
    Get current subscription status and usage for a customer
    """
    subscription = get_subscription(customer_email)

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


@router.get("/subscriptions/billing/{customer_email}")
async def get_billing_info(customer_email: str):
    """
    Get billing summary and invoice information for a customer
    """
    summary = get_customer_billing_summary(customer_email)
    return summary


@router.post("/subscriptions/upgrade")
async def upgrade_subscription(
    customer_email: str,
    new_tier_id: str,
):
    """
    Upgrade customer to a different tier (effective immediately)
    """
    if new_tier_id not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {new_tier_id}")

    old_subscription = get_subscription(customer_email)
    old_tier = old_subscription["tier_name"] if old_subscription else "None"

    # Subscribe to new tier (overwrites old subscription)
    subscribe_customer(customer_email, new_tier_id)
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
async def admin_list_subscriptions():
    """
    [ADMIN] List all active subscriptions with usage
    """
    from subscription_tiers import customer_subscriptions

    subscriptions = []
    for email, sub in customer_subscriptions.items():
        tier = get_tier(sub["tier_id"])
        subscriptions.append(
            {
                "customer_email": email,
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
async def admin_get_customer_subscriptions(customer_email: str):
    """
    [ADMIN] Get detailed subscription info for a customer
    """
    subscription = get_subscription(customer_email)

    if not subscription:
        return {
            "customer_email": customer_email,
            "status": "no_subscription",
        }

    return {
        "customer_email": customer_email,
        "subscription": subscription,
        "billing_summary": get_customer_billing_summary(customer_email),
    }
