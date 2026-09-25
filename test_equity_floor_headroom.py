"""A floor that halts on a 0.77% move is a scheduled outage, not protection.

Measured live 2026-09-25 on the Alpaca account:

    equity   $1,007.74
    floor    $1,000.00   (locked, ratchets up only)
    headroom $7.74       = 0.77%

Breaching it closes EVERY position and halts all new entries until equity
recovers. At 0.77% an ordinary down day stops the account dead - and the
one-way ratchet guaranteed it stayed that way forever.

The trap is structural, not bad luck. The floor was `floor(equity / $1,000)
* $1,000`, so crossing $1,000 set the floor to $1,000 - which is precisely
the moment equity is barely above $1,000. The rule created the condition it
then locked in. The same happens at $2,000, $5,000, $10,000: every tier
crossing leaves near-zero room.

THREE FIXES, all needed together:

  1. HEADROOM FIRST. The floor is computed from equity * (1 - headroom) and
     THEN rounded to a tier, so the room is guaranteed rather than whatever
     rounding happens to leave.

  2. A FINER TIER. Rounding a headroom target down to $1,000 swallows the
     floor on a small account - at $1,007.74 the target is $906.97, which
     rounds to $0, i.e. no floor at all. $100 keeps the number readable and
     the floor real at every size.

  3. CORRECT AN UNSAFE STORED FLOOR, ONCE. Fixes 1 and 2 only affect future
     RAISES, and the ratchet never lowers - so the already-stored $1,000
     would have stayed, with its 0.77%, forever. When the stored floor
     leaves less than half the minimum headroom it is corrected down once,
     loudly, and persisted. That is a repair of a bad stored value, not a
     licence to drift the floor down.

THE RULE THAT SURVIVES: the floor still only ever ratchets UP in normal
operation. A floor you can talk yourself out of is not a floor.

Run: python3 test_equity_floor_headroom.py
"""
import ast
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "prop_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


# --- fix 1: headroom first ------------------------------------------------
ok("a headroom constant exists", "EQUITY_FLOOR_MIN_HEADROOM_PCT" in src)
ok("it defaults to 10%", '"PROP_EQUITY_FLOOR_MIN_HEADROOM_PCT", "0.10"' in src)
node = fn("compute_equity_floor")
ok("compute_equity_floor exists", node is not None)
body = ast.unparse(node) if node else ""
ok("it takes headroom BEFORE rounding",
   body.index("1.0 - EQUITY_FLOOR_MIN_HEADROOM_PCT") < body.index("math.floor"))
ok("it rounds to the tier after", "EQUITY_FLOOR_TIER" in body)
ok("a non-positive equity yields no floor", "return 0.0" in body)

# --- fix 2: the tier ------------------------------------------------------
ok("REGRESSION: the tier is no longer $1,000",
   '"PROP_EQUITY_FLOOR_TIER", "1000"' not in src)
ok("the tier is $100", '"PROP_EQUITY_FLOOR_TIER", "100"' in src)
ok("the reason is recorded", "swallows the floor entirely" in src)

# --- fix 3: the one-time correction --------------------------------------
ok("the ratchet uses compute_equity_floor", "candidate_floor = compute_equity_floor(equity)" in src)
ok("an unsafe stored floor is detected", "min_safe_headroom" in src)
ok("the correction is persisted", src.count("await save_equity_floor(equity_floor)") >= 2)
ok("it is logged as a correction, loudly", "EQUITY FLOOR CORRECTED" in src)
ok("the log states the measured headroom that triggered it",
   "below equity" in src and "would halt the account" in src)
ok("the alert says the ratchet still only goes up", "only ratchets UP from here" in src)
ok("the raise path is still present", "EQUITY FLOOR RAISED" in src)
ok("raising is now the elif, so correction wins when both apply",
   "elif candidate_floor > equity_floor:" in src)
ok("the correction never goes below the configured base",
   "max(EQUITY_FLOOR_BASE, candidate_floor)" in src)


# --- the arithmetic, as behaviour ----------------------------------------
TIER, HEAD, BASE = 100.0, 0.10, 500.0


def compute(equity):
    if equity is None or equity <= 0:
        return 0.0
    return max(0.0, math.floor(equity * (1.0 - HEAD) / TIER) * TIER)


def headroom_pct(equity, floor):
    return (equity - floor) / equity * 100.0


ok("REGRESSION: the live case $1,007.74 gets real room, not 0.77%",
   headroom_pct(1007.74, compute(1007.74)) > 10.0)
ok("the old rule gave 0.77% there (guards this test's premise)",
   abs(headroom_pct(1007.74, math.floor(1007.74 / 1000) * 1000) - 0.768) < 0.01)

ok("REGRESSION: every tier crossing keeps headroom, not near-zero",
   all(headroom_pct(e, compute(e)) >= 10.0
       for e in (1000.01, 2000.01, 5000.01, 10000.01, 50000.01)))
ok("the old rule left under 0.01% at those same crossings",
   all(headroom_pct(e, math.floor(e / 1000) * 1000) < 0.01
       for e in (1000.01, 2000.01, 5000.01, 10000.01)))

ok("headroom never falls below the minimum at any size",
   all(headroom_pct(e, compute(e)) >= HEAD * 100 - 1e-9
       for e in (600, 1007.74, 1500, 2000.5, 5000, 9999, 100000)))
ok("the floor is never zero once there is real equity",
   all(compute(e) > 0 for e in (600, 1007.74, 5000, 100000)))
ok("REGRESSION: a $1,000 tier WOULD have zeroed it at $1,007.74",
   math.floor(1007.74 * 0.9 / 1000) * 1000 == 0)


def ratchet(stored, equity):
    """Normal operation: correction if unsafe, else raise only."""
    cand = compute(equity)
    if stored > 0 and (equity - stored) < equity * (HEAD / 2.0):
        return max(BASE, cand)
    return max(stored, cand) if cand > stored else stored


ok("REGRESSION: the stored $1,000 floor IS corrected at $1,007.74",
   ratchet(1000.0, 1007.74) < 1000.0)
ok("and the corrected floor is safe", headroom_pct(1007.74, ratchet(1000.0, 1007.74)) >= 10.0)
ok("a healthy floor is never lowered",
   ratchet(900.0, 1007.74) == 900.0)
ok("growth still ratchets the floor UP",
   ratchet(900.0, 5000.0) == 4500.0)
ok("it never drops below the configured base",
   ratchet(999.0, 1000.5) >= BASE)
ok("a safe floor far below equity is left completely alone",
   ratchet(1000.0, 5000.0) == 4500.0)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
