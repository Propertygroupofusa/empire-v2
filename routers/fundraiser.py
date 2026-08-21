"""Fundraising router (GoFundMe-style) using Stripe checkout + webhooks."""

from datetime import datetime
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_auth import require_admin_key
from database import get_db
from models import FundraiserCampaign, FundraiserDonation

log = logging.getLogger("fundraiser")
router = APIRouter()

# Stripe setup
try:
    import stripe

    STRIPE_AVAILABLE = True
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY
except Exception as e:
    log.warning(f"Stripe not available in fundraiser router: {e}")
    STRIPE_AVAILABLE = False
    STRIPE_SECRET_KEY = None
    STRIPE_PUBLISHABLE_KEY = None
    STRIPE_WEBHOOK_SECRET = None

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://empire-v2-production.up.railway.app").rstrip("/")


class FundraiserCampaignCreate(BaseModel):
    title: str
    description: Optional[str] = None
    beneficiary_name: Optional[str] = None
    goal_amount_cents: int = Field(..., gt=0)
    minimum_donation_cents: int = Field(default=500, gt=0)
    allow_custom_amount: bool = True
    currency: str = "usd"


class DonationCheckoutRequest(BaseModel):
    donor_name: Optional[str] = None
    donor_email: str
    amount_cents: int = Field(..., gt=0)
    message: Optional[str] = None


@router.get("/stripe-key")
async def fundraiser_stripe_key():
    """Return publishable Stripe key for fundraiser frontend."""
    if not STRIPE_AVAILABLE or not STRIPE_PUBLISHABLE_KEY:
        return {"publishable_key": None}
    return {"publishable_key": STRIPE_PUBLISHABLE_KEY}


@router.post("/campaigns", dependencies=[Depends(require_admin_key)])
async def create_campaign(payload: FundraiserCampaignCreate, db: AsyncSession = Depends(get_db)):
    campaign = FundraiserCampaign(
        title=payload.title,
        description=payload.description,
        beneficiary_name=payload.beneficiary_name,
        goal_amount_cents=payload.goal_amount_cents,
        minimum_donation_cents=payload.minimum_donation_cents,
        allow_custom_amount=payload.allow_custom_amount,
        currency=payload.currency.lower(),
        is_active=True,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return {"success": True, "campaign": campaign.to_dict()}


@router.get("/campaigns")
async def list_campaigns(include_inactive: bool = False, db: AsyncSession = Depends(get_db)):
    stmt = select(FundraiserCampaign).order_by(FundraiserCampaign.created_at.desc())
    if not include_inactive:
        stmt = stmt.where(FundraiserCampaign.is_active.is_(True))
    rows = await db.execute(stmt)
    campaigns = rows.scalars().all()
    return {"campaigns": [c.to_dict() for c in campaigns], "count": len(campaigns)}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(FundraiserCampaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    donations_result = await db.execute(
        select(FundraiserDonation)
        .where(
            FundraiserDonation.campaign_id == campaign_id,
            FundraiserDonation.status == "paid",
        )
        .order_by(FundraiserDonation.created_at.desc())
        .limit(25)
    )
    recent = donations_result.scalars().all()

    return {
        "campaign": campaign.to_dict(),
        "recent_donations": [
            {
                "donor_name": d.donor_name or "Anonymous",
                "amount_cents": d.amount_cents,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in recent
        ],
    }


@router.post("/campaigns/{campaign_id}/checkout")
async def create_donation_checkout(
    campaign_id: int,
    payload: DonationCheckoutRequest,
    db: AsyncSession = Depends(get_db),
):
    if not STRIPE_AVAILABLE or not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    campaign = await db.get(FundraiserCampaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not campaign.is_active:
        raise HTTPException(status_code=400, detail="Campaign is not active")

    if payload.amount_cents < campaign.minimum_donation_cents:
        minimum_dollars = campaign.minimum_donation_cents / 100
        raise HTTPException(
            status_code=400,
            detail=f"Minimum donation is ${minimum_dollars:.2f}",
        )

    donation = FundraiserDonation(
        campaign_id=campaign_id,
        donor_name=payload.donor_name,
        donor_email=payload.donor_email,
        amount_cents=payload.amount_cents,
        currency=campaign.currency,
        status="pending",
        message=payload.message,
    )
    db.add(donation)
    await db.commit()
    await db.refresh(donation)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": campaign.currency,
                        "unit_amount": payload.amount_cents,
                        "product_data": {
                            "name": f"Donation - {campaign.title}",
                            "description": campaign.description or "Fundraiser donation",
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=f"{APP_BASE_URL}/fundraiser/donate?campaign_id={campaign_id}&success=1",
            cancel_url=f"{APP_BASE_URL}/fundraiser/donate?campaign_id={campaign_id}&canceled=1",
            customer_email=payload.donor_email,
            metadata={
                "campaign_id": str(campaign_id),
                "donation_id": str(donation.id),
            },
            payment_intent_data={
                "metadata": {
                    "campaign_id": str(campaign_id),
                    "donation_id": str(donation.id),
                }
            },
        )
        donation.stripe_session_id = session.id
        await db.commit()
        return {
            "session_id": session.id,
            "checkout_url": getattr(session, "url", None),
            "donation_id": donation.id,
        }
    except Exception as e:
        donation.status = "failed"
        await db.commit()
        log.error(f"Donation checkout failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Unable to create checkout session")


@router.post("/webhook/stripe")
async def stripe_fundraiser_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    if not STRIPE_WEBHOOK_SECRET:
        return {"status": "webhook_secret_not_configured"}

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        log.error(f"Fundraiser webhook invalid payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        log.error(f"Fundraiser webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type")
    event_object = event.get("data", {}).get("object", {})
    metadata = event_object.get("metadata", {})
    donation_id = metadata.get("donation_id")

    if donation_id:
        donation = await db.get(FundraiserDonation, int(donation_id))
        if donation:
            if event_type == "checkout.session.completed":
                if donation.status != "paid":
                    donation.status = "paid"
                    donation.paid_at = datetime.utcnow()
                    donation.stripe_session_id = event_object.get("id") or donation.stripe_session_id
                    donation.stripe_payment_intent_id = event_object.get("payment_intent")

                    campaign = await db.get(FundraiserCampaign, donation.campaign_id)
                    if campaign:
                        campaign.amount_raised_cents = (campaign.amount_raised_cents or 0) + donation.amount_cents
                    await db.commit()
                    log.info(f"Donation {donation.id} completed for campaign {donation.campaign_id}")
            elif event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
                if donation.status == "pending":
                    donation.status = "failed"
                    await db.commit()
                    log.info(f"Donation {donation.id} marked failed from event {event_type}")

    return {"received": True}


@router.get("/donate", response_class=HTMLResponse)
async def fundraiser_page():
    """Serve fundraiser donation page."""
    possible_paths = [
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fundraiser_donate.html",
        ),
        "/app/fundraiser_donate.html",
        "fundraiser_donate.html",
    ]

    for path in possible_paths:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return HTMLResponse(content=f.read(), status_code=200)
        except Exception as e:
            log.warning(f"Error reading fundraiser page from {path}: {e}")
    raise HTTPException(status_code=404, detail="Fundraiser page not found")
