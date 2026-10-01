"""The audit layer's truth table must never confuse "not measured" with "fine".

WHAT THIS PROTECTS

On 2026-10-01 the live account read:

    exchange_truth_failures   0 rows, and READABLE
    branch_control_state      2 rows, both UNKNOWN
    backing, same moment      8 unbacked branches, $1,168.59 missing

A readable, empty failure table beside eight genuinely broken branches. The
measurement existed every cycle and never reached the table the gate reads.

The rule that matters most here is the one this fleet has now broken three
times in three different places: an UNREADABLE source is not a clean result.
A 429 made the concentration ceiling fail OPEN. A degraded census was read
as a $1,945 loss. Here, the same mistake would be a balance read that failed
quietly marking every branch MATCHED, or marking them all MISMATCH - both
are a verdict drawn from no evidence.

These tests RUN the functions against fixtures shaped like real
slice_backing output. The DB-touching path runs against a stub session that
records what was added, so the write rules are checked by executing them.

Run: python3 test_exchange_truth_recorder.py
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


import exchange_truth_recorder as rec        # noqa: E402
import audit_models as am                    # noqa: E402

M = rec.MATCHED


def measurement(rows=(), unknown=(), readable=True):
    return {"readable": readable, "rows": list(rows), "unknown": list(unknown)}


def row(pid, claimed, held, backed, short=None, why="because"):
    return {"product_id": pid, "asset": pid.split("-")[0],
            "claimed_units": claimed, "held_units": held, "backed": backed,
            "short_units": short if short is not None else max(0.0, claimed - held),
            "short_usd": 10.0, "why": why}


# ── 1. unreadable measurement changes NOTHING ────────────────────────
section("[1] an unreadable measurement is not a verdict")
for bad in (None, {}, {"readable": False, "reason": "balances unreadable"},
            measurement(readable=False)):
    ok(f"classify({str(bad)[:34]}) is empty", rec.classify(bad) == {})
ok("an unreadable pass with rows present STILL yields nothing",
   rec.classify({"readable": False,
                 "rows": [row("ZEC-USD", 1.0, 0.0, False)]}) == {},
   "a failed read must not be mined for verdicts")

# ── 2. backed / unbacked / unknown map to the right three ────────────
section("[2] the three verdicts")
v = rec.classify(measurement(
    rows=[row("ZEC-USD", 1.41, 1.33, True), row("QNT-USD", 0.676, 0.00097, False)],
    unknown=[{"product_id": "TIA-USD", "asset": "TIA", "claimed_units": 89.78,
              "reason": "balance not readable this pass"}]))
ok("a backed branch is MATCHED", v["ZEC-USD"]["status"] == rec.MATCHED)
ok("an unbacked branch is MISMATCH", v["QNT-USD"]["status"] == rec.MISMATCH)
ok("an unmeasured asset is STALE, never MISMATCH",
   v["TIA-USD"]["status"] == rec.STALE, v["TIA-USD"]["status"])
ok("the STALE row carries no exchange quantity",
   v["TIA-USD"]["exchange_quantity"] is None,
   "a number here would be a shortfall nobody measured")
ok("the STALE row carries no difference",
   v["TIA-USD"]["difference"] is None)
ok("the mismatch keeps both real quantities",
   v["QNT-USD"]["db_quantity"] == 0.676 and v["QNT-USD"]["exchange_quantity"] == 0.00097)
ok("a row already measured is not overwritten by an unknown entry",
   rec.classify(measurement(
       rows=[row("ZEC-USD", 1.0, 1.0, True)],
       unknown=[{"product_id": "ZEC-USD", "asset": "ZEC",
                 "reason": "late"}]))["ZEC-USD"]["status"] == rec.MATCHED)


# ── 3. the write rules, executed ─────────────────────────────────────
class _Res:
    def __init__(self, v): self._v = v
    def scalar_one_or_none(self): return self._v


class _Session:
    """Records what was added; serves pre-seeded control rows."""
    def __init__(self, existing=None):
        self.added = []
        self.existing = existing or {}
        self._last = None

    async def execute(self, stmt):
        # Every select in this module is for BranchControlState by bot_name.
        return _Res(self._next)

    def add(self, obj):
        self.added.append(obj)

    def of(self, cls):
        return [o for o in self.added if isinstance(o, cls)]


async def run(session, meas, p2b, seeded=None):
    """Drive record() with a controlled 'existing row' per product."""
    seeded = seeded or {}
    order = list(rec.classify(meas).keys())
    it = iter([seeded.get(p) for p in order])
    session._next = None

    real_exec = session.execute

    async def exec_(stmt):
        try:
            session._next = next(it)
        except StopIteration:
            session._next = None
        return _Res(session._next)
    session.execute = exec_
    try:
        return await rec.record(session, meas, product_to_bot=p2b,
                                branch_ids={p: 7 for p in order})
    finally:
        session.execute = real_exec


section("[3] a new mismatch writes a failure row and an event")
s = _Session()
meas = measurement(rows=[row("QNT-USD", 0.676, 0.00097, False)])
r = asyncio.get_event_loop().run_until_complete(
    run(s, meas, {"QNT-USD": "crypto_grid_14"}))
ok("one failure row written", len(s.of(am.ExchangeTruthFailure)) == 1)
f = s.of(am.ExchangeTruthFailure)[0]
ok("keyed on bot_name, the durable identity", f.bot_name == "crypto_grid_14")
ok("it records what the books claimed", f.db_quantity == 0.676)
ok("and what the venue really held", f.exchange_quantity == 0.00097)
ok("previous status is UNKNOWN for a branch never seen",
   f.previous_inventory_status == rec.UNKNOWN)
ok("severity is ERROR", f.severity == "ERROR")
ok("one timeline event too", len(s.of(am.BranchAuditEvent)) == 1)
ok("a control row is created", len(s.of(am.BranchControlState)) == 1)
ok("CREATED WITH execution_enabled FALSE - rule 4",
   s.of(am.BranchControlState)[0].execution_enabled is False)
ok("summary counts it", r["mismatch"] == 1 and r["failures_written"] == 1)

section("[4] an unchanged status writes NOTHING new")
s = _Session()
seeded = {"QNT-USD": am.BranchControlState(
    bot_name="crypto_grid_14", reconciliation_status=rec.MISMATCH,
    execution_enabled=False)}
r = asyncio.get_event_loop().run_until_complete(
    run(s, meas, {"QNT-USD": "crypto_grid_14"}, seeded))
ok("no second failure row for the same standing condition",
   len(s.of(am.ExchangeTruthFailure)) == 0,
   "30s cycles would otherwise write ~23,000 identical rows a day")
ok("no duplicate timeline event", len(s.of(am.BranchAuditEvent)) == 0)
ok("summary says nothing changed", r["changed"] == 0)
ok("but it still counted the measurement", r["mismatch"] == 1)

section("[5] recovery is news too, and is NOT a failure row")
s = _Session()
seeded = {"ZEC-USD": am.BranchControlState(
    bot_name="crypto_grid_2", reconciliation_status=rec.MISMATCH,
    execution_enabled=False)}
r = asyncio.get_event_loop().run_until_complete(
    run(s, measurement(rows=[row("ZEC-USD", 1.41, 1.33, True)]),
        {"ZEC-USD": "crypto_grid_2"}, seeded))
ok("the status moved to MATCHED",
   seeded["ZEC-USD"].reconciliation_status == rec.MATCHED)
ok("an event records the recovery", len(s.of(am.BranchAuditEvent)) == 1)
ok("NO failure row for a branch that got better",
   len(s.of(am.ExchangeTruthFailure)) == 0)
ok("the event names where it came from",
   s.of(am.BranchAuditEvent)[0].previous_state == {"reconciliation_status": rec.MISMATCH})

section("[6] an unreadable pass writes nothing at all")
s = _Session()
r = asyncio.get_event_loop().run_until_complete(
    run(s, {"readable": False, "reason": "429"}, {"ZEC-USD": "crypto_grid_2"}))
ok("nothing added", s.added == [])
ok("summary says it was skipped", r["skipped_unreadable"] is True)
ok("and claims nothing was measured", r["measured"] == 0)
ok("it does NOT report everything matched", r["matched"] == 0)

section("[7] a branch with no durable name is skipped, not guessed")
s = _Session()
r = asyncio.get_event_loop().run_until_complete(
    run(s, meas, {}))        # no product_to_bot entry
ok("no rows written without a bot_name", s.added == [])

section("[8] execution_enabled is never set anywhere in this module")
import ast                                   # noqa: E402
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "exchange_truth_recorder.py"), encoding="utf-8").read()
tree = ast.parse(src)
# CODE ONLY - the docstrings discuss execution_enabled at length on purpose,
# and matching prose is a false positive this repo has produced five times.
code = []
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.body = [n for n in node.body
                     if not (isinstance(n, ast.Expr)
                             and isinstance(n.value, ast.Constant)
                             and isinstance(n.value.value, str))]
body = ast.unparse(tree)
assigns = [ast.unparse(n) for n in ast.walk(ast.parse(body))
           if isinstance(n, ast.Assign)
           and any("execution_enabled" in ast.unparse(t) for t in n.targets)]
ok("no assignment to .execution_enabled", not assigns, str(assigns))
kw = [ast.unparse(n) for n in ast.walk(ast.parse(body))
      if isinstance(n, ast.keyword) and n.arg == "execution_enabled"
      and ast.unparse(n.value) != "False"]
ok("the only execution_enabled passed at construction is False", not kw, str(kw))
ok("no commit() - the caller owns the transaction", "commit(" not in body)
ok("it places no orders", "place_" not in body and "aiohttp" not in body)

print()
failed = 0
for label, res, detail in checks:
    if res is None:
        print(f"\n{label}")
    else:
        print(f"  {'PASS' if res else 'FAIL'}  {label}" + (f"   -> {detail}" if detail and not res else ""))
        failed += 0 if res else 1
total = sum(1 for _, r, _ in checks if r is not None)
print(f"\n{total - failed}/{total} checks passed")
print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
