"""Rank every branch by what the next dollar would actually do there.

READ-ONLY. It reads /grid-status and public Coinbase candles, scores every
branch, applies the live gates, and prints why each one was accepted or
rejected. It places no order, calls no write endpoint, moves no capital,
flips no flag, and imports no live bot module. Running it cannot move a
dollar. Nothing that trades imports it.

WHAT IT IS, AND THE ONE THING IT DELIBERATELY IS NOT

The account owner's design, 2026-10-07, is a continuous allocator: score
every coin, gate it, rank it, and automatically recycle freed capital into
the top-ranked opportunity. This file implements ALL OF THAT EXCEPT THE
LAST STEP. It produces the ranking and the ledger of reasons. It does not
deploy, and nothing here can.

That is not timidity, it is the measured result. Reallocating claim
between branches was tested on this fleet's real 30-day tape, including
the version that ranks by return per dollar:

    in sample          +$40 .. +$79
    OUT OF SAMPLE      LOST $88 .. $111
    yield-weighted     the WORST of every variant tried

and in rotation_study.py, ranking branches by last month's P&L lost to
ranking by last month's FILL COUNT two to one. A scorer whose top term is
expected profit is the variant that already lost money. So the score here
leads with the term that survived - how often the coin actually completes
a cycle - and the profit term is a multiplier on it, not the headline.

THE SCORE, every term measured rather than invented

    fire_rate      share of 4h windows in which a 3-rung grid at THIS
                   branch's own step completed at least one round trip,
                   measured over 60 days of 15m candles. This is the
                   fill-count signal that beat P&L ranking 2:1.
    net_per_100    dollars that grid netted per $100 deployed over the
                   same windows, AFTER the real 0.70% maker round trip.
    state_mult     the branch's volatility state right now. Measured
                   fleet-wide: VOLATILE earns 22x DORMANT per $100 and
                   fires 11x as often, and that held in 21 of 21 coins.
    room           free rungs. A branch with every rung filled cannot use
                   another dollar no matter how attractive it looks.

    score = fire_rate * net_per_100 * state_mult * room      (x100)

THE GATES, each one the live system's own and none of them invented here.
A branch failing any gate is REJECTED with the gate named:

    FULL            no free rung - capital would sit idle inside it
    FROZEN          drawdown breaker has paused this branch's buys
    CONCENTRATION   already at or above 20% of fleet claim
    VIABILITY       under the $15 keep-alive floor
    SHORT           claims units the wallet does not hold (ZEC, ACH)
    NO_EDGE         expected net below the fee-safe minimum
    DORMANT         state says the coin is not moving enough to cycle

    python3 opportunity_allocator.py [days]
"""
from __future__ import annotations

import sys

import market_state_study as MSS

BASE = MSS.BASE
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
HORIZON = 16                      # 4 hours, the same window the study used
CONCENTRATION_CAP = 0.20          # the owner's standing ceiling
VIABILITY_FLOOR = 15.00           # the live keep-alive floor
STATE_MULT = {"VOLATILE": 1.0, "ACTIVE": 0.25, "DORMANT": 0.05}


def profile(pid, step, days=DAYS):
    """fire rate, net per $100 and the live state, from candles alone."""
    bars, why = MSS.candles(pid, days=days)
    if not bars or len(bars) < 200:
        return None, f"no candles ({why or len(bars)} bars)"
    labels = MSS.classify(bars)
    fired = scored = 0
    net = 0.0
    for i, lab in enumerate(labels):
        if lab is None or i + 1 + HORIZON > len(bars):
            continue
        trips, n = MSS.grid_forward(bars, i, HORIZON, step)
        scored += 1
        net += n
        if trips:
            fired += 1
    if not scored:
        return None, "no classified windows"
    live = next((labels[i] for i in range(len(labels) - 1, -1, -1)
                 if labels[i]), ("DORMANT", "FLAT"))
    return {"fire_rate": fired / scored, "net_per_100": net / scored,
            "state": live[0], "trend": live[1], "windows": scored}, None


