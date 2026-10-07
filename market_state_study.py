"""Does labelling a coin's market state tell you anything the grid can use?

READ-ONLY AND OFFLINE OF THE ACCOUNT. Public Coinbase candles plus the
fleet's own /grid-status for the branch list. It places no order, calls no
write endpoint, imports no live bot module, and changes no configuration.
Running it cannot move a dollar. Nothing here is imported by anything that
trades.

THE ASK, in the account owner's words, 2026-10-07: the fleet should
"extract opportunity from the movement regardless of direction" - detect
whether a coin is DORMANT, ACTIVE, VOLATILE or in MOMENTUM, and let that
state drive the branch instead of twenty-one identical machines waiting on
the same three rungs.

THIS FILE DOES NOT BUILD THAT. It asks the one question that has to be
answered before any of it is worth writing:

    GIVEN a coin's state at a moment, does the grid earn more over the
    NEXT few hours than it earns from a coin in a different state?

If the answer is no, a state engine is a more elaborate way to make the
same money, and the honest move is to not build it.

WHAT IS ALREADY MEASURED, so it is not re-litigated here
---------------------------------------------------------------------
Three parts of the proposal have been tested on this fleet's real candles
and they did not survive. They are stated up front because a state engine
that quietly reintroduces them inherits their results.

  REDUCING ENTRIES IN A DOWNTREND is a trend filter, and a trend filter
  was measured: ungated +$123.95 against SMA20>SMA50 gated +$13.81, over
  the identical arithmetic with only the `allow` callback changed. It
  removes 89% of the P&L, because a grid earns from the chop it sits out.
  "Preserve cash in a bearish regime" is the same instruction with a
  different name, and it has to clear that result before it is built.

  CAPITAL FOLLOWING OPPORTUNITY between branches was measured: +$40 to
  +$79 in sample, LOST $88 to $111 out of sample, the yield-weighted
  version worst of all. Concentration capping at 20% was in that test.

  A BETTER ENTRY FILTER was measured across seven recorded features:
  best |rho| 0.167 against a 0.211 significance threshold at n=87. None
  of them predicted the outcome.

ONE PART DID SURVIVE, and it is the reason this file exists. In
rotation_study.py, ranking branches by LAST MONTH'S FILL COUNT beat
ranking them by last month's P&L two to one. Fill count is not a profit
forecast - it is a volatility proxy. That is the owner's instinct about
movement, already carrying evidence, and it is the hypothesis under test.

WHAT IS MEASURED HERE
---------------------------------------------------------------------
For every 15-minute bar in the window, from information available AT THAT
BAR ONLY:

  CLASSIFY   realised volatility against the coin's own 30-day median
             (DORMANT / ACTIVE / VOLATILE), crossed with trend direction
             from EMA20 against EMA50 (BEAR / FLAT / BULL). Thresholds are
             fixed below, chosen before any scoring ran, and never fitted
             to the result - a threshold tuned on the answer is a
             selection, not a test.

  SCORE      replay a plain 3-rung grid forward over the next HORIZON
             bars, starting flat, and count the round trips it COMPLETES
             net of the real 0.70% maker round trip. Dollars per $100 of
             deployed capital, which is the fleet's own unit.

Classification uses bars up to i. Scoring uses bars after i. No scored bar
is ever read by the classifier, so the separation is structural rather
than a promise.

WHAT THIS CANNOT MEASURE, stated rather than hidden
---------------------------------------------------------------------
The owner's feature list includes spread, net_edge, available and locked
inventory, working buy and sell orders, time since last fill, and time
since the last profitable close. NONE of those can be scored historically:
the account keeps no bar-by-bar record of them, so there is nothing to
replay. They are live-only inputs. A shadow recorder can log them going
forward, and in ~30 days they can be tested the same way. Until then, any
claim about them is an opinion.

Volume comes from the candle feed and is kept. It is exchange volume on
one venue, not the consolidated tape.

    python3 market_state_study.py [days] [horizon_bars]
    python3 market_state_study.py 90 16
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
import urllib.request
from datetime import datetime

BASE = "https://empire-v2-production.up.railway.app/api/trading-dashboard"
GRAN = 900                       # 15-minute bars
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
HORIZON = int(sys.argv[2]) if len(sys.argv) > 2 else 16   # 16 bars = 4 hours

FEE_LEG = 0.0035                 # live maker tier, per leg
FEE_RT = FEE_LEG * 2             # 0.70% round trip, what is actually billed
STEP = 0.03                      # fallback only; each branch's own step is
                                 # read from /grid-status and used instead
LEVELS = 3                       # the live cap
SLICE_USD = 100.0                # report in dollars per $100 of claim

# Thresholds fixed BEFORE any scoring ran. Do not tune these to the output.
VOL_DORMANT = 0.60               # realised vol below 0.60x the coin's median
VOL_VOLATILE = 1.40              # above 1.40x the median
TREND_FLAT = 0.0015              # |EMA20/EMA50 - 1| under this is FLAT

# A User-Agent is load-bearing. urllib defaults to "Python-urllib/3.x" and
# the candles endpoint answers that with 403 while serving curl a 200.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _get(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


CACHE_DIR = os.environ.get("MSS_CACHE", "/tmp/mss-candles")


def candles(product_id, days=DAYS, gran=GRAN, pause=0.34):
    """Oldest-first (ts, low, high, close, volume), deduplicated.

    The timestamp format is load-bearing: a literal "+" in a query string
    decodes as a space, so an isoformat() stamp returns an empty window.
    The endpoint wants a bare UTC stamp with a trailing Z.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, f"{product_id}-{days}d-{gran}s.json")
    if os.path.exists(cpath) and time.time() - os.path.getmtime(cpath) < 86400:
        try:
            return [tuple(r) for r in json.load(open(cpath))], None
        except Exception:                             # noqa: BLE001
            pass
    rows, now = {}, int(time.time())
    end, floor, err = now, now - days * 86400, None
    while end > floor:
        start = max(floor, end - 300 * gran)
        url = (f"https://api.exchange.coinbase.com/products/{product_id}/candles"
               f"?granularity={gran}"
               f"&start={datetime.utcfromtimestamp(start).isoformat()}Z"
               f"&end={datetime.utcfromtimestamp(end).isoformat()}Z")
        try:
            data = _get(url, timeout=30)
        except Exception as exc:                      # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            data = None
        if not data:
            if err is None:
                err = "empty window"
            break
        for c in data:
            # Coinbase: [time, low, high, open, close, volume]
            rows[int(c[0])] = (int(c[0]), float(c[1]), float(c[2]),
                               float(c[4]), float(c[5]))
        end = min(int(c[0]) for c in data) - gran
        time.sleep(pause)
    out = [rows[k] for k in sorted(rows)]
    if out:
        try:
            json.dump(out, open(cpath, "w"))
        except Exception:                             # noqa: BLE001
            pass
    return out, (err if not out else None)


