"""
NOTARY MATCHING BOT
=====================================
Runs alongside the FastAPI app as a background thread. Polls for
"requested" notarization jobs (routers/jobs.py) and auto-matches each one
to an eligible, verified notary worker in the same state - the automated
slice of "notary, tax prep, and legal document services" this platform
advertises but had no working match flow for until now.

SAFETY:
- Only touches Job/Worker/Client rows already created through the app's
  own routers - this bot never creates client-facing data itself, only
  matches existing requests to existing verified eligible workers.
- Uses its own database engine/session, deliberately separate from the
  FastAPI app's shared database.engine: asyncpg connections are bound to
  the event loop that created them, and this bot runs its own event loop
  in its own thread - sharing a connection pool across threads/loops
  causes "attached to a different loop" errors.
"""
import os
import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models import Job, Worker, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("notary_bot")

POLL_INTERVAL_SECONDS = int(os.getenv("NOTARY_BOT_POLL_INTERVAL", "60"))

_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./empire.db")
if _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# Own engine/session - see module docstring for why this isn't shared
# with database.py's instance.
_engine = create_async_engine(_DATABASE_URL, echo=False, pool_pre_ping=True)
_SessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def send_match_alert(subject: str, body: str, to_email: str):
    """Reuses the same GMAIL_EMAIL/GMAIL_PASSWORD SMTP pattern prop_bot.py
    and routers/orders.py already use - no new credentials to configure.
    No-ops quietly (just a log line) if creds or a recipient are missing."""
    sender_email = os.getenv("GMAIL_EMAIL", "")
    sender_password = os.getenv("GMAIL_PASSWORD", "")
    if not sender_email or not sender_password or not to_email:
        log.info(f"(match alert email skipped - creds or recipient missing) {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = to_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())
        log.info(f"📧 Match alert emailed to {to_email}")
    except Exception as e:
        log.warning(f"Match alert email failed: {e}")


async def match_pending_jobs():
    """One matching pass: for every requested notarization job, find an
    eligible verified notary (active, credentials verified, and either
    commissioned or RON-authorized in the job's state) and assign
    whichever eligible notary currently has the fewest active (matched or
    scheduled, not yet completed) jobs - so work spreads evenly across
    eligible notaries instead of always piling onto the same one.
    Returns the number of jobs matched this pass."""
    matched_count = 0
    async with _SessionLocal() as db:
        result = await db.execute(
            select(Job).where(Job.job_type == "notarization", Job.status == "requested")
        )
        pending_jobs = result.scalars().all()
        if not pending_jobs:
            return 0

        result = await db.execute(
            select(Worker).where(Worker.credentials_verified == True, Worker.status == "active")  # noqa: E712
        )
        verified_notaries = [w for w in result.scalars().all() if w.notary_commission_number or w.ron_authorized]

        # Current workload per notary, so the very first assignment this
        # pass already accounts for jobs matched in earlier passes.
        active_counts_result = await db.execute(
            select(Job.worker_id, func.count(Job.id))
            .where(Job.status.in_(["matched", "scheduled"]))
            .group_by(Job.worker_id)
        )
        active_counts = {worker_id: count for worker_id, count in active_counts_result.all()}

        for job in pending_jobs:
            eligible = [
                w for w in verified_notaries
                if w.notary_commission_state == job.state or (w.ron_authorized and w.ron_authorization_state == job.state)
            ]
            if not eligible:
                continue

            # Least-loaded eligible notary first. Ties broken by id for
            # deterministic, testable behavior.
            eligible.sort(key=lambda w: (active_counts.get(w.id, 0), w.id))
            notary = eligible[0]
            job.worker_id = notary.id
            job.status = "matched"
            job.matched_at = datetime.utcnow()
            matched_count += 1
            # Reflected immediately so a second job this same pass doesn't
            # pile onto the notary just assigned above.
            active_counts[notary.id] = active_counts.get(notary.id, 0) + 1
            log.info(f"✅ Matched job {job.id} ({job.state}) to notary {notary.email} (id={notary.id}, now {active_counts[notary.id]} active)")

            client = await db.get(Client, job.client_id)
            if client:
                send_match_alert(
                    "Your notarization request has been matched",
                    f"Hi {client.name},\n\nYour notarization request (job #{job.id}) has been matched with a "
                    f"notary licensed in {job.state}. You'll be contacted shortly to schedule your appointment.\n\n"
                    f"- Property Group USA Documents Platform",
                    client.email,
                )
            send_match_alert(
                "New notarization job matched to you",
                f"Hi {notary.name},\n\nYou've been matched to a new notarization job (#{job.id}) in {job.state}. "
                f"Log in to your worker dashboard to review and schedule it.\n\n"
                f"- Property Group USA Documents Platform",
                notary.email,
            )

        if matched_count:
            await db.commit()

    return matched_count


async def _run_loop():
    log.info(f"🖋️ Notary matching bot started - polling every {POLL_INTERVAL_SECONDS}s")
    while True:
        try:
            matched = await match_pending_jobs()
            if matched:
                log.info(f"🖋️ Matched {matched} notarization job(s) this cycle")
        except Exception as e:
            log.error(f"Notary matching cycle failed: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def run():
    asyncio.run(_run_loop())


if __name__ == "__main__":
    run()
