"""A "locking in gains" exit must not be the biggest loser in the book.

2026-09-25, from the live exit breakdown over the last 20 completed trades:

    PEAK PROFIT GIVEBACK - locking in gains   5   -$28.27   -$5.65/ea
    BRANCH BREACH - forced exit               1   -$26.27
    STOP HIT                                  1   -$25.28
    EQUITY FLOOR BREACH - forced exit         2   -$22.66
    ...
    QUICK PROFIT - real net gain taken fast    6    +$1.79   +$0.30/ea
    TARGET HIT                                 2   +$12.95   +$6.48/ea

The rule named for PROTECTING gains was the single worst exit category.

The cause was one condition:

    if new_peak_pnl_pct > 0 and peak_giveback >= max_giveback_pct:

ANY positive peak armed it, however small. Real lines from the replay:

    gave back 0.95% from a 0.11% peak  ->  closed at -0.84%
    gave back 1.17% from a 0.04% peak  ->  closed at -1.13%
    gave back 0.52% from a 0.01% peak  ->  closed at -0.51%

A position that ticked up 0.01% and fell back was closed at a loss and
logged as locking in gains - BEFORE the stop would have fired. The rule was
converting ordinary noise into realised losses and taking the decision away
from the stop that exists for it.

THE RULE THIS FILE PROTECTS: a giveback exit only arms once there is
something to give back. The peak must clear what a round trip costs; below
that there was never a real gain and the stop owns the exit.

DELIBERATELY NOT CHANGED: should_exit_position_momentum's trailing stop.
Its trail IS the position's only risk control - there is no separate stop
loss in that path - so disarming it at a low peak would leave a losing
position with nothing but the max-hold timer. Same-looking code, opposite
consequence.

Run: python3 test_giveback_arming.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fee_floor as ff

src = open(os.path.join(HERE, "alpaca_mean_reversion.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


mr = ast.unparse(fn("should_exit_position"))

# --- the arming threshold -------------------------------------------------
ok("REGRESSION: the giveback no longer arms on ANY positive peak",
   "new_peak_pnl_pct > 0 and peak_giveback" not in mr)
ok("it arms on a minimum peak instead", "new_peak_pnl_pct >= min_peak_to_arm" in mr)
ok("the threshold is a parameter, overridable per caller",
   "min_peak_to_arm_pct" in src)
ok("it defaults to the shared fee floor", "fee_floor.fee_floor_pct" in mr)
ok("it has a fallback if the floor cannot be imported", "except Exception" in mr)
ok("the fallback still demands a real margin", "ALPACA_ROUND_TRIP_PCT + 0.002" in mr)
ok("the round trip is defined locally so the module stays importable alone",
   "ALPACA_ROUND_TRIP_PCT = 0.0012" in src)
ok("the reason it changed is recorded with the real numbers",
   "0.11% peak" in src and "-$28.27" in src)

# --- the momentum path is deliberately untouched -------------------------
mo = ast.unparse(fn("should_exit_position_momentum"))
ok("REGRESSION: the momentum trailing stop is NOT disarmed",
   "min_peak_to_arm" not in mo)
ok("the momentum trail still fires purely on the trail distance",
   "unrealized_pnl_pct <= trailing_stop_pct" in mo)
ok("the reason it is left alone is documented", "only risk control" in src
   or "no separate stop" in src)


# --- the arithmetic, as behaviour ----------------------------------------
ARM = ff.fee_floor_pct(0.0012)
GIVEBACK = 0.005


def fires_old(peak, gave_back):
    return peak > 0 and gave_back >= GIVEBACK


def fires_new(peak, gave_back):
    return peak >= ARM and gave_back >= GIVEBACK


# The three real loss-making cases from the replay.
LOSSES = [(0.0011, 0.0095), (0.0004, 0.0117), (0.0001, 0.0052)]
for peak, gb in LOSSES:
    ok(f"REGRESSION: peak {peak*100:.2f}% giving back {gb*100:.2f}% no longer fires",
       fires_old(peak, gb) and not fires_new(peak, gb))
    ok(f"  and it would have exited at a LOSS ({(peak-gb)*100:+.2f}%)", peak - gb < 0)

# Real gains must still be protected.
GAINS = [(0.0075, 0.0072), (0.0210, 0.0060), (0.0500, 0.0080)]
for peak, gb in GAINS:
    ok(f"a real {peak*100:.2f}% peak giving back {gb*100:.2f}% STILL fires",
       fires_new(peak, gb))

ok("the rule can never fire on a peak below a round trip",
   not any(fires_new(p, 0.05) for p in (0.0, 0.0005, 0.001, 0.003)))
ok("it still fires on every genuine gain given back past the limit",
   all(fires_new(p, GIVEBACK) for p in (0.004, 0.01, 0.05, 0.2)))
ok("arming is strictly narrower than before - it never fires where the old rule did not",
   all(fires_old(p, g) or not fires_new(p, g)
       for p in (0.0, 0.0001, 0.003, 0.01, 0.05)
       for g in (0.0, 0.004, 0.005, 0.02)))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
