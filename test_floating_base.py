"""A flat branch must not keep measuring its next buy from a level the
market already left.

reference_price floats on every real fill - buy and sell both write it.
What it cannot do is float when there is no fill, which is exactly the
case that strands capital. Measured on the live fleet 2026-09-26: ONDO
needed a 5.46% dip against its own 2.50% step, TIA 3.81%. 4.27 points of
pure waiting between them.

reanchor_flat_grid_branches_now() fixed that from 2026-09-25, but only
when a human pressed it, so the drift rebuilt. These cover running it on
a schedule without loosening any of its safety.
"""

import ast
import os

import crypto_grid_bot as G

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


SRC = open(G.__file__).read()
TREE = ast.parse(SRC)
DRIVER = next((n for n in ast.walk(TREE)
               if isinstance(n, ast.AsyncFunctionDef)
               and "_last_grid_reanchor_at" in (ast.get_source_segment(SRC, n) or "")), None)
DRIVER_SRC = ast.get_source_segment(SRC, DRIVER) if DRIVER else ""

print("\nthe sweep runs on a schedule, throttled like every other sweep")

ok("a driver actually calls the re-anchor", DRIVER is not None)
ok("it calls the EXISTING function rather than a second copy of the rule",
   "reanchor_flat_grid_branches_now()" in DRIVER_SRC,
   "a second implementation of a money-touching rule is how they drift apart")
ok("it is throttled by its own interval",
   "GRID_REANCHOR_INTERVAL_SECONDS" in DRIVER_SRC)
ok("the throttle timestamp is set BEFORE the await, so a slow sweep "
   "cannot be started twice",
   DRIVER_SRC.index("_last_grid_reanchor_at = now")
   < DRIVER_SRC.index("await reanchor_flat_grid_branches_now()"))
ok("it is wrapped so a failure cannot stop the trading loop",
   "except Exception" in DRIVER_SRC and "re-anchor sweep error" in DRIVER_SRC)
ok("the default interval is an hour, not every cycle",
   G.GRID_REANCHOR_INTERVAL_SECONDS >= 900, G.GRID_REANCHOR_INTERVAL_SECONDS)

print("\nit can be switched off without a code change")

for raw, want in (("false", False), ("FALSE", False), ("0", False), ("no", False),
                  ("off", False), ("true", True), ("", True), ("anything", True)):
    os.environ["GRID_AUTO_REANCHOR"] = raw
    ok(f"GRID_AUTO_REANCHOR={raw!r:>10} -> {want}", G.auto_reanchor_enabled() is want)
os.environ.pop("GRID_AUTO_REANCHOR", None)
ok("unset defaults to ON", G.auto_reanchor_enabled() is True)
ok("and the driver honours the switch", "auto_reanchor_enabled()" in DRIVER_SRC)

print("\nnone of the safety in the underlying function was loosened")

RA = next(n for n in ast.walk(TREE)
          if isinstance(n, ast.AsyncFunctionDef)
          and n.name == "reanchor_flat_grid_branches_now")
RA_SRC = ast.get_source_segment(SRC, RA) or ""

ok("a branch holding open slices is still skipped",
   "if slices:" in RA_SRC and "reference is also its sell trigger" in RA_SRC)
ok("it still only ever moves the reference UPWARD",
   "if old and price <= old:" in RA_SRC,
   "a lower reference means a lower buy trigger - further from a fill, not nearer")
ok("it still writes reference_price and nothing else",
   RA_SRC.count("fresh.") == RA_SRC.count("fresh.reference_price"),
   "spacing, levels, allocation and active flags must stay untouched")
ok("it still places no order",
   "grid_buy" not in RA_SRC and "grid_sell" not in RA_SRC and "place_market" not in RA_SRC)
ok("a branch whose price cannot be read is skipped, not guessed",
   "no live price - not guessing" in RA_SRC)
ok("it re-checks for slices inside the write, against a race",
   "slice opened during re-anchor" in RA_SRC)

print("\nthe status says whether it is running")

STATUS = ast.get_source_segment(SRC, next(
    n for n in ast.walk(TREE)
    if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_grid_status")) or ""
ok("auto_reanchor_active is reported", "auto_reanchor_active" in STATUS)
ok("and so is the interval", "reanchor_interval_minutes" in STATUS)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
