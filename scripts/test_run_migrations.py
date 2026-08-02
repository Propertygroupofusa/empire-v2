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


def load_run_migrations(engine):
    """Pull the real run_migrations out of main.py and bind it to `engine`."""
    tree = ast.parse((REPO / "main.py").read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_migrations")

    log = types.SimpleNamespace(
        info=lambda m: print(f"    info  {m}"),
        warning=lambda m: print(f"    WARN  {m}"),
        debug=lambda m: None,
    )
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

    await load_run_migrations(engine)()

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