def main():
    gs = MSS._get(f"{BASE}/grid-status", timeout=90)
    branches = [b for b in (gs.get("branches") or []) if b.get("product_id")]
    total_claim = sum(float(b.get("allocated_usd") or 0) for b in branches)
    free_cash = float(gs.get("real_free_cash_usd") or 0)

    short = set()
    bo = gs.get("backing_owned") or {}
    for row in (bo.get("short_positions") or []):
        if row.get("product_id"):
            short.add(row["product_id"])

    rows = []
    for b in branches:
        pid = b["product_id"]
        step = float(b.get("grid_pct") or 0.03)
        alloc = float(b.get("allocated_usd") or 0)
        slices = int(b.get("open_slices") or 0)
        levels = max(int(b.get("num_levels") or 1), 1)
        prof, err = profile(pid, step)
        if prof is None:
            rows.append({"coin": pid.replace("-USD", ""), "score": 0.0,
                         "verdict": "REJECT", "gate": "UNREADABLE",
                         "why": err, "alloc": alloc, "state": "?",
                         "fire": 0.0, "net": 0.0, "room": 0})
            continue

        room = max(levels - slices, 0)
        gate = None
        if pid in short:
            gate = "SHORT"
        elif b.get("drawdown_breached"):
            gate = "FROZEN"
        elif room == 0:
            gate = "FULL"
        elif total_claim and alloc / total_claim >= CONCENTRATION_CAP:
            gate = "CONCENTRATION"
        elif alloc < VIABILITY_FLOOR:
            gate = "VIABILITY"
        elif prof["state"] == "DORMANT":
            gate = "DORMANT"
        elif prof["net_per_100"] <= 0:
            gate = "NO_EDGE"

        score = (prof["fire_rate"] * prof["net_per_100"]
                 * STATE_MULT.get(prof["state"], 0.05)
                 * min(room / levels, 1.0) * 100.0)
        rows.append({
            "coin": pid.replace("-USD", ""), "score": 0.0 if gate else score,
            "verdict": "REJECT" if gate else "QUALIFIED", "gate": gate or "",
            "why": "", "alloc": alloc, "state": f"{prof['state']}/{prof['trend']}",
            "fire": prof["fire_rate"], "net": prof["net_per_100"], "room": room})

    rows.sort(key=lambda r: (-r["score"], r["coin"]))
    print(f"CAPITAL ALLOCATOR - LIVE RANKING   (read-only, deploys nothing)")
    print(f"fleet claim ${total_claim:,.2f} across {len(branches)} branches - "
          f"free cash ${free_cash:,.2f} - {DAYS}d of candles per branch\n")
    print(f"  {'coin':7}{'score':>8}  {'verdict':<10}{'gate':<15}"
          f"{'state':<16}{'fire%':>7}{'net/$100':>10}{'rungs':>7}{'claim':>10}")
    print("  " + "-" * 92)
    for r in rows:
        print(f"  {r['coin']:7}{r['score']:>8.2f}  {r['verdict']:<10}"
              f"{r['gate']:<15}{r['state']:<16}{100*r['fire']:>6.1f}%"
              f"{r['net']:>10.4f}{r['room']:>7}{r['alloc']:>10,.2f}"
              + (f"   {r['why']}" if r["why"] else ""))

    q = [r for r in rows if r["verdict"] == "QUALIFIED"]
    print(f"\n  {len(q)} qualified, {len(rows) - len(q)} rejected.")
    if q:
        print(f"  Highest-ranked use of the next dollar: {q[0]['coin']} "
              f"(score {q[0]['score']:.2f}, {100*q[0]['fire']:.1f}% fire rate, "
              f"{q[0]['room']} free rung(s))")
    from collections import Counter
    for g, n in Counter(r["gate"] for r in rows if r["gate"]).most_common():
        print(f"    rejected on {g}: {n}")
    print("\n  NOTHING WAS DEPLOYED. This file cannot deploy. The ranking is")
    print("  the product; moving capital on it is a separate decision that")
    print("  the out-of-sample result above says to make slowly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
