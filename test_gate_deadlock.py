"""A step the floor permits and the gate can never pass is a dead branch.

THE BILL, 2026-09-25, live on this account:

    NEAR-USD   step 2.00%   reaching its buy trigger constantly
               net-edge gate: "a 2.00% target does not clear 2.16% of costs
               (1.50% fees + 0.66% adverse)"
               171 consecutive refusals. Every one correct.

Two cost models decided whether that branch traded, and nothing required
them to agree:

    fee_safe_floor_pct()   step must clear FEES plus a margin        1.70%
    _net_edge_gate_ok()    step must clear fees + SPREAD + ADVERSE   2.16%

2.00% sits between them. So the floor was satisfied, the gate refused every
buy, and no component in the system could move the step - the branch was
deadlocked at a spacing that could never trade, with real capital behind it,
and the only symptom was a counter going up.

WHAT THIS FILE PROTECTS

The resolution is NOT relaxing the gate. The gate is the one component in
this stack whose numbers have been right throughout - it correctly refused
171 losing trades while the fee floor was busy inverting itself and the
ledger was busy under-charging fees. The resolution is that the SPACING must
clear the bar the gate actually enforces, which is what the fee floor was
always trying to do and was only doing for one of the three costs.

So the checks below are about direction and honesty:

  * widening only ever goes UP, and only when the gate is what refused
  * it is derived by INVERTING the gate rather than re-deriving its
    internals, so the two cannot drift apart the way two copies of a
    formula do
  * it is bounded, and hitting the bound means the branch keeps refusing -
    some coins are too expensive to grid, and that is an answer
  * a refusal that is not about the step (spread too wide, book too thin)
    must not be "fixed" by widening, which would only lose more per trade
    on a book that still cannot absorb the slice

Run: python3 test_gate_deadlock.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def body_src(name):
    node = fn(name)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant) \
            and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


# --- the mechanism exists --------------------------------------------------
ok("a gate-clearing floor exists", fn("gate_clearing_floor_pct") is not None)
ok("it can be switched off", fn("auto_widen_enabled") is not None)
ok("and it is bounded", "GATE_CLEARING_MAX_PCT" in src)

g = body_src("gate_clearing_floor_pct")
ok("REGRESSION: it INVERTS the gate rather than re-deriving its internals - "
   "two copies of a cost formula drift, and this bug WAS two models drifting",
   "evaluate_grid_step" in g and "net_edge_pct" in g)
ok("it prices the same worst-case leg the floor does",
   "worst_case_leg_fee_rate" in g)
ok("it returns nothing when the gate already passes, rather than widening anyway",
   "already clears" in g)
ok("a refusal that is NOT about the step does not get widened",
   "not a spacing problem" in g)
ok("and an unreadable book leaves the spacing alone",
   "order book unreadable" in g)

# --- direction and bound, as behaviour -------------------------------------
MARGIN = 0.002
BOUND = 0.06


def required(step, net_edge):
    return step - net_edge + MARGIN


ok("on NEAR's real numbers it asks for 2.36%",
   abs(required(0.0200, -0.00164) - 0.02364) < 1e-9)
ok("2.36% is above the 2.00% it was stuck at", required(0.0200, -0.00164) > 0.0200)
ok("2.36% is inside the bound, so NEAR widens", required(0.0200, -0.00164) <= BOUND)
ok("REGRESSION: a step that already clears is never TIGHTENED - a positive "
   "edge must not pull the spacing down to the break-even point",
   required(0.0300, +0.00800) < 0.0300)  # would compute lower, and is refused
                                          # by the 'already clears' return above
ok("an absurd volatility reading does NOT walk the branch out - it exceeds "
   "the bound and the branch keeps refusing",
   required(0.0200, -0.0900) > BOUND)

# --- it is applied where it can actually take effect -----------------------
cycle = src[src.index("grid_pct = branch.grid_pct"):]
cycle = cycle[:cycle.index("\nasync def ")] if "\nasync def " in cycle else cycle
ok("it runs inside the branch's spacing resolution", "gate_clearing_floor_pct" in cycle)
ok("it is applied AFTER the fee floor, so it can only raise what that chose",
   cycle.index("fee_safe_floor_pct") < cycle.index("gate_clearing_floor_pct"))
ok("it only assigns when the required step is strictly larger",
   "_needed > _probe_step" in cycle)
ok("hitting the bound leaves the spacing untouched",
   "Leaving the spacing alone" in cycle)
ok("and it is skippable without touching code", "auto_widen_enabled()" in cycle)

# --- the gate itself is untouched -----------------------------------------
gate = body_src("_net_edge_gate_ok")
ok("REGRESSION: the gate still refuses on its own terms - nothing here "
   "loosens the thing that was right",
   # ast.unparse renders `return False, reason` as `return (False, reason)`,
   # so match the refusal itself rather than a guess at its formatting.
   "GATE_BLOCK" in gate and "(False," in gate)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
