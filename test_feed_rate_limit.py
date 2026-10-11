"""A repeating event does not just add noise to the feed - it EVICTS.

The Live Ops feed is a fixed-size window. Twice in one night a condition
that repeats every cycle filled it end to end: first PARKED_NO_EXIT at 32 of
40 rows, then - after that was removed - PARKED_SELL and PARKED_SELL_NOFILL
from two venue-refused products taking all 40 between them. Both times
GATE_PASS, GATE_BLOCK and CYCLE_ERROR were pushed out entirely, and
CYCLE_ERROR is the only place a LOST FILL is visible at all.

So instrumentation added to reveal a silent failure had twice begun hiding a
different one. These guard the fix: the first occurrence is the news, and a
condition that is merely still true stops rewriting itself for a while.
"""
import ast
import sys

sys.path.insert(0, ".")
import crypto_grid_bot as g  # noqa: E402

_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f"  -- {detail}" if detail and not cond else ""))


g._FEED_LAST_WRITE.clear()
T = 1000.0
ok("the first write for a product is allowed",
   g._feed_should_write("X", "AAA", T))
ok("an immediate repeat is suppressed",
   not g._feed_should_write("X", "AAA", T + 1))
ok("suppression is per PRODUCT, not global",
   g._feed_should_write("X", "BBB", T + 1))
ok("and per VERDICT, so a different event still lands",
   g._feed_should_write("Y", "AAA", T + 1))
ok("it writes again once the window has passed",
   g._feed_should_write("X", "AAA", T + g.FEED_REPEAT_SECONDS + 1))
ok("still suppressed one second BEFORE the window closes",
   not g._feed_should_write("X", "BBB", T + g.FEED_REPEAT_SECONDS - 1))
ok("the window is 15 minutes", abs(g.FEED_REPEAT_SECONDS - 900.0) < 1e-9)

# Instrumentation must never be able to stop a trade, and losing a row is
# worse than writing one too many - so an unreadable clock falls through to
# writing rather than raising or silently dropping.
ok("an unreadable clock does not raise",
   g._feed_should_write("Z", "CCC", float("nan")) in (True, False))


class _Boom:
    def __hash__(self): raise RuntimeError("unhashable")


ok("nor does an argument that cannot even be keyed",
   g._feed_should_write("Z", _Boom(), T) is True)

# ON THE TREE, NOT THE SPELLING. Both durable parked writes must sit behind
# the limiter; a new one added later without it would refill the window.
SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
guarded = set()
for n in ast.walk(TREE):
    if not isinstance(n, ast.If):
        continue
    if "_feed_should_write" not in ast.dump(n.test):
        continue
    for c in ast.walk(n):
        if (isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_record_gate_decision"
                and len(c.args) >= 3 and isinstance(c.args[2], ast.Constant)):
            guarded.add(c.args[2].value)
ok("PARKED_SELL is written behind the limiter", "PARKED_SELL" in guarded,
   f"guarded: {sorted(guarded)}")
ok("PARKED_SELL_NOFILL is written behind the limiter",
   "PARKED_SELL_NOFILL" in guarded, f"guarded: {sorted(guarded)}")

# The three buy-side refusals that repeat every cycle while a coin sits at its
# trigger. EXITING was the one that filled the window: measured 2026-10-11,
# APE-USD wrote 204 of the feed's 500 rows in three and a half hours.
# CASH_RESERVE did the same on 2026-10-10: ZEC-USD, 128 rows in two hours.
for verdict in ("EXITING", "CONCENTRATION", "UNBACKED", "INVENTORY_DEPTH",
                "CASH_RESERVE", "ADOPTED_RUNG"):
    ok(f"{verdict} is written behind the limiter", verdict in guarded,
       f"guarded: {sorted(guarded)}")

# And none of them is written anywhere WITHOUT it - a second, unguarded
# call for the same verdict would refill the window just the same.
_all_calls = [c.args[2].value for c in ast.walk(TREE)
              if isinstance(c, ast.Call)
              and getattr(c.func, "id", "") == "_record_gate_decision"
              and len(c.args) >= 3 and isinstance(c.args[2], ast.Constant)]
_guarded_calls = []
for n in ast.walk(TREE):
    if isinstance(n, ast.If) and "_feed_should_write" in ast.dump(n.test):
        for c in n.body:
            for cc in ast.walk(c):
                if (isinstance(cc, ast.Call)
                        and getattr(cc.func, "id", "") == "_record_gate_decision"
                        and len(cc.args) >= 3 and isinstance(cc.args[2], ast.Constant)):
                    _guarded_calls.append(cc.args[2].value)
for verdict in ("EXITING", "CONCENTRATION", "UNBACKED", "CASH_RESERVE",
                "ADOPTED_RUNG"):
    ok(f"every {verdict} write is guarded",
       _all_calls.count(verdict) == _guarded_calls.count(verdict) >= 1,
       f"{_all_calls.count(verdict)} calls, {_guarded_calls.count(verdict)} guarded")

# The limiter keys on (verdict, product): APE exiting must not silence ZEC.
g._FEED_LAST_WRITE.clear()
ok("one exiting coin does not silence another",
   g._feed_should_write("EXITING", "APE-USD", T)
   and g._feed_should_write("EXITING", "ZEC-USD", T + 1)
   and not g._feed_should_write("EXITING", "APE-USD", T + 60))
g._FEED_LAST_WRITE.clear()

# PARKED_NO_EXIT must stay gone entirely - it is a state, not an event.
ok("PARKED_NO_EXIT is no longer recorded at all",
   '"PARKED_NO_EXIT"' not in SRC.split("def _feed_should_write")[0]
   or "PARKED_NO_EXIT" not in [
       c.args[2].value for c in ast.walk(TREE)
       if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_record_gate_decision"
       and len(c.args) >= 3 and isinstance(c.args[2], ast.Constant)])

# CYCLE_ERROR must NOT be rate limited - a second distinct crash is news.
cyc = [c for c in ast.walk(TREE)
       if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_record_gate_decision"
       and len(c.args) >= 3 and isinstance(c.args[2], ast.Constant)
       and c.args[2].value == "CYCLE_ERROR"]
ok("CYCLE_ERROR is still recorded", len(cyc) >= 1)
ok("and is NOT behind the limiter - every crash is news",
   "CYCLE_ERROR" not in guarded, f"guarded: {sorted(guarded)}")

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
