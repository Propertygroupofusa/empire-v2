"""Outreach and campaign management router."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict

from database import get_db
from models import Campaign, CampaignContact

router = APIRouter()


class CampaignCreate(BaseModel):
    name: str
    description: Optional[str] = None
    outreach_type: str
    target_audience: Optional[dict] = None
    message_template: str
    scheduled_for: Optional[str] = None
    custom_metadata: Optional[dict] = None


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    message_template: Optional[str] = None
    scheduled_for: Optional[str] = None
    custom_metadata: Optional[dict] = None


class ContactStatusUpdate(BaseModel):
    status: str


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    status: str
    outreach_type: str
    target_audience: Optional[dict]
    message_template: str
    created_at: datetime
    updated_at: datetime
    scheduled_for: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_active: bool
    custom_metadata: Optional[dict] = None


class CampaignContactCreate(BaseModel):
    campaign_id: int
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    contact_data: Optional[dict] = None


class CampaignContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    contact_data: Optional[dict] = None
    status: str
    sent_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None
    replied_at: Optional[datetime] = None
    custom_metadata: Optional[dict] = None


@router.post("/campaigns", response_model=CampaignResponse)
async def create_campaign(campaign: CampaignCreate, db: AsyncSession = Depends(get_db)):
    """Create a new outreach campaign."""
    db_campaign = Campaign(
        name=campaign.name,
        description=campaign.description,
        outreach_type=campaign.outreach_type,
        target_audience=campaign.target_audience,
        message_template=campaign.message_template,
        scheduled_for=datetime.fromisoformat(campaign.scheduled_for) if campaign.scheduled_for else None,
        custom_metadata=campaign.custom_metadata,
    )
    db.add(db_campaign)
    await db.commit()
    await db.refresh(db_campaign)
    return db_campaign


@router.get("/campaigns", response_model=List[CampaignResponse])
async def list_campaigns(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """List all campaigns with optional filtering."""
    query = select(Campaign)

    if status:
        query = query.where(Campaign.status == status)
    if is_active is not None:
        query = query.where(Campaign.is_active == is_active)

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Get a specific campaign by ID."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.patch("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    campaign_update: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    update_data = campaign_update.model_dump(exclude_unset=True)
    if "scheduled_for" in update_data and update_data["scheduled_for"]:
        update_data["scheduled_for"] = datetime.fromisoformat(update_data["scheduled_for"])

    for key, value in update_data.items():
        setattr(campaign, key, value)

    campaign.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    db.delete(campaign)
    await db.commit()
    return {"message": "Campaign deleted"}


@router.post("/campaigns/{campaign_id}/contacts", response_model=CampaignContactResponse)
async def add_campaign_contact(
    campaign_id: int,
    contact: CampaignContactCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a contact to a campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Campaign not found")

    db_contact = CampaignContact(
        campaign_id=campaign_id,
        email=contact.email,
        phone=contact.phone,
        name=contact.name,
        contact_data=contact.contact_data,
    )
    db.add(db_contact)
    await db.commit()
    await db.refresh(db_contact)
    return db_contact


@router.get("/campaigns/{campaign_id}/contacts", response_model=List[CampaignContactResponse])
async def list_campaign_contacts(
    campaign_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List contacts for a campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Campaign not found")

    query = select(CampaignContact).where(CampaignContact.campaign_id == campaign_id)

    if status:
        query = query.where(CampaignContact.status == status)

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.patch("/campaigns/{campaign_id}/contacts/{contact_id}", response_model=CampaignContactResponse)
async def update_contact_status(
    campaign_id: int,
    contact_id: int,
    update: ContactStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update contact status (e.g., mark as sent, opened, replied)."""
    result = await db.execute(
        select(CampaignContact).where(
            (CampaignContact.id == contact_id) & (CampaignContact.campaign_id == campaign_id)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    contact.status = update.status
    if update.status == "sent":
        contact.sent_at = datetime.utcnow()
    elif update.status == "opened":
        contact.opened_at = datetime.utcnow()
    elif update.status == "clicked":
        contact.clicked_at = datetime.utcnow()
    elif update.status == "replied":
        contact.replied_at = datetime.utcnow()

    await db.commit()
    await db.refresh(contact)
    return contact


@router.delete("/campaigns/{campaign_id}/contacts/{contact_id}")
async def delete_contact(
    campaign_id: int,
    contact_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a contact from a campaign."""
    result = await db.execute(
        select(CampaignContact).where(
            (CampaignContact.id == contact_id) & (CampaignContact.campaign_id == campaign_id)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    db.delete(contact)
    await db.commit()
    return {"message": "Contact deleted"}


@router.post("/campaigns/{campaign_id}/activate")
async def activate_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Activate a campaign to start sending outreach."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "active"
    campaign.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Pause an active campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.status = "paused"
    campaign.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("/campaigns/{campaign_id}/stats")
async def get_campaign_stats(campaign_id: int, db: AsyncSession = Depends(get_db)):
    """Get campaign statistics."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    contacts_result = await db.execute(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign_id)
    )
    contacts = contacts_result.scalars().all()

    total = len(contacts)
    sent = sum(1 for c in contacts if c.sent_at)
    opened = sum(1 for c in contacts if c.opened_at)
    clicked = sum(1 for c in contacts if c.clicked_at)
    replied = sum(1 for c in contacts if c.replied_at)

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign.name,
        "total_contacts": total,
        "sent": sent,
        "opened": opened,
        "opened_rate": (opened / sent * 100) if sent > 0 else 0,
        "clicked": clicked,
        "click_rate": (clicked / sent * 100) if sent > 0 else 0,
        "replied": replied,
        "reply_rate": (replied / sent * 100) if sent > 0 else 0,
    }
