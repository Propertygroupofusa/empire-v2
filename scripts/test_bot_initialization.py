"""
Test that bot workers can actually be created.

WHY THIS EXISTS
---------------
The payments table was empty. Not "no pending payouts" - no rows at all,
ever. The chain:

  main.py:418  from passlib.context import CryptContext
               -> ModuleNotFoundError: No module named 'passlib'
               -> initialize_bot() dies on its first statement
               -> caught by its own `except Exception`, logged as a
                  warning, and the CALLER then logged
                  "Bot worker initialized" regardless
               -> zero bot workers, ever
               -> start_job_bot: "No bot workers found, skipping cycle"
               -> no jobs claimed, no jobs completed
               -> no Payment rows created (main.py:530 never reached)
               -> payments table empty, payout loop had nothing to pay

passlib is not in requirements.txt and nothing pulls it in transitively.
Adding it would not have helped: passlib's bcrypt backend reads
bcrypt.__about__.__version__, which bcrypt removed in 4.x, and the repo
pins bcrypt==5.0.0 - measured, with passlib installed alongside it:

    AttributeError: module 'bcrypt' has no attribute '__about__'
    ValueError: password cannot be longer than 72 bytes

worker_auth.py hit this, worked around it with bcrypt's native API, and
documented why - while three other call sites kept importing passlib.

WHAT IS ASSERTED
----------------
  1. no module imports passlib (the dependency is not declared, so any
     import of it is a latent ModuleNotFoundError in production)
  2. hash_password actually works against the PINNED bcrypt, and produces
     a hash that verifies - a "fix" that imports cleanly but cannot hash
     would fail identically at runtime
  3. initialize_bot no longer swallows its own exceptions, so the
     caller's error branch is reachable and a failure cannot be reported
     as success
  4. against a live Postgres, initialize_bot actually creates workers
     that start_job_bot's query can then find - the two use different
     code paths and the LIKE pattern between them has to agree

Check 4 needs a real database; it is skipped loudly without one. Checks
1-3 run everywhere, including CI.
"""
import ast
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PY_FILES = [p for p in REPO.rglob("*.py")
            if ".git" not in p.parts and "scripts" not in p.parts]


def check_no_passlib(failures):
    """passlib is not a declared dependency, so importing it anywhere is a
    production-only ModuleNotFoundError."""
    req = (REPO / "requirements.txt").read_text().lower()
    declared = "passlib" in req

    offenders = []
    for path in PY_FILES:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            mod = None
            if isinstance(n, ast.ImportFrom):
                mod = n.module or ""
            elif isinstance(n, ast.Import):
                mod = ",".join(a.name for a in n.names)
            if mod and "passlib" in mod:
                offenders.append(f"{path.relative_to(REPO)}:{n.lineno}")

    if offenders and not declared:
        failures.append(f"passlib imported but not in requirements.txt: "
                        f"{', '.join(offenders)}")
    elif declared:
        failures.append("passlib was added to requirements.txt - it is broken "
                        "against the pinned bcrypt==5.0.0 (reads "
                        "bcrypt.__about__, removed in 4.x). Use bcrypt directly.")
    else:
        print(f"  no passlib imports across {len(PY_FILES)} modules")


def check_hashing_works(failures):
    """A fix that imports cleanly but cannot hash fails identically at
    runtime, so exercise the real helper against the real bcrypt."""
    import bcrypt
    from worker_auth import hash_password, verify_password
    print(f"  bcrypt {bcrypt.__version__} (no __about__: "
          f"{not hasattr(bcrypt, '__about__')})")
    try:
        h = hash_password("auto_bot_password_123")
    except Exception as e:
        failures.append(f"hash_password raised against the pinned bcrypt: "
                        f"[{type(e).__name__}] {e}")
        return
    if not verify_password("auto_bot_password_123", h):
        failures.append("hash_password produced a hash that does not verify")
    elif verify_password("wrong", h):
        failures.append("verify_password accepts a wrong password")
    else:
        print(f"  hash_password works and round-trips: {h[:16]}...")


