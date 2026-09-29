"""The stop loss: the one place this bot sells at a loss on purpose.

It is in direct tension with _pick_profitable_slice_to_sell(), which exists
to PREVENT losing sales - written after the account owner caught a DOGE
branch closing -$1.57 across four trades. These tests assert the stop was
added WITHOUT loosening that rule, and that it reuses the proven sell path
rather than carrying its own copy of the order-placement code.
"""

import ast
import re
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
_TREE = ast.parse(SRC)
CYCLE_FN = next(n for n in ast.walk(_TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "run_grid_branch_cycle")
TREE = ast.parse(SRC)


def func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


CYCLE = ast.get_source_segment(SRC, func("run_grid_branch_cycle")) or ""


print("\nthe constant")
ok("GRID_STOP_LOSS_PCT is env-tunable", 'os.getenv("GRID_STOP_LOSS_PCT"' in SRC)
m = re.search(r'GRID_STOP_LOSS_PCT = float\(os\.getenv\("GRID_STOP_LOSS_PCT", "([^"]+)"\)\)', SRC)
ok("defaults to 0.08, the swept best", m is not None and m.group(1) == "0.08",
   f"got {m.group(1) if m else None}")
ok("an out-of-range value fails at import rather than trading",
   re.search(r"GRID_STOP_LOSS_PCT < 0 or GRID_STOP_LOSS_PCT >= 1", SRC) is not None
   and "GRID_STOP_LOSS_PCT must be in" in SRC)


print("\nit fires only BELOW entry - never on a rise")
# The distance is resolved per coin now (adaptive_stop), so the constant
# is no longer the literal in the comparison. The rule it protects is
# unchanged: strictly below entry, by the stop actually in force.
ok("trigger is price <= entry * (1 - stop)",
   "price <= _entry * (1 - _stop_pct)" in CYCLE,
   "a stop that could fire on a rise would dump winners")
ok("no comparison that would let it fire above entry", "price >= _entry" not in CYCLE)
# Asserted on the TREE, not on the source text. The previous version matched
# the literal `_entry = getattr(_sl, "entry_price", None)`, so it broke the
# moment that loop variable was renamed - and it broke for a rename that FIXED
# a live bug (the name `_sl` was shadowing the slice_lifecycle module and
# losing a real BTC-USD buy). A test that fails on a correct rename trains
# people to weaken it. This asserts the RULE instead: whatever the loop
# variable is called, the entry used by the stop comes from that slice's own
# entry_price, and never from the branch's reference_price.
def _stop_loop_entry_source():
    """(attribute read, loop variable name) the stop comparison's entry uses."""
    for loop in [n for n in ast.walk(CYCLE_FN) if isinstance(n, ast.For)]:
        fires_here = any(
            isinstance(c, ast.Compare)
            and ast.unparse(c).replace(" ", "").startswith("price<=_entry*(1-_stop_pct)")
            for c in ast.walk(loop))
        if not fires_here:
            continue
        target = loop.target.id if isinstance(loop.target, ast.Name) else None
        for node in ast.walk(loop):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and getattr(node.targets[0], "id", None) == "_entry"
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "getattr"
                    and len(node.value.args) >= 2
                    and isinstance(node.value.args[1], ast.Constant)):
                src = node.value.args[0]
                return node.value.args[1].value, (src.id if isinstance(src, ast.Name) else None), target
    return None, None, None


_attr, _src_name, _loop_var = _stop_loop_entry_source()
ok("it reads each slice's OWN entry price, not the branch reference",
   _attr == "entry_price" and _src_name is not None and _src_name == _loop_var,
   f"the stop's entry came from {_src_name!r}.{_attr!r} while the loop walks "
   f"{_loop_var!r} - reference_price moves on every fill; entry is what the "
   f"slice actually paid")
ok("a slice with no recorded entry is skipped, never sold",
   "if _entry and price <=" in CYCLE)


print("\nzero disables it")
ok("the whole block is guarded by a positive stop",
   "if _stop_pct > 0 and slices:" in CYCLE)
ok("and the configured constant is still what a failure falls back to",
   "_stop_pct = GRID_STOP_LOSS_PCT" in CYCLE,
   "an error resolving the stop must never leave a slice unprotected")
ok("a resolved stop of zero on an open slice is logged at WARNING",
   "NO STOP" in CYCLE)


print("\nthe never-sell-at-a-loss rule is NOT loosened")
ok("_pick_profitable_slice_to_sell still guards the normal path",
   "_pick_profitable_slice_to_sell(slices, price, real_fee_rate, exit_leg_rate)" in CYCLE)
ok("it still returns early when nothing is profitably sellable",
   "holding every slice, waiting for a genuinely profitable one" in CYCLE)
ok("the stop bypasses it in its own explicit branch, not by relaxing it",
   "if _stop_slice is not None:" in CYCLE)


print("\nit reuses the proven sell path rather than copying it")
ok("exactly ONE grid_sell call in the cycle",
   CYCLE.count("await grid_sell(") == 1,
   f"found {CYCLE.count('await grid_sell(')}; a second copy will drift from the first")
ok("exactly ONE _log_grid_trade call", CYCLE.count("_log_grid_trade(") == 1)
ok("the invented _book_grid_sale helper is gone from the file",
   "_book_grid_sale" not in SRC)


print("\nrealising a loss is loud")
stop_log = re.search(r"log\.(\w+)\([^)]*STOP LOSS", CYCLE, re.S)
ok("the stop-loss sale logs at WARNING",
   stop_log is not None and stop_log.group(1) == "warning",
   f"got log.{stop_log.group(1) if stop_log else 'NONE'} - a realised loss is never an info line")
ok("the log names the actual drop and the stop level",
   "past the" in CYCLE and "GRID_STOP_LOSS_PCT * 100" in CYCLE)


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
