#!/usr/bin/env python3
"""Where the money is, what moved it, and what is hurting it. READ ONLY.

Built because the question "it was going up and now it's going backwards,
why" took six separate endpoint reads to answer, and the answer turned out
to be three different things wearing the same costume:

  1. CASH THAT BECAME COIN. Cash falling is not money lost. The grid spends
     branch reserve to buy dips, and the wallet's cash line drops by exactly
     what it spent. That looks identical to a loss on a phone screen and is
     the opposite of one.
  2. MARKET MOVEMENT on coin already held.
  3. AN ASSET DROPPING OUT OF PRICING. A coin the census cannot price is
     excluded from the total, so the total falls with nothing having
     happened. A gap is not a loss, and this prints it separately.

It separates those three, every time, so none of them can be mistaken for
another.

It writes a snapshot so the next run can say what changed since the last
one. Nothing else is written, nothing is sent, and there is no order path:
every request is a GET.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://empire-v2-production.up.railway.app/api/trading-dashboard"
SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".money_truth_snapshot.json")


def get(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=90) as r:
            return json.loads(r.read().decode()) if r.status == 200 else None
    except Exception:
        return None


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def money(v, width=0):
    s = "unreadable" if v is None else f"${v:,.2f}"
    return f"{s:>{width}}" if width else s


def signed(v):
    return "unreadable" if v is None else f"{v:+,.2f}"


def deployed_by_branch(grid):
    out = {}
    for b in (grid or {}).get("branches") or []:
        tot = 0.0
        for s in b.get("slices") or []:
            e, q = num(s.get("entry_price")), num(s.get("qty"))
            if e is not None and q is not None:
                tot += e * q
        out[b["product_id"]] = tot
    return out


def main():
    census, grid, growth = get("/account-census"), get("/grid-status"), get("/growth-model")
    if not census or not census.get("available"):
        print("THE VENUE COULD NOT BE READ. No figure is printed rather than a "
              "wrong one - an unreadable account is not an empty account.")
        return 1

    now_total = num(census.get("total_usd"))
    now_cash = num(census.get("cash_usd"))
    now_coin = num(census.get("coin_usd"))
    cap = ((growth or {}).get("capital") or {})
    now_deployed = num(cap.get("deployed_usd"))
    now_unreal = num(cap.get("unrealized_usd"))
    unpriced = {u["asset"] for u in (census.get("unpriced") or [])}

    prev = None
    try:
        with open(SNAP) as f:
            prev = json.load(f)
    except Exception:
        pass

    print("=" * 72)
    print("WHERE THE MONEY IS")
    print("=" * 72)
    print(f"  total at the venue   {money(now_total, 12)}")
    print(f"    cash               {money(now_cash, 12)}")
    print(f"    coin               {money(now_coin, 12)}")
    print(f"  deployed in grids    {money(now_deployed, 12)}")
    print(f"  unrealized           {money(now_unreal, 12)}")
    if unpriced:
        print(f"  NOT IN THE TOTAL     {len(unpriced)} asset(s) could not be priced: "
              f"{', '.join(sorted(unpriced))}")
        print("                       the real total is higher by whatever they are worth")

    if prev:
        since = prev.get("as_of", "the last run")
        d_total = None if (now_total is None or prev.get("total") is None) else now_total - prev["total"]
        d_cash = None if (now_cash is None or prev.get("cash") is None) else now_cash - prev["cash"]
        d_dep = None if (now_deployed is None or prev.get("deployed") is None) else now_deployed - prev["deployed"]
        print()
        print("-" * 72)
        print(f"WHAT CHANGED SINCE {since}")
        print("-" * 72)
        print(f"  total               {signed(d_total):>12}")
        print(f"  cash                {signed(d_cash):>12}")
        print(f"  deployed into coin  {signed(d_dep):>12}")
        if d_cash is not None and d_dep is not None:
            spent = min(-d_cash, d_dep) if d_cash < 0 and d_dep > 0 else 0.0
            print()
            print(f"  OF THE {money(abs(d_cash))} THE CASH LINE MOVED:")
            print(f"    {money(spent)} became coin - the grid bought with it. NOT a loss.")
            rest = (-d_cash) - spent
            if abs(rest) > 0.01:
                print(f"    {money(rest)} left the cash line some other way - look at "
                      "withdrawals, a USD->USDC conversion, or another bot.")
        # New gaps are not losses.
        newly = unpriced - set(prev.get("unpriced") or [])
        if newly:
            print()
            print(f"  A GAP, NOT A LOSS: {', '.join(sorted(newly))} could be priced last "
                  "run and cannot now.")
            print("    Their value dropped OUT OF THE COUNT. Price them before reading "
                  "the total as a decline.")
        # What the deployment actually bought.
        pd = prev.get("deployed_by_branch") or {}
        nd = deployed_by_branch(grid)
        moves = sorted(((k, nd.get(k, 0.0) - pd.get(k, 0.0)) for k in set(pd) | set(nd)),
                       key=lambda t: -abs(t[1]))
        moves = [m for m in moves if abs(m[1]) > 0.01]
        if moves:
            print()
            print("  WHICH BRANCHES IT WENT INTO:")
            for k, v in moves[:10]:
                print(f"    {k:12s} {signed(v):>10}")

    # What is actually carrying the loss.
    conc = (cap.get("concentration") or {})
    print()
    print("-" * 72)
    print("WHAT IS CARRYING THE LOSS")
    print("-" * 72)
    rows = []
    for b in (grid or {}).get("branches") or []:
        u = num(b.get("total_unrealized_net_usd"))
        if u is not None:
            rows.append((b["product_id"], u, num(b.get("allocated_usd"))))
    rows.sort(key=lambda r: r[1])
    for pid, u, a in rows[:6]:
        share = f"{u / now_unreal * 100:5.1f}%" if now_unreal else "    ?"
        print(f"  {pid:12s} {money(u, 11)}  {share} of all unrealized   "
              f"on {money(a)} allocated")
    if conc.get("share_of_unrealized_pct") is not None:
        print()
        print(f"  THE OVER-WEIGHT COINS CARRY {conc['share_of_unrealized_pct']:.1f}% OF THE LOSS:")
        print(f"    {money(conc.get('allocated_usd'))} allocated -> "
              f"{money(conc.get('unrealized_usd'))} unrealized")
        print(f"    everything else: {money(conc.get('rest_allocated_usd'))} allocated -> "
              f"{money(conc.get('rest_unrealized_usd'))} unrealized")

    with open(SNAP, "w") as f:
        json.dump({"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                   "total": now_total, "cash": now_cash, "coin": now_coin,
                   "deployed": now_deployed, "unrealized": now_unreal,
                   "unpriced": sorted(unpriced),
                   "deployed_by_branch": deployed_by_branch(grid)}, f, indent=1)
    print()
    print("  snapshot saved - the next run will report what changed since now.")
    print("  every request above was a GET. nothing was placed, sold or moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
