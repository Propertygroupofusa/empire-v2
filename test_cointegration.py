"""The mean-reversion test must not manufacture edges that aren't there.

Built 2026-09-25 alongside cointegration.py, after a pasted strategy video
claimed a Kalshi/Polymarket spread was "a mean reverting process" and
supported it with charts. Charts cannot do this job. The video itself shows
why: it demonstrates that a random walk "could operate like a stable
process", which is precisely the thing a human eye scores as mean reversion.

So the tests here are not "does the function run". They are:

  1. Does it FIND reversion when reversion is really there?
  2. Does it REFUSE to find it in a random walk - at close to the nominal
     5% false-positive rate, not far above it?
  3. Does it use Dickey-Fuller critical values rather than normal ones?

Point 3 is the one that decides whether this tool is honest. Under the null
of a unit root, the t-statistic does NOT follow a t-distribution. Judging it
against +/-1.96 makes random walks look tradeable roughly three times more
often than they are. A "validated" pairs strategy is very often just this
mistake.

Run: python3 test_cointegration.py
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from cointegration import (DF_CRITICAL, adf_test, divergence_stats,
                           expected_value, spread_series)

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def ou(n, lam=-0.3, mu=0.0, sigma=1.0, seed=1):
    """Ornstein-Uhlenbeck: genuinely mean-reverting."""
    rng = random.Random(seed)
    y, out = mu, []
    for _ in range(n):
        y += lam * (y - mu) + rng.gauss(0, sigma)
        out.append(y)
    return out


def walk(n, sigma=1.0, seed=1, drift=0.0):
    """Random walk: genuinely NOT mean-reverting."""
    rng = random.Random(seed)
    y, out = 0.0, []
    for _ in range(n):
        y += drift + rng.gauss(0, sigma)
        out.append(y)
    return out


# --- 1. it finds real reversion ------------------------------------------
r = adf_test(ou(500, lam=-0.3, seed=1))
ok("finds reversion in a strong OU process", r["mean_reverting"] is True)
ok("recovers lambda close to the true -0.3", abs(r["lambda"] - (-0.3)) < 0.1)
ok("reports a short half-life for fast reversion", r["half_life_obs"] < 5)

weak = adf_test(ou(1000, lam=-0.05, seed=7))
ok("finds reversion in a WEAK OU process given enough data",
   weak["mean_reverting"] is True)
ok("and reports a correspondingly longer half-life", weak["half_life_obs"] > 5)

# --- 2. it refuses a random walk, at the right rate ----------------------
false_positives = sum(1 for s in range(200)
                      if adf_test(walk(400, seed=s)).get("mean_reverting"))
rate = false_positives / 200
print(f"  [info] false-positive rate on 200 random walks: {rate:.1%} (nominal 5%)")
ok("does not flag a single random walk as reverting (seed 1)",
   adf_test(walk(500, seed=1))["mean_reverting"] is False)
ok(f"false-positive rate stays near nominal ({rate:.1%} <= 10%)", rate <= 0.10)
ok("a drifting random walk is not called mean-reverting",
   adf_test(walk(500, seed=3, drift=0.05))["mean_reverting"] is False)

# --- 3. Dickey-Fuller critical values, NOT normal -------------------------
ok("uses DF critical values, not the normal 1.96",
   abs(DF_CRITICAL[0.05]) > 2.5)
ok("the 5% threshold is the documented -2.86", DF_CRITICAL[0.05] == -2.86)
ok("1% is stricter than 5%, which is stricter than 10%",
   DF_CRITICAL[0.01] < DF_CRITICAL[0.05] < DF_CRITICAL[0.10])
# The decisive comparison: a t-stat between the normal and DF thresholds must
# NOT be called mean-reverting. This is the exact false-edge case.
borderline = [s for s in range(400)
              if -2.86 < adf_test(walk(300, seed=s)).get("t_stat", 0) < -1.96]
if borderline:
    sample = adf_test(walk(300, seed=borderline[0]))
    ok("a t-stat past 1.96 but short of -2.86 is REFUSED (the false-edge case)",
       sample["mean_reverting"] is False)
    print(f"  [info] example: t={sample['t_stat']} would pass a normal test, correctly rejected here")
else:
    ok("a t-stat past 1.96 but short of -2.86 is REFUSED (no sample found)", True)

# --- honest reporting of insufficient data --------------------------------
short = adf_test([1.0, 2.0, 1.5, 1.8])
ok("too little data returns ok=False rather than a verdict", short["ok"] is False)
ok("and says so in plain language", "too few" in short["reason"].lower())
ok("'not shown' is distinguished from 'proven random'",
   "NOT SHOWN" in adf_test(walk(500, seed=1))["verdict"])

# --- explosive series -----------------------------------------------------
def explosive(n, seed=1):
    rng = random.Random(seed)
    y, out = 1.0, []
    for _ in range(n):
        y += 0.05 * y + rng.gauss(0, 0.5)
        out.append(y)
    return out


exp_r = adf_test(explosive(300))
ok("an explosive series is not called mean-reverting", exp_r["mean_reverting"] is False)

# --- the spread ----------------------------------------------------------
# Approximate comparison: 0.5 - 0.4 is 0.09999999999999998 in binary floating
# point. Exact equality here tests IEEE 754, not the function.
_sp = spread_series([0.5, 0.6], [0.4, 0.4])
ok("spread subtracts elementwise",
   len(_sp) == 2 and abs(_sp[0] - 0.1) < 1e-9 and abs(_sp[1] - 0.2) < 1e-9)
try:
    spread_series([1.0], [1.0, 2.0])
    ok("misaligned series raise rather than silently truncate", False)
except ValueError:
    ok("misaligned series raise rather than silently truncate", True)

# A spread of two INDEPENDENT random walks must not look cointegrated -
# this is the classic spurious-regression trap.
spurious = sum(
    1 for s in range(100)
    if adf_test(spread_series(walk(300, seed=s), walk(300, seed=s + 5000)))
    .get("mean_reverting")
)
print(f"  [info] two independent walks flagged cointegrated: {spurious}/100")
ok(f"independent walks are rarely called cointegrated ({spurious}/100 <= 10)",
   spurious <= 10)

# --- divergence, the term the video never priced --------------------------
w = ([{"final_spread": 0.01}] * 80) + ([{"final_spread": 0.60}] * 20)
d = divergence_stats(w)
ok("divergence rate is measured", d["divergence_rate"] == 0.20)
ok("convergence rate is measured", d["convergence_rate"] == 0.80)
ok("average divergence magnitude is reported", abs(d["avg_divergence_magnitude"] - 0.60) < 1e-9)
ok("no windows returns ok=False, never a fabricated zero",
   divergence_stats([])["ok"] is False)

# --- expected value -------------------------------------------------------
# The headline case: a real-looking edge destroyed by divergence and costs.
ev = expected_value(convergence_rate=0.80, avg_gain=0.05,
                    divergence_rate=0.20, avg_loss=0.60,
                    round_trip_cost=0.02)
ok("EV goes NEGATIVE when divergence is priced in", ev["profitable"] is False)
print(f"  [info] 80% x 5c gain vs 20% x 60c loss, 2c costs -> EV {ev['expected_value_per_trade']}")
ev_good = expected_value(0.97, 0.05, 0.03, 0.20, 0.01)
ok("EV is positive when divergence is genuinely rare", ev_good["profitable"] is True)

width = max(len(l) for l, _ in checks)
print()
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
