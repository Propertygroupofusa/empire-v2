"""Client service requests ("jobs") and matching to eligible workers.
Starts with job_type="notarization" (matched by notary_bot.py against
verified, credentialed notary workers) but the shape is generic enough
for this platform's other advertised verticals (tax prep, legal docs)."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import Job, Client, Worker

log = logging.getLogger("jobs")
router = APIRouter()

VALID_STATUSES = ("requested", "matched", "scheduled", "completed", "cancelled")


class NotarizationRequest(BaseModel):
    client_email: str
    client_name: str
    state: str  # US state jurisdiction the notarization must be performed in
    description: str = None


@router.post("/notarization/request")
async def request_notarization(payload: NotarizationRequest, db: AsyncSession = Depends(get_db)):
    """Client-facing intake: creates (or reuses) the client profile and
    opens a new notarization job in "requested" status. notary_bot.py
    picks this up and matches it to a verified, eligible notary in the
    same state - no separate client-registration step required first."""
    email = payload.client_email.lower()
    result = await db.execute(select(Client).where(Client.email == email))
    client = result.scalar_one_or_none()
    if not client:
        client = Client(email=email, name=payload.client_name)
        db.add(client)
        await db.commit()
        await db.refresh(client)
        log.info(f"New client registered via job intake: {client.email} (id={client.id})")

    job = Job(
        job_type="notarization",
        client_id=client.id,
        state=payload.state.upper(),
        description=payload.description,
        status="requested",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    log.info(f"New notarization job requested: id={job.id} client={client.email} state={job.state}")
    return job.to_dict()


@router.get("/{job_id}")
async def get_job(job_id: int, email: str, db: AsyncSession = Depends(get_db)):
    """Client-facing status lookup, scoped by ID+email like the other
    customer-portal patterns in this app - job_id is a small sequential
    integer with no other protection, so the requesting client's email is
    required as a second factor, and the same 404 is returned whether the
    ID doesn't exist or the email doesn't match."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    client = await db.get(Client, job.client_id)
    if not client or client.email.lower() != email.lower():
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_jobs(status: str = None, db: AsyncSession = Depends(get_db)):
    query = select(Job).order_by(Job.created_at.desc())
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(VALID_STATUSES)}")
        query = query.where(Job.status == status)
    result = await db.execute(query)
    return {"jobs": [j.to_dict() for j in result.scalars().all()]}


class MatchJobRequest(BaseModel):
    worker_id: int


@router.post("/{job_id}/match", dependencies=[Depends(require_admin_key)])
async def match_job(job_id: int, payload: MatchJobRequest, db: AsyncSession = Depends(get_db)):
    """Admin manual override to assign a specific worker to a job -
    notary_bot.py does this automatically for eligible verified notaries;
    this exists for cases needing a manual match (e.g. no automatic
    eligible match, or an admin override)."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "requested":
        raise HTTPException(status_code=400, detail=f"Job is '{job.status}', not 'requested' - cannot match")

    worker = await db.get(Worker, payload.worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if not worker.credentials_verified:
        raise HTTPException(status_code=400, detail="Worker's credentials are not verified")

    job.worker_id = worker.id
    job.status = "matched"
    job.matched_at = datetime.utcnow()
    await db.commit()
    await db.refresh(job)
    log.info(f"Job {job.id} manually matched to worker {worker.email} (id={worker.id})")
    return job.to_dict()


@router.post("/{job_id}/complete", dependencies=[Depends(require_admin_key)])
async def complete_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "completed"
    job.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(job)
    log.info(f"Job {job.id} marked completed")
    return job.to_dict()


@router.post("/{job_id}/cancel", dependencies=[Depends(require_admin_key)])
async def cancel_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "cancelled"
    await db.commit()
    await db.refresh(job)
    log.info(f"Job {job.id} cancelled")
    return job.to_dict()
