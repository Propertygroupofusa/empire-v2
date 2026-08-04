"""
Test that the payout loop can actually find a worker on Postgres.

WHY THIS EXISTS
---------------
Worker.id is Integer. Payment.worker_id is String. main.py's payout loop
compared them directly:

    select(Worker).where(Worker.id == payment.worker_id)

SQLAlchemy's Integer has no bind processor on the postgresql dialect, so
the Python str went straight through to asyncpg. Verified against a real
PostgreSQL 16 server, not inferred:

    asyncpg.exceptions.UndefinedFunctionError:
    operator does not exist: integer = character varying

That raised on EVERY payment, before Stripe was reached. With the failure
handler added in #126, each payment was then marked "failed" - so arming
PAYOUTS_ENABLED would have flipped the entire pending table to failed
rather than paying anyone, needing manual repair in the database.

routers/payments.py casts with int() at both of its call sites. Only the
background loop did not.

WHY SQLITE IS NOT ENOUGH
------------------------
SQLite coerces '7' == 7 and finds the worker happily, so the bug is
invisible there and a SQLite-only test would pass against the broken
code. Same trap as the pending-summary join. So:

  - the structural check runs everywhere, including CI
  - the live reproduction runs only when TEST_DATABASE_URL points at a
    real Postgres, and is skipped loudly otherwise rather than silently
    passing

Run the full version with:
    TEST_DATABASE_URL=postgresql+asyncpg://user@host:port/db \
        python scripts/test_payout_worker_lookup.py
"""
import ast
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def check_source(failures):
    """The loop must not hand a raw String into a comparison against
    Worker.id. Runs with no database at all, so CI covers it."""
    tree = ast.parse((REPO / "main.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "process_payouts_periodically")

    found = False
    for n in ast.walk(fn):
        # match: Worker.id == <something>
        if not (isinstance(n, ast.Compare)
                and isinstance(n.left, ast.Attribute) and n.left.attr == "id"
                and isinstance(n.left.value, ast.Name) and n.left.value.id == "Worker"):
            continue
        found = True
        rhs = n.comparators[0]
        raw_attr = (isinstance(rhs, ast.Attribute) and rhs.attr == "worker_id")
        if raw_attr:
            failures.append(
                f"main.py:{n.lineno}: Worker.id (Integer) compared directly to "
                f"payment.worker_id (String) - on Postgres this raises "
                f"'operator does not exist: integer = character varying' for "
                f"every payment")
        else:
            print(f"  main.py:{n.lineno}: Worker.id compared to a coerced value, "
                  f"not the raw String column")
    if not found:
        failures.append("no Worker.id comparison found in the payout loop; "
                        "this test's assumptions have gone stale")

    # the coercion must be guarded - a bare int() on junk data raises
    # ValueError and kills the cycle
    src = ast.get_source_segment((REPO / "main.py").read_text(), fn) or ""
    if "int(payment.worker_id)" in src and "ValueError" not in src:
        failures.append("worker_id is coerced with int() but ValueError is not "
                        "handled - one malformed row would kill the whole cycle")


async def check_live(url, failures):
    """Reproduce the original failure and prove the fix, on real Postgres."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.orm import declarative_base

    fake = types.ModuleType("database")
    fake.Base = declarative_base()
    sys.modules["database"] = fake
    import models
    Base, Worker, Payment = fake.Base, models.Worker, models.Payment

    engine = create_async_engine(url)
    try:
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.drop_all)
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(Worker(id=7, email="w@x.com", name="W", stripe_account_id="acct_7"))
            s.add(Payment(id="pay1", worker_id="7", worker_amount=10.0,
                          payout_status="pending"))
            await s.commit()

        raw = "7"  # exactly what the loop holds: a Python str

        # the OLD form must still be broken - if this stops raising, the
        # premise of the fix has changed and the test should say so
        async with Session() as s:
            try:
                await s.execute(select(Worker).where(Worker.id == raw))
                failures.append("the uncast comparison NO LONGER raises on this "
                                "Postgres; re-examine whether the fix is still needed")
            except Exception as e:
                first = str(e).splitlines()[0]
                if "operator does not exist" not in first:
                    failures.append(f"unexpected error from uncast form: {first}")
                else:
                    print(f"  reproduced on live Postgres: {first.split(': ', 2)[-1]}")

        # the NEW form must find the worker
        async with Session() as s:
            w = (await s.execute(
                select(Worker).where(Worker.id == int(raw)))).scalar_one_or_none()
            if w is None or w.stripe_account_id != "acct_7":
                failures.append(f"coerced lookup did not find the worker: {w}")
            else:
                print(f"  coerced lookup found worker id={w.id} "
                      f"stripe={w.stripe_account_id}")
    finally:
        await engine.dispose()


def main():
    failures = []
    print("Payout loop worker lookup\n")
    check_source(failures)

    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if url:
        print(f"\n  live check against {url.split('@')[-1]}")
        asyncio.run(check_live(url, failures))
    else:
        print("\n  SKIPPED live Postgres check (TEST_DATABASE_URL not set).")
        print("  SQLite cannot substitute here - it coerces '7' == 7, so it")
        print("  finds the worker even against the broken code and would")
        print("  report a false pass.")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  worker lookup is Postgres-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
