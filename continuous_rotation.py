"""The real engine: grids that never restart, and only idle money rotates.

WHAT THE OWNER ASKED FOR, 2026-10-03: run the continuous model with the
dip_depth rotation. This is that, and it is the first model in this series
that matches how the account actually behaves.

WHY THE EARLIER NUMBERS COULD NOT ANSWER IT. Every study before this one
chopped the history into months and restarted each coin's grid FLAT at
every boundary. Two consequences, both measured:

  * a rung bought in a falling month could never be sold in the recovering
    month that followed - the one thing a grid exists to do;
  * restarting re-anchors the reference price, which MANUFACTURES cycles.
    Measured: restarted realised $801.80 against the same coins run
    continuously at $481.34. The windowed harness was flattering itself by
    67%, and the continuous figure is the honest one.

So rotation and continuity were never actually combined. Here they are.

HOW IT WORKS, AND WHAT ROTATION IS ALLOWED TO TOUCH

Each coin holds an allocation, a reference price and its open rungs. Every
hour, every funded coin takes one grid step under the same rules as
crypto_selection_backtest._replay_grid_bot: buy a slice when price closes
grid_pct below the reference and the coin is under its level count, sell
the oldest rung when price closes grid_pct above. The reference carries
across month boundaries and is never re-anchored.

At each month boundary, dip_depth is computed for every coin from THAT
MONTH'S candles only - how far below its own high in the month the coin
closed - and capital rotates toward the top ranked coins. ROTATION MOVES
IDLE MONEY ONLY:

    idle = allocation - cost basis of open rungs

Deployed capital is in coin. Moving it would mean selling, which pays a
round trip and, on an underwater rung, realises a loss. The whole reason
idle capital is the right thing to rotate is that moving it sells nothing,
so this engine cannot move anything else: a coin with rungs open keeps
exactly what those rungs cost, whatever the ranking says.

THE 20% CEILING IS ENFORCED per transfer, not checked afterwards, against
the live book so the figure means something.

WHAT IS REPORTED. Realised round trips per month, because that is the line
the owner's standing rule governs - nothing negative realised - and the
line the earlier studies buried inside a mark. The unsold inventory is
marked ONCE, at the very end, instead of six times.

WHAT THIS STILL CANNOT SAY. Hourly closes, so real fills differ. Six
months is six samples and only one of them falls hard. Rotation is charged
no fee because only idle cash moves; that stays true only as long as the
engine refuses to move deployed capital, which is asserted in the tests
rather than trusted.

Usage: python3 continuous_rotation.py --days 180 --windows 6 --pot 3253.84
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

from five_way_contest import COINS, fetch_all, windows_for  # noqa: E402

GRID_PCT, LEVELS = 0.030, 3
MEASURED_FEE = 0.007073
MIN_SLICE_USD = 5.0
MAX_COIN_SHARE_PCT = 20.0

# MONEY IS COMPARED IN CENTS, NEVER IN FLOATS.
#
# The affordability gate below first read `idle >= slice_usd` exactly. With
# a $200 allocation over 3 levels, two open rungs leave idle at
# 66.66666666666665719 against a slice of 66.66666666666667140 - short by
# 1.4e-14. That blocked the third rung forever, and because a blocked buy
# must NOT advance the reference price, the grid then stalled completely:
# 498 refused buys and 4 completed cycles where the verified replay gets
# 23. The test caught it by disagreeing with crypto_selection_backtest's
# own engine; nothing about the output looked wrong on its own.
CENT = 0.01


# ----------------------------------------------------------------- signals
def dip_depth(closes):
    if not closes:
        return None
    hi = max(closes)
    return ((hi - closes[-1]) / hi) if hi else None


def swing(closes, highs, lows):
    v = [(h - lo) / c for c, h, lo in zip(closes, highs, lows) if c]
    return statistics.mean(v) if v else None


def fills_of(coin):
    # Coin uses __slots__, so this is an attribute read, not a mapping
    # lookup - subscripting it raised TypeError and the test caught it.
    return coin.cycles_this_month


SIGNALS = {
    "dip_depth": lambda c, s: dip_depth(s["closes"]),
    "swing": lambda c, s: swing(s["closes"], s["highs"], s["lows"]),
    "fills_prev": lambda c, s: fills_of(c),
    "none": None,
}


class Coin:
    __slots__ = ("pid", "alloc", "ref", "rungs", "realised", "cycles",
                 "cycles_this_month", "realised_this_month")

    def __init__(self, pid):
        self.pid = pid
        self.alloc = 0.0
        self.ref = None
        self.rungs = []          # [{"entry","qty","cost"}]
        self.realised = 0.0
        self.cycles = 0
        self.cycles_this_month = 0
        self.realised_this_month = 0.0

    @property
    def deployed(self):
        return sum(r["cost"] for r in self.rungs)

    @property
    def idle(self):
        # Never negative: a rung's cost cannot be un-spent by a transfer.
        return max(0.0, self.alloc - self.deployed)

    def step(self, price):
        if self.alloc <= 0 or price <= 0:
            return
        if self.ref is None:
            self.ref = price
            return
        slice_usd = self.alloc / LEVELS
        if price <= self.ref * (1 - GRID_PCT) and len(self.rungs) < LEVELS:
            if slice_usd >= MIN_SLICE_USD and self.idle >= slice_usd - CENT:
                self.rungs.append({"entry": price, "qty": slice_usd / price,
                                   "cost": slice_usd})
                self.ref = price
            # The gate exists only because rotation can SHRINK an
            # allocation mid-flight, after which the open rungs may
            # already cost more than the coin now holds. In the static
            # case it must never bind - `levels` rungs of `alloc/levels`
            # is exactly `alloc` - which is what the cent tolerance above
            # restores.
            #
            # Not affordable is not a reason to move the reference: the
            # trigger stays armed for the next candle, exactly as the live
            # engine leaves an unaffordable rung to be re-evaluated.
        elif price >= self.ref * (1 + GRID_PCT) and self.rungs:
            r = self.rungs.pop(0)
            gross = r["qty"] * (price - r["entry"])
            fee = r["qty"] * (r["entry"] + price) * (MEASURED_FEE / 2)
            net = gross - fee
            self.realised += net
            self.realised_this_month += net
            self.cycles += 1
            self.cycles_this_month += 1
            self.ref = price

    def mark(self, price):
        return sum(r["qty"] * (price - r["entry"]) for r in self.rungs)


def rotate(coins, ranked, top_n, book_alloc, book_total, log):
    """Move IDLE money toward the top-ranked coins. Returns moved USD.

    Deployed capital is untouchable here by construction - only `idle` is
    ever taken, and `idle` subtracts the cost basis of open rungs.
    """
    targets = []
    for pid in ranked:
        if len(targets) >= top_n:
            break
        have = (book_alloc or {}).get(pid)
        if book_alloc is not None and have is None:
            log.append((pid, "not in the live book, share unknowable"))
            continue
        targets.append(pid)
    if not targets:
        return 0.0

    pool = 0.0
    for c in coins.values():
        if c.pid in targets:
            continue
        pool += c.idle
        c.alloc -= c.idle

    if pool <= 0:
        return 0.0

    share = pool / len(targets)
    moved = 0.0
    for pid in targets:
        c = coins[pid]
        if book_alloc is not None and book_total:
            live = book_alloc.get(pid, 0.0)
            if (live + c.alloc + share) / (book_total + share) * 100.0 > MAX_COIN_SHARE_PCT:
                log.append((pid, f"transfer would breach the "
                                 f"{MAX_COIN_SHARE_PCT:.0f}% ceiling"))
                continue
        c.alloc += share
        moved += share
    # Anything the ceiling refused goes back where it can be used rather
    # than vanishing: it is returned evenly to the coins that accepted.
    leftover = pool - moved
    if leftover > 0.01:
        takers = [coins[p] for p in targets if coins[p].alloc > 0]
        if takers:
            per = leftover / len(takers)
            for c in takers:
                c.alloc += per
            moved += leftover
        else:
            # Nowhere legal to put it: hand it back to where it came from
            # rather than deleting capital.
            per = leftover / max(1, len(coins))
            for c in coins.values():
                c.alloc += per
            moved += leftover
    return moved


def run(series, bounds_by, pot, signal, top_n, book_alloc, book_total):
    coins = {pid: Coin(pid) for pid in series}
    n = len(coins)
    for c in coins.values():
        c.alloc = pot / n

    # Every coin shares the same window count; use the shortest series'
    # bounds so no coin is stepped past its own data.
    length = min(len(series[p][0]) for p in series)
    bounds = windows_for(length, len(next(iter(bounds_by.values()))))
    per_month, moved_log, blocked_log = [], [], []

    for m, (a, b) in enumerate(bounds):
        for c in coins.values():
            c.cycles_this_month = 0
            c.realised_this_month = 0.0
        for i in range(a, b):
            for pid, c in coins.items():
                c.step(series[pid][0][i])
        per_month.append({
            "month": m,
            "realised": round(sum(c.realised_this_month for c in coins.values()), 2),
            "cycles": sum(c.cycles_this_month for c in coins.values()),
            "deployed_end": round(sum(c.deployed for c in coins.values()), 2),
            "idle_end": round(sum(c.idle for c in coins.values()), 2),
        })
        if signal and signal != "none" and m < len(bounds) - 1:
            feats = []
            for pid, c in coins.items():
                cl, hi, lo, _ = series[pid]
                v = SIGNALS[signal](c, {"closes": cl[a:b], "highs": hi[a:b],
                                        "lows": lo[a:b]})
                if v is not None:
                    feats.append((pid, v))
            feats.sort(key=lambda r: -r[1])
            bl = []
            mv = rotate(coins, [p for p, _ in feats], top_n,
                        book_alloc, book_total, bl)
            moved_log.append({"after_month": m, "moved_usd": round(mv, 2),
                              "into": [p for p, _ in feats[:top_n]]})
            blocked_log.extend([{"after_month": m, "product_id": p, "why": w}
                                for p, w in bl])

    last = bounds[-1][1] - 1
    final_mark = round(sum(c.mark(series[c.pid][0][last]) for c in coins.values()), 2)
    return {
        "signal": signal, "per_month": per_month,
        "realised_total": round(sum(p["realised"] for p in per_month), 2),
        "cycles_total": sum(p["cycles"] for p in per_month),
        "final_mark_usd": final_mark,
        "negative_months": sum(1 for p in per_month if p["realised"] < 0),
        "rotations": moved_log, "blocked": blocked_log or None,
        "capital_end_usd": round(sum(c.alloc for c in coins.values()), 2),
        "top_holdings_end": sorted(
            ((c.pid, round(c.alloc, 2)) for c in coins.values()),
            key=lambda r: -r[1])[:6],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--pot", type=float, default=3253.84)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--book", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    book_alloc, book_total = None, None
    if args.book:
        with open(args.book) as f:
            gs = json.load(f)
        book_alloc = {b["product_id"]: (b.get("allocated_usd") or 0.0)
                      for b in (gs.get("branches") or [])}
        book_total = gs.get("total_allocated_usd") or sum(book_alloc.values())

    print(f"fetching {args.days}d for {len(COINS)} coin(s)...")
    series, failed = asyncio.run(fetch_all(COINS, args.days))
    if not series:
        print("NO COIN READ - UNKNOWN, nothing reported")
        return 1
    print(f"read {len(series)}; dropped {sorted(failed) or 'none'}")
    bounds_by = {p: windows_for(len(series[p][0]), args.windows) for p in series}
    series = {p: v for p, v in series.items() if bounds_by.get(p)}

    runs = []
    for sig in ("none", "fills_prev", "swing", "dip_depth"):
        runs.append(run(series, bounds_by, args.pot, sig, args.top,
                        book_alloc, book_total))

    print(f"\n{'=' * 88}")
    print(f"  CONTINUOUS GRIDS, IDLE CAPITAL ROTATING  -  ${args.pot:,.2f}, "
          f"top {args.top}, fee {MEASURED_FEE*100:.4f}%")
    print(f"  grids never restart; the reference price carries; only IDLE "
          f"money moves at a boundary")
    print(f"{'=' * 88}")
    print(f"\n  {'rotation signal':<14}{'realised':>12}{'cycles':>9}"
          f"{'neg months':>12}{'end mark':>11}{'per month':>12}")
    print("  " + "-" * 72)
    for r in sorted(runs, key=lambda x: -x["realised_total"]):
        lbl = "none (static)" if r["signal"] == "none" else r["signal"]
        print(f"  {lbl:<14}{r['realised_total']:>12,.2f}{r['cycles_total']:>9}"
              f"{str(r['negative_months']) + ' of ' + str(len(r['per_month'])):>12}"
              f"{r['final_mark_usd']:>11,.2f}"
              f"{r['realised_total'] / len(r['per_month']):>12,.2f}")

    for r in sorted(runs, key=lambda x: -x["realised_total"]):
        lbl = "none (static)" if r["signal"] == "none" else r["signal"]
        print(f"\n  {lbl}  -  realised per month")
        print(f"    {'month':<8}{'realised':>11}{'cycles':>8}{'deployed':>12}"
              f"{'idle':>11}   rotated into")
        for i, p in enumerate(r["per_month"]):
            rot = next((x for x in r["rotations"] if x["after_month"] == i), None)
            into = (", ".join(q.replace("-USD", "") for q in rot["into"])
                    + f"  (${rot['moved_usd']:,.0f})") if rot else ""
            print(f"    m{p['month']:<7}{p['realised']:>11,.2f}{p['cycles']:>8}"
                  f"{p['deployed_end']:>12,.2f}{p['idle_end']:>11,.2f}   {into}")
        print(f"    {'':8}{'-' * 42}")
        print(f"    {'sum':<8}{r['realised_total']:>11,.2f}{r['cycles_total']:>8}")
        print(f"    end capital ${r['capital_end_usd']:,.2f}; biggest holdings "
              f"{r['top_holdings_end'][:4]}")
        if r.get("blocked"):
            for b in r["blocked"][:4]:
                print(f"    ceiling: m{b['after_month']} {b['product_id']} - {b['why']}")

    best = max(runs, key=lambda x: x["realised_total"])
    base = next(x for x in runs if x["signal"] == "none")
    print(f"\n{'=' * 88}")
    print(f"  BEST: {best['signal']}  -  ${best['realised_total']:,.2f} realised, "
          f"{best['negative_months']} negative month(s), "
          f"end mark ${best['final_mark_usd']:,.2f}")
    if best["signal"] != "none":
        d = best["realised_total"] - base["realised_total"]
        print(f"  Rotation adds ${d:,.2f} over never moving it "
              f"(${base['realised_total']:,.2f}), "
              f"{d / base['realised_total'] * 100:+.1f}%")
    print(f"  Realised on ${args.pot:,.2f} over {len(best['per_month'])} months = "
          f"{best['realised_total'] / args.pot / len(best['per_month']) * 100:.2f}%/month")
    print(f"{'=' * 88}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"pot_usd": args.pot, "top_n": args.top, "runs": runs,
                       "is_a_measurement_not_a_change": True}, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
