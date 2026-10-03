"""Five ways to make $200 work, raced against each other on real candles.

WHAT THE ACCOUNT OWNER ASKED FOR, 2026-10-03: five different ways to make
money, $200 each, run them, and see which one wins CONSISTENTLY. Not which
one posts the biggest single number - which one keeps winning.

SHADOW MODE. This replays strategies against real historical Coinbase
candles. It places no order, moves no money, touches no live setting and
imports nothing from this module into any live bot. It is a measurement.

HOW IT ANSWERS "CONSISTENTLY" AND WHY THAT MATTERS. A single 180-day total
is one sample dressed up as a conclusion - this codebase has already been
burned twice calling two readings a trend. So the history is cut into SIX
consecutive 30-day windows and every strategy runs in every window on
every coin. What gets reported is:

    windows_won        how many of the six windows it topped
    windows_positive   how many it ended above zero in
    median_window_usd  its typical window, not its best one
    worst_window_usd   what it costs when it is wrong

A strategy that wins four windows out of six beats one that wins once by a
larger amount, because the second one cannot be relied on. The per-window
table is printed in full so nobody has to trust the summary.

THE FIVE, AND WHY THESE FIVE

  1. grid_10x3.0  - 10 rungs at 3.0% spacing. What the fleet would run on
                    its own: DEFAULT_GRID_LEVELS is 10 and every branch's
                    stored spacing is 3.0%.
  2. grid_3x3.0   - 3 rungs at 3.0%. THE CONTROL: what is live right now,
                    because the promoted candidate '3_levels_2.5pct' caps
                    every branch at 3 rungs and rewrites it every cycle.
  3. grid_10x2.0  - 10 rungs at 2.0%. Many small slices cycling fast, the
                    shape the owner described wanting ("rapid buying and
                    selling real quick").
  4. grid_5x2.0   - 5 rungs at 2.0%. An already-validated candidate from
                    crypto_grid_bot.GRID_LEVEL_SPACING_CANDIDATES.
  5. trail_trend  - NOT a grid at all. One directional position at a time,
                    ATR-based entry and target, hard stop with a breakeven
                    ratchet, then a percentage trailing stop once target is
                    reached. This is the family-tree / compound engine's
                    own live exit philosophy, replayed through
                    crypto_selection_backtest's existing primitive.

WHAT IS DELIBERATELY NOT IN THE RACE, with the measured reason:

  * hourly momentum   -$343.69 over 788 trades, 47.7% win rate
  * swing trading     -$133.00, 29.4% win rate
  * plain target/stop -$473.97 over 979 trades, 22.9% win rate
    (all three on this same account's own data; they were removed from
    STRATEGY_LAB_STRATEGIES for it. Racing a known loser to watch it lose
    again is theatre, not measurement.)
  * grid short        it mirrors the grid by SELLING rallies first, which
    on spot needs coin it does not hold. The only route is perpetual
    futures, and the standing instruction on this account is no leverage
    and no perps. Excluded as unbuildable, not as unpromising.

THE FEE IS THE WHOLE GAME AT THIS SIZE, SO IT IS RUN TWICE.
crypto_selection_backtest defaults to the TAKER round trip, 1.500%. This
fleet is measured at 99.08% maker across 218 legs and a blended round trip
of 0.7073%. Charging 1.5% to a book that pays 0.71% would bury strategies
that are actually profitable; charging 0.71% to a book that slipped to
taker would flatter them. So the contest runs at the measured rate AND at
the taker rate, and a winner that only wins at one of them is reported as
exactly that.

NO ENTRY FILTER. 22 filters were tested against 190 real trades on this
account and not one beat doing nothing; winners' median entry RSI is 28.3,
so the grid earns by buying oversold and a momentum filter contradicts its
own premise. Every strategy here runs ungated.

Usage: python3 five_way_contest.py [--days 180] [--windows 6] [--json out]
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

import aiohttp  # noqa: E402

import crypto_selection_backtest as bt  # noqa: E402

SPEND_USD = 200.0

# Measured on this account 2026-10-03: 218 legs, 99.08% maker, blended
# round trip 0.7073%. The taker figure is the module's own default.
MEASURED_ROUND_TRIP = 0.007073
TAKER_ROUND_TRIP = 0.015

# The coins the account actually holds branches on. Racing on coins it does
# not trade would answer a question nobody asked.
COINS = [
    "ZEC-USD", "XRP-USD", "XLM-USD", "ETH-USD", "SHIB-USD", "HBAR-USD",
    "APE-USD", "TON-USD", "LTC-USD", "ALGO-USD", "NEAR-USD", "BCH-USD",
    "PEPE-USD", "QNT-USD", "LINK-USD", "SOL-USD", "ACH-USD", "TIA-USD",
    "BTC-USD", "FLOKI-USD", "JASMY-USD", "ONDO-USD", "PRIME-USD",
]


def _grid(levels, pct):
    def run(closes, highs, lows, spend):
        return bt._replay_grid_bot(closes, highs, lows, spend=spend,
                                   grid_pct=pct, num_levels=levels)
    return run


def _trail(closes, highs, lows, spend):
    return bt._replay_with_exit_mode(closes, highs, lows, "trailing_stop",
                                     spend=spend)


CONTENDERS = {
    "grid_10x3.0": _grid(10, 0.030),
    "grid_3x3.0": _grid(3, 0.030),
    "grid_10x2.0": _grid(10, 0.020),
    "grid_5x2.0": _grid(5, 0.020),
    "trail_trend": _trail,
}
CONTROL = "grid_3x3.0"


async def fetch_all(coins, days, concurrency=4):
    """Real candles per coin. A coin that cannot be read is DROPPED, never
    zero-filled: a missing series is UNKNOWN, and a zero would quietly
    hand every strategy a free flat window."""
    out, failed = {}, {}
    sem = asyncio.Semaphore(concurrency)

    async def one(session, pid):
        async with sem:
            err = {}
            try:
                res = await bt.fetch_historical_candles(
                    session, pid, days=days, last_error_out=err)
            except Exception as e:
                failed[pid] = f"{type(e).__name__}: {e}"
                return
            if not res or not res[0]:
                failed[pid] = err.get("error") or "no candles returned"
                return
            out[pid] = res

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, p) for p in coins))
    return out, failed


def windows_for(n_candles, n_windows):
    """Consecutive, non-overlapping, oldest first. Equal size; the
    remainder is dropped from the OLD end so the newest window ends at the
    latest candle."""
    size = n_candles // n_windows
    if size < 48:                      # under two days is not a window
        return []
    start = n_candles - size * n_windows
    return [(start + i * size, start + (i + 1) * size) for i in range(n_windows)]


def run_contest(series, n_windows, fee_rate):
    bt.BACKTEST_ROUND_TRIP_FEE_RATE = fee_rate
    cells = []                          # one per (coin, window, strategy)
    skipped = []
    for pid, (closes, highs, lows, _times) in sorted(series.items()):
        wins = windows_for(len(closes), n_windows)
        if not wins:
            skipped.append({"product_id": pid, "why": "too few candles to split"})
            continue
        for w, (a, b) in enumerate(wins):
            c, h, lo = closes[a:b], highs[a:b], lows[a:b]
            for name, fn in CONTENDERS.items():
                try:
                    res = fn(c, h, lo, SPEND_USD)
                except Exception as e:
                    cells.append({"product_id": pid, "window": w, "strategy": name,
                                  "net_usd": None, "trades": None,
                                  "error": f"{type(e).__name__}: {e}"})
                    continue
                if not res:
                    # No trade is a real outcome: $0, zero trades. It is
                    # not an error and not a loss.
                    cells.append({"product_id": pid, "window": w, "strategy": name,
                                  "net_usd": 0.0, "trades": 0, "no_trade": True})
                    continue
                cells.append({"product_id": pid, "window": w, "strategy": name,
                              "net_usd": round(res["total_pnl"], 4),
                              "trades": res["num_trades"],
                              "win_rate_pct": round(res["win_rate"], 1)})
    return cells, skipped


def summarise(cells, n_windows):
    """Per-window totals, who won each, and the consistency figures."""
    per_window = {w: {} for w in range(n_windows)}
    unreadable = {}
    for c in cells:
        if c.get("net_usd") is None:
            unreadable[c["strategy"]] = unreadable.get(c["strategy"], 0) + 1
            continue
        d = per_window[c["window"]].setdefault(
            c["strategy"], {"net": 0.0, "trades": 0, "coins": 0, "positive_coins": 0})
        d["net"] += c["net_usd"]
        d["trades"] += c["trades"] or 0
        d["coins"] += 1
        if c["net_usd"] > 0:
            d["positive_coins"] += 1

    winners = {}
    for w in range(n_windows):
        row = per_window[w]
        if not row:
            continue
        best = max(row.items(), key=lambda kv: kv[1]["net"])
        winners[w] = best[0]

    summary = []
    for name in CONTENDERS:
        nets = [per_window[w][name]["net"] for w in range(n_windows)
                if name in per_window[w]]
        trades = sum(per_window[w][name]["trades"] for w in range(n_windows)
                     if name in per_window[w])
        pos_cells = sum(per_window[w][name]["positive_coins"] for w in range(n_windows)
                        if name in per_window[w])
        all_cells = sum(per_window[w][name]["coins"] for w in range(n_windows)
                        if name in per_window[w])
        if not nets:
            summary.append({"strategy": name, "readable": False,
                            "detail": "no window produced a readable result"})
            continue
        summary.append({
            "strategy": name,
            "readable": True,
            "windows_won": sum(1 for w, s in winners.items() if s == name),
            "windows_measured": len(nets),
            "windows_positive": sum(1 for n in nets if n > 0),
            "total_usd": round(sum(nets), 2),
            "median_window_usd": round(statistics.median(nets), 2),
            "best_window_usd": round(max(nets), 2),
            "worst_window_usd": round(min(nets), 2),
            "trades": trades,
            "coin_windows_positive": pos_cells,
            "coin_windows": all_cells,
            "hit_rate_pct": round(100.0 * pos_cells / all_cells, 1) if all_cells else None,
            "unreadable_cells": unreadable.get(name) or None,
        })
    # Ranked by CONSISTENCY first - windows won, then windows positive,
    # then the median window. Total is the tiebreaker, never the lead:
    # one huge window is what makes an unreliable strategy look good.
    summary.sort(key=lambda r: (
        -(r.get("windows_won") or 0), -(r.get("windows_positive") or 0),
        -(r.get("median_window_usd") or 0), -(r.get("total_usd") or 0)))
    return summary, per_window, winners


def print_report(label, fee_rate, summary, per_window, winners, n_windows,
                 coins_used, failed, skipped):
    print(f"\n{'=' * 78}")
    print(f"  {label}  -  round-trip fee {fee_rate * 100:.4f}%  -  ${SPEND_USD:,.0f} per strategy")
    print(f"{'=' * 78}")
    print(f"  {len(coins_used)} coin(s) read, {n_windows} consecutive windows each")
    if failed:
        print(f"  DROPPED, not zero-filled: {sorted(failed)}")
    if skipped:
        print(f"  skipped: {[s['product_id'] for s in skipped]}")

    print(f"\n  {'strategy':<14}{'won':>5}{'pos':>5}{'total':>11}{'median':>10}"
          f"{'best':>10}{'worst':>10}{'trades':>8}{'hit%':>7}")
    print("  " + "-" * 76)
    for r in summary:
        if not r.get("readable"):
            print(f"  {r['strategy']:<14}  UNREADABLE - {r['detail']}")
            continue
        print(f"  {r['strategy']:<14}{r['windows_won']:>5}"
              f"{str(r['windows_positive']) + '/' + str(r['windows_measured']):>5}"
              f"{r['total_usd']:>11,.2f}{r['median_window_usd']:>10,.2f}"
              f"{r['best_window_usd']:>10,.2f}{r['worst_window_usd']:>10,.2f}"
              f"{r['trades']:>8}{(r['hit_rate_pct'] or 0):>7.1f}")

    print(f"\n  per-window totals (window 0 is the OLDEST):")
    names = list(CONTENDERS)
    print("  " + " " * 14 + "".join(f"{('w' + str(w)):>11}" for w in range(n_windows)))
    for name in names:
        row = "".join(
            f"{per_window[w][name]['net']:>11,.2f}" if name in per_window[w]
            else f"{'--':>11}" for w in range(n_windows))
        print(f"  {name:<14}{row}")
    print("  " + " " * 14 + "".join(
        f"{('<' + winners.get(w, '?')[:9]):>11}" for w in range(n_windows)))
    print("                 ^ window winner")


def coin_ranking(cells, strategy, n_windows):
    """Which COINS win consistently under one strategy.

    This is the question $1,000 actually asks. Knowing the best config is
    no use without knowing which five coins to put it on, and a coin that
    posts one huge window is not a coin to fund - same reasoning as the
    strategy table above, applied one level down.
    """
    by = {}
    for c in cells:
        if c["strategy"] != strategy or c.get("net_usd") is None:
            continue
        d = by.setdefault(c["product_id"], {"nets": [], "trades": 0})
        d["nets"].append(c["net_usd"])
        d["trades"] += c["trades"] or 0
    rows = []
    for pid, d in by.items():
        nets = d["nets"]
        rows.append({
            "product_id": pid,
            "windows_measured": len(nets),
            "windows_positive": sum(1 for n in nets if n > 0),
            "total_usd": round(sum(nets), 2),
            "median_window_usd": round(statistics.median(nets), 2),
            "worst_window_usd": round(min(nets), 2),
            "best_window_usd": round(max(nets), 2),
            "trades": d["trades"],
            # EVERY window positive is the bar. A coin that lost a window
            # will lose one again.
            "never_lost_a_window": all(n >= 0 for n in nets),
        })
    rows.sort(key=lambda r: (-r["windows_positive"], -r["median_window_usd"],
                             -r["total_usd"]))
    return rows


def print_coin_ranking(rows, strategy, n_windows, top=12):
    print(f"\n  WHICH COINS WIN CONSISTENTLY under {strategy} "
          f"(${SPEND_USD:,.0f} each, {n_windows} windows):")
    print(f"  {'coin':<11}{'pos':>6}{'total':>10}{'median':>10}{'worst':>10}"
          f"{'best':>10}{'trades':>8}  never lost")
    print("  " + "-" * 76)
    for r in rows[:top]:
        print(f"  {r['product_id']:<11}"
              f"{str(r['windows_positive']) + '/' + str(r['windows_measured']):>6}"
              f"{r['total_usd']:>10,.2f}{r['median_window_usd']:>10,.2f}"
              f"{r['worst_window_usd']:>10,.2f}{r['best_window_usd']:>10,.2f}"
              f"{r['trades']:>8}  {'yes' if r['never_lost_a_window'] else 'no'}")
    clean = [r for r in rows if r["never_lost_a_window"] and r["total_usd"] > 0]
    print(f"\n  {len(clean)} coin(s) never lost a single window and ended positive: "
          f"{[r['product_id'] for r in clean[:8]]}")
    return clean


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--coins", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()] or COINS
    print(f"fetching {args.days}d of real hourly candles for {len(coins)} coin(s)...")
    series, failed = await fetch_all(coins, args.days)
    if not series:
        print("NO COIN COULD BE READ. That is UNKNOWN, not a result - nothing is "
              "reported and no winner is named.")
        return 1
    print(f"read {len(series)} coin(s); "
          f"{min(len(v[0]) for v in series.values())}-"
          f"{max(len(v[0]) for v in series.values())} candles each")

    out = {"spend_usd": SPEND_USD, "days": args.days, "windows": args.windows,
           "coins_read": sorted(series), "coins_dropped": failed or None,
           "is_a_measurement_not_a_change": True,
           "places_no_order": True, "runs": []}

    for label, rate in (("MEASURED - what this fleet actually pays", MEASURED_ROUND_TRIP),
                        ("STRESS - every leg at the taker rate", TAKER_ROUND_TRIP)):
        cells, skipped = run_contest(series, args.windows, rate)
        summary, per_window, winners = summarise(cells, args.windows)
        print_report(label, rate, summary, per_window, winners, args.windows,
                     series, failed, skipped)
        out["runs"].append({
            "label": label, "round_trip_fee_rate": rate, "summary": summary,
            "window_winners": winners,
            "per_window": {str(w): per_window[w] for w in per_window},
            "cells": cells, "skipped": skipped or None})

    # THE ANSWER, stated only if the two runs agree. A winner at one fee
    # rate and not the other has not won.
    tops = [r["runs"][0] for r in [out]] and [run["summary"][0]["strategy"]
                                              for run in out["runs"]
                                              if run["summary"]]
    print(f"\n{'=' * 78}")
    if len(set(tops)) == 1 and tops:
        name = tops[0]
        m = out["runs"][0]["summary"][0]
        print(f"  CONSISTENT WINNER AT BOTH FEE RATES: {name}")
        print(f"  {m['windows_won']} of {m['windows_measured']} windows won, "
              f"{m['windows_positive']} positive, median window ${m['median_window_usd']:,.2f}, "
              f"worst ${m['worst_window_usd']:,.2f} on ${SPEND_USD:,.0f}")
        out["consistent_winner"] = name
    else:
        print(f"  NO CONSISTENT WINNER. The measured-fee run says {tops[0] if tops else '?'}; "
              f"the taker-stress run says {tops[1] if len(tops) > 1 else '?'}.")
        print("  A strategy that only wins at one fee assumption has not won.")
        out["consistent_winner"] = None
        out["winner_disagrees_by_fee_rate"] = tops
    print(f"{'=' * 78}")

    # The coin ranking under whichever config actually won, at the
    # measured fee rate. Named only when a winner survived both runs.
    win = out.get("consistent_winner")
    if win:
        rank = coin_ranking(out["runs"][0]["cells"], win, args.windows)
        clean = print_coin_ranking(rank, win, args.windows)
        out["coin_ranking_under_winner"] = rank
        out["coins_that_never_lost_a_window"] = [r["product_id"] for r in clean]
    else:
        print("\n  No coin ranking is printed, because no config won at both "
              "fee rates and ranking coins under a config that did not win "
              "would be a number without a question.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
