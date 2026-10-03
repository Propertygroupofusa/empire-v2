"""The losing months, taken apart. How much of them is a loss at all?

THE OWNER'S ASK, 2026-10-03: the rotation table shows worst months of
-$355.51, -$174.90, -$29.91 and -$280.41, and only the hindsight row is
positive every month. He wants those negatives turned positive, or
explained into something he can use. He also said he hopes the all-positive
row "is the one that we have" - it is not, and it cannot be: that row picks
each month's winners after the fact. It is a ceiling, not a strategy, and
nothing here pretends otherwise.

WHAT THIS FOUND FIRST, BEFORE TRYING TO FIX ANYTHING.

crypto_selection_backtest._replay_grid_bot does not end a window by
discarding open rungs. It marks them to the window's last price and tags
them OPEN_AT_WINDOW_END, and _summarize_strategy_trades then adds that
mark into total_pnl alongside the completed round trips. So every number
in every table so far is REALISED CYCLES PLUS AN UNSOLD-INVENTORY MARK.

That matters enormously for a grid, because a grid is SUPPOSED to be
holding cheap inventory at the bottom of a fall. Marking it at the low and
calling the result a loss describes the window boundary, not the strategy.
The owner's own standing rule is that nothing negative is realised - so
the realised line is the one that decides, and it had never been shown
separately.

HOW THE SPLIT IS TAKEN. Not by reimplementing the replay - that mistake
has already been made once in this project, stubbing the very function
under test and declaring it healthy. Instead _summarize_strategy_trades is
wrapped so the REAL replay runs untouched and its raw tagged trade list is
captured on the way past. GRID_CYCLE entries are realised; the
OPEN_AT_WINDOW_END entries are the mark.

THEN THE ACTUAL FIX ATTEMPTS, each measured rather than argued:

  split          realised vs mark, per month, per policy. If realised is
                 positive in the months that showed a loss, the negatives
                 are inventory, not damage - and the answer is reporting,
                 not a new strategy.
  carry          the grid is not restarted flat each month. It runs
                 continuously across all 180 days, and only the UNDEPLOYED
                 portion rotates at each month boundary. This is what the
                 owner has been describing: idle rotates, deployed stays
                 until it sells. The windowed harness never let a month's
                 inventory be sold in the month that followed, which is
                 precisely where a grid earns it back.
A cash brake - deploy nothing after a hard fleet-wide fall - was planned
here as a third attempt and is NOT implemented, because the first two
dissolved the premise: realised profit never went negative in any month of
any policy, so there is no losing month for a brake to avoid. A brake
would only have suppressed the months in which the grid was buying cheap
inventory, which is the months it earns from. Saying so is cheaper than
building it.

Usage: python3 worst_month_study.py --days 180 --windows 6 --pot 3253.84
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys

sys.path.insert(0, "/home/user/empire-v2")
os.environ.setdefault("COINBASE_API_KEY_NAME", "unused-public-endpoint-only")
os.environ.setdefault("COINBASE_API_PRIVATE_KEY", "unused-public-endpoint-only")

import crypto_selection_backtest as bt  # noqa: E402
from five_way_contest import COINS, fetch_all, windows_for  # noqa: E402

GRID_PCT, LEVELS, BASE_ALLOC = 0.030, 3, 200.0
MEASURED_FEE = 0.007073
MIN_SLICE_USD = 5.0
MAX_COIN_SHARE_PCT = 20.0

_CAPTURED = {}


def replay_split(closes, highs, lows, spend=BASE_ALLOC):
    """The REAL replay, with its tagged trade list observed in passing.

    Returns realised (completed round trips), mark (open rungs valued at
    the window's last close), trades, and the open rung count. None when
    the replay itself declines the window.
    """
    real = bt._summarize_strategy_trades
    grabbed = {}

    def spy(trades, sp):
        grabbed["trades"] = list(trades or [])
        return real(trades, sp)

    bt._summarize_strategy_trades = spy
    try:
        res = bt._replay_grid_bot(closes, highs, lows, spend=spend,
                                  grid_pct=GRID_PCT, num_levels=LEVELS)
    finally:
        bt._summarize_strategy_trades = real
    if res is None:
        return None
    tr = grabbed.get("trades") or []
    realised = sum(v for tag, v in tr if tag == "GRID_CYCLE")
    mark = sum(v for tag, v in tr if tag == "OPEN_AT_WINDOW_END")
    # The split must reconstruct the published total exactly, or the
    # observation is wrong and nothing built on it can be trusted.
    if abs((realised + mark) - res["total_pnl"]) > 1e-6:
        raise AssertionError(
            f"split {realised + mark} does not reconstruct total "
            f"{res['total_pnl']} - the capture is not seeing the real trades")
    return {"realised": realised, "mark": mark, "total": res["total_pnl"],
            "cycles": sum(1 for tag, _ in tr if tag == "GRID_CYCLE"),
            "open_at_end": res.get("open_slices_at_end") or 0}


# --------------------------------------------------------------- continuous
def replay_carry(closes, highs, lows, spend, bounds):
    """One continuous grid over the whole series, reporting per segment.

    The windowed harness restarts the grid flat at every boundary, so a
    rung bought in a falling month can never be sold in the recovering
    month that follows - the one thing a grid is for. This runs the same
    rules once, start to finish, and attributes each completed cycle to
    the segment it closed in. Open rungs are marked ONLY at the very end,
    once, instead of six times.

    The rules are kept identical to _replay_grid_bot on purpose and the
    test suite pins them against it on a single window, where the two must
    agree exactly.
    """
    n = len(closes)
    if n < 2:
        return None
    slice_usd = spend / LEVELS
    seg_of = {}
    for s, (a, b) in enumerate(bounds):
        for k in range(a, b):
            seg_of[k] = s
    per = {s: {"realised": 0.0, "cycles": 0} for s in range(len(bounds))}
    open_slices, reference = [], closes[bounds[0][0]]
    i = bounds[0][0] + 1
    end = bounds[-1][1]
    while i < end:
        price = closes[i]
        if price <= reference * (1 - GRID_PCT) and len(open_slices) < LEVELS:
            open_slices.append({"entry": price, "qty": slice_usd / price})
            reference = price
        elif price >= reference * (1 + GRID_PCT) and open_slices:
            slot = open_slices.pop(0)
            gross = slot["qty"] * (price - slot["entry"])
            fee = slot["qty"] * (slot["entry"] + price) * (MEASURED_FEE / 2)
            s = seg_of.get(i, len(bounds) - 1)
            per[s]["realised"] += gross - fee
            per[s]["cycles"] += 1
            reference = price
        i += 1
    final = closes[end - 1]
    mark = sum(s["qty"] * (final - s["entry"]) for s in open_slices)
    return {"per_segment": per, "final_mark": mark,
            "open_at_end": len(open_slices)}


def blocked(pid, add, alloc, book):
    if not alloc or not book:
        return False
    have = alloc.get(pid)
    if have is None:
        return True
    return (have + add) / (book + add) * 100.0 > MAX_COIN_SHARE_PCT


def pick(ranked, pot, n, alloc, book):
    each = pot / n if n else 0.0
    out = []
    for p in ranked:
        if blocked(p, each, alloc, book):
            continue
        out.append(p)
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--pot", type=float, default=3253.84)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--book", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    alloc, book = None, None
    if args.book:
        with open(args.book) as f:
            gs = json.load(f)
        alloc = {b["product_id"]: (b.get("allocated_usd") or 0.0)
                 for b in (gs.get("branches") or [])}
        book = gs.get("total_allocated_usd") or sum(alloc.values())

    bt.BACKTEST_ROUND_TRIP_FEE_RATE = MEASURED_FEE
    print(f"fetching {args.days}d for {len(COINS)} coin(s)...")
    series, failed = asyncio.run(fetch_all(COINS, args.days))
    if not series:
        print("NO COIN READ - UNKNOWN, nothing reported")
        return 1
    print(f"read {len(series)}; dropped {sorted(failed) or 'none'}")

    # ---- windowed split, per coin per month
    split, fills, bounds_by = {}, {}, {}
    for pid, (c, h, lo, _t) in series.items():
        bnds = windows_for(len(c), args.windows)
        if not bnds:
            continue
        bounds_by[pid] = bnds
        for w, (a, b) in enumerate(bnds):
            r = replay_split(c[a:b], h[a:b], lo[a:b])
            if r is None:
                continue
            split.setdefault(w, {})[pid] = r
            fills.setdefault(w, {})[pid] = r["cycles"]
    months = sorted(split)

    # ---- the four policies the owner is looking at
    def rank_fills(prev):
        return [p for p, _ in sorted(fills.get(prev, {}).items(),
                                     key=lambda r: -r[1])]

    def rank_pnl(prev):
        return [p for p, _ in sorted(split.get(prev, {}).items(),
                                     key=lambda r: -r[1]["total"])]

    policies = {"static_all23": None,
                "rotate_by_pnl_top3": rank_pnl,
                "rotate_by_fills_top3": rank_fills}

    rows = {}
    for name, ranker in policies.items():
        per = []
        for w in months:
            if ranker is None:
                chosen = sorted(split[w])
            else:
                if w - 1 not in split:
                    per.append(None)
                    continue
                chosen = pick([p for p in ranker(w - 1) if p in split[w]],
                              args.pot, args.top, alloc, book)
            if not chosen or (args.pot / len(chosen)) / LEVELS < MIN_SLICE_USD:
                per.append(None)
                continue
            each = args.pot / len(chosen)
            sc = each / BASE_ALLOC
            per.append({
                "realised": round(sum(split[w][p]["realised"] * sc for p in chosen), 2),
                "mark": round(sum(split[w][p]["mark"] * sc for p in chosen), 2),
                "total": round(sum(split[w][p]["total"] * sc for p in chosen), 2),
                "coins": chosen})
        rows[name] = per

    print(f"\n{'=' * 86}")
    print(f"  THE LOSING MONTHS, SPLIT  -  ${args.pot:,.2f} pot, top {args.top}, "
          f"fee {MEASURED_FEE*100:.4f}%")
    print(f"  realised = completed round trips.  mark = unsold rungs valued at "
          f"the month's last price.")
    print(f"{'=' * 86}")
    for name, per in rows.items():
        print(f"\n  {name}")
        print(f"    {'month':<8}{'TOTAL':>12}{'realised':>12}{'mark':>12}   coins")
        neg_total = neg_real = 0
        for w, r in zip(months, per):
            if r is None:
                print(f"    m{w:<7}{'--':>12}{'--':>12}{'--':>12}   (no prior month)")
                continue
            if r["total"] < 0:
                neg_total += 1
            if r["realised"] < 0:
                neg_real += 1
            flag = "  <-- shows a loss" if r["total"] < 0 else ""
            print(f"    m{w:<7}{r['total']:>12,.2f}{r['realised']:>12,.2f}"
                  f"{r['mark']:>12,.2f}   "
                  f"{', '.join(p.replace('-USD','') for p in r['coins'][:3])}{flag}")
        got = [r for r in per if r]
        print(f"    {'':8}{'-' * 36}")
        print(f"    {'sum':<8}{sum(r['total'] for r in got):>12,.2f}"
              f"{sum(r['realised'] for r in got):>12,.2f}"
              f"{sum(r['mark'] for r in got):>12,.2f}")
        print(f"    months showing a loss on TOTAL: {neg_total}   "
              f"on REALISED: {neg_real}")

    # ---- continuous carry, same policy-free question: does letting the
    # grid keep its inventory across the boundary change the picture?
    print(f"\n{'=' * 86}")
    print(f"  CONTINUOUS vs RESTARTED - all 23 coins, equal weight, no rotation")
    print(f"  the windowed model restarts the grid flat every month; this one "
          f"never does")
    print(f"{'=' * 86}")
    carry = {s: 0.0 for s in range(len(months))}
    carry_cycles = {s: 0 for s in range(len(months))}
    carry_mark = 0.0
    each = args.pot / max(1, len(series))
    for pid, (c, h, lo, _t) in series.items():
        bnds = bounds_by.get(pid)
        if not bnds:
            continue
        r = replay_carry(c, h, lo, BASE_ALLOC, bnds)
        if r is None:
            continue
        sc = each / BASE_ALLOC
        for s, d in r["per_segment"].items():
            carry[s] += d["realised"] * sc
            carry_cycles[s] += d["cycles"]
        carry_mark += r["final_mark"] * sc
    print(f"\n    {'month':<8}{'realised (continuous)':>24}{'cycles':>10}"
          f"{'realised (restarted)':>24}")
    st = rows["static_all23"]
    for w in months:
        a = carry.get(w, 0.0)
        b = st[w]["realised"] if st[w] else None
        print(f"    m{w:<7}{a:>24,.2f}{carry_cycles.get(w,0):>10}"
              f"{(f'{b:,.2f}' if b is not None else '--'):>24}")
    print(f"    {'':8}{'-' * 58}")
    print(f"    {'sum':<8}{sum(carry.values()):>24,.2f}"
          f"{sum(carry_cycles.values()):>10}"
          f"{sum(r['realised'] for r in st if r):>24,.2f}")
    print(f"\n    one single mark at the very end, not six: "
          f"${carry_mark:,.2f}")
    print(f"    months with NEGATIVE realised, continuous: "
          f"{sum(1 for v in carry.values() if v < 0)} of {len(months)}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"pot_usd": args.pot, "policies": rows,
                       "continuous_realised": carry,
                       "continuous_cycles": carry_cycles,
                       "continuous_final_mark": round(carry_mark, 2),
                       "is_a_measurement_not_a_change": True}, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
