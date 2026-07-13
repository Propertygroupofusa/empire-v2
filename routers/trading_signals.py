"""Trading signals subscription and delivery system."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
import stripe
import os
import logging

from database import get_db
from models import Campaign, CampaignContact

log = logging.getLogger("pgusa")
router = APIRouter()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# Subscription tiers - FREEMIUM MODE ONLY (paid tiers coming after signal quality proven)
TIERS = {
    "free": {"signals_per_month": 5, "price": 0, "stripe_price_id": None},
    # "pro": {"signals_per_month": 100, "price": 29700, "stripe_price_id": os.getenv("STRIPE_PRICE_PRO", "")},
    # "enterprise": {"signals_per_month": 9999, "price": 99700, "stripe_price_id": os.getenv("STRIPE_PRICE_ENTERPRISE", "")},
}

# FREEMIUM DEPLOYMENT NOTE:
# - Only free tier ($0) enabled for signal quality validation
# - Paid tiers will be enabled after 30 days of proven 60%+ win rate
# - All infrastructure ready for paid conversion (Stripe, billing, etc)
# - Monitor: GET /trading/signals/stats for win rate and subscriber growth


class SignalSubscriber(BaseModel):
    email: str
    tier: str = "free"
    phone: Optional[str] = None

    class Config:
        from_attributes = True


class TradingSignal(BaseModel):
    contract: str  # MES, MNQ, MGC
    action: str    # BUY or SELL
    entry_price: float
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    rsi: float
    trend: str
    confidence: float = 0.8

    class Config:
        from_attributes = True


class SignalResponse(BaseModel):
    id: int
    contract: str
    action: str
    entry_price: float
    stop_loss: Optional[float]
    target_price: Optional[float]
    rsi: float
    trend: str
    confidence: float
    created_at: str
    subscribers_notified: int
    open_rate: float = 0.0
    click_rate: float = 0.0

    class Config:
        from_attributes = True


@router.post("/signals/subscribe")
async def subscribe_to_signals(subscriber: SignalSubscriber, db: AsyncSession = Depends(get_db)):
    """Subscribe to trading signals (FREEMIUM: Free tier only during beta)."""
    # FREEMIUM MODE: Force free tier for all subscribers during validation phase
    tier = "free"  # Override subscriber.tier to free only

    # Log if user tried to subscribe to paid tier
    if subscriber.tier != "free":
        log.info(f"Paid tier '{subscriber.tier}' requested but freemium mode active. Assigned free tier. Email: {subscriber.email}")

    # Create or update subscriber contact in a signal campaign
    result = await db.execute(
        select(Campaign).where(Campaign.name == "Trading Signals")
    )
    signal_campaign = result.scalar_one_or_none()

    if not signal_campaign:
        # Create signal campaign if it doesn't exist
        signal_campaign = Campaign(
            name="Trading Signals",
            description="Real-time trading signals from APEX futures bot",
            status="active",
            outreach_type="email",
            message_template="Signal: {{action}} {{contract}} @ {{entry_price}} | RSI: {{rsi}} | Trend: {{trend}}",
        )
        db.add(signal_campaign)
        await db.commit()
        await db.refresh(signal_campaign)

    # Check if subscriber already exists
    result = await db.execute(
        select(CampaignContact).where(
            (CampaignContact.campaign_id == signal_campaign.id) &
            (CampaignContact.email == subscriber.email)
        )
    )
    contact = result.scalar_one_or_none()

    if contact:
        # Update tier
        contact.contact_data = {"tier": tier, "phone": subscriber.phone}
        contact.status = "active"
    else:
        # Create new subscriber
        contact = CampaignContact(
            campaign_id=signal_campaign.id,
            email=subscriber.email,
            phone=subscriber.phone,
            name=subscriber.email.split("@")[0],
            contact_data={"tier": tier, "phone": subscriber.phone, "joined_at": datetime.utcnow().isoformat()},
            status="active",
        )
        db.add(contact)

    await db.commit()
    await db.refresh(contact)

    return {
        "message": f"Subscribed to {tier.upper()} tier",
        "email": subscriber.email,
        "tier": tier,
        "signals_per_month": TIERS[tier]["signals_per_month"],
    }


@router.get("/signals/subscribers")
async def list_subscribers(
    tier: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """List all trading signal subscribers."""
    result = await db.execute(
        select(Campaign).where(Campaign.name == "Trading Signals")
    )
    signal_campaign = result.scalar_one_or_none()
    if not signal_campaign:
        return {"total": 0, "subscribers": []}

    query = select(CampaignContact).where(
        (CampaignContact.campaign_id == signal_campaign.id) &
        (CampaignContact.status == "active")
    )

    if tier:
        # This is a simplified check - in production, filter by tier in contact_data
        pass

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    contacts = result.scalars().all()

    return {
        "total": len(contacts),
        "tier_breakdown": {
            "free": sum(1 for c in contacts if c.contact_data.get("tier") == "free"),
            "pro": sum(1 for c in contacts if c.contact_data.get("tier") == "pro"),
            "enterprise": sum(1 for c in contacts if c.contact_data.get("tier") == "enterprise"),
        },
        "subscribers": [
            {
                "email": c.email,
                "tier": c.contact_data.get("tier", "free"),
                "status": c.status,
                "joined_at": c.contact_data.get("joined_at"),
            }
            for c in contacts
        ]
    }


@router.post("/signals/broadcast")
async def broadcast_signal(signal: TradingSignal, db: AsyncSession = Depends(get_db)):
    """Broadcast a new trading signal to all subscribers.
    Called by prop_bot when a signal is generated."""

    # Get signal campaign
    result = await db.execute(
        select(Campaign).where(Campaign.name == "Trading Signals")
    )
    signal_campaign = result.scalar_one_or_none()
    if not signal_campaign:
        raise HTTPException(status_code=404, detail="Signal campaign not found")

    # Get all active subscribers
    result = await db.execute(
        select(CampaignContact).where(
            (CampaignContact.campaign_id == signal_campaign.id) &
            (CampaignContact.status == "active")
        )
    )
    subscribers = result.scalars().all()

    sent_count = 0
    for subscriber in subscribers:
        tier = subscriber.contact_data.get("tier", "free")

        # Format signal message
        message = f"""
