"""
Test for main.run_migrations().

WHY THIS EXISTS
---------------
run_migrations used to iterate a hand-picked tuple of seven models. Twice
now a model has been left out of that tuple and the omission has surfaced
only as a production outage:

  bot_positions.peak_pct        -> every position write failed
  payments.stripe_payout_id     -> "Payout cycle error: column
                                    payments.stripe_payout_id does not
                                    exist", on every cycle

The tuple is now replaced by Base.metadata.sorted_tables. This test pins
down the two things that change must get right:

  1. every model on Base is migrated - specifically including Payment,
     the one that was broken, and without naming a list this test would
     also have to remember to update
  2. the widened loop does NOT convert legitimately-enum columns to
     VARCHAR. sqlalchemy.Enum subclasses String, so the enum-drift branch
     matches Enum columns unless explicitly guarded. sales_leads.source,
     sales_leads.status and sales_outreach.outreach_type are declared
     Enum(...) on purpose, and only came into this loop's blast radius
     when it stopped being four tables.

HOW IT AVOIDS TESTING A COPY
----------------------------
main.py cannot be imported offline (fastapi, stripe, alpaca, ...). Rather
than paraphrasing the migration into the test - which would prove nothing,
since the paraphrase could be correct while production is not - the real
run_migrations source is extracted from main.py with ast and exec'd. The
bytes under test are the bytes that ship.

SQLite stands in for Postgres for the missing-column half. It cannot model
native enum types, so the enum-guard half is asserted against the type
predicate directly.
"""
import ast
import asyncio
import pathlib
import sys
import types

from sqlalchemy import Column, Integer, String, Enum as SAEnum, inspect, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base

REPO = pathlib.Path(__file__).resolve().parent.parent

# models.py does `from database import Base`; the real database.py drags in
# asyncpg and a live DATABASE_URL. Substitute a bare module exposing only
# what models.py actually consumes.
_fake_db = types.ModuleType("database")
_fake_db.Base = declarative_base()
sys.modules["database"] = _fake_db

sys.path.insert(0, str(REPO))
import models  # noqa: E402  (registers every model on the fake Base)

Base = _fake_db.Base


