"""Does the scorer's ranking beat not ranking at all? Walk-forward, on real candles.

READ-ONLY AND OFFLINE OF THE ACCOUNT. Public Coinbase candles plus
/grid-status for the branch list and each branch's own step. Places no
order, calls no write endpoint, imports no live bot module.

THE QUESTION, and why the obvious version of it is worthless

shadow_allocator ranks the fleet and records what it WOULD have funded.
Before that ledger has a single day in it, the ranking rule itself can be
tested on history - and it has to be, because the ONLY thing that makes a
ranking worth having is that the branch it puts first does better
afterwards than the branches it puts last.

The worthless version: compute each coin's score over all 60 days, then
check that high scorers earned more over those same 60 days. That is a
restatement, not a test - the score is built from the thing it is being
scored against.

So every evaluation here is strictly walk-forward:

    at time t, the score uses ONLY bars in [t - LOOKBACK, t]
    the outcome uses ONLY bars in (t, t + HORIZON]

and those two sets never touch. A coin's rank at t cannot see one bar of
the window it is graded on.

WHAT IS COMPARED, because "the top pick made money" means nothing on its own

Four portfolios, scored on the identical forward windows:

    TOP 1      fund only the highest-ranked coin
    TOP 3      split across the three highest-ranked
    EQUAL      fund every coin equally - the no-ranking baseline, and the
               one that matters. This is what the fleet does today.
    BOTTOM 3   the three lowest-ranked. If ranking carries information,
               this must be the WORST of the four. A ranking whose bottom
               does as well as its top is noise with an ordering.

A ranking is only worth building if TOP beats EQUAL. Beating BOTTOM is
necessary, not sufficient - any monotone function of volatility does that.

WHAT IS DELIBERATELY LEFT OUT

The live score multiplies by free rungs, because a full branch cannot take
a dollar. Rung occupancy is an ACCOUNT fact, not a market fact, and no
historical record of it exists per bar - so it cannot be replayed and is
excluded here. This file therefore tests the MARKET half of the score
only: fire rate, net per $100, and volatility state. The rung term is real
and it is what made 12 of 21 branches ineligible on 2026-10-07, but it is
a capacity constraint, not a prediction, and mixing the two would let a
capacity fact launder itself into evidence about a signal.

    python3 allocator_backtest.py [lookback_days] [horizon_bars] [stride_bars]
"""
from __future__ import annotations

import statistics as st
import sys

import market_state_study as MSS
import opportunity_allocator as OA

LOOKBACK_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
HORIZON = int(sys.argv[2]) if len(sys.argv) > 2 else 16      # 16 bars = 4h
STRIDE = int(sys.argv[3]) if len(sys.argv) > 3 else 16       # re-rank every 4h
BARS_PER_DAY = 96                                            # 15m bars
CANDLE_DAYS = 60


def precompute(bars, labels, step):
    """grid_forward at EVERY bar, once. Walk-forward windows then become a
    windowed mean over these arrays instead of a quadratic re-simulation -
    the first version re-ran the grid 7.4 million times and would not have
    finished."""
    n = len(bars)
    fired = [0] * n
    net = [0.0] * n
    valid = [False] * n
    for i in range(n - HORIZON - 1):
        if labels[i] is None:
            continue
        trips, v = MSS.grid_forward(bars, i, HORIZON, step)
        fired[i] = 1 if trips else 0
        net[i] = v
        valid[i] = True
    # prefix sums so a trailing window is O(1)
    cf, cn, cv = [0], [0.0], [0]
    for i in range(n):
        cf.append(cf[-1] + fired[i])
        cn.append(cn[-1] + net[i])
        cv.append(cv[-1] + (1 if valid[i] else 0))
    return {"cf": cf, "cn": cn, "cv": cv, "net": net}


def trailing_profile(pre, labels, i, lookback_bars):
    """Fire rate, net per $100 and state at bar i, from bars <= i ONLY.

    The window ends at i - HORIZON so that every window inside it has also
    CLOSED by bar i. A window still open at i would be grading the score on
    bars the score is not allowed to see.
    """
    hi = i - HORIZON + 1
    lo = max(0, i - lookback_bars)
    if hi <= lo:
        return None
    scored = pre["cv"][hi] - pre["cv"][lo]
    if scored < 20 or labels[i] is None:
        return None
    return {"fire_rate": (pre["cf"][hi] - pre["cf"][lo]) / scored,
            "net_per_100": (pre["cn"][hi] - pre["cn"][lo]) / scored,
            "state": labels[i][0]}


