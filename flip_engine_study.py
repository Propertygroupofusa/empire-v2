"""Which signal picks the coin that is about to flip? Measured, walk-forward.

THE OWNER'S ASK, 2026-10-03, in his words: rotation should "look for the
next coin that's profitable - momentum, bullets, whatever. The next coin
is ready to go." And it should "consistently and continually flip and make
money." So this tests selection SIGNALS against each other: at the end of
each month, which measurable property of a coin predicts that its grid
will flip rungs profitably in the month that follows?

rotation_study.py already established that rotating idle allocation beats
leaving it idle ($801.68 against $0.00 over five months on $3,253.84), and
that ranking by last month's FILL COUNT beat ranking by last month's P&L
two to one. This asks whether anything beats fill count.

STRICTLY WALK-FORWARD. Every signal is computed from the candles of window
N-1 only, then used to allocate window N. Nothing reads the window it is
being scored on. The hindsight basket is carried as the unreachable
ceiling so the gap to perfect selection stays visible.

THE SIGNALS, AND WHY EACH ONE IS PLAUSIBLE

  fills_prev    trades the grid actually completed last month. The current
                champion. Backward-looking by construction.
  swing         mean hourly (high-low)/close. The MECHANICAL driver: a
                grid fills when price crosses its step, so a coin that
                swings more should cross more. Measures capacity, not luck.
  crossings     how many times price actually crossed +/-3.0% from a
                running reference last month - the trigger count itself,
                counted directly off the candles rather than inferred.
  rangebound    swing divided by net drift. The ideal grid coin moves a
                lot and ends where it started; a coin that trends gives
                the grid one fill and then holds a bag.
  momentum      last month's return, which the owner asked for by name.
                Included to be tested, not because it is expected to win:
                22 entry filters were already measured against 190 real
                trades on this account and none beat doing nothing.
  oversold      RSI(14) at the end of last month, lowest first. The
                winners' median entry RSI on this account is 28.3, so
                "already beaten down" is the condition the grid earns in.
  dip_depth     how far below its own 30-day high the coin closed. The
                literal reading of "ready to go": a dip already in
                progress is a rung about to fill.

FLIP AND COMPOUND. Each policy is scored twice: flat, where the pot is the
same every month, and compounded, where last month's realised profit is
added to the pot before the next allocation. That is the owner's own rule -
"the compound always come after the profit is made" - so nothing is
compounded before it is earned, and a losing month shrinks the pot.

THE 20% CEILING IS ENFORCED, not footnoted. The unguarded fill-count
policy already broke it once by picking ZEC. A pick that would push a coin
past 20% of the book is replaced by the next one down the ranking.

WHAT THIS CANNOT SAY. Six windows, so five scored months - five samples.
Signals are computed on hourly closes. And rotation is charged no exit fee
because the capital rotated is UNSPENT allocation: it is not in coin, so
moving it sells nothing. That would be false for deployed capital.

Usage: python3 flip_engine_study.py --days 180 --windows 6 --pot 3253.84
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
from five_way_contest import COINS, fetch_all, windows_for  # noqa: E402

GRID_PCT = 0.030
LEVELS = 3
BASE_ALLOC = 200.0
MIN_SLICE_USD = 5.0
MEASURED_FEE = 0.007073
MAX_COIN_SHARE_PCT = 20.0
TOP_N = 3


# ----------------------------------------------------------------- signals
def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-(n + 1):-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _crossings(closes, pct=GRID_PCT):
    """How many times price crossed pct away from a running reference -
    the grid's own trigger, counted off the candles. A completed flip
    needs a down-crossing and then an up-crossing, so the number of
    round trips available is the smaller of the two counts."""
    if not closes:
        return 0
    ref = closes[0]
    down = up = 0
    for p in closes:
        if p <= ref * (1 - pct):
            down += 1
            ref = p
        elif p >= ref * (1 + pct):
            up += 1
            ref = p
    return min(down, up)


def signals(closes, highs, lows, fills_prev):
    if not closes or len(closes) < 20:
        return None
    swings = [(h - lo) / c for c, h, lo in zip(closes, highs, lows) if c]
    swing = statistics.mean(swings) if swings else None
    drift = abs(closes[-1] - closes[0]) / closes[0] if closes[0] else None
    hi = max(closes)
    return {
        "fills_prev": fills_prev,
        "swing": swing,
        "crossings": _crossings(closes),
        # A coin that swings hard and ends where it started. +1e-4 keeps a
        # perfectly flat coin from dividing by zero and reading infinite.
        "rangebound": (swing / (drift + 1e-4)) if swing is not None and drift is not None else None,
        "momentum": (closes[-1] / closes[0] - 1.0) if closes[0] else None,
        "oversold": _rsi(closes),
        "dip_depth": ((hi - closes[-1]) / hi) if hi else None,
    }


# Higher is better unless listed here.
LOWER_IS_BETTER = {"oversold"}
SIGNAL_NAMES = ["fills_prev", "swing", "crossings", "rangebound",
                "momentum", "oversold", "dip_depth"]

# COMBINATIONS, BY AVERAGE RANK, not by adding raw values - the signals are
# in different units and summing a percentage to a trade count would just
# weight whichever happens to be numerically larger.
#
# A WARNING THAT BELONGS NEXT TO THE CODE, NOT IN A FOOTNOTE: seven single
# signals and four combinations against five months of data is eleven
# candidates on five samples. Picking the best of eleven is itself a
# selection, and the gap between the top few is inside the noise. These are
# tested to see whether combining a return signal with a stability signal
# helps AT ALL, not to crown a winner by a dollar.
COMBOS = {
    "dip+rangebound": ("dip_depth", "rangebound"),
    "dip+swing": ("dip_depth", "swing"),
    "dip+crossings": ("dip_depth", "crossings"),
    "rangebound+oversold": ("rangebound", "oversold"),
}


def combo_ranking(feat_window, parts, available):
    """Average rank across the parts. A coin missing any part is dropped,
    never given a default rank - a fabricated rank is a fabricated opinion."""
    ranks = {}
    for sig in parts:
        rows = [(p, f.get(sig)) for p, f in feat_window.items()
                if f and f.get(sig) is not None and p in available]
        if not rows:
            return []
        rows.sort(key=lambda r: r[1], reverse=sig not in LOWER_IS_BETTER)
        for i, (pid, _) in enumerate(rows):
            ranks.setdefault(pid, []).append(i)
    full = [(pid, sum(v) / len(v)) for pid, v in ranks.items()
            if len(v) == len(parts)]
    full.sort(key=lambda r: r[1])
    return [p for p, _ in full]


# ----------------------------------------------------------------- scoring
def blocked(pid, add_usd, alloc, book_total):
    if not alloc or not book_total:
        return False
    have = alloc.get(pid)
    if have is None:
        return True            # not in the book: share unknowable -> blocked
    return (have + add_usd) / (book_total + add_usd) * 100.0 > MAX_COIN_SHARE_PCT


def pick(ranked, pot, n, alloc, book_total):
    each = pot / n if n else 0.0
    kept, skipped = [], []
    for pid in ranked:
        if blocked(pid, each, alloc, book_total):
            skipped.append(pid)
            continue
        kept.append(pid)
        if len(kept) >= n:
            break
    return kept, skipped


def run(results, feats, windows, pot0, signal, n, alloc, book_total,
        compound=False):
    """results[w][pid] = {'net','trades'} measured at BASE_ALLOC.
    feats[w][pid] = signal dict computed from window w's candles."""
    nets, picks, pot = [], [], pot0
    for w in windows:
        prev = w - 1
        if prev not in feats or prev not in results:
            nets.append(None)
            picks.append([])
            continue
        if signal in COMBOS:
            ranked = combo_ranking(feats[prev], COMBOS[signal], results[w])
        else:
            cand = [(pid, f.get(signal)) for pid, f in feats[prev].items()
                    if f and f.get(signal) is not None and pid in results[w]]
            cand.sort(key=lambda r: r[1], reverse=signal not in LOWER_IS_BETTER)
            ranked = [p for p, _ in cand]
        if not ranked:
            nets.append(None)
            picks.append([])
            continue
        chosen, _sk = pick(ranked, pot, n, alloc, book_total)
        if not chosen or (pot / len(chosen)) / LEVELS < MIN_SLICE_USD:
            nets.append(None)
            picks.append([])
            continue
        each = pot / len(chosen)
        net = sum(results[w][p]["net"] * (each / BASE_ALLOC) for p in chosen)
        nets.append(round(net, 2))
        picks.append(chosen)
        if compound:
            # THE OWNER'S RULE: compound only after the profit is made. A
            # losing month shrinks the pot; it is never topped back up.
            pot = max(0.0, pot + net)
    return {"signal": signal, "compound": compound, "nets": nets,
            "picks": picks, "final_pot_usd": round(pot, 2)}


