"""What YOUR grid configuration does across simulated price paths.

THIS IS NOT A BACKTEST. No historical prices are used. It runs the grid's
real mechanics - buy a slice a step below the reference, sell the oldest
slice a step above, cap at num_levels, pay the real round-trip fee - over
randomly generated paths, and reports the distribution of outcomes.

What that is good for: showing how the strategy behaves as a function of
volatility and drift, which is a property of the STRATEGY and does not
depend on any particular history.

What it cannot tell you: whether these coins will behave like these paths.
Only real fills answer that.

Every figure below is labelled simulated. Run: python3 grid_scenario_model.py
"""
import random
import statistics

SLICE_USD = 24.16      # $72.47 / 3 levels
LEVELS = 3
STEP = 0.025           # live override: 2.5%
FEE_ROUND_TRIP = 0.010 # taker both legs, the live tier
HOURS = 24 * 7         # one week per path
PATHS = 3000


def run_path(hourly_vol, drift_per_hour, seed):
    """One grid, one price path. Returns (realized, unrealized, round_trips)."""
    rng = random.Random(seed)
    price = 100.0
    reference = price
    held = []                      # (entry_price, qty)
    realized = 0.0
    trips = 0

    for _ in range(HOURS):
        # 12 five-minute steps per hour, so intrabar moves can trigger levels
        for _ in range(12):
            price *= (1 + rng.gauss(drift_per_hour / 12, hourly_vol / (12 ** 0.5)))
            if len(held) < LEVELS and price <= reference * (1 - STEP):
                held.append((price, SLICE_USD / price))
                reference = price
            elif held and price >= held[0][0] * (1 + STEP):
                entry, qty = held.pop(0)
                gross = (price - entry) * qty
                realized += gross - (entry * qty + price * qty) * (FEE_ROUND_TRIP / 2)
                reference = price
                trips += 1

    unrealized = sum(qty * price - entry * qty for entry, qty in held)
    return realized, unrealized, trips


def summarize(label, hourly_vol, drift_per_hour):
    rows = [run_path(hourly_vol, drift_per_hour, s) for s in range(PATHS)]
    realized = [r for r, _, _ in rows]
    total = [r + u for r, u, _ in rows]
    trips = [t for _, _, t in rows]
    pos = sum(1 for t in total if t > 0) / len(total) * 100
    return {
        "label": label,
        "trips_per_day": statistics.mean(trips) / 7,
        "realized_med": statistics.median(realized),
        "total_med": statistics.median(total),
        "total_p10": sorted(total)[int(0.10 * len(total))],
        "total_p90": sorted(total)[int(0.90 * len(total))],
        "pct_positive": pos,
    }


if __name__ == "__main__":
    print("=" * 78)
    print("  GRID SCENARIO MODEL - SIMULATED PRICE PATHS, NOT HISTORICAL DATA")
    print("=" * 78)
    print(f"  one branch: ${SLICE_USD * LEVELS:.2f} across {LEVELS} levels · "
          f"{STEP*100:.1f}% spacing · {FEE_ROUND_TRIP*100:.1f}% round trip")
    print(f"  {PATHS:,} simulated paths of {HOURS//24} days each\n")
    print(f"  {'scenario':<28}{'trips/day':>10}{'realized':>10}{'TOTAL':>9}"
          f"{'p10':>9}{'p90':>9}{'% up':>7}")
    print("  " + "-" * 74)

    scenarios = [
        ("quiet chop  (0.4%/h, flat)",      0.004,  0.0),
        ("normal chop (0.8%/h, flat)",      0.008,  0.0),
        ("volatile    (1.5%/h, flat)",      0.015,  0.0),
        ("drifting up   (0.8%/h, +0.05%)",  0.008,  0.0005),
        ("drifting down (0.8%/h, -0.05%)",  0.008, -0.0005),
        ("trending down (0.8%/h, -0.15%)",  0.008, -0.0015),
    ]
    out = [summarize(*s) for s in scenarios]
    for r in out:
        print(f"  {r['label']:<28}{r['trips_per_day']:>10.2f}"
              f"{r['realized_med']:>10.2f}{r['total_med']:>9.2f}"
              f"{r['total_p10']:>9.2f}{r['total_p90']:>9.2f}{r['pct_positive']:>6.0f}%")

    print("\n  realized = closed round trips only (what a dashboard shows as profit)")
    print("  TOTAL    = realized + unrealized (what the account is actually worth)")
    print("  p10/p90  = the 10th and 90th percentile of TOTAL across paths")
    print("  % up     = share of paths ending net positive\n")

    fleet = [r for r in out if "normal chop" in r["label"]][0]
    print(f"  Scaled to 7 branches in the 'normal chop' case:")
    print(f"    ~{fleet['trips_per_day']*7:.1f} round trips/day · "
          f"median TOTAL ${fleet['total_med']*7:+,.2f}/week · "
          f"{fleet['pct_positive']:.0f}% of paths positive")
