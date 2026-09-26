"""The three holes that merged an eight-branch fleet into three.

2026-09-26, 00:59Z: rebalance_flat_grid_branches_now() collapsed the fleet
to BTC $69.23 / NEAR $207.69 / ARB $276.92. Nothing was lost - the total
stayed $553.84 - but half the account landed on ARB, 1 round trip in 30
days, while BONK/FLOKI/DOGE/ETC/BCH (26 of the fleet's 34 monthly round
trips) were abandoned.

Each test below fails if one of the three fixes is removed. No network,
no database - these read the source, because the failure was structural:
the code was reachable and ungated, not wrong at runtime.
"""

import ast
import sys

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)


def func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


print("\n1. the bulk rebalance is gated on the same switch the sweep uses")

fn = func("rebalance_flat_grid_branches_now")
ok("rebalance_flat_grid_branches_now exists", fn is not None)

calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
names = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in calls}
ok("it calls is_grid_auto_rotate_active() before doing anything",
   "is_grid_auto_rotate_active" in names,
   "an ungated fleet-wide merge is reachable by anything that hits the URL")

# The gate must be an early return, not advisory.
body = fn.body
guard = None
for stmt in body:
    if isinstance(stmt, ast.If) and any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "is_grid_auto_rotate_active"
            for n in ast.walk(stmt.test)):
        guard = stmt
        break
ok("the gate is an `if` guard, not just a logged note", guard is not None)
ok("the guard RETURNS rather than falling through",
   guard is not None and any(isinstance(n, ast.Return) for n in ast.walk(guard)))

# And it must sit before any rotation happens.
rot_lines = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_maybe_rotate_one_grid_branch"]
ok("the guard sits BEFORE the first rotation call",
   guard is not None and rot_lines and guard.lineno < min(rot_lines),
   f"guard@{getattr(guard, 'lineno', None)} first rotate@{min(rot_lines) if rot_lines else None}")


print("\n2. the bulk loop no longer waives every cooldown at once")

after_sale_vals = []
for n in ast.walk(fn):
    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_maybe_rotate_one_grid_branch":
        for kw in n.keywords:
            if kw.arg == "after_sale":
                after_sale_vals.append(getattr(kw.value, "value", None))
ok("it calls _maybe_rotate_one_grid_branch with after_sale specified",
   len(after_sale_vals) >= 1, f"got {after_sale_vals}")
ok("after_sale is False in the BULK loop, so the cooldown applies",
   all(v is False for v in after_sale_vals),
   "after_sale=True means 'this one branch just sold'; a fleet sweep is not that")

# The genuine post-sale path must KEEP after_sale=True - that one is correct.
sale_fn = func("run_grid_branch_cycle")
post_sale_true = any(
    isinstance(n, ast.Call)
    and getattr(n.func, "id", "") == "_maybe_rotate_one_grid_branch"
    and any(kw.arg == "after_sale" and getattr(kw.value, "value", None) is True
            for kw in n.keywords)
    for n in ast.walk(sale_fn)) if sale_fn else False
ok("the real post-sale path still uses after_sale=True (not over-corrected)",
   post_sale_true,
   "a branch that genuinely just sold its last slice still redeploys immediately")


print("\n3. the auto-rotate toggle fails CLOSED when its state is missing")

tog = func("is_grid_auto_rotate_active")
ok("is_grid_auto_rotate_active exists", tog is not None)

missing_row_returns = []
for stmt in ast.walk(tog):
    if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare):
        left = stmt.test.left
        if getattr(left, "id", "") == "row" and any(
                isinstance(c, ast.Constant) and c.value is None for c in stmt.test.comparators):
            for n in ast.walk(stmt):
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant):
                    missing_row_returns.append(n.value.value)
ok("a missing row returns False, not True", missing_row_returns == [False],
   f"got {missing_row_returns} - a control that switches itself on when its "
   f"state is absent is not a switch")

src_tog = ast.get_source_segment(SRC, tog) or ""
ok("the missing-row case is logged, not silent", "log." in src_tog)
ok("an EXISTING row still decides normally (deliberate 'on' is preserved)",
   "row.base_capital" in src_tog)


print("\n4. the endpoint is still reachable - this is a gate, not a removal")
ROUTER = open("routers/trading_dashboard.py").read()
ok("POST /grid-status/rebalance-flat-branches still exists",
   "rebalance-flat-branches" in ROUTER)
ok("it still calls through to the module",
   "rebalance_flat_grid_branches_now" in ROUTER)


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