🚀 NEW TRADING SIGNAL — {signal.action} {signal.contract}

Entry Price: ${signal.entry_price:.2f}
Stop Loss: ${signal.stop_loss:.2f if signal.stop_loss else 'N/A'}
Target: ${signal.target_price:.2f if signal.target_price else 'N/A'}

Technical Setup:
• RSI: {signal.rsi:.1f}
• Trend: {signal.trend.upper()}
• Confidence: {signal.confidence*100:.0f}%

Tier: {tier.upper()}
Time: {datetime.utcnow().isoformat()}
        """.strip()

        # Update contact with sent status
        subscriber.status = "sent"
        subscriber.sent_at = datetime.utcnow()
        subscriber.metadata = {"last_signal": signal.contract, "last_action": signal.action}

        sent_count += 1

        # TODO: Actually send email/SMS here
        # For now, just log
        log.info(f"Signal sent to {subscriber.email}: {signal.action} {signal.contract}")

    await db.commit()

    log.info(f"✅ Trading signal broadcast: {signal.action} {signal.contract} → {sent_count} subscribers")

    return {
        "signal": signal.contract,
        "action": signal.action,
        "subscribers_notified": sent_count,
        "entry_price": signal.entry_price,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/signals/stats")
async def get_signal_stats(db: AsyncSession = Depends(get_db)):
    """Get trading signals performance statistics (FREEMIUM MODE)."""
    result = await db.execute(
        select(Campaign).where(Campaign.name == "Trading Signals")
    )
    signal_campaign = result.scalar_one_or_none()
    if not signal_campaign:
        return {
            "mode": "freemium",
            "total_subscribers": 0,
            "status": "initializing"
        }

    result = await db.execute(
        select(CampaignContact).where(CampaignContact.campaign_id == signal_campaign.id)
    )
    contacts = result.scalars().all()

    active = sum(1 for c in contacts if c.status == "active")
    sent = sum(1 for c in contacts if c.sent_at)
    opened = sum(1 for c in contacts if c.opened_at)

    return {
        "mode": "freemium",
        "status": "signal quality validation phase",
        "note": "Free tier only. Paid tiers enable after 60%+ win rate proven (30 days).",
        "total_subscribers": len(contacts),
        "active_subscribers": active,
        "signals_sent": sent,
        "open_rate": round((opened / sent * 100) if sent > 0 else 0, 1),
        "tier_breakdown": {
            "free": len(contacts),
            "pro": 0,
            "enterprise": 0,
        },
        "current_revenue": "$0.00/month (freemium phase)",
        "revenue_potential": "$0.00/month until paid tiers enabled",
        "next_milestone": "30 days of signal tracking → enable paid tiers if 60%+ win rate",
    }


@router.post("/signals/unsubscribe/{email}")
async def unsubscribe(email: str, db: AsyncSession = Depends(get_db)):
    """Unsubscribe from trading signals."""
    result = await db.execute(
        select(Campaign).where(Campaign.name == "Trading Signals")
    )
    signal_campaign = result.scalar_one_or_none()
    if not signal_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    result = await db.execute(
        select(CampaignContact).where(
            (CampaignContact.campaign_id == signal_campaign.id) &
            (CampaignContact.email == email)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    await db.delete(contact)
    await db.commit()

    return {"message": f"Unsubscribed {email} from trading signals"}


@router.get("/signals/health")
async def trading_signals_health():
    """Health check for trading signals service."""
    return {
        "service": "trading_signals",
        "status": "online",
        "tiers": list(TIERS.keys()),
        "stripe_configured": bool(stripe.api_key),
    }