def check_failure_is_loud(failures):
    """initialize_bot must not swallow its own exceptions, or the caller's
    error branch is dead code and a total failure logs as success."""
    src = (REPO / "main.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "initialize_bot")

    def creates_a_worker(nodes):
        """Does this subtree construct a Worker or add one to the session?
        Checked on the AST directly - an earlier version of this test
        substring-matched ast.dump() for 'Worker(' and silently never
        fired, because dump renders a call as Name(id='Worker')."""
        for node in nodes:
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                if isinstance(f, ast.Name) and f.id == "Worker":
                    return True
                if isinstance(f, ast.Attribute) and f.attr == "add":
                    return True
        return False

    offending = [t for t in ast.walk(fn)
                 if isinstance(t, ast.Try) and creates_a_worker(t.body)]
    if offending:
        lines = ", ".join(f"main.py:{t.lineno}" for t in offending)
        failures.append(f"initialize_bot wraps worker creation in try/except "
                        f"({lines}) - it swallows its own failure and the "
                        f"caller then logs success, which is how zero bot "
                        f"workers went unnoticed for months")
    else:
        print("  initialize_bot propagates failures to its caller")


async def check_live(url, failures):
    """initialize_bot must create workers that start_job_bot can find.
    The two use separate code paths and their LIKE patterns must agree."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.orm import declarative_base

    import database as real_db  # noqa: F401  (real module, real engine)
    from models import Worker

    engine = create_async_engine(url)
    try:
        async with engine.begin() as c:
            await c.run_sync(real_db.Base.metadata.create_all)

        # point the app's session factory at this throwaway database
        real_db.engine = engine
        real_db.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

        # Extract the real initialize_bot rather than importing main.py,
        # which pulls in uvicorn/alpaca/anthropic and the entire app. The
        # function does all of its own imports internally, so it needs
        # almost nothing injected - and this runs the shipped bytes, not a
        # reimplementation.
        import logging
        tree = ast.parse((REPO / "main.py").read_text())
        fnode = next(n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "initialize_bot")
        ns = {"os": os, "log": logging.getLogger("test")}
        exec(compile(ast.Module(body=[fnode], type_ignores=[]),
                     "main.py", "exec"), ns)
        initialize_bot = ns["initialize_bot"]

        os.environ.pop("STRIPE_SECRET_KEY", None)  # no live Stripe calls
        await initialize_bot()

        async with real_db.AsyncSessionLocal() as s:
            # the exact query start_job_bot uses
            found = (await s.execute(
                select(Worker).where(Worker.email.like("%bot%pgusa.local"))
            )).scalars().all()

        if not found:
            failures.append("initialize_bot ran but start_job_bot's query finds "
                            "no workers - the LIKE patterns disagree")
        else:
            for w in found:
                print(f"  created + findable: {w.email} (hash "
                      f"{'set' if w.password_hash else 'MISSING'})")
                if not w.password_hash:
                    failures.append(f"{w.email} has no password_hash")
        if len(found) != 2:
            failures.append(f"expected 2 bot workers, got {len(found)}")

        # idempotence: a second run must not duplicate
        await initialize_bot()
        async with real_db.AsyncSessionLocal() as s:
            again = (await s.execute(
                select(Worker).where(Worker.email.like("%bot%pgusa.local"))
            )).scalars().all()
        if len(again) != len(found):
            failures.append(f"re-running initialize_bot changed the worker count "
                            f"{len(found)} -> {len(again)}")
        else:
            print(f"  idempotent: still {len(again)} workers after a second run")
    finally:
        await engine.dispose()


def main_():
    failures = []
    print("Bot worker initialization\n")
    check_no_passlib(failures)
    check_hashing_works(failures)
    check_failure_is_loud(failures)

    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if url:
        print(f"\n  live check against {url.split('@')[-1]}")
        asyncio.run(check_live(url, failures))
    else:
        print("\n  SKIPPED live check (TEST_DATABASE_URL not set) - the")
        print("  create-then-find round trip needs a real database.")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  bot workers can be created and found")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
