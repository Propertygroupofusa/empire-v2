"""
Test that study-material generation cannot be called while disabled.

WHY THIS EXISTS
---------------
/study/upload-and-generate is the only endpoint in routers/study.py that
spends money, and it had no authentication in front of it.

verify_user() accepts any string containing "@" as an identity,
auto-creates a StudyUser at tier="free", and returns that user's tier. It
verifies nothing - no password, no signature, no email confirmation. So:

  - the paywall does not hold: the free allowance is 5/month keyed on
    email, and a different email is another 5, forever
  - a paying customer can be impersonated: Bearer <their-email> yields
    tier="paid" and unlimited generations on their subscription

Each call makes TWO claude-3-5-sonnet requests (vision to read the image,
then generation), and the application has no rate limiting anywhere. A
public, unauthenticated, uncapped endpoint that bills the operator per
request and cannot charge for any of it.

The gate is a stopgap, not the fix. The fix is authentication.

WHAT IS ASSERTED
----------------
  1. the flag defaults to OFF - a fresh deploy must not be spending money
  2. the guard raises BEFORE verify_user, which creates database rows for
     any email handed to it. Gating afterwards would still let an
     unauthenticated caller fill the table.
  3. the guard raises before ANY Claude call - asserted by counting real
     invocations against a fake client, not by trusting the return
  4. the whole token-spending surface is behind it: every
     client.messages.create in the module is reachable only from the
     gated endpoint
  5. enabling the flag restores the endpoint, so this is a switch and not
     a deletion

No network, no API keys, no database.
"""
import ast
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
MODULE = REPO / "routers" / "study.py"
FLAG = "STUDY_GENERATION_ENABLED"


def load_module(enabled):
    """Import routers.study fresh with the flag in a known state."""
    for name in list(sys.modules):
        if name in ("routers.study", "database", "models", "payments_pause"):
            del sys.modules[name]
    os.environ[FLAG] = "true" if enabled else "false"
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")

    from sqlalchemy.orm import declarative_base
    fake_db = types.ModuleType("database")
    fake_db.Base = declarative_base()
    fake_db.get_db = lambda: None
    sys.modules["database"] = fake_db

    import importlib
    return importlib.import_module("routers.study")


class CountingClaude:
    """Records every Claude call instead of making one."""

    def __init__(self):
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, *a, **k):
        self.calls += 1
        raise AssertionError("a Claude call was made while the gate was closed")


class FakeUpload:
    filename = "page.jpg"

    async def read(self):
        return b"x" * 1024


def main():
    failures = []
    print("Study generation gate\n")
    src = MODULE.read_text()
    tree = ast.parse(src)

    # ── 1. defaults to off ─────────────────────────────────────────────
    if f'os.getenv("{FLAG}", "false")' not in src:
        failures.append(f"{FLAG} does not default to \"false\"")
    else:
        print(f"  {FLAG} defaults to OFF")

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "upload_and_generate"),
              None)
    if fn is None:
        print("  FAIL  upload_and_generate not found")
        return 1

    # ── 2. the guard precedes verify_user ──────────────────────────────
    guard_lines = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.If) and FLAG in ast.dump(n.test)
                   and any(isinstance(b, ast.Raise) for b in ast.walk(n))]
    verify_lines = [n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "verify_user"]
    if not guard_lines:
        failures.append(f"no {FLAG} guard that raises inside upload_and_generate")
    elif verify_lines and min(guard_lines) > min(verify_lines):
        failures.append(f"the guard (line {min(guard_lines)}) comes AFTER verify_user "
                        f"(line {min(verify_lines)}) - verify_user auto-creates a "
                        f"StudyUser row for any email, so an unauthenticated caller "
                        f"could still fill the table")
    else:
        print(f"  guard raises at line {min(guard_lines)}, before verify_user "
              f"at line {min(verify_lines) if verify_lines else 'n/a'}")

    # ── 4. the whole spend surface is behind this one endpoint ─────────
    spenders = {n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "messages"}
    helpers = {f.name for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(f.lineno < ln <= (f.end_lineno or f.lineno) for ln in spenders)}
    callers = set()
    for f in ast.walk(tree):
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(f):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers:
                callers.add(f.name)
    stray = callers - {"upload_and_generate"}
    if stray:
        failures.append(f"token-spending helpers {sorted(helpers)} are also reachable "
                        f"from {sorted(stray)}, which the gate does not cover")
    else:
        print(f"  all {len(spenders)} Claude call sites sit in {sorted(helpers)}, "
              f"reached only from upload_and_generate")

    # ── 3. closed: no Claude call, HTTP 503 ────────────────────────────
    study = load_module(enabled=False)
    claude = CountingClaude()
    study.client = claude
    from fastapi import HTTPException
    try:
        asyncio.run(study.upload_and_generate(
            file=FakeUpload(), material_type="guide",
            authorization="Bearer attacker@example.com", db=None))
        failures.append("disabled endpoint returned normally instead of raising")
    except HTTPException as e:
        if e.status_code != 503:
            failures.append(f"expected HTTP 503, got {e.status_code}")
        else:
            print(f"  closed: HTTP 503, {claude.calls} Claude calls made")
    except Exception as e:
        # Anything that is not a 503 means the request got PAST the gate.
        # db is deliberately None, so execution reaching verify_user dies
        # on AttributeError - which is a missing gate, not a broken test.
        # Caught explicitly, because an uncaught traceback here kills the
        # script before it prints a verdict, and "the test errored" reads
        # very differently from "the gate is gone".
        failures.append(f"the gate did not stop the request - execution reached "
                        f"{type(e).__name__}: {e}. Only a 503 means the guard "
                        f"fired; anything else means it is missing or too late.")
    if claude.calls:
        failures.append(f"{claude.calls} Claude call(s) made while disabled")

    # ── 5. it is a switch, not a deletion ──────────────────────────────
    study_on = load_module(enabled=True)
    if not study_on.STUDY_GENERATION_ENABLED:
        failures.append("setting the flag to 'true' did not enable the module")
    else:
        # Reaches past the gate; the next thing it does is validate the
        # material type, so an invalid one proves we got through without
        # needing a database or a real Claude key.
        try:
            asyncio.run(study_on.upload_and_generate(
                file=FakeUpload(), material_type="not-a-type",
                authorization="Bearer x@y.com", db=None))
            failures.append("enabled endpoint accepted an invalid material_type")
        except HTTPException as e:
            if e.status_code == 400:
                print("  enabled: request passes the gate and reaches validation")
            elif e.status_code == 503:
                failures.append("still 503 with the flag on - the gate cannot be reopened")
            else:
                failures.append(f"unexpected status with flag on: {e.status_code}")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  generation is off by default, spends nothing while off, "
          "and re-enables cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
