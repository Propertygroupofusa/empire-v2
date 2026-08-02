"""
Test that Integer primary keys without a default get repaired.

WHY THIS EXISTS
---------------
Fixing the passlib import (#129) let initialize_bot and bot_autoscaler
actually run for the first time. They immediately hit the next problem:

  ERROR:bot_autoscaler:Auto-scaler error:
  (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError)
  <class 'asyncpg.exceptions.NotNullViolationError'>: null value in
  column "id" of relation "workers" violates not-null constraint

The model declares Worker.id as an Integer primary key, so SQLAlchemy
omits it from the INSERT and expects the database to generate it (hence
RETURNING workers.id). But the real workers table was created outside the
ORM as a plain INTEGER PRIMARY KEY - no SERIAL, no IDENTITY, no DEFAULT.
Nothing generates a value, and the NOT NULL implied by PRIMARY KEY
rejects the row. Every bot-worker insert failed this way.

This is the same drift class run_migrations already exists for - the real
table disagreeing with the model - so it is repaired the same way:
generically, across every table, rather than special-casing the one that
happened to surface it. clients, jobs and bookings all have Integer
primary keys and could have been created identically.

WHY SQLITE CANNOT TEST THIS
---------------------------
SQLite treats INTEGER PRIMARY KEY as an alias for rowid and assigns a
value automatically, so the broken schema inserts fine there. A SQLite
test would pass against the bug. That is the third time in this codebase
SQLite has been unable to see a Postgres-only defect, so this test
requires a real Postgres and SKIPS LOUDLY without one rather than
reporting a false pass.

    TEST_DATABASE_URL=postgresql+asyncpg://user@host:port/db \
        python scripts/test_pk_autoincrement.py
"""
import ast
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The workers table exactly as production has it: a plain INTEGER PRIMARY
# KEY with no sequence behind it.
BROKEN_WORKERS = """
CREATE TABLE workers (
  id INTEGER PRIMARY KEY,
  email VARCHAR UNIQUE, name VARCHAR, phone VARCHAR, status VARCHAR,
  created_at TIMESTAMP, updated_at TIMESTAMP, password_hash VARCHAR,
  w9_submitted BOOLEAN, w9_legal_name VARCHAR, w9_tax_classification VARCHAR,
  w9_tin_last4 VARCHAR, w9_address TEXT, credentials_submitted BOOLEAN,
  credentials_verified BOOLEAN, notary_commission_number VARCHAR,
  notary_commission_state VARCHAR, notary_commission_expires VARCHAR,
  ron_authorized BOOLEAN, ron_authorization_state VARCHAR,
  stripe_account_id VARCHAR, custom_metadata JSON
)
"""