def _ema(values, span):
    """EMA at every index, seeded on the first value. None until warm."""
    k, out, cur = 2.0 / (span + 1.0), [], None
    for i, v in enumerate(values):
        cur = v if cur is None else v * k + cur * (1.0 - k)
        out.append(cur if i >= span else None)
    return out


def classify(bars):
    """A (vol_state, trend_state) label for every bar, from bars <= i only."""
    closes = [b[3] for b in bars]
    highs, lows = [b[2] for b in bars], [b[1] for b in bars]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)

    # True range per bar, then a 16-bar realised-volatility window.
    tr = [0.0]
    for i in range(1, len(bars)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])) / closes[i - 1])
    atr = [None] * len(bars)
    for i in range(16, len(bars)):
        atr[i] = st.mean(tr[i - 15:i + 1])

    labels = [None] * len(bars)
    for i in range(len(bars)):
        if atr[i] is None or ema50[i] is None:
            continue
        # The coin's own median ATR over everything seen SO FAR - never
        # the whole-window median, which would read the future.
        seen = [a for a in atr[16:i + 1] if a]
        if len(seen) < 96:                            # need ~1 day of history
            continue
        med = st.median(seen)
        if med <= 0:
            continue
        ratio = atr[i] / med
        vol = ("DORMANT" if ratio < VOL_DORMANT
               else "VOLATILE" if ratio > VOL_VOLATILE else "ACTIVE")
        drift = ema20[i] / ema50[i] - 1.0
        trend = ("BULL" if drift > TREND_FLAT
                 else "BEAR" if drift < -TREND_FLAT else "FLAT")
        labels[i] = (vol, trend)
    return labels


