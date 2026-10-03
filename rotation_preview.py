#!/usr/bin/env python3
"""Dry run of the concentration rotation. READ ONLY - places nothing.

Fetches the live fleet and the live coin league over plain GET, hands them
to concentration_rotation.plan(), and prints every slice the rotation can
see with the exact arithmetic that decides SELL or WAIT.

There is no order path in this file at all. No POST, no session, no
Coinbase auth header. Grep it: the only verb is GET.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import concentration_rotation as rot

BASE = "https://empire-v2-production.up.railway.app/api/trading-dashboard"
COST = rot.DEFAULT_ROUND_TRIP_COST_PCT
MARGIN = rot.MIN_NET_MARGIN_PCT
LIMIT = rot.CONCENTRATION_LIMIT_PCT


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=90) as r:
        if r.status != 200:
            return None
        return json.loads(r.read().decode())


def money(v):
    return f"${v:,.2f}" if v is not None else "unreadable"


def main():
    # --bump COIN=PCT shifts that branch's price IN MEMORY ONLY, so the
    # SELL path can be seen firing without waiting for the market. It
    # changes nothing anywhere; the fetched price is never written back.
    bumps = {}
    for a in sys.argv[1:]:
        if a.startswith("--bump="):
            c, _, v = a[7:].partition("=")
            bumps[c.upper()] = float(v)

    grid = get("/grid-status")
    league = get("/coin-league")
    if not grid or not (grid.get("branches") or []):
        print("CANNOT READ THE FLEET - this pass would be skipped, not treated as empty.")
        return 1

    branches = grid["branches"]
    for b in branches:
        coin = (b.get("product_id") or "").split("-")[0].upper()
        if coin in bumps and rot._num(b.get("current_price")) is not None:
            b["current_price"] = rot._num(b["current_price"]) * (1 + bumps[coin] / 100.0)
    # Destination candidates, in the shape the planner wants.
    cands = []
    for row in ((league or {}).get("ranked") or []) + ((league or {}).get("still_qualifying") or []):
        cands.append({"coin": row.get("coin"),
                      "edge_pct": row.get("edge_pct_per_trade"),
                      "trades": row.get("trades"),
                      "allocated_usd": row.get("deployed_usd")})

    plan = rot.plan(branches, cands)
    total = sum(rot._num(b.get("allocated_usd")) or 0 for b in branches)

    print("=" * 74)
    print("CONCENTRATION ROTATION - DRY RUN.  PLACES NOTHING.")
    print("=" * 74)
    for c, v in bumps.items():
        print(f"*** HYPOTHETICAL: {c} price moved {v:+.2f}% in memory only ***")
    print(f"fleet allocated      {money(total)}  across {len(branches)} branches")
    print(f"concentration limit  {LIMIT:.1f}% of the fleet per coin")
    print(f"round trip cost      {COST:.4f}%  (fees + measured adverse selection)")
    print(f"margin required      {MARGIN:.2f}% net ON TOP of that cost")
    print()

    print("--- EVERY BRANCH, BY SHARE OF THE FLEET " + "-" * 34)
    rows = sorted(branches, key=lambda b: -(rot._num(b.get("allocated_usd")) or 0))
    for b in rows:
        a = rot._num(b.get("allocated_usd"))
        share = (a / total * 100.0) if (a is not None and total) else None
        flag = "OVER" if (share is not None and share > LIMIT) else "    "
        n = len(b.get("slices") or [])
        print(f"  {flag}  {str(b.get('product_id')):12s} {money(a):>12s}  "
              f"{(f'{share:5.2f}%' if share is not None else ' n/a '):>7s}  {n:>2d} slice(s)")
    print()

    if not plan["over_limit"]:
        print("Nothing is over the limit. The rotation has no work to do.")
    for o in plan["over_limit"]:
        print(f"--- OVER THE LIMIT: {o['product_id']}  "
              f"{o['share_pct']:.2f}% of the fleet, {money(o['over_by_usd'])} above the ceiling ---")

    print()
    print("--- SLICE BY SLICE, FOR EVERY OVER-LIMIT BRANCH " + "-" * 26)
    for mv in plan["moves"]:
        br = next((b for b in branches if b.get("product_id") == mv["product_id"]), {})
        price = rot._num(br.get("current_price"))
        print(f"\n  {mv['product_id']}   action = {mv['action']}   "
              f"price now {money(price)}")
        sell_idx = {s["index"] for s in (mv.get("sell") or [])}
        slices = br.get("slices") or []
        if not slices:
            print("     no open slices")
        print(f"     {'#':>3} {'entry':>11} {'qty':>13} {'stake':>11} "
              f"{'net after cost':>15} {'verdict':>9}  needs")
        for i, s in enumerate(slices):
            entry = rot._num(s.get("entry_price"))
            qty = rot._num(s.get("qty"))
            if entry is None or qty is None or qty <= 0:
                print(f"     {i:>3} {'unreadable':>11}")
                continue
            net = rot.exit_net_pct(entry, price, COST)
            stake = entry * qty
            if net is None:
                verdict, needs = "SKIP", "price unreadable"
            elif i in sell_idx:
                verdict = "SELL"
                needs = f"clears now, books +{money(stake * net / 100.0)}"
            else:
                verdict = "wait"
                # Price at which this slice would clear cost + margin.
                target = entry * (1 + (COST + MARGIN) / 100.0)
                move = (target / price - 1) * 100.0 if price else None
                needs = (f"price {money(target)}  (+{move:.2f}% from here)"
                         if move is not None else "price unreadable")
            print(f"     {i:>3} {money(entry):>11} {qty:>13.8f} {money(stake):>11} "
                  f"{(f'{net:+.2f}%' if net is not None else 'n/a'):>15} "
                  f"{verdict:>9}  {needs}")
        if mv["action"] == "SELL":
            print(f"     would sell {len(mv['sell'])} slice(s), freeing {money(mv['frees_usd'])}, "
                  f"booking {money(mv['profit_usd'])} of realized profit")
        else:
            print(f"     {mv['why']}")

    print()
    print("--- WHAT THIS PASS WOULD ACTUALLY DO " + "-" * 37)
    sells = [(m["product_id"], s) for m in plan["moves"]
             if m["action"] == "SELL" for s in m["sell"]]
    if not sells:
        print("  NOTHING. No market sell would be placed.")
        print("  Every slice on every over-limit branch is below the round trip,")
        print("  so selling any of them would book a loss. The rotation waits.")
    else:
        cap = 3  # MAX_SELLS_PER_PASS in the worker
        ordered = sorted(sells, key=lambda t: -t[1]["net_pct"])
        print(f"  {len(sells)} slice(s) qualify; the worker caps one pass at {cap}.")
        for pid, s in ordered[:cap]:
            print(f"    SELL  {s['qty']:.8f} {pid}  at +{s['net_pct']:.2f}% net  "
                  f"-> frees {money(s['frees_usd'])}, profit {money(s['profit_usd'])}")
        for pid, s in ordered[cap:]:
            print(f"    held for a later pass: {s['qty']:.8f} {pid} at +{s['net_pct']:.2f}%")
    print()
    print(f"  total freed        {money(plan['frees_usd'])}")
    print(f"  realized profit    {money(plan['profit_booked_usd'])}")
    d = plan["destination"]
    print(f"  destination        " + (f"{d['coin']}  (edge {d['edge_pct']:+.4f}%/trade "
          f"on {d['trades']} trades)" if d else "none - freed cash would sit until a coin qualifies"))
    print()
    print("  nothing above was sent anywhere. this script has no order path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
