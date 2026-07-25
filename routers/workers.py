"""Worker/contractor registration, login, credential submission, and
admin verification - the supply side of the marketplace (workers get
matched to client jobs once notary_bot.py or an admin verifies their
credentials, then self-service their own jobs/bookings via /me once
logged in).

Route ordering matters here: /me and its sub-paths are a single path
segment, the same shape as the dynamic /{worker_id} - they're registered
BEFORE /{worker_id} so "me" is never accidentally matched as a worker_id
(which would 422 on int conversion instead of falling through)."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from worker_auth import hash_password, verify_password, create_worker_token, require_worker_auth
from models import Worker, Job, Booking

log = logging.getLogger("workers")
router = APIRouter()


class WorkerRegisterRequest(BaseModel):
    email: str
    name: str
    password: str
    phone: str = None


@router.post("/register")
async def register_worker(payload: WorkerRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Creates or returns an existing worker profile by email - idempotent
    so a worker can re-submit the same intake form without erroring. A
    re-registration attempt does NOT change the password of an existing
    account - that's a deliberate choice (password changes should go
    through a dedicated flow, not implicitly via re-submitting intake)."""
    email = payload.email.lower()
    result = await db.execute(select(Worker).where(Worker.email == email))
    worker = result.scalar_one_or_none()
    if worker:
        return worker.to_dict()

    worker = Worker(email=email, name=payload.name, phone=payload.phone, password_hash=hash_password(payload.password))
    db.add(worker)
    await db.commit()
    await db.refresh(worker)
    log.info(f"New worker registered: {worker.email} (id={worker.id})")
    return worker.to_dict()


class WorkerLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login_worker(payload: WorkerLoginRequest, db: AsyncSession = Depends(get_db)):
    """Real worker login - returns a JWT for the /me self-service
    endpoints below. Same error either way (wrong email vs wrong
    password vs no password set yet) so this can't be used to enumerate
    which emails are registered."""
    result = await db.execute(select(Worker).where(Worker.email == payload.email.lower()))
    worker = result.scalar_one_or_none()
    if not worker or not verify_password(payload.password, worker.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_worker_token(worker.id, worker.email)
    return {"access_token": token, "token_type": "bearer", "worker": worker.to_dict()}


@router.get("/me")
async def get_my_profile(worker_id: int = Depends(require_worker_auth), db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker.to_dict()


class NotaryCredentialsRequest(BaseModel):
    notary_commission_number: str = None
    notary_commission_state: str = None
    notary_commission_expires: str = None  # ISO date string, e.g. "2027-06-30"
    ron_authorized: bool = False
    ron_authorization_state: str = None
    w9_legal_name: str = None
    w9_tax_classification: str = None
    w9_tin_last4: str = None
    w9_address: str = None


def _apply_credentials(worker: Worker, payload: NotaryCredentialsRequest):
    worker.notary_commission_number = payload.notary_commission_number
    worker.notary_commission_state = payload.notary_commission_state
    worker.notary_commission_expires = payload.notary_commission_expires
    worker.ron_authorized = payload.ron_authorized
    worker.ron_authorization_state = payload.ron_authorization_state
    worker.w9_legal_name = payload.w9_legal_name
    worker.w9_tax_classification = payload.w9_tax_classification
    worker.w9_tin_last4 = payload.w9_tin_last4
    worker.w9_address = payload.w9_address
    worker.w9_submitted = bool(payload.w9_legal_name)
    worker.credentials_submitted = True
    worker.credentials_verified = False  # any resubmission requires re-verification


@router.post("/me/credentials")
async def submit_my_credentials(
    payload: NotaryCredentialsRequest,
    worker_id: int = Depends(require_worker_auth),
    db: AsyncSession = Depends(get_db),
):
    """Self-service credential submission for the logged-in worker. Sets
    credentials_submitted=True but NOT credentials_verified - verification
    is a separate, admin-only step (see /{worker_id}/verify) since these
    are real legal credentials that shouldn't be trusted just because
    someone typed them in."""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    _apply_credentials(worker, payload)
    await db.commit()
    await db.refresh(worker)
    log.info(f"Worker {worker.email} (id={worker.id}) submitted credentials via self-service - awaiting verification")
    return worker.to_dict()


@router.get("/me/jobs")
async def list_my_jobs(worker_id: int = Depends(require_worker_auth), db: AsyncSession = Depends(get_db)):
    """All jobs matched to the logged-in worker, any status - self-service
    replacement for needing an admin to look this up."""
    result = await db.execute(select(Job).where(Job.worker_id == worker_id).order_by(Job.created_at.desc()))
    return {"jobs": [j.to_dict() for j in result.scalars().all()]}


class ScheduleMyJobRequest(BaseModel):
    scheduled_start: datetime
    scheduled_end: datetime = None
    meeting_link: str = None


@router.post("/me/jobs/{job_id}/schedule")
async def schedule_my_job(
    job_id: int,
    payload: ScheduleMyJobRequest,
    worker_id: int = Depends(require_worker_auth),
    db: AsyncSession = Depends(get_db),
):
    """Self-service scheduling for one of the logged-in worker's own
    matched jobs - same effect as the admin-gated POST /bookings, but
    scoped to jobs actually assigned to this worker, no admin key needed."""
    job = await db.get(Job, job_id)
    if not job or job.worker_id != worker_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "matched":
        raise HTTPException(status_code=400, detail=f"Job is '{job.status}', not 'matched' - schedule after matching")

    booking = Booking(
        job_id=job.id,
        worker_id=worker_id,
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
    log.info(f"Worker {worker_id} self-scheduled booking {booking.id} for job {job.id}")
    return booking.to_dict()


@router.post("/me/jobs/{job_id}/complete")
async def complete_my_job(
    job_id: int,
    worker_id: int = Depends(require_worker_auth),
    db: AsyncSession = Depends(get_db),
):
    """Self-service completion for one of the logged-in worker's own
    jobs - scoped so a worker can only complete jobs actually assigned to
    them, not anyone else's."""
    job = await db.get(Job, job_id)
    if not job or job.worker_id != worker_id:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "completed"
    job.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(job)
    log.info(f"Worker {worker_id} self-completed job {job.id}")
    return job.to_dict()


@router.get("/{worker_id}")
async def get_worker(worker_id: int, email: str, db: AsyncSession = Depends(get_db)):
    """Worker-facing lookup, scoped by ID+email like the client/order
    portal patterns elsewhere in this app - worker_id is a small
    sequential integer with no other protection, so the registered email
    is required as a second factor, and the same 404 is returned whether
    the ID doesn't exist or the email doesn't match."""
    worker = await db.get(Worker, worker_id)
    if not worker or worker.email.lower() != email.lower():
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker.to_dict()


class LegacyNotaryCredentialsRequest(NotaryCredentialsRequest):
    email: str


@router.post("/{worker_id}/credentials")
async def submit_credentials(worker_id: int, payload: LegacyNotaryCredentialsRequest, db: AsyncSession = Depends(get_db)):
    """Legacy email-scoped credential submission (pre-login). Prefer
    POST /me/credentials once logged in - this remains for a worker who
    hasn't set up login yet."""
    worker = await db.get(Worker, worker_id)
    if not worker or worker.email.lower() != payload.email.lower():
        raise HTTPException(status_code=404, detail="Worker not found")

    _apply_credentials(worker, payload)
    await db.commit()
    await db.refresh(worker)
    log.info(f"Worker {worker.email} (id={worker.id}) submitted credentials - awaiting verification")
    return worker.to_dict()


class VerifyCredentialsRequest(BaseModel):
    verified: bool


@router.post("/{worker_id}/verify", dependencies=[Depends(require_admin_key)])
async def verify_credentials(worker_id: int, payload: VerifyCredentialsRequest, db: AsyncSession = Depends(get_db)):
    """Admin-only: approve or reject a worker's submitted credentials.
    Only verified workers are eligible for job matching (see
    notary_bot.py / GET /workers/notaries/available)."""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if not worker.credentials_submitted:
        raise HTTPException(status_code=400, detail="Worker has not submitted credentials yet")

    worker.credentials_verified = payload.verified
    await db.commit()
    await db.refresh(worker)
    log.info(f"Worker {worker.email} (id={worker.id}) credentials {'verified' if payload.verified else 'rejected'}")
    return worker.to_dict()


@router.get("/notaries/available", dependencies=[Depends(require_admin_key)])
async def list_available_notaries(state: str = None, db: AsyncSession = Depends(get_db)):
    """Verified, active notaries eligible for job matching - optionally
    filtered by the commission/RON-authorization state a job needs. This
    is the same eligibility notary_bot.py checks when auto-matching; this
    endpoint just exposes it for admin visibility/debugging (the bot
    queries the database directly, not through this HTTP endpoint)."""
    query = select(Worker).where(Worker.credentials_verified == True, Worker.status == "active")  # noqa: E712
    result = await db.execute(query)
    notaries = [w for w in result.scalars().all() if w.notary_commission_number or w.ron_authorized]

    if state:
        notaries = [
            w for w in notaries
            if w.notary_commission_state == state or (w.ron_authorized and w.ron_authorization_state == state)
        ]

    return {"notaries": [w.to_dict() for w in notaries]}


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_workers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Worker).order_by(Worker.created_at.desc()))
    return {"workers": [w.to_dict() for w in result.scalars().all()]}