def summarise(r, pot0, common):
    v = [r["nets"][i] for i in common]
    if not v or any(x is None for x in v):
        return {**r, "readable": False}
    return {
        "signal": r["signal"], "compound": r["compound"], "readable": True,
        "total_usd": round(sum(v), 2),
        "median_usd": round(statistics.median(v), 2),
        "worst_usd": round(min(v), 2), "best_usd": round(max(v), 2),
        "positive": sum(1 for x in v if x > 0), "months": len(v),
        "median_pct": round(statistics.median(v) / pot0 * 100, 2),
        "final_pot_usd": r["final_pot_usd"], "picks": r["picks"],
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--pot", type=float, default=3253.84)
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--book", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    alloc, book_total = None, None
    if args.book:
        with open(args.book) as f:
            gs = json.load(f)
        alloc = {b["product_id"]: (b.get("allocated_usd") or 0.0)
                 for b in (gs.get("branches") or [])}
        book_total = gs.get("total_allocated_usd") or sum(alloc.values())

    print(f"fetching {args.days}d for {len(COINS)} coin(s)...")
    series, failed = await fetch_all(COINS, args.days)
    if not series:
        print("NO COIN COULD BE READ - UNKNOWN, nothing reported")
        return 1
    print(f"read {len(series)}; dropped {sorted(failed) or 'none'}")

    bt.BACKTEST_ROUND_TRIP_FEE_RATE = MEASURED_FEE
    results, feats = {}, {}
    wins = None
    for pid, (c, h, lo, _t) in series.items():
        ws = windows_for(len(c), args.windows)
        if not ws:
            continue
        wins = range(len(ws))
        for w, (a, b) in enumerate(ws):
            cc, hh, ll = c[a:b], h[a:b], lo[a:b]
            res = bt._replay_grid_bot(cc, hh, ll, spend=BASE_ALLOC,
                                      grid_pct=GRID_PCT, num_levels=LEVELS)
            net = res["total_pnl"] if res else 0.0
            tr = res["num_trades"] if res else 0
            results.setdefault(w, {})[pid] = {"net": net, "trades": tr}
            feats.setdefault(w, {})[pid] = signals(cc, hh, ll, tr)
    windows = list(wins or [])

    rows = []
    for sig in SIGNAL_NAMES + list(COMBOS):
        for comp in (False, True):
            rows.append(run(results, feats, windows, args.pot, sig, args.top,
                            alloc, book_total, compound=comp))
    # The ceiling, for reference only.
    ceil_nets, ceil_picks = [], []
    for w in windows:
        data = results.get(w) or {}
        best = sorted(data, key=lambda p: -data[p]["net"])[:args.top]
        each = args.pot / max(1, len(best))
        ceil_nets.append(round(sum(data[p]["net"] * (each / BASE_ALLOC)
                                   for p in best), 2))
        ceil_picks.append(best)
    rows.append({"signal": "hindsight_best", "compound": False,
                 "nets": ceil_nets, "picks": ceil_picks,
                 "final_pot_usd": args.pot})

    common = [i for i in range(len(windows))
              if all(r["nets"][i] is not None for r in rows
                     if r["signal"] != "hindsight_best")]
    summ = [summarise(r, args.pot, common) for r in rows]
    flat = sorted([s for s in summ if s.get("readable") and not s["compound"]
                   and s["signal"] != "hindsight_best"],
                  key=lambda s: -s["total_usd"])

    print(f"\n{'=' * 84}")
    print(f"  WHICH SIGNAL PICKS THE NEXT COIN  -  ${args.pot:,.2f} pot, top "
          f"{args.top}, {len(common)} scored month(s), fee {MEASURED_FEE*100:.4f}%")
    print(f"  every signal reads month N-1 only; the 20% ceiling is enforced")
    print(f"{'=' * 84}")
    print(f"\n  {'signal':<14}{'total':>11}{'median':>10}{'%/mo':>7}{'worst':>10}"
          f"{'pos':>6}   |{'compounded':>12}{'end pot':>11}")
    print("  " + "-" * 82)
    for s in flat:
        c = next((x for x in summ if x.get("readable") and x["compound"]
                  and x["signal"] == s["signal"]), None)
        ct = f"{c['total_usd']:>12,.2f}" if c else f"{'--':>12}"
        cp = f"{c['final_pot_usd']:>11,.2f}" if c else f"{'--':>11}"
        print(f"  {s['signal']:<14}{s['total_usd']:>11,.2f}{s['median_usd']:>10,.2f}"
              f"{s['median_pct']:>7.2f}{s['worst_usd']:>10,.2f}"
              f"{str(s['positive']) + '/' + str(s['months']):>6}   |{ct}{cp}")
    hb = next((x for x in summ if x["signal"] == "hindsight_best"), None)
    if hb and hb.get("readable"):
        print(f"  {'hindsight':<14}{hb['total_usd']:>11,.2f}{hb['median_usd']:>10,.2f}"
              f"{hb['median_pct']:>7.2f}{hb['worst_usd']:>10,.2f}"
              f"{str(hb['positive']) + '/' + str(hb['months']):>6}   <- ceiling")

    print(f"\n  per month (m1 is the first scorable month):")
    for s in flat:
        cells = "".join(f"{v:>11,.2f}" if v is not None else f"{'--':>11}"
                        for v in next(r["nets"] for r in rows
                                      if r["signal"] == s["signal"] and not r["compound"]))
        print(f"  {s['signal']:<14}{cells}")

    if flat:
        w = flat[0]
        print(f"\n{'=' * 84}")
        print(f"  BEST SIGNAL: {w['signal']}  -  ${w['total_usd']:,.2f} over "
              f"{w['months']} month(s), median ${w['median_usd']:,.2f} "
              f"({w['median_pct']:.2f}%/mo), worst ${w['worst_usd']:,.2f}, "
              f"{w['positive']}/{w['months']} positive")
        base = next((s for s in flat if s["signal"] == "fills_prev"), None)
        if base and base["signal"] != w["signal"]:
            print(f"  Against the current champion fills_prev "
                  f"(${base['total_usd']:,.2f}): "
                  f"{w['total_usd'] - base['total_usd']:+,.2f}")
        else:
            print("  fills_prev is still the champion - no forward-looking "
                  "signal tested here beat it.")
        print(f"{'=' * 84}")
        for w2, pk in zip(windows, next(r["picks"] for r in rows
                                        if r["signal"] == w["signal"] and not r["compound"])):
            print(f"    m{w2}: {', '.join(p.replace('-USD','') for p in pk) or '(no prior month)'}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"pot_usd": args.pot, "top_n": args.top,
                       "scored_months": len(common), "summary": summ,
                       "is_a_measurement_not_a_change": True}, f, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