def load_run_migrations(engine, sink):
    """Run the shipped run_migrations, not a copy of it."""
    from sqlalchemy import text, inspect, String, Integer, Enum as SAEnum
    from sqlalchemy.dialects.postgresql import ENUM as PGEnum

    tree = ast.parse((REPO / "main.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_migrations")

    def cap(level):
        def _log(m):
            sink.append((level, str(m)))
            if level != "debug":
                print(f"      {level:7} {m}")
        return _log

    log = types.SimpleNamespace(info=cap("info"), warning=cap("WARNING"),
                                debug=cap("debug"))
    ns = {"engine": engine, "log": log, "text": text, "inspect": inspect,
          "String": String, "Integer": Integer, "SAEnum": SAEnum, "PGEnum": PGEnum}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["run_migrations"]


async def run(url, failures):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    import database as real_db
    from models import Worker
    Base = real_db.Base

    engine = create_async_engine(url)
    try:
        async with engine.begin() as c:
            # one statement per execute - asyncpg uses prepared
            # statements and rejects multiple commands in one
            await c.execute(text("DROP SCHEMA public CASCADE"))
            await c.execute(text("CREATE SCHEMA public"))
            await c.execute(text(BROKEN_WORKERS))
            # everything else at full width so the run has to no-op on them
            for t in Base.metadata.sorted_tables:
                if t.name != "workers":
                    await c.run_sync(t.create)

        real_db.engine = engine
        real_db.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        Session = real_db.AsyncSessionLocal

        # ── 1. reproduce production ─────────────────────────────────────
        async with Session() as s:
            s.add(Worker(email="bot1@pgusa.local", name="Job Bot 1",
                         status="active", password_hash="$2b$12$fake"))
            try:
                await s.commit()
                failures.append("BEFORE the repair the insert SUCCEEDED - the "
                                "fixture no longer reproduces the production bug")
            except Exception as e:
                first = str(e).splitlines()[0]
                if "NotNullViolationError" not in first or '"id"' not in first:
                    failures.append(f"unexpected pre-repair error: {first}")
                else:
                    print(f"  reproduced: {first.split('>: ', 1)[-1]}")

        # ── 2. repair ───────────────────────────────────────────────────
        logs = []
        await load_run_migrations(engine, logs)()

        fixed = [m for lvl, m in logs
                 if lvl == "info" and "auto-increment restored" in m]
        if not any("workers.id" in m for m in fixed):
            failures.append(f"migration did not repair workers.id (repairs: {fixed})")
        else:
            print(f"  repaired: {[m for m in fixed if 'workers.id' in m][0]}")

        summary = [m for lvl, m in logs
                   if lvl == "info" and m.startswith("Migration summary:")]
        if not summary or "autoincrement_fixed=0" in summary[0]:
            failures.append(f"summary does not report the repair: {summary}")
        else:
            print(f"  {summary[0]}")

        # ── 3. the insert must now work ─────────────────────────────────
        async with Session() as s:
            s.add(Worker(email="bot1@pgusa.local", name="Job Bot 1",
                         status="active", password_hash="$2b$12$fake"))
            s.add(Worker(email="bot2@pgusa.local", name="Job Bot 2",
                         status="active", password_hash="$2b$12$fake"))
            try:
                await s.commit()
            except Exception as e:
                failures.append(f"insert STILL fails after repair: "
                                f"{str(e).splitlines()[0]}")
                return

        async with Session() as s:
            got = (await s.execute(
                select(Worker).where(Worker.email.like("%bot%pgusa.local"))
            )).scalars().all()
        ids = sorted(w.id for w in got)
        print(f"  inserted and findable: {[(w.id, w.email) for w in got]}")
        if len(got) != 2:
            failures.append(f"expected 2 workers, got {len(got)}")
        if any(i is None for i in ids) or len(set(ids)) != len(ids):
            failures.append(f"primary keys not generated uniquely: {ids}")

        # ── 4. idempotent, and must not collide with existing rows ──────
        #
        # A repair that restarts the sequence at 1 would hand out a key
        # that already exists the moment the table is non-empty.
        logs2 = []
        await load_run_migrations(engine, logs2)()
        again = [m for lvl, m in logs2 if lvl == "info"
                 and "auto-increment restored" in m and "workers.id" in m]
        if again:
            failures.append("repair re-ran on an already-fixed column; it is "
                            "not detecting the default it just installed")
        else:
            print("  idempotent: second run does not re-repair workers.id")

        async with Session() as s:
            s.add(Worker(email="bot3@pgusa.local", name="Job Bot 3",
                         status="active", password_hash="$2b$12$fake"))
            try:
                await s.commit()
            except Exception as e:
                failures.append(f"insert after re-run collided: "
                                f"{str(e).splitlines()[0]}")

        # ── 5. repairing a table that ALREADY HAS ROWS ──────────────────
        #
        # The dangerous case, and the one production is actually in: the
        # workers table is not necessarily empty. A repair that creates a
        # fresh sequence starting at 1 would hand out a primary key that
        # already exists, and the very first insert after the "fix" would
        # fail on a duplicate key. Rebuild the broken table with rows at
        # high ids and confirm the sequence starts above them.
        async with engine.begin() as c:
            await c.execute(text("DROP TABLE workers CASCADE"))
            await c.execute(text(BROKEN_WORKERS))
            await c.execute(text(
                "INSERT INTO workers (id, email, name, status) VALUES "
                "(41, 'a@x.com', 'A', 'active'), (42, 'b@x.com', 'B', 'active')"))

        logs3 = []
        await load_run_migrations(engine, logs3)()

        async with Session() as s:
            w = Worker(email="after@x.com", name="After", status="active",
                       password_hash="$2b$12$fake")
            s.add(w)
            try:
                await s.commit()
                print(f"  populated table: existing ids 41,42 -> new row got id={w.id}")
                if w.id is None or w.id <= 42:
                    failures.append(f"sequence collided with existing rows: new id "
                                    f"{w.id} is not above the existing MAX of 42")
            except Exception as e:
                failures.append(f"insert into repaired populated table failed: "
                                f"{str(e).splitlines()[0]}")
    finally:
        await engine.dispose()


def main():
    failures = []
    print("Integer primary key auto-increment repair\n")

    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        print("  SKIPPED - needs a real Postgres (TEST_DATABASE_URL).")
        print("  SQLite CANNOT substitute: INTEGER PRIMARY KEY is a rowid")
        print("  alias there and is populated automatically, so the broken")
        print("  schema inserts fine and the test would report a false pass.")
        print("\n  SKIPPED (not a pass)")
        return 0

    asyncio.run(run(url, failures))
    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  missing primary-key sequences are detected and repaired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
