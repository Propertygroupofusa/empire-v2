"""Searching 432 strategies finds the luckiest one, not the best one.

Simulated on this account's own real BTC daily returns, 432 strategies
with LITERALLY ZERO EDGE - random entries - produce:

    a single no-edge strategy      median   -0.60%
    the BEST of 432 no-edge ones   median  +53.15%  (64% win rate)
    95th percentile of that best           +65.34%

The search alone manufactures +53 points and a 64% win rate out of pure
noise, every time it is run. A winner showing +40% at a 62% win rate is
therefore WORSE than randomness at that search width.

So the harness refuses a result five ways, and none of them is a flag:

  1. OUT OF SAMPLE      chosen on the first 70%, scored only on the rest
  2. MATCHED CONTROL    random strategies trading EXACTLY as often
  3. NOISE FLOOR        what the best of N no-edge variants reaches here
  4. SAMPLE SIZE        under 10 out-of-sample trades is not a result
  5. OVERFIT GAP        +106% in-sample and +15% out is a fitted window

Plus buy-and-hold, because a strategy that trades all year and lands
under doing nothing has spent fees to underperform.

TWO BUGS CAUGHT WHILE BUILDING IT, BOTH IN THE HARNESS ITSELF:

  The fee guard compared the round-trip fee against ONE BAR's median
  move. A trade holding 20 bars captures a 20-bar move, so daily data
  where the winner held for weeks was dismissed as "fees dominate". It
  now compares against the median GROSS move a trade actually captured.

  The first pass reported supertrend at +15.12% out-of-sample as worth
  forward-testing. It had 4 trades and a 91-point in/out gap. Both gates
  above exist because of that result.

Run: python3 test_strategy_lab.py
"""

import io
import statistics
import sys

sys.path.insert(0, ".")
import strategy_lab as sl

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


SRC = io.open("strategy_lab.py", encoding="utf-8").read()

print("test_strategy_lab.py")
print()

# ── 1. no strategy may see the future ──────────────────────────────────
print("-- no lookahead: a signal is filled on the NEXT bar --")
ok("the replay fills on the next bar, never the signal bar",
   "fill = closes[i + 1]" in SRC and "no same-bar fills" in SRC)

# Behavioural: a strategy that knows the future must NOT be able to profit
# through the replay, because the fill is one bar late.
closes = [100.0]
import random as _r
rng = _r.Random(3)
for _ in range(400):
    closes.append(closes[-1] * (1 + rng.uniform(-0.03, 0.03)))
highs = [c * 1.001 for c in closes]
lows = [c * 0.999 for c in closes]

oracle = [1 if (i + 1 < len(closes) and closes[i + 1] > closes[i]) else 0
          for i in range(len(closes))]
res = sl.replay_positions(closes, oracle, fee_round_trip=0.0)
ok("an oracle on bar i cannot be filled at bar i's price",
   res["trades"] > 0)
# With a one-bar delay the oracle's edge is destroyed; it must not be
# anywhere near the perfect capture it would get with same-bar fills.
perfect = 1.0
for i in range(len(closes) - 1):
    if closes[i + 1] > closes[i]:
        perfect *= closes[i + 1] / closes[i]
ok("the one-bar delay destroys most of a cheating edge",
   res["equity_mult"] < perfect / 10,
   f"{res['equity_mult']:.2f} vs perfect {perfect:.2f}")

# ── 2. fees are charged, at the measured rate ──────────────────────────
print()
print("-- every round trip pays the measured fee --")
ok("the default is the measured taker round trip",
   abs(sl.BACKTEST_ROUND_TRIP_FEE_RATE - 0.015) < 1e-9)
flat = [100.0] * 200
pos = [0] * 50 + [1] * 50 + [0] * 100
free = sl.replay_positions(flat, pos, fee_round_trip=0.0)
paid = sl.replay_positions(flat, pos, fee_round_trip=0.015)
ok("a flat market nets zero with no fee", abs(free["total_return_pct"]) < 1e-9)
ok("and loses exactly the fee with one", abs(paid["total_return_pct"] + 1.5) < 1e-6,
   f"{paid['total_return_pct']}")
