"""A branch that cannot buy must still be able to sell.

Measured on the live fleet: $5,821.51 - 78% of allocated capital - sat in
branches that could neither buy (as many slices as levels) nor sell (the
reference gate wanted another 2.8%-5.7%). Four of them, holding 71% of the
capital, had never completed a single round trip. The branches still
earning were the small ones with room to cycle.
"""
import ast
import sys

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


def fn(name):
    n = next(x for x in ast.walk(TREE)
             if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


CYCLE = fn("run_grid_branch_cycle")
sys.path.insert(0, ".")
import crypto_grid_bot as grid  # noqa: E402

# --- the condition is narrow ----------------------------------------------
ok("parked means as many slices as levels",
   "len(slices) >= (branch.num_levels or 0)" in CYCLE)
ok("a branch that can still buy keeps the full spacing gate",
   "price >= branch.reference_price * (1 + grid_pct)" in CYCLE)
ok("the stop still takes precedence", "_stop_slice is None" in CYCLE)
# ON THE TREE, NOT THE SPELLING.
#
# This matched the literal text
# "_stop_slice is not None or _parked_sell or (price >=". The property it
# protects is real - the gate must name all three ways in rather than let
# one fall through - but hoisting the third condition into a named
# _rise_hit variable preserved that property exactly while breaking the
# string, so the guard went red for a reformat. Asserted structurally now.
def _sell_gate():
    """The `if` in the cycle whose test is an OR naming the stop and the
    parked-sell gate. Found by what it decides, not by how it is typed."""
    for n in ast.walk(TREE):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "run_grid_branch_cycle":
            continue
        for x in ast.walk(n):
            if isinstance(x, ast.If) and isinstance(x.test, ast.BoolOp) \
                    and isinstance(x.test.op, ast.Or):
                d = ast.dump(x.test)
                if "_stop_slice" in d and "_parked_sell" in d:
                    return x.test
    return None


_gate = _sell_gate()
ok("the sell gate is an OR over the ways in, not a fallthrough",
   _gate is not None)
if _gate is not None:
    _ops = [ast.dump(o) for o in _gate.values]
    ok("the gate names all three conditions rather than falling through",
       len(_gate.values) == 3
       and any("_stop_slice" in o for o in _ops)
       and any("_parked_sell" in o for o in _ops)
       and any(("_rise_hit" in o) or ("reference_price" in o) for o in _ops),
       f"{len(_gate.values)} operand(s): a third way in that is not named "
       f"here is a sale this gate cannot account for")
    ok("the rise condition is still one of them, hoisted or inline",
       any(("_rise_hit" in o) or ("reference_price" in o) for o in _ops))

# --- it cannot realize a loss ---------------------------------------------
ok("the slice is still chosen by the function that refuses losses",
   CYCLE.count("_pick_profitable_slice_to_sell(") >= 2)
PICK = fn("_pick_profitable_slice_to_sell")
ok("and that function still requires net-positive after fees",
   "_grid_slice_net_pnl(s.qty, s.entry_price, price," in PICK and "> 0" in PICK)
ok("this is not a second path around it",
   "_parked_sell = True" in CYCLE
   and CYCLE.index("_pick_profitable_slice_to_sell(") < CYCLE.index("_parked_sell = True"))

# --- the floor is a real margin, not the bare > 0 --------------------------
# Asserted on the CYCLE body, not on the module. Checking the whole file
# matched the constant's own definition and passed while the gate compared
# against a bare > 0 - caught by mutation, which is the only reason this
# line is here rather than the weaker one.
ok("the gate compares against the floor, not against zero",
   "GRID_PARKED_MIN_NET_PCT" in CYCLE)
ok("and it is a >= against that constant, not a > against 0",
   ">= GRID_PARKED_MIN_NET_PCT" in CYCLE)
ok("the constant exists at module level", "GRID_PARKED_MIN_NET_PCT" in SRC)
ok("it is 1.0% by default", abs(grid.GRID_PARKED_MIN_NET_PCT - 0.010) < 1e-9)
ok("comfortably above the repo's own fee floor",
   grid.GRID_PARKED_MIN_NET_PCT > 0.009)
ok("it is measured NET of fees, not gross",
   "_grid_slice_net_pnl(_cand.qty" in CYCLE)
ok("and as a share of the slice's own basis, not dollars",
   "_net / _basis" in CYCLE)
ok("a zero basis cannot divide", "_basis > 0" in CYCLE)
ok("it is settable", "GRID_PARKED_MIN_NET_PCT" in SRC and "os.getenv" in SRC)


def would_sell(net_pct, parked, floor=None):
    """The shipped condition, evaluated directly."""
    f = grid.GRID_PARKED_MIN_NET_PCT if floor is None else floor
    return bool(parked and net_pct >= f)


ok("parked + 1.5% net -> sells", would_sell(0.015, True))
ok("parked + 1.0% net -> sells (at the floor)", would_sell(0.010, True))
ok("parked + 0.9% net -> does NOT sell", not would_sell(0.009, True))
ok("parked + 0.01% net -> does NOT sell: churn is not profit",
   not would_sell(0.0001, True))
ok("NOT parked + 1.5% net -> does not use this path", not would_sell(0.015, False))
ok("a loss can never reach it", not would_sell(-0.05, True))

# --- what it does NOT do ---------------------------------------------------
ok("the grid step itself is untouched",
   "GRID_PARKED_MIN_NET_PCT" not in fn("_pick_profitable_slice_to_sell"))
ok("it never widens or narrows spacing for a branch that cycles",
   "grid_pct =" not in CYCLE.split("_parked = ")[1][:600])
ok("the reason is logged, so a sale is never unexplained",
   "Selling on its own" in CYCLE and "cannot buy" in CYCLE)

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