def main():
    gs = MSS._get(f"{OA.BASE}/grid-status", timeout=90)
    branches = [(b["product_id"], float(b.get("grid_pct") or 0.03))
                for b in (gs.get("branches") or []) if b.get("product_id")]
    lookback_bars = LOOKBACK_DAYS * BARS_PER_DAY

    print(f"{len(branches)} branches - {CANDLE_DAYS}d of 15m candles - "
          f"score from a trailing {LOOKBACK_DAYS}d window - "
          f"graded on the next {HORIZON * 15 / 60:.0f}h - re-ranked every "
          f"{STRIDE * 15 / 60:.0f}h")
    print("the scoring window and the grading window never overlap\n")

    data = {}
    for pid, step in branches:
        bars, why = MSS.candles(pid, days=CANDLE_DAYS)
        if not bars or len(bars) < lookback_bars + HORIZON + 200:
            print(f"  {pid:11} SKIPPED - {why or f'{len(bars)} bars'}")
            continue
        labels = MSS.classify(bars)
        data[pid] = (bars, labels, step, precompute(bars, labels, step))
        print(f"  {pid:11} {len(bars)} bars")

    if len(data) < 6:
        print("\nnot enough readable coins to rank")
        return 1

    n_bars = min(len(v[0]) for v in data.values())
    start = lookback_bars + 200
    picks = {"TOP 1": [], "TOP 3": [], "EQUAL": [], "BOTTOM 3": []}
    rounds = 0
    top_beat_equal = 0

    for i in range(start, n_bars - HORIZON - 1, STRIDE):
        scored = []
        for pid, (bars, labels, step, pre) in data.items():
            p = trailing_profile(pre, labels, i, lookback_bars)
            if p is None:
                continue
            score = (p["fire_rate"] * p["net_per_100"]
                     * OA.STATE_MULT.get(p["state"], 0.05) * 100.0)
            fwd = pre["net"][i]
            scored.append((score, pid, fwd))
        if len(scored) < 6:
            continue
        scored.sort(key=lambda x: -x[0])
        rounds += 1
        top1 = scored[0][2]
        top3 = st.mean([s[2] for s in scored[:3]])
        equal = st.mean([s[2] for s in scored])
        bot3 = st.mean([s[2] for s in scored[-3:]])
        picks["TOP 1"].append(top1)
        picks["TOP 3"].append(top3)
        picks["EQUAL"].append(equal)
        picks["BOTTOM 3"].append(bot3)
        if top1 > equal:
            top_beat_equal += 1

    if not rounds:
        print("\nno complete walk-forward rounds")
        return 1

    print(f"\n{'=' * 72}")
    print(f"{rounds:,} walk-forward rounds, {len(data)} coins ranked in each\n")
    print(f"  {'portfolio':12}{'mean $/100':>13}{'median':>10}"
          f"{'won':>8}{'total $/100':>14}")
    print("  " + "-" * 57)
    for k in ("TOP 1", "TOP 3", "EQUAL", "BOTTOM 3"):
        v = picks[k]
        won = 100.0 * sum(1 for x in v if x > 0) / len(v)
        print(f"  {k:12}{st.mean(v):>+13.4f}{st.median(v):>+10.4f}"
              f"{won:>7.1f}%{sum(v):>+14.2f}")

    eq = st.mean(picks["EQUAL"])
    t1 = st.mean(picks["TOP 1"])
    t3 = st.mean(picks["TOP 3"])
    b3 = st.mean(picks["BOTTOM 3"])
    diff = [a - b for a, b in zip(picks["TOP 1"], picks["EQUAL"])]
    sd = st.stdev(diff) if len(diff) > 1 else 0.0
    t = (st.mean(diff) / (sd / len(diff) ** 0.5)) if sd else 0.0

    print(f"\n  TOP 1 beat EQUAL in {100.0 * top_beat_equal / rounds:.1f}% of rounds")
    print(f"  mean edge of TOP 1 over EQUAL: {t1 - eq:+.4f} per $100, t = {t:+.2f}")
    print(f"  TOP 1 over BOTTOM 3:           {t1 - b3:+.4f} per $100")
    print(f"  TOP 3 over EQUAL:              {t3 - eq:+.4f} per $100")

    print("\n  VERDICT")
    if abs(t) < 2.0:
        print("    NO MEASURABLE EDGE. |t| < 2, so the difference between")
        print("    ranking and not ranking is inside the noise. On this")
        print("    evidence the ranking should NOT be given capital.")
    elif t >= 2.0 and t1 > eq:
        print("    THE RANKING CARRIES INFORMATION on this window. That is")
        print("    necessary and still not sufficient: one window is one")
        print("    window, and the rung constraint excluded here is what")
        print("    actually binds on the live fleet.")
    else:
        print("    THE RANKING IS BACKWARDS on this window - the top picks")
        print("    did WORSE than funding everything equally. Do not deploy it.")

    print("\n  Capacity, not prediction, is excluded here on purpose - see the")
    print("  module docstring. No capital was moved and this file cannot move any.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