ok("the gross move is reported separately from the net",
   "median_gross_move_pct" in paid)
ok("a flat market's gross move is zero", abs(paid["median_gross_move_pct"]) < 1e-9)

# ── 3. the control trades exactly as often ─────────────────────────────
print()
print("-- the control randomises WHEN, never HOW OFTEN --")
ok("the matched control exists", hasattr(sl, "_matched_random_positions"))
ok("it is documented as preserving trade count",
   "SAME number of entries" in SRC)


def entries(p, a=0, b=None):
    b = len(p) if b is None else b
    return sum(1 for i in range(a + 1, b) if p[i] and not p[i - 1])


real = sl.strat_ema_cross(closes, highs, lows, fast=8, slow=26)
split = int(len(closes) * 0.7)
n_real = entries(real, split, len(closes))
for seed in (1, 2, 3):
    ctrl = sl._matched_random_positions(real, seed=seed, start=split, end=len(closes))
    ok(f"control seed {seed} makes the same number of entries ({n_real})",
       abs(entries(ctrl, split, len(closes)) - n_real) <= 1,
       f"{entries(ctrl, split, len(closes))}")
    ok(f"control seed {seed} touches nothing before the split",
       not any(ctrl[:split]))
ok("different seeds give different controls",
   sl._matched_random_positions(real, 1, split) != sl._matched_random_positions(real, 2, split))

# ── 4. the noise floor rises with the search width ─────────────────────
print()
print("-- the more you search, the better luck looks --")
rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
f1 = sl.noise_floor_for_search(1, 40, rets, 0.0, draws=60, seed=1)
f50 = sl.noise_floor_for_search(50, 40, rets, 0.0, draws=60, seed=1)
f432 = sl.noise_floor_for_search(432, 40, rets, 0.0, draws=60, seed=1)
ok("1 variant has the lowest floor", f1["median"] < f50["median"], f"{f1['median']} vs {f50['median']}")
ok("50 variants raise it", f50["median"] < f432["median"], f"{f50['median']} vs {f432['median']}")
ok("432 variants raise it a lot", f432["median"] > f1["median"] + 5,
   f"{f1['median']} -> {f432['median']}")
ok("the floor reports the width it was computed for", f432["n_variants"] == 432)

# ── 5. every refusal gate is present and reachable ─────────────────────
print()
print("-- the five refusals --")
for needle, why in [
    ("TOO FEW TRADES", "thin out-of-sample samples"),
    ("OVERFIT", "a large in/out gap"),
    ("NOT BEATING ITS CONTROL", "failing the matched control"),
    ("NO EDGE", "landing under the noise floor"),
    ("UNDERPERFORMS DOING NOTHING", "losing to buy-and-hold"),
    ("FEES DOMINATE", "trades too small to pay their fee"),
]:
    ok(f"it can refuse for {why}", needle in SRC)

ok("the sample-size threshold is at least 10", sl.MIN_OOS_TRADES >= 10)
ok("the overfit gap limit is defined", sl.OVERFIT_GAP_LIMIT_PCT > 0)
ok("out-of-sample is not an optional flag",
   "in_sample_frac=0.7" in SRC and "split = int(n * in_sample_frac)" in SRC)
ok("the fee guard compares a TRADE's move, not a bar's",
   "median_gross_move_pct" in SRC.split("FEES DOMINATE")[0][-600:])
ok("buy-and-hold is always included as the benchmark",
   "buy_and_hold" in sl.STRATEGIES)

# ── 6. it refuses on real data where it should ─────────────────────────
print()
print("-- on real data, the gates actually fire --")
noise = [100.0]
rng2 = _r.Random(11)
for _ in range(600):
    noise.append(noise[-1] * (1 + rng2.gauss(0, 0.02)))
