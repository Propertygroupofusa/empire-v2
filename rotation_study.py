"""Idle allocation is not a column. What would rotating it have earned?

THE QUESTION, in the account owner's own words, 2026-10-03: idle unspent
allocation "should be rotation capital". So this measures exactly that -
not whether rotation sounds better, but what it would have returned.

WHAT IT RUNS ON. five_way_contest.py already replayed the live grid engine
on real hourly Coinbase candles, 180 days, 23 held coins, six consecutive
30-day windows, at the measured 0.7073% maker round trip. This reads those
per-coin per-window results and asks a different question of them: given a
fixed pot of capital, which ALLOCATION POLICY would have done best?

NO LOOKAHEAD, AND THAT IS THE WHOLE POINT. Every rotating policy here
decides window N's allocation using only what was knowable at the END of
window N-1. A policy that picks each window's winners with hindsight is
not a strategy, it is a scoreboard - so the hindsight basket is included,
clearly labelled, as the CEILING nobody can actually reach.

THE POLICIES

  hold_idle        the honest baseline: capital sits unspent. Earns
                   exactly $0.00, every window, forever. This is what
                   $526.56 is doing in five branches right now.
  static_all23     spread evenly over all 23 held coins, never moved.
                   Roughly the shape of the live book.
  rotate_by_pnl    each window, move the whole pot into the N coins with
                   the best net in the PREVIOUS window.
  rotate_by_fills  each window, move it into the N coins that TRADED most
                   in the previous window. Activity, not profit - a coin
                   that fires often is a coin whose rungs are cycling.
  rotate_by_both   ranked by previous-window net per trade, requiring at
                   least one trade. Quality of fill, not just count.
  hindsight_best   THE CEILING, not a policy: each window's actual best N,
                   chosen after the fact. Unreachable by construction and
                   reported only so the gap to it is visible.

THE ONE SIMPLIFICATION, STATED PLAINLY. The contest measured each coin at
$200. A different allocation is scaled linearly from that, which holds
while every slice still clears the venue minimum: at the smallest pot here
the slice is $35 against a $5 floor, so it does. It would NOT hold for a
pot small enough to push slices under the floor, and this refuses to
report such a case rather than scaling through it.

WHAT THIS CANNOT SAY. Six windows is six samples. Window 1 lost money for
every strategy on the full book, so any policy's worst case is dominated
by one month. And rotation here is free: real rotation pays a round trip
to exit a coin, which this does NOT charge, because the capital being
rotated is UNSPENT allocation - it is not in coin, so moving it sells
nothing. That is only true of idle capital, and it is why idle capital is
the right thing to rotate and deployed capital is not.

Usage: python3 rotation_study.py --contest contest180.json --pot 526.56
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys

# The contest measured every coin at this allocation.
BASE_ALLOC = 200.0
# A slice under this is not placeable (MIN_TRADE_USD in crypto_grid_bot).
MIN_SLICE_USD = 5.0
# The winning config's rung count, so slice size can be checked.
LEVELS = 3

# THE ACCOUNT OWNER'S OWN STANDING LIMIT: no coin over 20% of the fleet.
#
# This is not a tuning knob and it is not lowered to make a policy look
# better. It is here because the unguarded policy BREAKS it: ranking by
# last window's fill count put ZEC in window 2, and $175 on top of ZEC's
# $2,271 would have taken it to 28.8% of the book. A policy that violates
# a standing risk limit is not a policy that can be run, however well it
# backtests, so the guard is part of the measurement rather than a caveat
# printed underneath it.
MAX_COIN_SHARE_PCT = 20.0


def load_cells(path, strategy="grid_3x3.0", run=0):
    with open(path) as f:
        d = json.load(f)
    cells = d["runs"][run]["cells"]
    by = {}
    for c in cells:
        if c["strategy"] != strategy or c.get("net_usd") is None:
            continue
        by.setdefault(c["window"], {})[c["product_id"]] = {
            "net": c["net_usd"], "trades": c["trades"] or 0}
    return by, d["runs"][run]["round_trip_fee_rate"], sorted(
        {p for w in by.values() for p in w})


def scaled(net_at_base, alloc):
    return net_at_base * (alloc / BASE_ALLOC)


def slice_usd(alloc, n_coins):
    return (alloc / n_coins) / LEVELS if n_coins else 0.0


def concentration_blocked(pid, add_usd, current_alloc, book_total):
    """Would adding `add_usd` to `pid` break the 20% ceiling? UNKNOWN reads
    as blocked: without the live book this cannot be judged, and guessing
    in favour of the trade is how a limit gets quietly crossed."""
    if not current_alloc or not book_total:
        return False, None            # no book supplied: guard not in force
    have = current_alloc.get(pid)
    if have is None:
        return True, "not in the live book, so its share cannot be judged"
    share = (have + add_usd) / (book_total + add_usd) * 100.0
    if share > MAX_COIN_SHARE_PCT:
        return True, (f"would reach {share:.1f}% of the book, over the "
                      f"{MAX_COIN_SHARE_PCT:.0f}% ceiling")
    return False, None


def run_policy(by, windows, pot, picker, top_n, label,
               current_alloc=None, book_total=None):
    """picker(prev_window_data, all_coins) -> list of product_ids, or None
    for 'every coin' (static). Returns per-window nets and the picks."""
    nets, picks = [], []
    picks_blocked = {}
    for w in windows:
        data = by.get(w) or {}
        if not data:
            nets.append(None)
            picks.append([])
            continue
        prev = by.get(w - 1) if w > min(windows) else None
        chosen = picker(prev, sorted(data))
        if chosen is None:
            chosen = sorted(data)
        chosen = [c for c in chosen if c in data]
        # Walk the ranking in order and take the first top_n that the
        # ceiling allows, rather than taking the top n and dropping the
        # blocked ones - otherwise a blocked pick silently shrinks the pot's
        # spread instead of being replaced.
        if top_n and current_alloc:
            each = pot / top_n
            kept, blocked = [], []
            for c in chosen:
                stop, why = concentration_blocked(c, each, current_alloc, book_total)
                if stop:
                    blocked.append((c, why))
                    continue
                kept.append(c)
                if len(kept) >= top_n:
                    break
            if blocked:
                picks_blocked.setdefault(w, []).extend(blocked)
            chosen = kept
        chosen = chosen[:top_n or len(chosen)]
        if not chosen:
            # No basis to choose on (the first window of a rotating
            # policy has no previous window). That is UNKNOWN for this
            # window, not a zero - zero would silently credit the policy
            # with sitting out a losing month.
            nets.append(None)
            picks.append([])
            continue
        each = pot / len(chosen)
        if slice_usd(pot, len(chosen)) < MIN_SLICE_USD:
            nets.append(None)
            picks.append([])
            continue
        nets.append(round(sum(scaled(data[c]["net"], each) for c in chosen), 2))
        picks.append(chosen)
    return {"policy": label, "nets": nets, "picks": picks,
            "blocked_by_ceiling": {str(k): v for k, v in picks_blocked.items()} or None}


def pick_top(prev, key, n):
    def picker(prev_data, all_coins):
        if prev_data is None:
            return []
        rows = [(p, d) for p, d in prev_data.items() if p in all_coins]
        if key == "pnl":
            rows.sort(key=lambda r: -r[1]["net"])
        elif key == "fills":
            rows.sort(key=lambda r: -r[1]["trades"])
        else:                                    # net per trade, needs a trade
            rows = [r for r in rows if r[1]["trades"] > 0]
            rows.sort(key=lambda r: -(r[1]["net"] / r[1]["trades"]))
        return [p for p, _ in rows[:n]]
    return picker


def summarise(res, pot):
    nets = [n for n in res["nets"] if n is not None]
    if not nets:
        return {**res, "readable": False,
                "detail": "no window could be measured for this policy"}
    return {
        "policy": res["policy"],
        "readable": True,
        "windows_measured": len(nets),
        "windows_positive": sum(1 for n in nets if n > 0),
        "total_usd": round(sum(nets), 2),
        "median_window_usd": round(statistics.median(nets), 2),
        "best_window_usd": round(max(nets), 2),
        "worst_window_usd": round(min(nets), 2),
        "median_pct_of_pot": round(statistics.median(nets) / pot * 100, 2),
        "nets": res["nets"],
        "picks": res["picks"],
        "blocked_by_ceiling": res.get("blocked_by_ceiling"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contest", default="/tmp/claude-0/contest180.json")
    ap.add_argument("--pot", type=float, default=526.56)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--json", default="")
    ap.add_argument("--book", default="",
                    help="grid-status JSON; enables the 20% concentration guard")
    args = ap.parse_args()

    current_alloc, book_total = None, None
    if args.book:
        with open(args.book) as f:
            gs = json.load(f)
        current_alloc = {b["product_id"]: (b.get("allocated_usd") or 0.0)
                         for b in (gs.get("branches") or [])}
        book_total = gs.get("total_allocated_usd") or sum(current_alloc.values())

    by, fee, coins = load_cells(args.contest)
    windows = sorted(by)
    if not windows:
        print("the contest file carried no readable windows - nothing is reported")
        return 1

    pot, n = args.pot, args.top
    G = {"current_alloc": current_alloc, "book_total": book_total}
    policies = [
        run_policy(by, windows, pot, lambda p, a: [], 0, "hold_idle"),
        run_policy(by, windows, pot, lambda p, a: None, 0, "static_all23"),
        run_policy(by, windows, pot, pick_top(by, "pnl", n), n,
                   f"rotate_by_pnl_top{n}", **G),
        run_policy(by, windows, pot, pick_top(by, "fills", n), n,
                   f"rotate_by_fills_top{n}", **G),
        run_policy(by, windows, pot, pick_top(by, "quality", n), n,
                   f"rotate_by_both_top{n}", **G),
    ]
    # hold_idle earns zero by definition, not by measurement.
    policies[0]["nets"] = [0.0] * len(windows)
    policies[0]["picks"] = [[] for _ in windows]

    # The ceiling: each window's real best n, chosen after the fact.
    ceil_nets, ceil_picks = [], []
    for w in windows:
        data = by[w]
        best = sorted(data, key=lambda p: -data[p]["net"])[:n]
        each = pot / len(best)
        ceil_nets.append(round(sum(scaled(data[c]["net"], each) for c in best), 2))
        ceil_picks.append(best)
    policies.append({"policy": f"hindsight_best_top{n}",
                     "nets": ceil_nets, "picks": ceil_picks})

    rows = [summarise(p, pot) for p in policies]

    # COMPARE ON THE WINDOWS EVERY POLICY CAN SCORE.
    #
    # A rotating policy cannot score the first window - it has no previous
    # window to choose from - so its row covers five windows where the
    # static rows cover six. Ranking "5 of 6 positive" against "4 of 5"
    # put the static policy first and the best policy third, which is the
    # same apples-to-oranges mistake this project has been burned by
    # before. So the ranking, and the headline, use only the windows that
    # are common to all of them.
    common = [i for i in range(len(windows))
              if all(r["nets"][i] is not None for r in rows if r.get("readable"))]
    for r in rows:
        if not r.get("readable"):
            continue
        v = [r["nets"][i] for i in common]
        r["common_windows"] = len(v)
        r["common_total_usd"] = round(sum(v), 2)
        r["common_median_usd"] = round(statistics.median(v), 2) if v else None
        r["common_worst_usd"] = round(min(v), 2) if v else None
        r["common_positive"] = sum(1 for x in v if x > 0)
    real = [r for r in rows if r.get("readable") and "hindsight" not in r["policy"]]
    real.sort(key=lambda r: (-(r["common_total_usd"] or 0),
                             -(r["common_median_usd"] or 0)))

    print(f"\n{'=' * 80}")
    print(f"  ROTATING ${pot:,.2f} OF IDLE ALLOCATION  -  {len(windows)} windows, "
          f"{len(coins)} coins, fee {fee * 100:.4f}%")
    print(f"  every rotating policy decides window N from window N-1 only")
    print(f"{'=' * 80}")
    print(f"\n  {'policy':<24}{'pos':>6}{'total':>11}{'median':>10}"
          f"{'%/30d':>8}{'best':>10}{'worst':>10}")
    print("  " + "-" * 78)
    for r in rows:
        if not r.get("readable"):
            print(f"  {r['policy']:<24}  UNREADABLE - {r['detail']}")
            continue
        tag = "  <- ceiling, not reachable" if "hindsight" in r["policy"] else ""
        print(f"  {r['policy']:<24}"
              f"{str(r['windows_positive']) + '/' + str(r['windows_measured']):>6}"
              f"{r['total_usd']:>11,.2f}{r['median_window_usd']:>10,.2f}"
              f"{r['median_pct_of_pot']:>8.2f}{r['best_window_usd']:>10,.2f}"
              f"{r['worst_window_usd']:>10,.2f}{tag}")

    print(f"\n  per window (w0 oldest; '--' = no prior window to choose from):")
    print("  " + " " * 24 + "".join(f"{('w' + str(w)):>11}" for w in windows))
    for r in rows:
        if not r.get("readable"):
            continue
        cells = "".join(f"{v:>11,.2f}" if v is not None else f"{'--':>11}"
                        for v in r["nets"])
        print(f"  {r['policy']:<24}{cells}")

    best = real[0] if real else None
    print(f"\n  ranked on the {len(common)} window(s) every policy can score "
          f"(w{', w'.join(str(windows[i]) for i in common)}):")
    print(f"  {'policy':<24}{'total':>11}{'median':>10}{'worst':>10}{'pos':>6}")
    print("  " + "-" * 61)
    for r in rows:
        if not r.get("readable"):
            continue
        tag = "   <- ceiling" if "hindsight" in r["policy"] else ""
        print(f"  {r['policy']:<24}{r['common_total_usd']:>11,.2f}"
              f"{r['common_median_usd']:>10,.2f}{r['common_worst_usd']:>10,.2f}"
              f"{str(r['common_positive']) + '/' + str(r['common_windows']):>6}{tag}")

    print(f"\n{'=' * 80}")
    if best:
        hold = next(r for r in rows if r["policy"] == "hold_idle")
        stat = next((r for r in rows if r["policy"] == "static_all23"), None)
        print(f"  BEST REACHABLE POLICY: {best['policy']}")
        print(f"  Over the {best['common_windows']} comparable window(s): "
              f"${best['common_total_usd']:,.2f} total, median "
              f"${best['common_median_usd']:,.2f} "
              f"({best['common_median_usd'] / pot * 100:.2f}% of the pot per 30 days), "
              f"worst ${best['common_worst_usd']:,.2f}, "
              f"{best['common_positive']} of {best['common_windows']} positive.")
        print(f"  Against leaving it idle: ${best['common_total_usd'] - hold['common_total_usd']:,.2f} more.")
        if stat and stat["common_total_usd"] > 0:
            print(f"  Against spreading it evenly and never moving it: "
                  f"{best['common_total_usd'] / stat['common_total_usd']:.1f}x "
                  f"(${stat['common_total_usd']:,.2f}).")
    print(f"{'=' * 80}")

    for r in rows:
        if r.get("readable") and r["picks"] and "static" not in r["policy"] \
                and "hold" not in r["policy"]:
            print(f"\n  {r['policy']} chose:")
            for w, pk in zip(windows, r["picks"]):
                print(f"    w{w}: {', '.join(p.replace('-USD', '') for p in pk) or '(no prior window)'}")
            src = next((x for x in policies if x["policy"] == r["policy"]), None)
            for w, bl in ((src or {}).get("blocked_by_ceiling") or {}).items():
                for pid, why in bl:
                    print(f"      w{w} SKIPPED {pid}: {why}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"pot_usd": pot, "top_n": n, "fee_rate": fee,
                       "windows": windows, "policies": rows,
                       "is_a_measurement_not_a_change": True}, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
