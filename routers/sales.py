"""Sales Agent Router — API endpoints for lead management and outreach"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import logging

from database import get_db
from models.sales import Lead, Outreach, Response, LeadStatus, LeadSource, OutreachType
from sales_agent import process_new_leads, process_followups, generate_daily_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sales", tags=["sales"])


# ============================================================
# Pydantic Schemas
# ============================================================

class LeadCreate(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    company_name: str
    company_website: Optional[str] = None
    company_size: Optional[str] = None
    industry: Optional[str] = None
    target_product: str  # "video_production", "property_group", "ai_course"
    source: str = "manual"


class LeadResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    company_name: str
    fit_score: int
    status: str
    times_contacted: int
    created_at: datetime
    last_contact_date: Optional[datetime]

    class Config:
        from_attributes = True


class OutreachRecord(BaseModel):
    id: int
    lead_id: int
    subject: str
    sent_at: datetime
    status: str
    opened_at: Optional[datetime]

    class Config:
        from_attributes = True


class DailyReportResponse(BaseModel):
    date: str
    outreach_sent_today: int
    responses_received_today: int
    total_leads: int
    hot_leads: List[dict]


# ============================================================
# Endpoints
# ============================================================

@router.post("/leads", response_model=LeadResponse)
async def create_lead(lead_data: LeadCreate, db: AsyncSession = Depends(get_db)):
    """Add a new lead manually"""
    try:
        # Check if lead already exists
        stmt = select(Lead).where(Lead.email == lead_data.email)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            raise HTTPException(status_code=400, detail="Lead already exists")

        lead = Lead(
            first_name=lead_data.first_name,
            last_name=lead_data.last_name,
            email=lead_data.email,
            phone=lead_data.phone,
            company_name=lead_data.company_name,
            company_website=lead_data.company_website,
            company_size=lead_data.company_size,
            industry=lead_data.industry,
            target_product=lead_data.target_product,
            source=LeadSource.MANUAL,
            status=LeadStatus.NEW
        )
        db.add(lead)
        await db.commit()
        await db.refresh(lead)

        logger.info(f"Created new lead: {lead.email}")
        return lead

    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create lead: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/leads", response_model=List[LeadResponse])
async def list_leads(
    status: Optional[str] = None,
    target_product: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """List leads with optional filters"""
    stmt = select(Lead)

    if status:
        stmt = stmt.where(Lead.status == status)
    if target_product:
        stmt = stmt.where(Lead.target_product == target_product)

    stmt = stmt.limit(limit).order_by(Lead.created_at.desc())
    result = await db.execute(stmt)
    leads = result.scalars().all()

    return leads


@router.get("/leads/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single lead"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.post("/process-leads")
async def process_leads_manual(
    limit: int = 50,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db)
):
    """Manually trigger lead research and outreach"""
    if background_tasks:
        background_tasks.add_task(process_new_leads, db, limit)
        return {"status": "processing", "message": f"Started processing up to {limit} leads"}
    else:
        await process_new_leads(db, limit)
        return {"status": "completed", "message": f"Processed leads"}


@router.post("/process-followups")
async def process_followups_manual(
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db)
):
    """Manually trigger follow-up emails"""
    if background_tasks:
        background_tasks.add_task(process_followups, db)
        return {"status": "processing", "message": "Started processing follow-ups"}
    else:
        await process_followups(db)
        return {"status": "completed", "message": "Processed follow-ups"}


@router.get("/outreach", response_model=List[OutreachRecord])
async def list_outreach(
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """Get recent outreach activity"""
    stmt = select(Outreach).limit(limit).order_by(Outreach.sent_at.desc())
    result = await db.execute(stmt)
    outreaches = result.scalars().all()
    return outreaches


@router.get("/daily-report", response_model=DailyReportResponse)
async def get_daily_report(db: AsyncSession = Depends(get_db)):
    """Get today's sales activity report"""
    report = await generate_daily_report(db)
    return report


@router.get("/stats")
async def get_sales_stats(db: AsyncSession = Depends(get_db)):
    """Get overall sales statistics"""
    # Total leads
    stmt_total = select(Lead)
    result = await db.execute(stmt_total)
    total_leads = len(result.scalars().all())

    # Leads by status
    stmt_status = select(Lead)
    result = await db.execute(stmt_status)
    all_leads = result.scalars().all()

    status_counts = {}
    for lead in all_leads:
        status = lead.status.value if hasattr(lead.status, 'value') else lead.status
        status_counts[status] = status_counts.get(status, 0) + 1

    # Average fit score
    fit_scores = [l.fit_score for l in all_leads if l.fit_score]
    avg_fit = sum(fit_scores) / len(fit_scores) if fit_scores else 0

    # Outreach count
    stmt_outreach = select(Outreach)
    result = await db.execute(stmt_outreach)
    total_outreach = len(result.scalars().all())

    # Response count
    stmt_responses = select(Response)
    result = await db.execute(stmt_responses)
    total_responses = len(result.scalars().all())

    response_rate = (total_responses / total_outreach * 100) if total_outreach > 0 else 0

    return {
        "total_leads": total_leads,
        "status_breakdown": status_counts,
        "average_fit_score": round(avg_fit, 1),
        "total_outreach_sent": total_outreach,
        "total_responses": total_responses,
        "response_rate_percent": round(response_rate, 1)
    }


@router.post("/leads/{lead_id}/mark-responded")
async def mark_lead_responded(lead_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a lead as having responded"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.status = LeadStatus.RESPONDED
    lead.last_response_date = datetime.utcnow()
    await db.commit()

    return {"status": "updated", "lead_id": lead_id}


@router.post("/leads/{lead_id}/qualify")
async def qualify_lead(lead_id: int, qualified: bool, db: AsyncSession = Depends(get_db)):
    """Mark a lead as qualified or disqualified"""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.status = LeadStatus.QUALIFIED if qualified else LeadStatus.DISQUALIFIED
    await db.commit()

    return {"status": "updated", "lead_id": lead_id, "qualified": qualified}
