"""Client service requests ("jobs") and matching to eligible workers.
Starts with job_type="notarization" (matched by notary_bot.py against
verified, credentialed notary workers) but the shape is generic enough
for this platform's other advertised verticals (tax prep, legal docs)."""
import logging
import os
from datetime import datetime

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from admin_auth import require_admin_key
from models import Job, Client, Worker, NotaryPayout
from payments_pause import payments_paused, PAUSE_MESSAGE

log = logging.getLogger("jobs")
router = APIRouter()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

VALID_STATUSES = ("requested", "matched", "scheduled", "completed", "cancelled")

# Placeholder starting prices for each notarization service tier - tune
# these to your actual market rates whenever you're ready, no code changes
# needed elsewhere since every price flows from this one dict. Snapshotted
# onto Job.price at request time, so changing a price here never rewrites
# an already-placed job.
SERVICE_TIERS = {
    "standard": {
        "label": "Standard Notarization",
        "description": "In-person or RON, 1-2 signatures",
        "price": 35.00,
    },
    "ron": {
        "label": "Remote Online Notarization (RON)",
        "description": "Live video session with a RON-authorized notary",
        "price": 45.00,
    },
    "business_legal": {
        "label": "Business & Legal Documents",
        "description": "Contracts, affidavits, powers of attorney",
        "price": 55.00,
    },
    "real_estate_closing": {
        "label": "Real Estate Closing / Loan Signing Package",
        "description": "Full closing document package",
        "price": 200.00,
    },
}

# The notary's cut of what the client paid, taken at job-completion time
# (see create_payout_for_completed_job below) - the platform keeps the
# remainder. Overridable via env var without a code change if the split
# terms ever change.
NOTARY_PAYOUT_SHARE = float(os.getenv("NOTARY_PAYOUT_SHARE", "0.80"))


@router.get("/service-tiers")
async def get_service_tiers():
    """Public, no-auth - lets the client intake page render live pricing
    instead of hardcoding dollar amounts into the HTML, so SERVICE_TIERS
    above stays the single source of truth for price."""
    return {"tiers": [{"key": key, **info} for key, info in SERVICE_TIERS.items()]}


class NotarizationRequest(BaseModel):
    client_email: str
    client_name: str
    state: str  # US state jurisdiction the notarization must be performed in
    service_tier: str
    description: str = None


@router.post("/notarization/request")
async def request_notarization(payload: NotarizationRequest, db: AsyncSession = Depends(get_db)):
    """Client-facing intake: creates (or reuses) the client profile and
    opens a new notarization job in "requested" status, priced from
    SERVICE_TIERS. The job isn't matched to a notary until it's actually
    paid (see notary_bot.py and POST /{job_id}/match) - call
    POST /{job_id}/create-checkout next to collect payment."""
    if payload.service_tier not in SERVICE_TIERS:
        raise HTTPException(status_code=400, detail=f"service_tier must be one of: {', '.join(SERVICE_TIERS)}")

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
        service_tier=payload.service_tier,
        price=SERVICE_TIERS[payload.service_tier]["price"],
        paid=False,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    log.info(f"New notarization job requested: id={job.id} client={client.email} state={job.state} tier={job.service_tier} price=${job.price:.2f}")
    return job.to_dict()


@router.post("/{job_id}/create-checkout")
async def create_job_checkout(job_id: int, db: AsyncSession = Depends(get_db)):
    """Creates a Stripe Checkout session for the job's price - same
    pattern as routers/orders.py's video-order checkout. The client pays
    upfront, before a notary ever picks up the job; notary_bot.py and the
    manual /match endpoint both refuse to match a job that isn't paid."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.paid:
        raise HTTPException(status_code=400, detail="Job already paid")
    if payments_paused():
        raise HTTPException(status_code=503, detail=PAUSE_MESSAGE)

    client = await db.get(Client, job.client_id)

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Notarization: {SERVICE_TIERS[job.service_tier]['label']}",
                            "description": job.description or SERVICE_TIERS[job.service_tier]["description"],
                        },
                        "unit_amount": round(job.price * 100),
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url="https://empire-v2-production.up.railway.app/get-notarized?paid=1&job_id=" + str(job.id),
            cancel_url="https://empire-v2-production.up.railway.app/get-notarized",
            customer_email=client.email if client else None,
            metadata={"job_id": job.id},
        )
    except Exception as e:
        log.error(f"Stripe checkout creation failed for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Payment processing error")

    job.stripe_session_id = checkout_session.id
    await db.commit()
    log.info(f"Stripe checkout session created for job {job_id}")
    return {"checkout_url": checkout_session.url}


@router.post("/webhook/stripe")
async def jobs_stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Marks a job paid once Stripe confirms the checkout completed - same
    verify-then-dispatch pattern as routers/orders.py's webhook, with its
    own per-router signing secret (falls back to the shared one)."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET_JOBS") or os.getenv("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET_JOBS (or STRIPE_WEBHOOK_SECRET) not configured")
        return {"status": "success"}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        job_id = int(session["metadata"]["job_id"])
        job = await db.get(Job, job_id)
        if job:
            # Stripe redelivers webhook events - without this check a
            # redelivery would be harmless here (paid is already True) but
            # logging it as a fresh payment every time would be misleading.
            if job.paid:
                log.info(f"Job {job_id} already marked paid, ignoring duplicate webhook")
                return {"status": "success"}

            job.paid = True
            await db.commit()
            log.info(f"Payment received for job {job_id} via Stripe (${job.price:.2f})")

    return {"status": "success"}


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
    if not job.paid:
        raise HTTPException(status_code=400, detail="Job has not been paid for yet - cannot match a notary to unpaid work")

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


async def create_payout_for_completed_job(db: AsyncSession, job: Job) -> None:
    """Creates the NotaryPayout row a completed, paid job owes its
    assigned notary - NOTARY_PAYOUT_SHARE (80% by default) of what the
    client paid, snapshotted now so a later change to the split or to
    SERVICE_TIERS pricing never rewrites an already-earned payout. No-ops
    if the job was never actually paid or never assigned a worker - there's
    nothing to split in either case. Caller is responsible for committing;
    this only adds to the session."""
    if not job.worker_id or not job.paid or not job.price:
        return
    payout = NotaryPayout(job_id=job.id, worker_id=job.worker_id, amount=round(job.price * NOTARY_PAYOUT_SHARE, 2))
    db.add(payout)
    log.info(f"Created payout of ${payout.amount:.2f} for worker {job.worker_id} on job {job.id}")


@router.post("/{job_id}/complete", dependencies=[Depends(require_admin_key)])
async def complete_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "completed"
    job.completed_at = datetime.utcnow()
    await create_payout_for_completed_job(db, job)
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
