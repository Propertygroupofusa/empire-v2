"""
Entry-signal sweep for prop_bot.

WHY THIS EXISTS
---------------
The three-way exit backtest returned profit factor 0.90 / 0.76 / 0.94 - all
below 1.0, with RSI as the dominant exit reason in every arm (181 / 217 /
328 of the closes). That is the signature of exits not being the binding
constraint. No exit rule manufactures an edge out of entries that do not
have one, so the question moves upstream: does ANY RSI threshold on
5-minute ETF bars produce profit factor above 1.0?

THE TRAP THIS IS BUILT TO AVOID
-------------------------------
Sweeping ~60 configurations against one 30-day sample is close to
guaranteed to surface something that looks profitable. With 60 draws, a few
clear 1.0 on luck alone. Reporting the best one as "the answer" would be
the single easiest way to lose real money on this whole exercise.

So the sample is split chronologically:
  - the first 60% (IN-SAMPLE) is what the grid is scored on
  - the last 40% (OUT-OF-SAMPLE) is never used for selection

The winner is chosen purely on in-sample profit factor, then re-run
untouched on out-of-sample. If the edge is real it survives; if it was
noise it collapses, and the collapse is the finding.

Two diagnostics printed alongside, both aimed at the same question:
  - how many of the 60 configs cleared PF 1.0 in-sample (if it is many,
    the grid is fitting noise, not finding structure)
  - how many of THOSE also cleared 1.0 out-of-sample (a real edge should
    persist at a far higher rate than chance)

Reuses the exit backtest's fetch/RSI/ATR/simulate machinery, which in turn
imports the real prop_bot - so nothing here can drift from production.
"""
import os
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import backtest_prop_target as bt
import prop_bot as bot

# Grid. RSI_BUY_BELOW is the entry gate (lower = more selective, fewer and
# deeper dips). RSI_SELL_ABOVE feeds the profit-taking RSI reversal exit.
BUY_GRID = [25, 30, 35, 40, 45, 50]
SELL_GRID = [50, 55, 60, 65, 70]
FILTER_GRID = [True, False]        # 1H trend confluence filter on/off

IN_SAMPLE_FRAC = 0.60
EXIT_MODE = os.getenv("SWEEP_EXIT_MODE", "atr")   # best of the three arms


def slice_all(bars_by, rsi_by, atr_by, lo_frac, hi_frac):
    """Chronological slice, applied identically to bars and both derived
    series so index alignment is preserved. Safe because RSI/ATR at index i
    were computed from bars <= i - slicing cannot introduce lookahead."""
    b, r, a = {}, {}, {}
    for sym, bars in bars_by.items():
        lo, hi = int(len(bars) * lo_frac), int(len(bars) * hi_frac)
        if hi - lo < 100:
            continue
        b[sym], r[sym], a[sym] = bars[lo:hi], rsi_by[sym][lo:hi], atr_by[sym][lo:hi]
    return b, r, a


def profit_factor(trades):
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def run_one(bars, rsi, hourly, atr, buy, sell, use_filter):
    """One config. Thresholds are patched onto the real module because
    simulate() reads them from there, so the sweep exercises the same
    constants production would."""
    saved = (bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE)
    try:
        bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE = buy, sell
        res = bt.simulate(bars, rsi, hourly, atr, EXIT_MODE, use_trend_filter=use_filter)
    finally:
        bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE = saved
    return {"pf": profit_factor(res["trades"]),
            "trades": len(res["trades"]),
            "final": res["final_equity"],
            "ret": (res["final_equity"] - bt.STARTING_CASH) / bt.STARTING_CASH * 100}


