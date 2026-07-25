"""Scheduling for a matched job (e.g. a RON session appointment)."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import Booking, Job, Client

log = logging.getLogger("bookings")
router = APIRouter()


class CreateBookingRequest(BaseModel):
    job_id: int
    scheduled_start: datetime
    scheduled_end: datetime = None
    meeting_link: str = None


@router.post("", dependencies=[Depends(require_admin_key)])
async def create_booking(payload: CreateBookingRequest, db: AsyncSession = Depends(get_db)):
    """Schedules an appointment for a job that's already been matched to a
    worker (see routers/jobs.py). Admin-gated for now - there's no worker
    self-service auth/session system built yet, just email-scoped lookups,
    so scheduling goes through admin until that exists."""
    job = await db.get(Job, payload.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "matched":
        raise HTTPException(status_code=400, detail=f"Job is '{job.status}', not 'matched' - schedule after matching")

    booking = Booking(
        job_id=job.id,
        worker_id=job.worker_id,
        client_id=job.client_id,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        meeting_link=payload.meeting_link,
        status="scheduled",
    )
    db.add(booking)
    job.status = "scheduled"
    await db.commit()
    await db.refresh(booking)
    log.info(f"Booking {booking.id} scheduled for job {job.id} at {booking.scheduled_start.isoformat()}")
    return booking.to_dict()


@router.get("/{booking_id}")
async def get_booking(booking_id: int, email: str, db: AsyncSession = Depends(get_db)):
    """Client-facing lookup, scoped by ID+email like the other
    customer-portal patterns in this app."""
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    client = await db.get(Client, booking.client_id)
    if not client or client.email.lower() != email.lower():
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking.to_dict()


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_bookings(status: str = None, db: AsyncSession = Depends(get_db)):
    query = select(Booking).order_by(Booking.scheduled_start.desc())
    if status:
        query = query.where(Booking.status == status)
    result = await db.execute(query)
    return {"bookings": [b.to_dict() for b in result.scalars().all()]}


@router.post("/{booking_id}/complete", dependencies=[Depends(require_admin_key)])
async def complete_booking(booking_id: int, db: AsyncSession = Depends(get_db)):
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking.status = "completed"
    job = await db.get(Job, booking.job_id)
    if job:
        job.status = "completed"
        job.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(booking)
    log.info(f"Booking {booking.id} marked completed (job {booking.job_id} also completed)")
    return booking.to_dict()


@router.post("/{booking_id}/cancel", dependencies=[Depends(require_admin_key)])
async def cancel_booking(booking_id: int, db: AsyncSession = Depends(get_db)):
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking.status = "cancelled"
    await db.commit()
    await db.refresh(booking)
    log.info(f"Booking {booking.id} cancelled")
    return booking.to_dict()
