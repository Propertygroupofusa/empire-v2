"""Sizing decides how much of an edge you keep, and how much pain you take
collecting it. It cannot create one.

WHY THIS EXISTS

Every figure this lab produced was full-equity: each trade risks the whole
balance and each win compounds into the next position. Nothing is traded
that way, and the distortion runs in a specific direction - full-equity
compounding rewards a long unbroken win streak far more than it punishes the
drawdown that follows, so a ranking built on it tilts toward whatever was
luckiest in SEQUENCE rather than whatever had the best edge.

It also makes both halves of the headline wrong at once. The return is the
best case and the drawdown is the worst case, and they are the same number
seen twice.

THE THREE THINGS THAT MUST HOLD

  1. The default cannot move. Every verdict already recorded was computed at
     full equity; if adding sizing changed those numbers, the record would
     silently rewrite itself.
  2. Sizing must look BACKWARD only. A sizing rule that peeks forward is the
     same lookahead bug as a strategy that does, and it is harder to catch
     because it moves the size rather than the signal.
  3. Volatility targeting must actually take less risk when the market is
     violent. That is the entire claim, and it is measurable.

AND THE ONE THING IT MUST NOT CLAIM

Sizing does not turn a losing strategy into a winner. A negative edge, sized
beautifully, still loses - it just takes longer. Any result suggesting
otherwise is an arithmetic error, so this file checks the sign survives.

Run: python3 test_position_sizing.py
"""
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import strategy_lab as LAB  # noqa: E402

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def make(seed=21, n=900, calm=0.015, wild=0.05, drift=0.0007):
    """A series with a deliberately violent stretch in the middle."""
    r = random.Random(seed)
    c = [100.0]
    for i in range(n):
        v = wild if 300 < i < 450 else calm
        c.append(max(0.01, c[-1] * (1 + r.gauss(drift, v))))
    return c, [x * 1.006 for x in c], [x * 0.994 for x in c]


closes, highs, lows = make()
pos = LAB.strat_sma_cross(closes, highs, lows, 20, 50)

# --- 1. the default cannot move -------------------------------------------
base = LAB.replay_positions(closes, pos)
explicit = LAB.replay_positions(closes, pos, sizing="full")
ok("the default is still full-equity", base["sizing"] == "full")
ok("REGRESSION: default and explicit 'full' are identical, so no recorded "
   "verdict is silently rewritten",
   base["total_return_pct"] == explicit["total_return_pct"]
   and base["max_drawdown_pct"] == explicit["max_drawdown_pct"]
   and base["trades"] == explicit["trades"])

# --- 2. sizing scales risk AND reward, together ---------------------------
quarter = LAB.replay_positions(closes, pos, sizing="fixed_fraction", fraction=0.25)
ok("a fraction takes the same trades, not fewer", quarter["trades"] == base["trades"])
ok("a quarter of the equity moves the result toward zero, not toward profit",
   abs(quarter["total_return_pct"]) < abs(base["total_return_pct"]))
ok("and shrinks the drawdown with it - risk and reward move together",
   quarter["max_drawdown_pct"] <= base["max_drawdown_pct"])
ok("the sign of the edge survives sizing - sizing cannot rescue a loser",
   (quarter["total_return_pct"] >= 0) == (base["total_return_pct"] >= 0))
ok("every result says how it was sized, so two modes never share a column",
   base["sizing_detail"] and quarter["sizing_detail"]
   and base["sizing_detail"] != quarter["sizing_detail"])

# --- 3. volatility targeting really does back off in violent markets ------
calm_sizes = [LAB._position_fraction(closes, i, "vol_target", 0.25, 0.015, 20)
              for i in range(100, 280)]
wild_sizes = [LAB._position_fraction(closes, i, "vol_target", 0.25, 0.015, 20)
              for i in range(330, 440)]
ok("volatility targeting takes materially LESS size in the violent stretch",
   statistics.mean(wild_sizes) < statistics.mean(calm_sizes) * 0.6)
ok("and never more than the cap, however quiet the market gets",
   all(f <= LAB.MAX_POSITION_FRACTION for f in calm_sizes + wild_sizes))
ok("and never negative", all(f >= 0 for f in calm_sizes + wild_sizes))

# --- 4. no lookahead ------------------------------------------------------
# The size at bar i must depend only on bars <= i. Rewriting the FUTURE must
# not change it; rewriting the PAST must.
future = list(closes)
for j in range(500, len(future)):
    future[j] = future[j] * 3.0
ok("REGRESSION: size at a bar is unchanged by anything after it",
   LAB._position_fraction(closes, 400, "vol_target", 0.25, 0.015, 20)
   == LAB._position_fraction(future, 400, "vol_target", 0.25, 0.015, 20))

past = list(closes)
for j in range(385, 401):
    past[j] = past[j] * (1 + 0.2 * (1 if j % 2 else -1))
ok("but it DOES respond to the bars before it (or it is reading nothing)",
   LAB._position_fraction(closes, 400, "vol_target", 0.25, 0.015, 20)
   != LAB._position_fraction(past, 400, "vol_target", 0.25, 0.015, 20))

ok("with no volatility estimate yet it falls back to the fraction, never to "
   "full equity - an unknown risk is not a small one",
   LAB._position_fraction(closes, 1, "vol_target", 0.25, 0.015, 20) == 0.25)

# --- 5. the fee check still reads a PRICE MOVE ----------------------------
# median_gross_move_pct feeds the "FEES DOMINATE" refusal. It has to stay the
# raw move a trade captured; if sizing scaled it, a 10%-sized strategy would
# look like its trades had shrunk and the refusal would fire on strategies it
# should pass - or stop firing on ones it should refuse.
tenth = LAB.replay_positions(closes, pos, sizing="fixed_fraction", fraction=0.10)
ok("REGRESSION: the gross move a trade captured is not scaled by position size",
   abs(tenth["median_gross_move_pct"] - base["median_gross_move_pct"]) < 1e-9)

# --- 6. the anatomy still describes the same trades ------------------------
ok("break-even win rate is a RATIO, so sizing does not move it",
   abs((quarter["break_even_win_rate_pct"] or 0)
       - (base["break_even_win_rate_pct"] or 0)) < 0.2)
ok("nor the win rate - sizing changes how much, never which trades",
   quarter["win_rate"] == base["win_rate"])

ok("the mode list is what the code accepts",
   set(LAB.SIZING_MODES) == {"full", "fixed_fraction", "vol_target"})

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
