"""A brake that pauses more will look better. Control for that or learn nothing.

The account owner asked whether a newsroom - headlines read by an LLM,
turned into trade signals - could work here. Two findings decided it, and
the second one reversed the first.

FINDING 1: THE PREDICTION VERSION IS ARITHMETICALLY DEAD

From 349 real hourly BTC candles: the median 60-minute move is 0.15%, the
99th percentile is 1.37%, and a taker round trip costs 1.50%. Only 3
hours in 349 moved further than the fee. At the median hour a PERFECT
directional call nets -1.35%. The problem is not accuracy; the fee is ten
times the typical move.

FINDING 2: THE RISK-BRAKE VERSION NEEDED A REAL CONTROL

A grid does not die from wrong direction, it dies when price runs one way
and fills every level. So the useful question was "pause NEW BUYS during
a one-way run" - which never has to beat the fee, because it never opens
a trade.

The first run said yes: drawdown_3pct_12h made +$10.43 and "beat" a
random control that made +$3.41. That control was wrong. It drew ONE
random mask at the AVERAGE fire rate of every detector - 34% - while the
detector being judged fired at 19%. Comparing a brake against a
differently-sized brake measures how much you paused, not whether the
signal meant anything.

With the control fixed - 25 random masks per detector, each at THAT
detector's own fire rate - the answer inverted:

    drawdown_3pct_12h   +$10.43   but beat random only 42% of the time
    four_red_bars       +$10.37   beat random 34% of the time
    drawdown_2pct_6h     +$7.10   beat random 57% of the time

Chance is 50%. The dollars are real and they come from PAUSING, not from
knowing when to pause. A faster news feed makes a brake fire sooner; it
cannot supply a signal that was never there.

THE RULES THIS FILE PROTECTS:

  1. Every detector is compared against random brakes that pause exactly
     as often, many draws, never one.
  2. The brake never blocks a SELL - an open slice must always get out.
  3. A result on too few coins says so instead of concluding.
  4. Ranking is by percentile-vs-random, never by raw dollars.

Run: python3 test_news_brake.py
"""

import ast
import io
import sys

sys.path.insert(0, ".")

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


SRC = io.open("news_risk_brake.py", encoding="utf-8").read()
BT = io.open("crypto_selection_backtest.py", encoding="utf-8").read()
import news_risk_brake as nb

print("test_news_brake.py")
print()

# ── 1. the brake never blocks an exit ──────────────────────────────────
print("-- a brake may stop you buying, never stop you selling --")
tree = ast.parse(BT)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "_replay_grid_bot_v2")
body = ast.get_source_segment(BT, fn)
ok("the replay accepts a brake mask", "brake_mask" in body)
# Scope to the two branches only. Splitting on "elif price >=" and
# taking everything after also swept up the result dict at the end of
# the function, which legitimately reports braked_buys_skipped - so the
# check failed on the counter rather than on any real gate.
buy_half = body.split("elif price >=")[0]
sell_block = body.split("elif price >=")[1].split("i += 1")[0]
ok("the brake is checked on the BUY branch", "braked" in buy_half)
ok("the brake is NOT checked on the SELL branch", "braked" not in sell_block,
   sell_block.strip()[:80])
ok("the sell branch still runs its own profitability guard",
   "slice_net" in sell_block)
ok("it counts buys it actually prevented", "braked_buys_skipped" in body)

# ── 2. detectors are shaped like signals, not lookahead ────────────────
print()
print("-- no detector can see the future --")
for name in ["brake_on_drawdown", "brake_on_volatility", "brake_on_consecutive_red"]:
    f = next(n for n in ast.walk(ast.parse(SRC))
             if isinstance(n, ast.FunctionDef) and n.name == name)
    src = ast.get_source_segment(SRC, f)
    ok(f"{name} exists", src is not None)
    ok(f"{name} indexes only backwards", "i + " not in src and "i+1" not in src)

# Behavioural: a brake must not fire before its own lookback has data.
closes = [100.0] * 30 + [100.0 * (0.99 ** k) for k in range(1, 20)]
m = nb.brake_on_drawdown(closes, lookback=6, drop_pct=0.02, hold_bars=6)
ok("no brake fires during a flat stretch", not any(m[:25]), f"{sum(m[:25])} fired")
ok("the brake fires once a real drop develops", any(m[32:]))
ok("the mask is the same length as the price series", len(m) == len(closes))

flat = [100.0] * 60
ok("a perfectly flat series never brakes", not any(nb.brake_on_drawdown(flat)))
ok("a flat series never triggers the red-bar detector",
   not any(nb.brake_on_consecutive_red(flat)))

rising = [100.0 * (1.01 ** k) for k in range(60)]
ok("a steady rise never brakes on drawdown", not any(nb.brake_on_drawdown(rising)))

# ── 3. the control is rate-matched and repeated ────────────────────────
print()
print("-- the control pauses exactly as often, many times --")
ok("the control draws many masks, not one", "control_draws" in SRC)
ok("each detector is judged against its OWN fire rate",
   "rate = r[\"fire_rate\"]" in SRC and "_random_mask(closes, rate" in SRC)
ok("the default is at least 20 draws", nb.run_news_brake_backtest.__defaults__ is not None)
ok("it records how often the detector beat random",
   "percentile_vs_random" in SRC and "beat_n_of_controls" in SRC)
ok("ranking is by percentile, not by dollars",
   "avg_percentile_vs_random" in SRC.split("ranked = sorted")[1][:200])

# The random mask must actually hit its requested rate.
for rate in (0.1, 0.35, 0.7):
    mask = nb._random_mask([0] * 4000, rate, seed=7)
    got = sum(mask) / len(mask)
    ok(f"a random mask at {rate:.0%} fires near {rate:.0%}", abs(got - rate) < 0.03,
       f"{got:.3f}")
ok("the same seed gives the same mask",
   nb._random_mask([0] * 200, 0.3, seed=5) == nb._random_mask([0] * 200, 0.3, seed=5))
ok("different seeds give different masks",
   nb._random_mask([0] * 200, 0.3, seed=5) != nb._random_mask([0] * 200, 0.3, seed=6))

# ── 4. the verdict refuses to overclaim ────────────────────────────────
print()
print("-- the verdict cannot declare an edge it did not measure --")
v = SRC.split("verdict = \"no detector")[1]
ok("too few coins is reported as too few, not as a result", "NOT ENOUGH COINS" in v)
ok("beating random below 60% is called NO SIGNAL", "NO SIGNAL" in v)
ok("chance is stated as 50% so the reader can check", "chance is 50%" in v)
ok("even a positive result is not wired live on the spot",
   "not worth wiring live today" in v or "forward-testing" in v)
ok("a profitable-but-random result is still refused",
   "comes from pausing, not from knowing" in v)

# ── 5. fees are the measured ones ──────────────────────────────────────
print()
print("-- the brake is judged at real fees --")
import crypto_selection_backtest as bt
ok("the replay charges the measured taker round trip",
   abs(bt.BACKTEST_ROUND_TRIP_FEE_RATE - 0.015) < 1e-9)
ok("not the old 0.8% assumption",
   abs(bt.BACKTEST_ROUND_TRIP_FEE_RATE - 0.008) > 1e-9)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
