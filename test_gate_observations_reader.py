"""The denial log must be READABLE, and an unreadable one must say UNKNOWN.

WHY THIS FILE EXISTS

branch_audit_service ships EXECUTION_GATE_MODE=observe, and states in its
own comment what observing buys: "a day of evidence showing exactly which
branches would have been refused and why, before that refusal becomes real
money not being deployed."

The gate was wired into the order path on 2026-10-01 and the audit rows
were being written - and NOTHING COULD READ THEM. No endpoint selected from
allocator_denials, execution_authority_events, exchange_truth_failures or
branch_control_state. An evidence log with no reader cannot inform the
decision it exists to inform.

THE RULE THIS FILE PROTECTS, and it is the one that matters most here:

    A TABLE THAT COULD NOT BE READ REPORTS None, NEVER 0.

"0 denials observed" from a failed read is indistinguishable from "the gate
examined everything and found nothing wrong" - and it argues FOR flipping
the gate to enforce, on the strength of evidence that was never collected.
That is the exact failure mode of the $8,172 phantom (a degraded census
read as a loss) and of the 20% concentration ceiling failing OPEN on a 429
(an unreadable book read as an empty one). Same bug, third place.

It runs the endpoint FUNCTION, against stub sessions - it does not grep the
source. A test that greps cannot tell a comment from code, and reading a
pass count instead of output is how two failing tests got called passing.

Run: python3 test_gate_observations_reader.py
"""
import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

checks = []


def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))


def section(t):
    checks.append((t, None, ""))


