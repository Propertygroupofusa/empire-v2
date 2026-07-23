"""Worker/contractor registration, credential submission, and admin
verification - the supply side of the marketplace (workers get matched to
client jobs once notary_bot.py or an admin verifies their credentials)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import Worker

log = logging.getLogger("workers")
router = APIRouter()


class WorkerRegisterRequest(BaseModel):
    email: str
    name: str
    phone: str = None


@router.post("/register")
async def register_worker(payload: WorkerRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Creates or returns an existing worker profile by email - idempotent
    so a worker can re-submit the same intake form without erroring."""
    email = payload.email.lower()
    result = await db.execute(select(Worker).where(Worker.email == email))
    worker = result.scalar_one_or_none()
    if worker:
        return worker.to_dict()

    worker = Worker(email=email, name=payload.name, phone=payload.phone)
    db.add(worker)
    await db.commit()
    await db.refresh(worker)
    log.info(f"New worker registered: {worker.email} (id={worker.id})")
    return worker.to_dict()


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


class NotaryCredentialsRequest(BaseModel):
    email: str
    notary_commission_number: str = None
    notary_commission_state: str = None
    notary_commission_expires: str = None  # ISO date string, e.g. "2027-06-30"
    ron_authorized: bool = False
    ron_authorization_state: str = None
    w9_legal_name: str = None
    w9_tax_classification: str = None
    w9_tin_last4: str = None
    w9_address: str = None


@router.post("/{worker_id}/credentials")
async def submit_credentials(worker_id: int, payload: NotaryCredentialsRequest, db: AsyncSession = Depends(get_db)):
    """Worker submits notary commission info, RON authorization, and W9
    details for admin review. Sets credentials_submitted=True but NOT
    credentials_verified - verification is a separate, admin-only step
    (see /verify below) since these are real legal credentials that
    shouldn't be trusted just because someone typed them in."""
    worker = await db.get(Worker, worker_id)
    if not worker or worker.email.lower() != payload.email.lower():
        raise HTTPException(status_code=404, detail="Worker not found")

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
