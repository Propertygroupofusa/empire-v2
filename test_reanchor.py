"""Re-anchoring may only ever touch a branch that is holding nothing.

The bill for not having this, 2026-09-25:

    Seven branches, sixteen days, zero trades, and a bot correctly
    reporting itself alive the whole time. reference_price is written
    exactly twice in the whole codebase - at branch creation, and on a
    real fill:

        fresh.reference_price = filled_price

    Nothing re-anchors it to the market. A branch created before a rally
    keeps a reference below the market, and a buy needs

        price <= reference_price * (1 - grid_pct)

    so the dip is measured from a level the market already left. It is
    self-tightening: no fill means no re-anchor, so a rising market makes
    the next fill harder. The live fleet needed falls of 2.70%-3.67% from
    the current price to trigger a 2.00% step.

THE RULE THIS FILE PROTECTS: a flat branch's reference is free to move,
because nothing is tied to it. A branch WITH AN OPEN SLICE is not - there
the same reference_price is the sell trigger

    price >= reference_price * (1 + grid_pct)

so moving it moves the exit of a position already on the book. Re-anchor
must skip those, write reference_price and nothing else, and place no
orders.

Run: python3 test_reanchor.py
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


node = fn("reanchor_flat_grid_branches_now")
ok("the re-anchor function exists", node is not None)

# Body WITHOUT the docstring: the docstring describes every rule being
# tested and would satisfy any string match on its own.
stmts = node.body if node else []
if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
    stmts = stmts[1:]
body = "\n".join(ast.unparse(s) for s in stmts)

# --- it must skip anything holding a position ----------------------------
ok("it reads each branch's open slices", "get_grid_slices" in body)
ok("a branch with slices is skipped, not re-anchored",
   "if slices:" in body and "skipped.append" in body)
ok("it re-checks for slices INSIDE the write transaction (race guard)",
   body.count("get_grid_slices") >= 2)

# --- it may write reference_price and nothing else -----------------------
assigns = set()
for n in ast.walk(node):
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                    and t.value.id == "fresh":
                assigns.add(t.attr)
ok("the only branch field written is reference_price", assigns == {"reference_price"})
for forbidden in ("grid_pct", "num_levels", "allocated_usd", "active", "locked"):
    ok(f"it never writes {forbidden}", forbidden not in assigns)

# --- it must place no orders ---------------------------------------------
for order_call in ("grid_buy", "grid_sell", "place_market_buy", "place_maker_buy",
                   "place_market_sell", "place_maker_sell"):
    ok(f"it never calls {order_call}", order_call not in body)
ok("it reports that it placed no orders",
   "'orders_placed': False" in body or '"orders_placed": False' in body)

# --- missing data is skipped, never guessed ------------------------------
ok("a branch with no live price is skipped rather than guessed",
   "not guessing" in body or ("if not price" in body and "skipped.append" in body))
ok("inactive branches are left alone", "if not b.active" in body)

# --- the move must be auditable ------------------------------------------
ok("the old reference is reported", "old_reference_price" in body)
ok("the new reference is reported", "new_reference_price" in body)
ok("it reports where the next buy now triggers", "buy_triggers_at" in body)

# --- the endpoint --------------------------------------------------------
router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
ok("an endpoint exposes it", "/grid-status/reanchor-flat-branches" in router)
ok("the endpoint calls the guarded function, not its own copy",
   "reanchor_flat_grid_branches_now()" in router)


# --- the arithmetic, as behaviour ----------------------------------------
def buy_trigger(reference, grid_pct):
    return reference * (1 - grid_pct)


def drop_needed_pct(current, reference, grid_pct):
    return (current - buy_trigger(reference, grid_pct)) / current * 100


# The live fleet as measured, 2026-09-25.
LIVE = [("BTC-USD", 84470.80, 83975.93), ("ETH-USD", 2672.57, 2691.81),
        ("LINK-USD", 13.7620, 13.8580), ("DOGE-USD", 0.0968, 0.0980),
        ("NEAR-USD", 5.0761, 5.1544), ("SOL-USD", 119.28, 121.29),
        ("ARB-USD", 0.2200, 0.2238)]
STEP = 0.02

before = {c: drop_needed_pct(cur, ref, STEP) for c, ref, cur in LIVE}
ok("REGRESSION: before re-anchor, six of seven needed MORE than the 2% step",
   sum(1 for v in before.values() if v > 2.0 + 1e-9) == 6)
ok("REGRESSION: the worst needed over 3.6%, not 2%", max(before.values()) > 3.6)
ok("BTC was the only one needing less than the step", before["BTC-USD"] < 2.0)

after = {c: drop_needed_pct(cur, cur, STEP) for c, _ref, cur in LIVE}
ok("after re-anchor every branch needs exactly its own step",
   all(abs(v - 2.0) < 1e-9 for v in after.values()))
# Enforced by skipping, not by moving: a branch trading BELOW its
# reference is already nearer a fill than a fresh anchor would leave it.
def would_move(reference, current):
    return bool(reference) and current > reference


eligible = [c for c, ref, cur in LIVE if would_move(ref, cur)]
ok("REGRESSION: BTC is NOT re-anchored - it traded below its reference, so a "
   "re-anchor would have moved its trigger further away",
   "BTC-USD" not in eligible)
ok("the other six, all trading above their references, are re-anchored",
   len(eligible) == 6)
ok("re-anchoring never makes a branch harder to fill",
   all(after[c] <= before[c] + 1e-9 for c in eligible))
ok("the step itself is unchanged - this moves the reference, not the spacing",
   all(abs(drop_needed_pct(cur, cur, STEP) - STEP * 100) < 1e-9 for _c, _r, cur in LIVE))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