class _Row:
    """A stub result row. Any column the endpoint reads but this row does
    not define comes back None - which is what a real row with a NULL in
    it does, and it proves the endpoint tolerates one rather than raising."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _OkSession:
    """Hands back a canned row set for every query."""
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Result(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _BrokenSession:
    """Every read raises - the case this file exists for."""
    async def execute(self, stmt):
        raise RuntimeError("relation \"allocator_denials\" does not exist")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _factory(session):
    return lambda: session


import routers.trading_dashboard as td           # noqa: E402

GET = td.get_gate_observations


def run(session, **kw):
    real = td.get_session_factory
    td.get_session_factory = lambda: _factory(session)
    try:
        return asyncio.get_event_loop().run_until_complete(GET(**kw))
    finally:
        td.get_session_factory = real


# ── 1. an unreadable table is UNKNOWN, never a quiet day ──────────────
section("[1] an unreadable denial log reports UNKNOWN, not zero")
out = run(_BrokenSession())
ok("denials.readable is False", out["denials"]["readable"] is False)
ok("denials.total is None, NOT 0",
   out["denials"]["total"] is None,
   f"got {out['denials']['total']!r}")
ok("it is not the integer 0", out["denials"]["total"] != 0 or out["denials"]["total"] is None)
ok("the failure reason is carried, not swallowed",
   "does not exist" in (out["denials"].get("unreadable") or ""))
ok("verdict is UNREADABLE", out["verdict"] == "UNREADABLE")
ok("the detail forbids reading it as quiet",
   "not a quiet window" in out["detail"].lower()
   or "nothing was measured" in out["detail"].lower(),
   out["detail"])
for k in ("denials_by_branch", "recent_denials", "authority_events",
          "truth_failures", "control_state"):
    ok(f"{k} is also marked unreadable", out[k]["readable"] is False)
ok("truth_failures.total is None too", out["truth_failures"]["total"] is None)
ok("control_state.rows_seeded is None too",
   out["control_state"]["rows_seeded"] is None)

# ── 2. a REAL zero is reported as a real zero, and still hedged ───────
section("[2] a readable empty log is a real zero - and says what it cannot mean")
out = run(_OkSession([]))
ok("readable is True", out["denials"]["readable"] is True)
ok("total is 0, not None", out["denials"]["total"] == 0)
ok("verdict is NOTHING_OBSERVED", out["verdict"] == "NOTHING_OBSERVED")
ok("it warns the path may simply not have run",
   "has not run" in out["detail"], out["detail"])

# ── 3. observe-only denials are counted APART from real refusals ──────
section("[3] OBSERVED_ is not the same as blocked")
rows = [
    _Row(reason_code="OBSERVED_INVENTORY_MISMATCH", gate_failed="exchange_truth", n=7),
    _Row(reason_code="INVENTORY_MISMATCH", gate_failed="exchange_truth", n=2),
]
out = run(_OkSession(rows))
ok("total counts both", out["denials"]["total"] == 9,
   f"got {out['denials']['total']}")
ok("observed_only counts only the OBSERVED_ rows",
   out["denials"]["observed_only"] == 7,
   f"got {out['denials']['observed_only']}")
ok("the detail says nothing was stopped",
   "NOT binding" in out["detail"] or "proceeded anyway" in out["detail"],
   out["detail"])

# ── 4. the mode is reported, and read from the service not guessed ────
section("[4] gate mode comes from branch_audit_service")
import branch_audit_service as bas              # noqa: E402
_prev = os.environ.get("EXECUTION_GATE_MODE")
os.environ["EXECUTION_GATE_MODE"] = "enforce"
out = run(_OkSession(rows))
ok("mode reads enforce from the env the service reads",
   out["gate_mode"] == "enforce", f"got {out['gate_mode']!r}")
ok("gate_is_binding is True under enforce", out["gate_is_binding"] is True)
ok("verdict flips to ENFORCING", out["verdict"] == "ENFORCING")
os.environ["EXECUTION_GATE_MODE"] = "observe"
out = run(_OkSession(rows))
ok("observe is not binding", out["gate_is_binding"] is False)
ok("verdict is OBSERVED_ONLY", out["verdict"] == "OBSERVED_ONLY")
if _prev is None:
    os.environ.pop("EXECUTION_GATE_MODE", None)
else:
    os.environ["EXECUTION_GATE_MODE"] = _prev
ok("the service's own default is observe, not enforce",
   bas.gate_mode() == "observe" or _prev == "enforce")

# ── 5. it is READ-ONLY. No write may reach this endpoint. ─────────────
section("[5] the endpoint cannot write")
import ast                                       # noqa: E402
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "routers", "trading_dashboard.py"),
            encoding="utf-8").read()
_fn = next(n for n in ast.walk(ast.parse(_src))
           if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "get_gate_observations")
# CODE ONLY. A docstring mentioning "commit" is prose, not a write, and
# matching it was a real false positive five separate times today.
_body = ast.unparse(ast.Module(body=[n for n in _fn.body
                                     if not (isinstance(n, ast.Expr)
                                             and isinstance(n.value, ast.Constant)
                                             and isinstance(n.value.value, str))],
                               type_ignores=[]))
for bad in ("commit(", "session.add", ".delete(", "flush(", "insert(",
            "update(", "place_market", "place_maker"):
    ok(f"no {bad} in the executable body", bad not in _body)
ok("it selects", "select" in _body)

# ── 6. the window and row count are bounded ───────────────────────────
section("[6] one read is bounded")
out = run(_OkSession([]), hours=99999.0, limit=10_000)
ok("hours is clamped to 30 days", out["window_hours"] <= 24 * 30,
   f"got {out['window_hours']}")
ok("a negative window cannot invert the query",
   run(_OkSession([]), hours=-5.0)["window_hours"] >= 0)
ok("the row cap is reported", out["row_cap"] == td.GATE_OBSERVATION_ROW_CAP)
ok("the cap is a real bound", td.GATE_OBSERVATION_ROW_CAP > 0)

# ── report ────────────────────────────────────────────────────────────
print()
failed = 0
for label, res, detail in checks:
    if res is None:
        print(f"\n{label}")
    else:
        print(f"  {'PASS' if res else 'FAIL'}  {label}"
              + (f"   -> {detail}" if detail and not res else ""))
        if not res:
            failed += 1
print()
total = sum(1 for _, r, _ in checks if r is not None)
print(f"{total - failed}/{total} checks passed")
print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
