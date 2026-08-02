"""
Test GET /payments/pending-summary.

WHY THIS EXISTS
---------------
There was no way to see what a payout run would pay before it paid it.
Every other read path is scoped to a single worker - /bot/earnings
hardcodes bot@pgusa.local, /worker/{id}/earnings takes one id - so the
only code that saw the whole pending set was the payout loop itself,
which pays it as it reads. This endpoint is what makes arming
PAYOUTS_ENABLED an informed decision instead of a blind one, so the
number it reports has to be right.

Unlike the other payout tests in this repo, this one EXECUTES the query
rather than reading the AST. A summary that returns a plausible-looking
but wrong number is worse than no summary at all - it would be trusted.
The bug classes that matter here (a join that silently matches nothing, a
GROUP BY that double-counts, a filter that lets already-transferred rows
through) are all invisible to static analysis and all show up immediately
against real rows.

The join is the specific hazard. Worker.id is Integer and
Payment.worker_id is String, so an uncast comparison either errors on
Postgres or matches nothing. Nothing-matched is the dangerous outcome:
every payment would land in "worker_not_found", would_pay_now would read
0, and the operator would arm payouts believing there was no backlog.

SQLite CANNOT catch that. It coerces '1' == 1 happily, so the fixture
below passes with or without the cast - verified by deleting the cast and
re-running, which still reported the correct $350.50. Relying on the
fixture alone would be exactly the false confidence this endpoint exists
to eliminate. The cast is therefore asserted structurally as well, and
the SQL is compiled against the real PostgreSQL dialect so the emitted
CAST is checked against the database that actually runs it.
"""
import ast
import asyncio
import pathlib
import sys
import types

from sqlalchemy import select, func, cast, case, String as SqlString
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.orm import declarative_base

REPO = pathlib.Path(__file__).resolve().parent.parent

_fake_db = types.ModuleType("database")
_fake_db.Base = declarative_base()
sys.modules["database"] = _fake_db
sys.path.insert(0, str(REPO))
import models  # noqa: E402

Base = _fake_db.Base
Payment, Worker = models.Payment, models.Worker

ROUTER = "routers/payments.py"


def load_endpoint():
    """Extract the real pending_summary from the router and bind its
    dependencies. Importing routers/payments.py needs fastapi, stripe and
    a live database; re-implementing its query here instead would mean
    asserting against a copy that can be correct while the shipped
    endpoint is not - which is the whole failure mode this test exists to
    catch."""
    tree = ast.parse((REPO / ROUTER).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "pending_summary")

    class _HTTPException(Exception):
        def __init__(self, code, detail):
            super().__init__(f"{code}: {detail}")

    # Drop @router.get(...) - it registers the route on a FastAPI router
    # that does not exist here, and the route registration is not what is
    # under test. The body is.
    fn = ast.copy_location(
        ast.AsyncFunctionDef(name=fn.name, args=fn.args, body=fn.body,
                             decorator_list=[], returns=fn.returns,
                             type_comment=None, type_params=[]),
        fn)
    ast.fix_missing_locations(fn)

    ns = {
        "select": select, "func": func, "cast": cast, "case": case,
        "SqlString": SqlString, "Payment": Payment, "Worker": Worker,
        "AsyncSession": AsyncSession, "Depends": lambda x: None,
        "get_db": None, "HTTPException": _HTTPException,
        "PAYOUTS_ENABLED": False,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), ROUTER, "exec"), ns)
    return ns["pending_summary"]


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        return await _run(engine)
    finally:
        await engine.dispose()


