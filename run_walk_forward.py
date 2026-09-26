"""Run the harness against a registered idea. Real candles, no mocks.

Usage:  python3 run_walk_forward.py [idea] [candle_dir]

The cross-rate idea is registered first because the answer is already
known from hand analysis - it produced +3.252% in-sample and -0.137% out
of sample. If the harness does not return FAIL on it, the harness is
broken, not the idea.
"""
from __future__ import annotations

import glob
import itertools
import json
import os
import statistics
import sys
from datetime import datetime

import walk_forward as wf


def load_candles(directory):
    """{'BTC': {day: close}} from cached Coinbase daily candle files."""
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "cd_*-USD.json"))):
        asset = os.path.basename(path).split("cd_")[1].split("-USD")[0]
        rows = json.load(open(path))
        rows.sort(key=lambda r: r[0])
        out[asset] = {datetime.utcfromtimestamp(r[0]).strftime("%Y-%m-%d"): r[4]
                      for r in rows}
    return out


def cross_rate_idea(prices):
    """Rotate on a cross-rate z-score: the idea that was proposed for the TV.

    direction -1 is the proposal itself (sell the rich leg, buy the cheap
    one). +1 is its opposite, included because the ratios were found to
    trend, which would put the proposal on the wrong side.
    """
    assets = sorted(prices)
    days = sorted(set.intersection(*[set(v) for v in prices.values()]))
    pairs = list(itertools.combinations(assets, 2))

    configs = [{"direction": d, "gate": g, "window": w, "hold": h}
               for d in (-1, +1)
               for g in (1.5, 2.0, 2.5, 3.0)
               for w in (20, 30, 60)
               for h in (5, 10, 21, 42)]

    def run_fn(cfg, lo, hi):
        g, w, h, d = cfg["gate"], cfg["window"], cfg["hold"], cfg["direction"]
        out = []
        for a, b in pairs:
            ratio = [prices[a][x] / prices[b][x] for x in days]
            for i in range(max(w, lo), min(hi, len(days) - h)):
                hist = ratio[i - w:i]
                sd = statistics.pstdev(hist)
                if sd <= 0:
                    continue
                z = (ratio[i] - statistics.mean(hist)) / sd
                if abs(z) < g:
                    continue
                rich, cheap = (a, b) if z > 0 else (b, a)
                lng, shrt = (cheap, rich) if d < 0 else (rich, cheap)
                j = i + h
                out.append(((prices[lng][days[j]] / prices[lng][days[i]] - 1)
                            - (prices[shrt][days[j]] / prices[shrt][days[i]] - 1)) * 100)
        return out

    return configs, run_fn, days, "cross-rate rotation on a z-score"


IDEAS = {"cross-rate": cross_rate_idea}


def main():
    idea = sys.argv[1] if len(sys.argv) > 1 else "cross-rate"
    where = sys.argv[2] if len(sys.argv) > 2 else "."
    prices = load_candles(where)
    if not prices:
        print(f"no candle files in {where}")
        return 1

    configs, run_fn, days, label = IDEAS[idea](prices)
    cut = wf.split_index(len(days), 0.70)
    print(f"{len(prices)} coins, {len(days)} sessions")
    print(f"TRAIN {days[0]}..{days[cut - 1]}   TEST {days[cut]}..{days[-1]}")
    print(f"{len(configs)} configurations\n")

    r = wf.run(configs, run_fn, train_hi=cut, test_lo=cut, test_hi=len(days),
               hold_of=lambda c: c["hold"], label=label)

    print("=" * 72)
    print(r["headline"])
    print("=" * 72)
    o = r["out_of_sample"]
    print(f"\nchosen        {r['chosen_config']}")
    print(f"searched      {r['configurations_tested']} configurations")
    print(f"\nOUT OF SAMPLE   (this is the result)")
    print(f"  net           {o['net_pct']:+.3f}%  per round, after {o['fee_pct']}% fees")
    print(f"  win rate      {o['win_rate_pct']}%")
    print(f"  observations  {o['observations']:,}  ->  {o['independent_rounds']} independent rounds")
    print(f"  t             {o['t_raw']} raw  ->  {o['t_overlap_corrected']} corrected for "
          f"{o['hold_days']}-day overlap")
    print(f"  bar           {o['t_threshold']}  (noise produces this from a "
          f"{r['configurations_tested']}-wide search; the naive bar is {o['t_naive_bar']})")
    s = r["selection_only"]
    print(f"\nSELECTION ONLY  (chose the config; NOT evidence about it)")
    print(f"  net           {s['net_pct']:+.3f}%     t {s['t_raw']} raw / "
          f"{s['t_overlap_corrected']} corrected")
    print(f"\n{r['decay_note']}")
    return 0 if r["verdict"] != wf.PASS else 0


if __name__ == "__main__":
    sys.exit(main())