def main():
    if not bt.HEADERS["APCA-API-KEY-ID"]:
        print("FAIL: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.", file=sys.stderr)
        return 1

    print("prop_bot ENTRY-SIGNAL sweep")
    print(f"  exit rule held fixed at: {EXIT_MODE}")
    print(f"  grid: {len(BUY_GRID)} buy x {len(SELL_GRID)} sell x "
          f"{len(FILTER_GRID)} filter = {len(BUY_GRID)*len(SELL_GRID)*len(FILTER_GRID)} configs")
    print(f"  split: first {IN_SAMPLE_FRAC:.0%} selects, last {1-IN_SAMPLE_FRAC:.0%} validates\n")

    bars_by, rsi_by, hourly_by, atr_by = {}, {}, {}, {}
    for symbol in bt.SYMBOLS:
        bars = bt.fetch_bars(symbol, "5Min", bt.LOOKBACK_DAYS)
        if len(bars) < 300:
            print(f"  ! {symbol}: only {len(bars)} bars, skipping", file=sys.stderr)
            continue
        bars_by[symbol] = bars
        rsi_by[symbol] = bt.rsi_trend_series([b["c"] for b in bars])
        atr_by[symbol] = bt.atr_pct_series(bars)
        hourly_by[symbol] = bt.hourly_trend_series(
            bt.fetch_bars(symbol, "1Hour", bt.LOOKBACK_DAYS + 10))
        print(f"  {symbol}: {len(bars)} bars")
        time.sleep(0.3)

    if not bars_by:
        print("FAIL: no usable market data.", file=sys.stderr)
        return 1

    is_b, is_r, is_a = slice_all(bars_by, rsi_by, atr_by, 0.0, IN_SAMPLE_FRAC)
    os_b, os_r, os_a = slice_all(bars_by, rsi_by, atr_by, IN_SAMPLE_FRAC, 1.0)
    print(f"\n  in-sample bars {sum(len(v) for v in is_b.values())}, "
          f"out-of-sample {sum(len(v) for v in os_b.values())}")

    rows = []
    for use_filter in FILTER_GRID:
        for buy in BUY_GRID:
            for sell in SELL_GRID:
                r = run_one(is_b, is_r, hourly_by, is_a, buy, sell, use_filter)
                r.update(buy=buy, sell=sell, filt=use_filter)
                rows.append(r)

    rows.sort(key=lambda r: -r["pf"])
    print(f"\n  IN-SAMPLE, top 10 of {len(rows)} by profit factor")
    print(f"  {'buy<':>5} {'sell>':>6} {'1Hfilt':>7} {'PF':>7} {'trades':>7} {'return':>9}")
    for r in rows[:10]:
        print(f"  {r['buy']:>5} {r['sell']:>6} {str(r['filt']):>7} "
              f"{r['pf']:>7.3f} {r['trades']:>7} {r['ret']:>8.2f}%")

    cleared = [r for r in rows if r["pf"] > 1.0]
    print(f"\n  configs clearing PF 1.0 in-sample: {len(cleared)} / {len(rows)}")

    # ── the actual test: does the in-sample winner survive untouched? ────
    best = rows[0]
    oos = run_one(os_b, os_r, hourly_by, os_a,
                  best["buy"], best["sell"], best["filt"])
    print("\n  " + "=" * 62)
    print(f"  IN-SAMPLE WINNER: buy<{best['buy']}  sell>{best['sell']}  "
          f"filter={best['filt']}")
    print(f"    in-sample      PF {best['pf']:.3f}   {best['trades']} trades   {best['ret']:+.2f}%")
    print(f"    OUT-OF-SAMPLE  PF {oos['pf']:.3f}   {oos['trades']} trades   {oos['ret']:+.2f}%")
    verdict = ("HELD UP" if oos["pf"] > 1.0 else "COLLAPSED")
    print(f"    -> {verdict}")
    print("  " + "=" * 62)

    # ── how many in-sample winners survive? the anti-luck check ─────────
    if cleared:
        survived = 0
        for r in cleared:
            o = run_one(os_b, os_r, hourly_by, os_a, r["buy"], r["sell"], r["filt"])
            survived += (o["pf"] > 1.0)
        print(f"\n  of the {len(cleared)} configs that cleared 1.0 in-sample, "
              f"{survived} also cleared it out-of-sample "
              f"({survived / len(cleared) * 100:.0f}%)")
        print("  A real edge should persist far more often than a coin flip.")
        print("  Near or below ~50% means the grid found noise, not structure.")
    else:
        print("\n  No configuration cleared profit factor 1.0 even in-sample.")
        print("  That is the cleanest possible answer: on this data, no RSI")
        print("  threshold on 5-minute ETF bars produces an edge, and the")
        print("  problem is the signal itself rather than its parameters.")

    print("\n  One sample, one regime, one asset class. A surviving config is")
    print("  evidence worth acting on carefully - never proof.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
