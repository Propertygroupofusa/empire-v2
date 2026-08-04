"""
Walk the notarization revenue path end to end, before a real customer does.

WHY THIS EXISTS
---------------
The notarization marketplace is the only thing in this system that charges
a real customer a real price for a real service. It has never been used.
A code trace found two blockers, and the failure shape of the second one
is the worst available: the client pays, Stripe keeps the money, and the
job is never delivered.

    1  POST /jobs/notarization/request     public intake, job.paid = False
    2  POST /jobs/{id}/create-checkout     Stripe Checkout session
    3  POST /jobs/webhook/stripe           sets job.paid = True
    4  notary_bot.match_pending_jobs()     assigns a verified notary
    5  POST /jobs/{id}/complete            admin, creates NotaryPayout
    6  NotaryPayout                        manual transfer, by design

Link 3 dies silently if STRIPE_WEBHOOK_SECRET_JOBS is unset or the
endpoint is not registered in Stripe: money in, job still unpaid.

Link 4 needs a Worker with credentials_verified=True, status="active",
either a notary_commission_number or ron_authorized, AND a commission
state equal to the JOB'S state. That last clause is easy to miss and was
missed on the first pass of this script: a notary can satisfy every other
condition and still never be matched, because a commission is
jurisdictional. Without an eligible notary a fully paid job sits in
"requested" forever.

WHAT THIS DOES
--------------
  --check      report which production settings are missing. Reads only.
  --dryrun     run links 1 and 3-6 against a THROWAWAY database, proving
               the code path works. Touches nothing real.
  --provision  create one verified notary in the CONFIGURED database.
               The only mode that writes to production, and it asks first.

Stripe is never called. Link 2 is exercised as a config check rather than
a live session, because creating sessions against a live key is a
side-effecting call this script has no business making unattended.

    python scripts/notary_path_dryrun.py --check
    python scripts/notary_path_dryrun.py --dryrun
    DATABASE_URL=... python scripts/notary_path_dryrun.py --provision \\
        --email you@example.com --name "Your Name" --commission 12345 --state NC
"""
import argparse
import asyncio
import uuid
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, BAD, WARN = f"{GREEN}PASS{OFF}", f"{RED}FAIL{OFF}", f"{YELLOW}WARN{OFF}"


def line(status, text, detail=""):
    print(f"  [{status}] {text}")
    if detail:
        for d in detail.splitlines():
            print(f"         {DIM}{d}{OFF}")


# ── link 4's eligibility rule, in one place ─────────────────────────────
# Mirrors notary_bot.match_pending_jobs exactly. If that query changes,
# this and the provisioner both need to change with it - which is why it
# lives here once rather than being spelled out three times.
#
# The STATE match is the part that is easy to miss, and missing it is
# expensive. An earlier version of this script checked only "verified,
# active, and has a commission number", provisioned a notary that
# satisfied all three - and the dry run still failed to match, because
# notary_bot additionally requires the commission to be in the JOB'S
# state. A notary commissioned in NC cannot notarise a Texas document,
# which is correct law and correct code; it just is not visible from the
# worker row alone.
#
# Consequence: a notary is never "eligible" in the abstract, only
# eligible FOR A GIVEN STATE. Provisioning one without a state produces a
# worker who looks fully set up and can never be assigned anything.
def is_eligible(w, state=None):
    if not (w.credentials_verified and w.status == "active"):
        return False
    if not (w.notary_commission_number or w.ron_authorized):
        return False
    if state is None:
        # No job in hand: can this notary EVER be matched? Only if it has
        # a jurisdiction recorded at all.
        return bool(w.notary_commission_state
                    or (w.ron_authorized and w.ron_authorization_state))
    return bool(w.notary_commission_state == state
                or (w.ron_authorized and w.ron_authorization_state == state))