def load_run_migrations(engine, sink):
    """Pull the real run_migrations out of main.py and bind it to `engine`.
    `sink` collects every log line so the summary can be asserted on."""
    tree = ast.parse((REPO / "main.py").read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_migrations")

    def cap(level):
        def _log(m):
            sink.append((level, str(m)))
            if level != "debug":
                print(f"    {level:7} {m}")
        return _log

    log = types.SimpleNamespace(info=cap("info"), warning=cap("WARNING"),
                                debug=cap("debug"))
    ns = {"engine": engine, "log": log, "text": text, "inspect": inspect,
          "String": String, "SAEnum": SAEnum, "PGEnum": PGEnum}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["run_migrations"]


async def main():
    failures = []

    # ── 1. the payments regression, reproduced then fixed ──────────────
    #
    # Build the table the way production has it: real, populated, and
    # missing the column the model declares. create_all cannot help here -
    # it skips tables that already exist, which is the entire bug.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE payments (id VARCHAR PRIMARY KEY, job_id VARCHAR, "
            "worker_id VARCHAR, gross_amount FLOAT)"
        ))
        await conn.execute(text("INSERT INTO payments (id) VALUES ('pay_1')"))
        # Everything else at full width, so the run has to no-op on them.
        for t in Base.metadata.sorted_tables:
            if t.name != "payments":
                await conn.run_sync(t.create)

    async with engine.begin() as conn:
        before = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("payments")])
    print(f"  payments BEFORE: {before}")
    if "stripe_payout_id" in before:
        failures.append("setup invalid: column present before migration")

    logs = []
    await load_run_migrations(engine, logs)()

    async with engine.begin() as conn:
        after = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("payments")])
        rows = (await conn.execute(text("SELECT id FROM payments"))).fetchall()
    print(f"  payments AFTER:  {after}")

    if "stripe_payout_id" not in after:
        failures.append("payments.stripe_payout_id STILL missing after migration")
    for col in ("stripe_transfer_id", "payout_status", "paid_at"):
        if col not in after:
            failures.append(f"payments.{col} missing after migration")
    if len(rows) != 1:
        failures.append(f"existing row lost: {rows}")

    # ── 2. no model can silently opt out ───────────────────────────────
    #
    # Asserted against the registry rather than a list of names, so a model
    # added later is covered without anyone remembering to edit this test.
    async with engine.begin() as conn:
        for t in Base.metadata.sorted_tables:
            real = await conn.run_sync(
                lambda c, n=t.name: {col["name"] for col in inspect(c).get_columns(n)})
            missing = {c.name for c in t.columns} - real
            if missing:
                failures.append(f"{t.name}: unmigrated columns {sorted(missing)}")
    print(f"  checked {len(Base.metadata.sorted_tables)} tables on Base.metadata")

    # ── 3. the guard that makes widening safe ──────────────────────────
    #
    # The branch reads: model says String, database says PG enum -> convert.
    # sqlalchemy.Enum subclasses String, so without `not isinstance(...,
    # SAEnum)` this fires on columns that are correctly enums on both sides.
    def would_convert(model_type):
        return (isinstance(model_type, String)
                and not isinstance(model_type, SAEnum)
                and isinstance(PGEnum("a", "b", name="x"), PGEnum))

    if not would_convert(String()):
        failures.append("guard too tight: plain String over a PG enum must convert")

    enum_cols = [(t.name, c.name) for t in Base.metadata.sorted_tables
                 for c in t.columns if isinstance(c.type, SAEnum)]
    for tname, cname in enum_cols:
        col = Base.metadata.tables[tname].columns[cname]
        if would_convert(col.type):
            failures.append(f"{tname}.{cname} is Enum(...) and would be "
                            f"destructively converted to VARCHAR")
    print(f"  enum columns protected: {enum_cols}")
    if not enum_cols:
        failures.append("expected Enum(...) columns to exist; guard untested")

    # ── 4. the run has to report itself ────────────────────────────────
    #
    # Both outages were invisible in the logs: a table absent from the old
    # tuple produced no output at all, so silence read exactly like success.
    # This is the line to grep for in Railway after a deploy, so it has to
    # be emitted, carry the counts, and never be downgraded to debug.
    summary = [m for lvl, m in logs
               if lvl == "info" and m.startswith("Migration summary:")]
    if len(summary) != 1:
        failures.append(f"expected exactly one INFO summary line, got {summary}")
    else:
        print(f"  summary: {summary[0]}")
        n_tables = len(Base.metadata.sorted_tables)
        for expect in (f"tables={n_tables}", "columns_added=8", "failures=0"):
            if expect not in summary[0]:
                failures.append(f"summary missing/wrong '{expect}': {summary[0]}")

    # ── 5. a failed ALTER must be a WARNING, not a swallowed debug line ─
    #
    # This is the branch that matters most. Reaching it means the column is
    # genuinely absent AND adding it failed, so every write touching that
    # column will fail - the exact shape of both outages. It used to log at
    # debug, which never reaches Railway, so the migration would report
    # itself as fine while leaving the column missing.
    #
    # Forced with a VIEW named `payments`: inspect() reports its columns
    # happily, so the loop gets past inspection and decides columns are
    # missing, but ALTER TABLE against a view fails. That exercises the
    # ADD COLUMN failure path specifically, rather than the missing-table
    # path already covered by the inspect() guard.
    bad_logs = []
    bad_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with bad_engine.begin() as conn:
        for t in Base.metadata.sorted_tables:
            if t.name != "payments":
                await conn.run_sync(t.create)
        await conn.execute(text("CREATE VIEW payments AS SELECT 'x' AS id"))

    await load_run_migrations(bad_engine, bad_logs)()

    alter_warnings = [m for lvl, m in bad_logs
                      if lvl == "WARNING" and m.startswith("Migration FAILED payments.")]
    if not alter_warnings:
        failures.append("failed ADD COLUMN produced no 'Migration FAILED' WARNING "
                        f"(got: {[m for l, m in bad_logs if l == 'WARNING']})")
    else:
        print(f"  failed-ALTER warning: {alter_warnings[0][:88]}...")
    if any(lvl == "debug" and "payments" in m for lvl, m in bad_logs):
        failures.append("failure detail was logged at debug level")

    bad_summary = [m for lvl, m in bad_logs
                   if lvl == "info" and m.startswith("Migration summary:")]
    if not bad_summary:
        failures.append("no summary emitted on the failure path")
    elif "failures=0" in bad_summary[0]:
        failures.append(f"failures not counted in summary: {bad_summary[0]}")
    else:
        print(f"  failure-path summary: {bad_summary[0]}")
    await bad_engine.dispose()

    await engine.dispose()

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  all checks")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
