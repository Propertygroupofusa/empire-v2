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
ok("it reads each slice's OWN entry price, not the branch reference",
   '_entry = getattr(_sl, "entry_price", None)' in CYCLE,
   "reference_price moves on every fill; entry is what the slice actually paid")
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
