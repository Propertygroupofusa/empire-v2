#!/usr/bin/env python3
"""Where every fleet dollar sits, and what that band has ACTUALLY returned.

This exists because of a question that looks obvious and has a
counter-intuitive answer: "the small coins aren't growing - find something
that runs off a smaller amount."

The fleet already ran that experiment. It is in the trade book:

    Aug 30 - Sep 09   82 closes   median notional $16.73   $0.238 per trip
    Sep 26 - Sep 30   74 closes   median notional $36.57   $0.854 per trip

Trade size roughly doubled and profit per trip went up 3.6x. Smaller was
tried first and it earned a quarter a trip. So this script does NOT look for
a way to trade smaller. It answers the question underneath it - which
dollars are earning, which are not, and why - using only measured numbers.

READ-ONLY. It places no order, moves no cash, and changes no threshold. Every
dollar figure it prints about the future is arithmetic on a rate already
observed, and is labelled as such.

Usage:  python3 scripts/capital_map.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.getenv("EMPIRE_BASE", "https://empire-v2-production.up.railway.app")
API = BASE + "/api/trading-dashboard"

# The bands are not arbitrary. MIN_TRADE_USD is $5 (crypto_grid_bot.py:88),
# so $0-15 is "legal but thin" and everything above is where the fee floor
# stops dominating. The boundaries are round numbers chosen once, ahead of
# looking at the answer, so the bucketing cannot be tuned to flatter a story.
BANDS = [(0, 15), (15, 25), (25, 50), (50, 100), (100, float("inf"))]

# A branch whose capital is 20%+ of the fleet breaches the owner's standing
# concentration ceiling. Reported, never acted on.
CONCENTRATION_CEILING_PCT = 20.0

# Below this many closed round trips a band is reported but never quoted as
# the benchmark. Chosen before looking at which band it would exclude.
MIN_TRIPS_TO_QUOTE = 15

gaps = []


def get(path, timeout=60):
    url = path if path.startswith("http") else API + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        gaps.append(f"{path}: {type(e).__name__}: {e}")
        return None


def money(x):
    if x is None:
        return "UNKNOWN"
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def band_label(lo, hi):
    return f"${lo:.0f}-{'up' if hi == float('inf') else f'${hi:.0f}'}"


def band_of(notional):
    for lo, hi in BANDS:
        if lo <= notional < hi:
            return (lo, hi)
    return BANDS[-1]


def measured_bands(trades):
    """What each notional band has really returned, per closed round trip.

    Returns {band: {...}}. A band nobody has traded is absent, NOT zero - a
    gap is not a zero, and an unmeasured band must never be quoted as one
    that earns nothing.
    """
    by = defaultdict(list)
    for t in trades:
        entry, qty = t.get("entry_price"), t.get("qty")
        pnl = t.get("pnl")
        if entry is None or qty is None or pnl is None:
            continue
        notional = entry * qty
        if notional <= 0:
            continue
        by[band_of(notional)].append((notional, pnl))
    out = {}
    for band, rows in by.items():
        n = len(rows)
        total = sum(p for _, p in rows)
        wins = sum(1 for _, p in rows if p > 0)
        out[band] = {
            "trips": n,
            "total_usd": total,
            "per_trip_usd": total / n,
            "win_pct": 100.0 * wins / n,
            "net_pct_per_trip": sum(p / nl for nl, p in rows) / n * 100.0,
        }
    return out


def branch_mobility(b):
    """Can this branch complete a round trip, and if not, what stops it?

    The two rules are the live ones, not a paraphrase:
      parked  - crypto_grid_bot.py:7004, full on rungs OR every slice adopted
      sell    - crypto_grid_bot.py:7050, price vs the BRANCH reference, not
                the slice's own entry
    """
    slices = b.get("slices") or []
    levels = b.get("num_levels") or 0
    if not slices:
        return ("EMPTY", None, "no open slices - the branch is free to buy")

    adopted_only = all(s.get("adopted") for s in slices)
    parked = len(slices) >= levels or adopted_only

    gp, ref, px = b.get("grid_pct"), b.get("reference_price"), b.get("current_price")
    if not gp or not ref or not px:
        return ("UNKNOWN", None, "sell trigger unreadable - grid_pct, reference "
                                 "or price missing. Not a verdict either way.")

    trigger = ref * (1 + gp)
    gap_pct = None if px >= trigger else (trigger / px - 1) * 100.0

    if not parked:
        if gap_pct is None:
            return ("MOBILE", 0.0, "can buy, and price is already past the rise trigger")
        return ("MOBILE", gap_pct, f"can buy; sell needs {gap_pct:+.2f}% more")

    why = ("every slice is adopted, so the branch is parked however many rungs "
           "are free") if adopted_only and len(slices) < levels else \
          f"full on rungs ({len(slices)}/{levels})"
    if gap_pct is None:
        return ("PARKED", 0.0, f"{why}; price is already past the rise trigger")
    return ("PARKED", gap_pct, f"{why}; cannot buy, and sell needs {gap_pct:+.2f}% more")


def main():
    gs = get("/grid-status")
    th = get("/grid-status/trade-history?limit=1000")
    gm = get("/growth-model")

    if gs is None or th is None:
        print("CANNOT MAP THE CAPITAL - a feed did not answer:")
        for g in gaps:
            print("   " + g)
        print("\nThis is CANNOT READ, not 'nothing is wrong'. Re-run.")
        return 2

    branches = gs.get("branches") or []
    trades = th.get("recent_trades") or []
    truncated = th.get("recent_trades_truncated")

    print("=" * 74)
    print("CAPITAL MAP - where every fleet dollar sits")
    print(f"as of {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 74)

    # ---- 1. What each trade size has actually returned -------------------
    bands = measured_bands(trades)
    print(f"\n1. WHAT EACH TRADE SIZE HAS ACTUALLY RETURNED  "
          f"({len(trades)} closed round trips"
          f"{', LIST TRUNCATED - this is a floor' if truncated else ''})")
    print(f"   {'band':>11} {'trips':>6} {'total':>10} {'per trip':>10} "
          f"{'win':>7} {'net%/trip':>10}")
    for lo, hi in BANDS:
        m = bands.get((lo, hi))
        if not m:
            print(f"   {band_label(lo, hi):>11} {'-':>6} {'never traded - UNKNOWN, not zero':>45}")
            continue
        print(f"   {band_label(lo, hi):>11} {m['trips']:6d} {money(m['total_usd']):>10} "
              f"{money(m['per_trip_usd']):>10} {m['win_pct']:6.1f}% "
              f"{m['net_pct_per_trip']:+9.3f}%")
    print("   Win rate stays high at every size. What changes with size is the")
    print("   DOLLARS per trip, because the fee floor is a bigger share of a")
    print("   small trip. Trading smaller raises the trip count and lowers the")
    print("   take per trip; the book above is what that trade-off really paid.")

    # ---- 2. Every branch, by whether it can complete a round trip --------
    realized = defaultdict(float)
    for t in trades:
        if t.get("pnl") is not None:
            realized[t.get("product_id")] += t["pnl"]

    total_alloc = sum((b.get("allocated_usd") or 0.0) for b in branches)
    rows = []
    for b in branches:
        alloc = b.get("allocated_usd") or 0.0
        levels = b.get("num_levels") or 1
        state, gap, why = branch_mobility(b)
        rows.append({
            "product": b.get("product_id"),
            "alloc": alloc,
            "slice_usd": alloc / levels if levels else 0.0,
            "state": state, "gap": gap, "why": why,
            "unreal": b.get("total_unrealized_net_usd"),
            "realized": realized.get(b.get("product_id"), 0.0),
            "share_pct": (100.0 * alloc / total_alloc) if total_alloc else 0.0,
        })
    rows.sort(key=lambda r: -r["alloc"])

    print(f"\n2. EVERY BRANCH - can it complete a round trip?"
          f"   (allocated {money(total_alloc)})")
    print(f"   {'product':<11} {'alloc':>10} {'share':>6} {'slice':>8} "
          f"{'realized':>9} {'state':>8}  why")
    for r in rows:
        mark = "  <-- OVER CEILING" if r["share_pct"] >= CONCENTRATION_CEILING_PCT else ""
        print(f"   {r['product']:<11} {money(r['alloc']):>10} {r['share_pct']:5.1f}% "
              f"{money(r['slice_usd']):>8} {money(r['realized']):>9} "
              f"{r['state']:>8}  {r['why']}{mark}")

    # ---- 3. The split that answers the question -------------------------
    mobile = [r for r in rows if r["state"] in ("MOBILE", "EMPTY")]
    stuck = [r for r in rows if r["state"] == "PARKED"]
    unknown = [r for r in rows if r["state"] == "UNKNOWN"]

    m_alloc = sum(r["alloc"] for r in mobile)
    s_alloc = sum(r["alloc"] for r in stuck)
    u_alloc = sum(r["alloc"] for r in unknown)
    m_real = sum(r["realized"] for r in mobile)
    s_real = sum(r["realized"] for r in stuck)

    print("\n3. THE SPLIT")
    print(f"   CAN trade now : {money(m_alloc):>10} across {len(mobile):2d} branch(es), "
          f"realized {money(m_real)}")
    print(f"   CANNOT (parked): {money(s_alloc):>10} across {len(stuck):2d} branch(es), "
          f"realized {money(s_real)}")
    if unknown:
        print(f"   UNKNOWN        : {money(u_alloc):>10} across {len(unknown):2d} branch(es) "
              f"- unreadable, which is a third verdict, not a zero")
    if total_alloc:
        print(f"   {100.0 * s_alloc / total_alloc:.1f}% of allocated capital cannot currently "
              f"complete a round trip.")

    if stuck:
        worst = sorted((r for r in stuck if r["gap"] is not None),
                       key=lambda r: -(r["gap"] or 0))[:5]
        if worst:
            print("\n   Furthest from being able to sell (price still has to travel this far -")
            print("   that is a distance, NOT a forecast that it will):")
            for r in worst:
                print(f"     {r['product']:<11} {money(r['alloc']):>10}  needs {r['gap']:+.2f}%")

    # ---- 4. The ceiling, stated as arithmetic ---------------------------
    print("\n4. WHAT THE STUCK CAPITAL IS COSTING - ARITHMETIC, NOT A FORECAST")
    # A band with a handful of trips is not a measurement, and calling the
    # richest-looking one "best" is how a 5-trip fluke becomes a plan. Only
    # bands with a real sample are eligible to be quoted as the benchmark.
    eligible = {b: m for b, m in bands.items() if m["trips"] >= MIN_TRIPS_TO_QUOTE}
    best = max(eligible.items(), key=lambda kv: kv[1]["per_trip_usd"]) if eligible else None
    thin = sorted((b for b, m in bands.items() if m["trips"] < MIN_TRIPS_TO_QUOTE),
                  key=lambda b: b[0])
    if best and s_alloc > 0:
        band, m = best
        print(f"   The best band with a real sample is {band_label(*band)} at "
              f"{money(m['per_trip_usd'])} per trip over {m['trips']} trips.")
        if thin:
            print("   Ignored as too thin to quote (under "
                  f"{MIN_TRIPS_TO_QUOTE} trips): "
                  + ", ".join(f"{band_label(*b)} ({bands[b]['trips']})" for b in thin)
                  + " - thin, NOT bad.")
        print(f"   {money(s_alloc)} is parked. At that band's slice size it would be")
        print(f"   roughly {int(s_alloc // max(band[0], 1))} slices' worth of capital.")
        print("   It does NOT follow that moving it would earn that rate. Capital can")
        print("   be parked BECAUSE its branch found nothing worth buying, and an")
        print("   adopted branch is parked on a rule, not on a view. Treat this as the")
        print("   top of the range, never the middle.")
    else:
        print("   Not computable from this pass - no band measured, or nothing parked.")

    if gm and isinstance(gm.get("capital"), dict):
        cap = gm["capital"]
        nw, nwp = cap.get("not_working_usd"), cap.get("not_working_pct")
        if nw is not None:
            print(f"\n   /growth-model separately reports {money(nw)} not working "
                  f"({nwp}% of capital).")
            print("   That is idle capital INSIDE branches plus free cash - a different")
            print("   cut from the parked total above. The two are not additive.")

    # ---- 4b. Would NEW money help? ---------------------------------------
    #
    # Asked directly: "how can we take three to four hundred dollars we
    # already have and deploy it so it starts making money for us." The
    # honest answer is a measurement, not an opinion, and it turned out to
    # refute the premise: the branches that could spend new money are
    # already sitting on idle capital they are not spending. They are
    # waiting on a dip that passes the gate, not on funding.
    print("\n4b. WOULD NEW MONEY HELP? - only branches that can BUY can use it")
    takers = []
    for b in branches:
        sl = b.get("slices") or []
        levels = b.get("num_levels") or 0
        adopted_only = bool(sl) and all(x.get("adopted") for x in sl)
        parked = len(sl) >= levels or adopted_only
        if parked or b.get("buys_paused") or b.get("drawdown_breached"):
            continue
        alloc = b.get("allocated_usd") or 0.0
        spent = sum((x.get("qty") or 0) * (x.get("entry_price") or 0) for x in sl)
        takers.append({
            "product": b.get("product_id"),
            "idle": alloc - spent,
            "free_rungs": levels - len(sl),
            "realized": realized.get(b.get("product_id"), 0.0),
        })
    if not takers:
        print("   NO branch can buy right now. Every one is parked, breakered or "
              "paused, so new money would sit as cash. Funding is not the blocker.")
    else:
        idle_here = sum(t["idle"] for t in takers)
        print(f"   {len(takers)} of {len(branches)} branches can take new money at all, "
              f"and they ALREADY hold {money(idle_here)} of idle capital:")
        for t in sorted(takers, key=lambda x: -x["realized"]):
            print(f"     {t['product']:<11} idle {money(t['idle']):>10}  "
                  f"{t['free_rungs']} free rung(s)  realized {money(t['realized'])}")
        print(f"   Adding cash makes that {money(idle_here)} into a larger idle pile.")
        print("   These branches are waiting for a dip their gate accepts, NOT for")
        print("   funding. New money buys nothing until a buy fires, and the same")
        print("   dip fires it whether the branch holds this much or more.")
        print("   Deciding what a branch is allocated is a money-moving call and is")
        print("   the owner's, never this script's.")

    # ---- 5. Owner decisions, named and left alone -----------------------
    print("\n5. OWNER DECISIONS - named here, NOT taken here")
    over = [r for r in rows if r["share_pct"] >= CONCENTRATION_CEILING_PCT]
    for r in over:
        print(f"   - {r['product']} is {r['share_pct']:.1f}% of allocated capital, past the "
              f"{CONCENTRATION_CEILING_PCT:.0f}% ceiling.")
    if stuck:
        print("   - Parked branches free up by price moving, by the owner reconciling")
        print("     them, or not at all. Reconciling and closing are write-guarded and")
        print("     are the owner's calls. This script does not make them.")
    print("   - Nothing here recommends lowering a floor, tightening spacing, or")
    print("     force-closing a red branch. A forced close books a real loss.")

    if gaps:
        print("\nGAPS THIS PASS (a gap is not a zero):")
        for g in gaps:
            print("   " + g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
