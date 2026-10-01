#!/usr/bin/env python3
"""Where is every dollar and every coin. READ ONLY.

Answers, from exchange truth rather than from the books:

  PRIORITY 1  DB inventory vs Coinbase inventory, per product
  PRIORITY 2  which branch holds which capital, and what blocks its release
  PRIORITY 4  the concentration denominators, all of them, side by side

It reconciles nothing and places nothing. Every request is a GET. The
reconciliation RULES live in reconcile_worker.py and slice_reconcile.py and
are not duplicated here - this reports what those rules would be looking at.

DO NOT read a difference here as a loss. A branch claiming coin the wallet
does not hold is a bookkeeping divergence; whether the coin was sold, never
filled, or moved is a question this cannot answer and does not guess.
"""
from __future__ import annotations

import json
import urllib.request

BASE = "https://empire-v2-production.up.railway.app/api/trading-dashboard"


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


def usd(v, w=0):
    s = "unreadable" if v is None else f"${v:,.2f}"
    return f"{s:>{w}}" if w else s


def main():
    census, grid, growth = get("/account-census"), get("/grid-status"), get("/growth-model")
    if not census or not census.get("available") or not grid:
        print("EXCHANGE OR FLEET UNREADABLE - no audit printed rather than a wrong one.")
        return 1

    held = {}
    for h in census.get("holdings") or []:
        a = (h.get("asset") or "").upper()
        if a:
            held[a] = h
    branches = grid.get("branches") or []

    # ---------- PRIORITY 1 ----------
    print("=" * 108)
    print("PRIORITY 1 - EXCHANGE / DB INVENTORY TRUTH")
    print("=" * 108)
    print(f"{'PRODUCT':11s} {'DB_QTY':>18} {'COINBASE_QTY':>18} {'DIFFERENCE':>16} "
          f"{'RESERVED':>13} {'AVAILABLE':>13}  {'STATUS':9s} ACTION")
    rows, short_usd, over_usd = [], 0.0, 0.0
    for b in sorted(branches, key=lambda x: x.get("product_id") or ""):
        pid = b.get("product_id") or "?"
        asset = pid.split("-")[0].upper()
        db_qty = sum((num(s.get("qty")) or 0.0) for s in (b.get("slices") or []))
        h = held.get(asset)
        price = num(b.get("current_price"))
        if h is None:
            cb_qty = num(census.get("held_including_zero", {}).get(asset))
            reserved = avail = None
        else:
            cb_qty = num(h.get("units"))
            reserved = num(h.get("locked_units"))
            avail = num(h.get("available_units"))
        if cb_qty is None:
            status, action = "UNKNOWN", "balance unreadable - fail closed, no allocation"
            diff = None
        else:
            diff = cb_qty - db_qty
            tol = max(abs(db_qty) * 1e-6, 1e-9)
            gap_usd = abs(diff) * (price or 0.0)
            if abs(diff) <= tol:
                status, action = "MATCH", "allocator may trade this branch"
                diff = 0.0
            elif diff < 0:
                status = "DB_ONLY"
                action = (f"book claims {abs(diff):.8f} more than the venue holds "
                          f"({usd(gap_usd)}) - reconcile; do NOT sell the difference")
                short_usd += gap_usd
            else:
                status = "CB_ONLY"
                action = (f"venue holds {diff:.8f} more than the book "
                          f"({usd(gap_usd)}) - unbranched inventory, adopt or leave")
                over_usd += gap_usd
        rows.append((pid, status))
        f8 = lambda v: "unreadable" if v is None else f"{v:,.8f}"
        print(f"{pid:11s} {f8(db_qty):>18} {f8(cb_qty):>18} {f8(diff):>16} "
              f"{f8(reserved):>13} {f8(avail):>13}  {status:9s} {action}")
    n = {s: sum(1 for _, x in rows if x == s) for s in ("MATCH", "DB_ONLY", "CB_ONLY", "UNKNOWN")}
    print()
    print(f"  MATCH {n['MATCH']}   DB_ONLY {n['DB_ONLY']}   CB_ONLY {n['CB_ONLY']}   UNKNOWN {n['UNKNOWN']}")
    print(f"  book claims {usd(short_usd)} of coin the venue does not hold")
    print(f"  venue holds {usd(over_usd)} of coin no branch claims")
    print(f"  branches an allocator must NOT fund until reconciled: "
          f"{n['DB_ONLY'] + n['UNKNOWN']} of {len(rows)}")

    # ---------- PRIORITY 2 ----------
    print()
    print("=" * 108)
    print("PRIORITY 2 - CAPITAL RELEASE, BRANCH BY BRANCH")
    print("=" * 108)
    print(f"{'BRANCH':11s} {'ALLOCATED':>11} {'DEPLOYED':>11} {'RESERVE':>10} "
          f"{'UNREALIZED':>11} {'SELLABLE$':>11} {'RELEASABLE':>11} {'BLOCKED':>10}  REASON")
    tot = {"alloc": 0.0, "rel": 0.0, "blk": 0.0}
    cost = 1.6407      # the corrected round trip, from concentration_rotation
    for b in sorted(branches, key=lambda x: -(num(x.get("allocated_usd")) or 0)):
        pid = b.get("product_id") or "?"
        asset = pid.split("-")[0].upper()
        alloc = num(b.get("allocated_usd")) or 0.0
        price = num(b.get("current_price"))
        slices = b.get("slices") or []
        deployed = sum((num(s.get("entry_price")) or 0) * (num(s.get("qty")) or 0) for s in slices)
        reserve = alloc - deployed
        unreal = num(b.get("total_unrealized_net_usd"))
        h = held.get(asset) or {}
        avail_units = num(h.get("available_units"))
        # Value the venue would actually let you sell right now.
        sellable_usd = (avail_units * price) if (avail_units is not None and price) else None
        # A slice releases capital only if its exit clears the round trip.
        at_profit = 0.0
        for s in slices:
            e, q = num(s.get("entry_price")), num(s.get("qty"))
            if e and q and price:
                if (price / e - 1.0) * 100.0 - cost > 0:
                    at_profit += e * q
        reasons = []
        if avail_units is not None and price and sellable_usd < deployed * 0.99:
            reasons.append("venue holds less than the book")
        if at_profit <= 0 and deployed > 0:
            reasons.append("every slice under the round trip")
        locked = num(h.get("locked_units"))
        if locked:
            reasons.append(f"{locked:,.4f} units reserved by resting orders")
        if not slices:
            reasons.append("no open slices")
        releasable = min(at_profit, sellable_usd) if (sellable_usd is not None) else None
        releasable = releasable if releasable and releasable > 0 else 0.0
        blocked = max(0.0, alloc - releasable)
        tot["alloc"] += alloc; tot["rel"] += releasable; tot["blk"] += blocked
        print(f"{pid:11s} {usd(alloc,11)} {usd(deployed,11)} {usd(reserve,10)} "
              f"{usd(unreal,11)} {usd(sellable_usd,11)} {usd(releasable,11)} "
              f"{usd(blocked,10)}  {'; '.join(reasons) or 'free to cycle'}")
    print()
    print(f"  {'TOTAL':11s} allocated {usd(tot['alloc'])}   releasable NOW {usd(tot['rel'])}   "
          f"blocked {usd(tot['blk'])}")

    # ---------- PRIORITY 4 ----------
    cap = (growth or {}).get("capital") or {}
    print()
    print("=" * 108)
    print("PRIORITY 4 - THE CONCENTRATION DENOMINATORS")
    print("=" * 108)
    total_equity = num(census.get("total_usd"))
    cash = num(census.get("cash_usd"))
    allocated = num(cap.get("allocated_usd"))
    deployed_all = num(cap.get("deployed_usd"))
    reserved_cap = num(cap.get("idle_in_branch_usd"))
    coin = num(census.get("coin_usd"))
    print(f"  total_account_equity   {usd(total_equity,12)}   census total_usd")
    print(f"  available_cash         {usd(cash,12)}")
    print(f"  allocated_capital      {usd(allocated,12)}   branches' claim")
    print(f"  deployed (coin)        {usd(deployed_all,12)}")
    print(f"  reserved_capital       {usd(reserved_cap,12)}   claimed but unspent")
    print(f"  coin_exposure          {usd(coin,12)}")
    print()
    print(f"  {'COIN':9s} {'% OF TOTAL EQUITY':>19} {'% OF ALLOCATED':>16}   the two answers differ")
    for b in sorted(branches, key=lambda x: -(num(x.get("allocated_usd")) or 0))[:4]:
        pid = b.get("product_id")
        a = num(b.get("allocated_usd")) or 0.0
        asset = pid.split("-")[0].upper()
        mkt = num((held.get(asset) or {}).get("usd")) or 0.0
        p_eq = (mkt / total_equity * 100) if total_equity else None
        p_al = (a / allocated * 100) if allocated else None
        flag = ""
        if p_eq is not None and p_al is not None:
            over_eq, over_al = p_eq > 20.0, p_al > 20.0
            flag = "  <-- DISAGREE" if over_eq != over_al else ""
        print(f"  {pid:9s} {(f'{p_eq:.2f}%' if p_eq is not None else '?'):>19} "
              f"{(f'{p_al:.2f}%' if p_al is not None else '?'):>16}{flag}")
    print()
    print("  concentration_gate.py judges MARKET VALUE over TOTAL EQUITY (cash included).")
    print("  concentration_rotation.over_limit judges ALLOCATED over FLEET ALLOCATION.")
    print("  Those are different denominators and they disagree. One must be authoritative.")
    print()
    print("  nothing above was reconciled, sold or moved. every request was a GET.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