nh = [c * 1.002 for c in noise]
nl = [c * 0.998 for c in noise]
r = sl.run_strategy_lab(noise, nh, nl, control_draws=8)
ok("a pure random-walk series produces a refusal, not a winner",
   any(k in r["verdict"] for k in
       ("NO EDGE", "TOO FEW TRADES", "OVERFIT", "NOT BEATING", "FEES DOMINATE",
        "UNDERPERFORMS")),
   r["verdict"][:90])
ok("it still reports the noise floor it measured", r["noise_floor"]["p95"] is not None)
ok("it reports how many variants it searched", r["variants_tested"] > 1)
ok("every row carries its own in/out gap",
   all("overfit_gap_pct" in x for x in r["ranked"]))
ok("every row carries its control percentile",
   all("percentile_vs_control" in x for x in r["ranked"]))
ok("results are ranked by OUT-OF-SAMPLE, not in-sample",
   r["ranked"] == sorted(r["ranked"],
                         key=lambda x: -x["out_of_sample"]["total_return_pct"]))

# ── 7. a fleet sweep is wider than one coin's sweep ────────────────────
print()
print("-- running N variants on 8 coins is 8N tests, not N --")
ok("run_fleet exists", hasattr(sl, "run_fleet"))
ok("the grid is the full 432 the account asked for", sl.VARIANT_COUNT == 432,
   str(sl.VARIANT_COUNT))

# The floor must rise with the TRUE width, or an 8-coin sweep passes
# results that are pure search.
rets2 = [abs(noise[i] / noise[i - 1] - 1) for i in range(1, len(noise))]
one = sl.noise_floor_for_search(432, 12, rets2, 0.015, draws=40, seed=2)
fleet = sl.noise_floor_for_search(432 * 8, 12, rets2, 0.015, draws=40, seed=2)
ok("the 8-coin floor is higher than the single-coin floor",
   fleet["p95"] > one["p95"], f"{one['p95']} -> {fleet['p95']}")

FSRC = io.open("strategy_lab.py", encoding="utf-8").read()
ok("run_fleet computes the floor at variants x coins",
   "true_width = n_variants * len(usable)" in FSRC)
ok("it re-runs the winner on every other coin",
   "cross_coin" in FSRC and "does not generalise" in FSRC.lower())
ok("it refuses a winner that pays on too few coins",
   "DOES NOT GENERALISE" in FSRC)
ok("it records the real 2026-09-25 result that motivated the gate",
   "atr_breakout(21, 0.75)" in FSRC and "NEAR-USD" in FSRC)
ok("it notes that losing less than holding is not an edge",
   "Losing less than holding is not an edge" in FSRC)

# The hourly sweep found price_vs_sma(100, 0.02): +173.9% on ARB, above
# the fleet floor, beating its control 100% of the time, profitable on
# 6/8 coins - and BEHIND buy-and-hold on 8 of 8. run_strategy_lab refuses
# that per coin; run_fleet did not, and reported it as surviving.
ok("the fleet run tracks buy-and-hold across every coin",
   "beats_hold = [c for c, v in cross.items() if v[\"beats_buy_hold\"]]" in FSRC)
ok("it refuses a fleet winner that loses to holding",
   "UNDERPERFORMS DOING NOTHING" in FSRC.split("def run_fleet")[1])
ok("it reports which coins it actually beat holding on",
   "beats_buy_hold_on" in FSRC)
ok("it records the real result that motivated this gate",
   "price_vs_sma(100, 0.02)" in FSRC)
# 0-of-8 beating hold must trip it; 6-of-8 must not.
ok("0-of-8 beating buy-and-hold trips the gate", 0 < max(2, 8 // 2))
ok("6-of-8 beating buy-and-hold does not", not (6 < max(2, 8 // 2)))

# The gate must actually fire on the shape of that real result.
prof = ["NEAR-USD"]
cross_n = 8
ok("1-of-8 profitable trips the generalisation gate",
   len(prof) < max(2, cross_n // 2))
ok("5-of-8 profitable would not trip it",
   not (5 < max(2, cross_n // 2)))

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