def grid_forward(bars, i, horizon, step=STEP):
    """Round trips a flat 3-rung grid completes over bars i+1 .. i+horizon.

    Starts FLAT at bar i's close, so every state is scored on the same
    footing and no state inherits inventory from another. Fills are taken
    on the bar's low/high, which is the optimistic side; the same optimism
    applies to every state, so the COMPARISON between states survives it
    even though the absolute level does not.
    """
    ref = bars[i][3]
    rungs = [ref * (1.0 - step * (n + 1)) for n in range(LEVELS)]
    open_at, trips, gross = [None] * LEVELS, 0, 0.0
    for j in range(i + 1, min(i + 1 + horizon, len(bars))):
        lo, hi = bars[j][1], bars[j][2]
        for n in range(LEVELS):
            if open_at[n] is None and lo <= rungs[n]:
                open_at[n] = rungs[n]
            elif open_at[n] is not None:
                target = open_at[n] * (1.0 + step)
                if hi >= target:
                    gross += (target / open_at[n] - 1.0)
                    trips += 1
                    open_at[n] = None
    net = (gross - trips * FEE_RT) * SLICE_USD
    return trips, net


def main():
    gs = _get(f"{BASE}/grid-status", timeout=90)
    # Each branch's OWN step, not one number for all of them. The live
    # fleet runs 1.139% on BTC and 3.000% on TIA; simulating every coin at
    # 3% makes the tight ones look dead when they are not.
    branches = [(b["product_id"], float(b.get("grid_pct") or STEP))
                for b in (gs.get("branches") or []) if b.get("product_id")]
    print(f"{len(branches)} branches - {DAYS}d of {GRAN // 60}m candles - "
          f"horizon {HORIZON} bars ({HORIZON * GRAN // 3600}h) - "
          f"each branch's own step x {LEVELS} rungs - "
          f"fee {FEE_RT * 100:.2f}% RT")
    print("classification reads bars <= i, scoring reads bars > i\n")

    pooled, percoin = {}, {}
    for pid, step in branches:
        bars, why = candles(pid)
        if not bars or len(bars) < 200:
            print(f"  {pid:11} SKIPPED - {why or f'{len(bars)} bars'}")
            continue
        labels = classify(bars)
        for i, lab in enumerate(labels):
            if lab is None or i + 1 + HORIZON > len(bars):
                continue
            trips, net = grid_forward(bars, i, HORIZON, step)
            for key in (lab, (lab[0], "ANY"), ("ANY", lab[1])):
                d = pooled.setdefault(key, {"n": 0, "trips": 0,
                                            "net": [], "any": 0})
                d["n"] += 1
                d["trips"] += trips
                d["net"].append(net)
                if trips:
                    d["any"] += 1
            c = percoin.setdefault(pid, {})
            cd = c.setdefault(lab[0], {"n": 0, "net": 0.0, "any": 0})
            cd["n"] += 1
            cd["net"] += net
            if trips:
                cd["any"] += 1
        print(f"  {pid:11} {len(bars):>6} bars scored at step "
              f"{step * 100:.3f}%")

    def show(title, keys):
        print(f"\n{title}")
        print(f"  {'state':22}{'moments':>9}{'trips/mo':>10}"
              f"{'fired%':>9}{'net $/100':>12}{'median':>10}")
        print("  " + "-" * 70)
        for k in keys:
            d = pooled.get(k)
            if not d or not d["n"]:
                continue
            name = f"{k[0]}/{k[1]}"
            print(f"  {name:22}{d['n']:>9,}{d['trips'] / d['n']:>10.3f}"
                  f"{100.0 * d['any'] / d['n']:>8.1f}%"
                  f"{st.mean(d['net']):>+12.3f}{st.median(d['net']):>+10.3f}")

    print("\n" + "=" * 72)
    show("BY VOLATILITY STATE - the hypothesis under test",
         [("DORMANT", "ANY"), ("ACTIVE", "ANY"), ("VOLATILE", "ANY")])
    show("BY TREND STATE - the trend filter, re-asked",
         [("ANY", "BEAR"), ("ANY", "FLAT"), ("ANY", "BULL")])
    show("THE FULL CROSS", [(v, t)
                            for v in ("DORMANT", "ACTIVE", "VOLATILE")
                            for t in ("BEAR", "FLAT", "BULL")])

    # THE CRITERION THIS FILE SET ITSELF. A pooled VOLATILE win can come
    # from a handful of coins that are simply more volatile than the rest,
    # which would make the state a proxy for the coin and buy nothing. The
    # only way to tell is WITHIN each coin.
    print("\n" + "=" * 72)
    print("WITHIN EACH COIN - does the state beat the coin it sits in?\n")
    print(f"  {'coin':11}{'DORMANT $/100':>15}{'ACTIVE':>10}{'VOLATILE':>11}"
          f"{'V - D':>10}{'V fired%':>10}")
    print("  " + "-" * 67)
    wins = 0
    scored = 0
    for pid in sorted(percoin):
        c = percoin[pid]
        def avg(state):
            d = c.get(state)
            return (d["net"] / d["n"]) if d and d["n"] else None
        dv, av, vv = avg("DORMANT"), avg("ACTIVE"), avg("VOLATILE")
        vd = c.get("VOLATILE")
        fired = (100.0 * vd["any"] / vd["n"]) if vd and vd["n"] else 0.0
        if dv is None or vv is None:
            continue
        scored += 1
        if vv > dv:
            wins += 1
        print(f"  {pid:11}{dv:>+15.4f}{(av if av is not None else 0):>+10.4f}"
              f"{vv:>+11.4f}{vv - dv:>+10.4f}{fired:>9.1f}%")
    if scored:
        print(f"\n  VOLATILE beat DORMANT in {wins} of {scored} coins "
              f"({100.0 * wins / scored:.0f}%). A coin-by-coin split near 50%")
        print("  means the pooled result is the coins, not the state.")

    print("\nHOW TO READ IT")
    print("  trips/mo   round trips the grid COMPLETED per classified moment.")
    print("  fired%     share of moments where at least one trip completed -")
    print("             the owner's 'something should be happening'.")
    print("  net $/100  dollars per $100 deployed, AFTER the 0.70% round trip.")
    print()
    print("  THE TEST: if VOLATILE does not beat DORMANT on net $/100 by more")
    print("  than the spread between coins, state detection buys nothing and")
    print("  the engine should not be built.")
    print()
    print("  THE TRAP: a BEAR row that scores well is NOT permission to gate")
    print("  entries in a downtrend. It is the same number the trend filter")
    print("  already failed on - ungated +$123.95 against gated +$13.81. The")
    print("  grid earns in a fall because it is BUYING the fall.")
    print()
    print("  NOT MEASURED: spread, net edge, inventory, working orders, time")
    print("  since last fill. No historical record exists. Live-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