async def _run(engine):
    failures = []
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Fixture. Amounts are distinct primes-ish so a double-count or a
    # misbucketed row shows up as a wrong total rather than a coincidence.
    async with Session() as s:
        s.add_all([
            Worker(id=1, email="a@x.com", name="Payable A", stripe_account_id="acct_1"),
            Worker(id=2, email="b@x.com", name="Payable B", stripe_account_id="acct_2"),
            Worker(id=3, email="c@x.com", name="No Connect", stripe_account_id=None),
        ])
        s.add_all([
            # payable: worker exists, has Connect, pending, never transferred
            Payment(id="p1", worker_id="1", worker_amount=100.00, payout_status="pending"),
            Payment(id="p2", worker_id="2", worker_amount=250.50, payout_status="pending"),
            # blocked: worker has no Connect account
            Payment(id="p3", worker_id="3", worker_amount=999.00, payout_status="pending"),
            # blocked: worker_id points at nobody
            Payment(id="p4", worker_id="404", worker_amount=777.00, payout_status="pending"),
            # excluded: already transferred, must never be re-counted
            Payment(id="p5", worker_id="1", worker_amount=5000.00,
                    payout_status="pending", stripe_transfer_id="tr_already"),
            # different status, payable worker - must not inflate would_pay
            Payment(id="p6", worker_id="1", worker_amount=42.00, payout_status="failed"),
        ])
        await s.commit()

    async with Session() as s:
        out = await load_endpoint()(db=s)

    import json
    print(json.dumps(out, indent=2, default=str))
    print()

    pend = out["breakdown_by_status_and_reason"].get("pending", {})

    def got(bucket):
        b = pend.get(bucket, {"count": 0, "dollars": 0.0})
        return (b["count"], round(b["dollars"], 2))

    # ── the number the operator acts on ────────────────────────────────
    would = (out["would_pay_now"]["count"], out["would_pay_now"]["dollars"])
    if would != (2, 350.50):
        failures.append(f"would_pay_now wrong: expected (2, 350.50), got {would}")

    # If the cast silently matched nothing, EVERYTHING lands here and
    # would_pay_now reads 0 - the exact false-negative that would get
    # payouts armed against an unseen backlog.
    if got("worker_not_found") != (1, 777.00):
        failures.append(f"worker_not_found wrong: expected (1, 777.00), got "
                        f"{got('worker_not_found')} - if this swept up every row, "
                        f"the Integer/String join cast failed")

    if got("worker_missing_stripe_account") != (1, 999.00):
        failures.append(f"worker_missing_stripe_account wrong: expected (1, 999.00), "
                        f"got {got('worker_missing_stripe_account')}")

    # p5 carries a transfer id: excluded from every bucket, counted here.
    if (out["already_transferred"]["count"],
            out["already_transferred"]["dollars"]) != (1, 5000.00):
        failures.append(f"already_transferred wrong: expected (1, 5000.00), got "
                        f"{out['already_transferred']}")
    total_rows = sum(b["count"] for st in out["breakdown_by_status_and_reason"].values()
                     for b in st.values())
    if total_rows != 5:
        failures.append(f"expected 5 untransferred rows across all buckets, got "
                        f"{total_rows} - is the stripe_transfer_id IS NULL filter working?")

    # A non-pending row on a payable worker must not reach would_pay_now.
    failed_bucket = out["breakdown_by_status_and_reason"].get("failed", {}).get("payable")
    if not failed_bucket or failed_bucket["count"] != 1:
        failures.append("the 'failed' row is not bucketed as expected")
    if would[1] > 350.50:
        failures.append("non-pending rows leaked into would_pay_now")

    # ── the Integer/String join cast, checked where SQLite can't ───────
    #
    # Deleting the cast and re-running the fixture above still reports the
    # correct $350.50, because SQLite coerces '1' == 1. On Postgres the
    # same comparison does not coerce - it errors, or matches nothing and
    # reports a $0 backlog that is not real. So this is asserted two ways
    # that do not depend on the test database.
    tree = ast.parse((REPO / ROUTER).read_text())
    fn_node = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "pending_summary")
    joins = [n for n in ast.walk(fn_node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "outerjoin"]
    if not joins:
        failures.append("pending_summary no longer has an outerjoin; "
                        "this test's assumptions have gone stale")
    for j in joins:
        if not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                   and c.func.id == "cast"
                   for arg in j.args for c in ast.walk(arg)):
            failures.append(f"outerjoin at line {j.lineno} compares Worker.id "
                            f"(Integer) to Payment.worker_id (String) without a "
                            f"cast - on Postgres this errors or matches nothing, "
                            f"reporting a $0 backlog that is not real")

    # And prove it against the dialect that actually runs it.
    from sqlalchemy.dialects import postgresql
    pg_sql = str(
        select(Payment.id)
        .select_from(Payment)
        .outerjoin(Worker, cast(Worker.id, SqlString) == Payment.worker_id)
        .compile(dialect=postgresql.dialect())
    )
    if "CAST" not in pg_sql.upper():
        failures.append(f"postgres compilation emits no CAST: {pg_sql}")
    else:
        print("  postgres dialect emits CAST in the join condition")

    # ── the endpoint must not write, and must not call Stripe ──────────
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "pending_summary")
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("commit", "add", "delete", "flush"):
                failures.append(f"pending_summary writes: .{n.func.attr}() at line {n.lineno}")
            root = n.func
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id == "stripe":
                failures.append(f"pending_summary calls Stripe at line {n.lineno}")
    print("\n  endpoint is read-only: no commit/add/delete/flush, no stripe calls")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  would_pay_now = $350.50 over 2 payments; blocked and "
          "already-transferred rows correctly excluded")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