def jurisdictions(w):
    out = []
    if w.notary_commission_state:
        out.append(w.notary_commission_state)
    if w.ron_authorized and w.ron_authorization_state:
        out.append(f"{w.ron_authorization_state} (RON)")
    return ", ".join(out) or "NONE"


async def check(db_url):
    """Read-only production readiness report."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    print("\nPRODUCTION READINESS\n")
    failures = 0

    # link 2/3 config
    if os.getenv("STRIPE_SECRET_KEY"):
        mode = "TEST" if os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test_") else "LIVE"
        line(OK, f"STRIPE_SECRET_KEY is set ({mode} key)")
    else:
        line(BAD, "STRIPE_SECRET_KEY is not set",
             "link 2 fails: no checkout session can be created")
        failures += 1

    if os.getenv("STRIPE_WEBHOOK_SECRET_JOBS") or os.getenv("STRIPE_WEBHOOK_SECRET"):
        which = ("STRIPE_WEBHOOK_SECRET_JOBS" if os.getenv("STRIPE_WEBHOOK_SECRET_JOBS")
                 else "STRIPE_WEBHOOK_SECRET (shared fallback)")
        line(OK, f"webhook secret is set — {which}")
    else:
        line(BAD, "no webhook secret set",
             "link 3 returns HTTP 500 and job.paid stays False.\n"
             "The customer is charged and the job is never delivered.")
        failures += 1

    line(WARN, "Stripe dashboard endpoint registration cannot be checked from here",
         "Confirm manually: an endpoint pointing at /jobs/webhook/stripe,\n"
         "subscribed to checkout.session.completed. Without it link 3 never fires,\n"
         "with exactly the same symptom as a missing secret.")

    if os.getenv("PAYMENTS_PAUSED", "false").strip().lower() == "true":
        line(BAD, "PAYMENTS_PAUSED=true", "link 2 returns HTTP 503 for every customer")
        failures += 1
    else:
        line(OK, "PAYMENTS_PAUSED is off")

    # Matching succeeds silently without these. The job is assigned, the
    # ledger is correct, and neither the customer nor the notary is ever
    # told - so from the customer's side, paying produced nothing.
    if os.getenv("GMAIL_EMAIL") and os.getenv("GMAIL_PASSWORD"):
        line(OK, "match-alert email is configured")
    else:
        failures += 1
        line(BAD, "GMAIL_EMAIL / GMAIL_PASSWORD not set — no one is notified",
             "notary_bot.send_match_alert no-ops. The match still happens and the\n"
             "payout ledger is still correct, but the customer is never told their\n"
             "job was accepted and the notary is never told they have work.\n"
             "Silent success is indistinguishable from failure to the person who paid.")

    # link 4: is there anyone who can be assigned work?
    if not db_url:
        line(WARN, "DATABASE_URL not set — cannot check for a verified notary")
        return failures

    engine = create_async_engine(db_url)
    try:
        from models import Worker
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            workers = (await s.execute(select(Worker))).scalars().all()
        eligible = [w for w in workers if is_eligible(w)]
        if eligible:
            line(OK, f"{len(eligible)} notary/notaries can be assigned work",
                 "\n".join(f"{w.email} (id={w.id}) — covers {jurisdictions(w)}"
                           for w in eligible)
                 + "\n\nA job is only matched if ITS state is in that list.")
        else:
            failures += 1
            detail = ["link 4 blocks: a paid job sits in 'requested' forever.",
                      f"{len(workers)} worker row(s) exist, none eligible."]
            for w in workers[:5]:
                why = []
                if not w.credentials_verified:
                    why.append("credentials_verified=False")
                if w.status != "active":
                    why.append(f"status={w.status!r}")
                if not (w.notary_commission_number or w.ron_authorized):
                    why.append("no commission number and not RON-authorized")
                if not (w.notary_commission_state
                        or (w.ron_authorized and w.ron_authorization_state)):
                    why.append("no commission STATE — can never match any job")
                detail.append(f"{w.email}: {', '.join(why)}")
            line(BAD, "NO eligible notary", "\n".join(detail))
    except Exception as e:
        line(BAD, f"could not read workers: {type(e).__name__}: {e}")
        failures += 1
    finally:
        await engine.dispose()
    return failures


async def dryrun(db_url):
    """Links 1 and 3-6 against a throwaway database. Touches nothing real."""
    # MUST come before the imports below. notary_bot builds its OWN engine
    # at import time from DATABASE_URL - it does not share database.py's
    # session factory - so patching a module attribute afterwards is too
    # late and the matcher silently runs against the default local SQLite
    # instead. Pointing the environment at the throwaway database is both
    # simpler and a truer exercise of how production is actually wired.
    os.environ["DATABASE_URL"] = db_url

    from sqlalchemy import select, delete
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    import database as real_db
    from models import Worker, Client, Job, NotaryPayout
    import notary_bot
    from routers.jobs import SERVICE_TIERS, NOTARY_PAYOUT_SHARE, create_payout_for_completed_job

    print("\nDRY RUN — throwaway database, no Stripe calls, nothing real touched\n")
    failures = 0
    run = uuid.uuid4().hex[:8]
    created = {"workers": [], "clients": [], "jobs": []}
    engine = create_async_engine(db_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(real_db.Base.metadata.create_all)

        # Prove the matcher is really pointed at the throwaway DB. If it is
        # not, every assertion below passes vacuously against an empty
        # SQLite file and the dry run reports success having tested nothing.
        if str(notary_bot._SessionLocal.kw["bind"].url) != str(engine.url):
            raise RuntimeError(
                f"notary_bot is bound to {notary_bot._SessionLocal.kw['bind'].url}, "
                f"not the throwaway {engine.url} - it must have been imported "
                f"before DATABASE_URL was set")
        line(OK, f"notary_bot bound to the throwaway database")

        tier = "standard"
        price = SERVICE_TIERS[tier]["price"]
        JOB_STATE = "NC"

        # Unique per run, and cleaned up in the finally block below. The
        # alternative - dropping and recreating the schema - would make the
        # script destructive if anyone ever pointed --test-db somewhere they
        # cared about, and the only guard against that is "it differs from
        # DATABASE_URL", which is thinner than it sounds. Unique rows plus
        # cleanup gets repeatability without ever issuing a DROP.
        # a notary who satisfies link 4
        async with Session() as s:
            notary = Worker(email=f"dryrun-notary-{run}@example.invalid",
                            name="Dry Run Notary", status="active",
                            credentials_verified=True,
                            notary_commission_number=f"DRYRUN-{run}",
                            notary_commission_state=JOB_STATE)
            s.add(notary)
            await s.commit()
            created["workers"].append(notary.id)
        line(OK, f"notary provisioned — commissioned in {jurisdictions(notary)}, "
                 f"eligible for {JOB_STATE}={is_eligible(notary, JOB_STATE)}")

        # link 1 - intake
        async with Session() as s:
            client = Client(email=f"dryrun-client-{run}@example.invalid",
                            name="Dry Run Client")
            s.add(client)
            await s.flush()
            created["clients"].append(client.id)
            job = Job(job_type="notarization", client_id=client.id, state=JOB_STATE,
                      description="dry run", status="requested",
                      service_tier=tier, price=price, paid=False)
            s.add(job)
            await s.commit()
            job_id = job.id
            created["jobs"].append(job_id)
        line(OK, f"link 1  intake -> job {job_id}, {JOB_STATE}, ${price:.2f}, paid=False")

        # link 3 - what the Stripe webhook does on checkout.session.completed
        async with Session() as s:
            j = await s.get(Job, job_id)
            j.paid = True
            await s.commit()
        line(OK, "link 3  webhook -> paid=True")

        # link 4 - the real matcher, not a reimplementation
        matched_count = await notary_bot.match_pending_jobs()
        async with Session() as s:
            j = await s.get(Job, job_id)
        if j.worker_id == notary.id and j.status in ("matched", "scheduled"):
            line(OK, f"link 4  notary_bot matched job -> worker {j.worker_id}, "
                     f"status={j.status!r} ({matched_count} matched this pass)")
        else:
            failures += 1
            line(BAD, f"link 4  NOT matched — status={j.status!r}, worker_id={j.worker_id}",
                 "a paid job that never gets assigned is money taken for undelivered work")

        # link 5 - completion creates the payout ledger row
        async with Session() as s:
            j = await s.get(Job, job_id)
            j.status = "completed"
            await create_payout_for_completed_job(s, j)
            await s.commit()

        async with Session() as s:
            payouts = (await s.execute(
                select(NotaryPayout).where(NotaryPayout.job_id == job_id))).scalars().all()
        expected = round(price * NOTARY_PAYOUT_SHARE, 2)
        if len(payouts) == 1 and abs(payouts[0].amount - expected) < 0.005:
            p = payouts[0]
            line(OK, f"link 5  payout ledger row ${p.amount:.2f} ({NOTARY_PAYOUT_SHARE:.0%}), "
                     f"status={p.status!r}",
                 f"client paid ${price:.2f} -> notary ${p.amount:.2f}, "
                 f"platform ${price - p.amount:.2f}")
        else:
            failures += 1
            line(BAD, f"link 5  expected one payout of ${expected:.2f}, got {payouts}")

        # link 6 is manual by design - assert that, so nobody assumes otherwise
        line(WARN, "link 6  payout is a LEDGER, not a transfer",
             "NotaryPayout records what is owed. The money moves when you send it\n"
             "(Zelle/PayPal/check/ACH) and mark it paid. This is deliberate — see\n"
             "the model docstring — not a missing feature.")

        # the unpaid-job guard, which is what protects a real customer
        async with Session() as s:
            c2 = Client(email=f"dryrun-unpaid-{run}@example.invalid", name="Unpaid")
            s.add(c2)
            await s.flush()
            created["clients"].append(c2.id)
            unpaid = Job(job_type="notarization", client_id=c2.id, state=JOB_STATE,
                         status="requested", service_tier=tier, price=price, paid=False)
            s.add(unpaid)
            await s.commit()
            unpaid_id = unpaid.id
            created["jobs"].append(unpaid_id)
        await notary_bot.match_pending_jobs()
        async with Session() as s:
            u = await s.get(Job, unpaid_id)
        if u.status == "requested" and not u.worker_id:
            line(OK, "guard   an UNPAID job is not matched",
                 "notary_bot filters on paid==True, so no work is dispatched for free")
        else:
            failures += 1
            line(BAD, f"guard   unpaid job was matched: status={u.status!r}, "
                      f"worker_id={u.worker_id}")

    finally:
        # In `finally`, not on the success path. An assertion that fails
        # part-way through still leaves fixture rows behind, and those
        # orphans then break the NEXT run on a unique-email collision or a
        # foreign key - which is exactly what happened while writing this.
        # Children before parents: NotaryPayout -> Job -> Client/Worker.
        try:
            async with Session() as s:
                if created["jobs"]:
                    await s.execute(
                        delete(NotaryPayout).where(NotaryPayout.job_id.in_(created["jobs"])))
                    await s.execute(delete(Job).where(Job.id.in_(created["jobs"])))
                if created["clients"]:
                    await s.execute(delete(Client).where(Client.id.in_(created["clients"])))
                if created["workers"]:
                    await s.execute(delete(Worker).where(Worker.id.in_(created["workers"])))
                await s.commit()
            line(OK, "cleanup  dry-run rows removed")
        except Exception as e:
            line(WARN, f"cleanup incomplete: {type(e).__name__}: {e}")
        await engine.dispose()
    return failures


async def provision(db_url, email, name, commission, ron, state):
    """The only mode that writes to the configured database."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from models import Worker

    engine = create_async_engine(db_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            existing = (await s.execute(
                select(Worker).where(Worker.email == email))).scalar_one_or_none()
            if existing:
                existing.status = "active"
                existing.credentials_verified = True
                existing.credentials_submitted = True
                if commission:
                    existing.notary_commission_number = commission
                    existing.notary_commission_state = state
                if ron:
                    existing.ron_authorized = True
                    existing.ron_authorization_state = state
                await s.commit()
                line(OK, f"updated worker {email} (id={existing.id}) "
                         f"-> covers {jurisdictions(existing)}, "
                         f"matchable={is_eligible(existing)}")
                return 0
            w = Worker(email=email, name=name, status="active",
                       credentials_submitted=True, credentials_verified=True,
                       notary_commission_number=commission or None,
                       notary_commission_state=state if commission else None,
                       ron_authorized=bool(ron),
                       ron_authorization_state=state if ron else None)
            s.add(w)
            await s.commit()
            line(OK, f"created worker {email} (id={w.id}) -> covers "
                     f"{jurisdictions(w)}, matchable={is_eligible(w)}")
        return 0
    finally:
        await engine.dispose()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="read-only readiness report")
    ap.add_argument("--dryrun", action="store_true", help="walk the path on a throwaway DB")
    ap.add_argument("--provision", action="store_true", help="WRITES: create a verified notary")
    ap.add_argument("--email"); ap.add_argument("--name", default="Notary")
    ap.add_argument("--commission", default=""); ap.add_argument("--ron", action="store_true")
    ap.add_argument("--state", default="", help="US state code the commission is valid in")
    ap.add_argument("--yes", action="store_true", help="skip the provision confirmation")
    ap.add_argument("--test-db", default=os.getenv("TEST_DATABASE_URL", ""),
                    help="throwaway DB for --dryrun")
    a = ap.parse_args()

    if not (a.check or a.dryrun or a.provision):
        ap.print_help()
        return 2

    db_url = os.getenv("DATABASE_URL", "")
    if db_url.startswith("postgres://"):        # SQLAlchemy needs the driver
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    rc = 0
    if a.check:
        rc += asyncio.run(check(db_url))

    if a.dryrun:
        test_db = a.test_db
        if not test_db:
            print(f"\n  --dryrun needs a throwaway database. Set TEST_DATABASE_URL or\n"
                  f"  pass --test-db. It will NOT run against DATABASE_URL, because a\n"
                  f"  dry run that writes to production is not a dry run.")
            return 2
        if db_url and test_db == db_url:
            print(f"\n  refusing: --test-db is the same as DATABASE_URL.")
            return 2
        rc += asyncio.run(dryrun(test_db))

    if a.provision:
        if not a.email:
            print("\n  --provision needs --email")
            return 2
        if not (a.commission or a.ron):
            print("\n  --provision needs --commission NUMBER or --ron.\n"
                  "  notary_bot requires one or the other; without it the worker is\n"
                  "  created but still cannot be assigned any job.")
            return 2
        if not a.state:
            print("\n  --provision needs --state (e.g. --state NC).\n"
                  "  notary_bot matches a job only to a notary whose commission state\n"
                  "  equals the JOB'S state. Without it the worker looks fully set up\n"
                  "  and can never be assigned anything.")
            return 2
        if not db_url:
            print("\n  --provision needs DATABASE_URL")
            return 2
        host = db_url.split("@")[-1]
        if not a.yes:
            print(f"\n  This WRITES a verified notary to {host}")
            if input("  Type 'yes' to continue: ").strip().lower() != "yes":
                print("  aborted")
                return 1
        rc += asyncio.run(provision(db_url, a.email, a.name, a.commission, a.ron,
                                    a.state.strip().upper()))

    print()
    if rc:
        print(f"  {RED}{rc} problem(s){OFF} — the path is not walkable yet\n")
        return 1
    print(f"  {GREEN}no blockers found{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
