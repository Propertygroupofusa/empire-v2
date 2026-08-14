"""
DEMO / PAPER ONLY. Places no orders, touches no account, changes no config.

Answers two questions the point-estimate backtest cannot:

  1. What capital would the MEASURED edge require to earn a target per day?
  2. If we deployed this, what is the realistic RANGE of outcomes - not the
     average, the range - including how often it loses money?

A backtest returns one number: "+0.53% out-of-sample". That number is a
single draw from a distribution. Deploying on it is like judging a coin
from one flip. So this bootstraps the ACTUAL out-of-sample trade P&Ls
(resampling with replacement) into many alternative futures and reports
the spread, because the spread is what determines whether an edge this
thin is worth trading.

Uses the winning config from the entry sweep, out-of-sample only. The
in-sample window selected that config, so its in-sample numbers are
optimistic by construction and are deliberately not used here.
"""
import os
import random
import statistics
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import backtest_prop_target as bt
import prop_bot as bot

# Winning config from the sweep. Out-of-sample PF 1.083.
BUY_BELOW = int(os.getenv("DEMO_BUY_BELOW", "50"))
SELL_ABOVE = int(os.getenv("DEMO_SELL_ABOVE", "60"))
USE_TREND_FILTER = os.getenv("DEMO_TREND_FILTER", "false").lower() == "true"
EXIT_MODE = os.getenv("DEMO_EXIT_MODE", "atr")

IN_SAMPLE_FRAC = 0.60
N_PATHS = int(os.getenv("DEMO_PATHS", "20000"))
DAILY_TARGET = float(os.getenv("DEMO_DAILY_TARGET", "1000"))

# Round-trip cost the backtest does NOT model. Alpaca charges no
# commission, but spread and slippage are real and land on every trade.
# On a 1.08 profit factor across hundreds of trades this is not a rounding
# error, so it is applied explicitly rather than quietly ignored.
SLIPPAGE_BPS = float(os.getenv("DEMO_SLIPPAGE_BPS", "2"))   # 0.02% per round trip


def main():
    if not bt.HEADERS["APCA-API-KEY-ID"]:
        print("FAIL: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.", file=sys.stderr)
        return 1

    print("=" * 68)
    print("  DEMO PROJECTION - no orders placed, no config changed")
    print("=" * 68)
    print(f"  config under test : buy<{BUY_BELOW}  sell>{SELL_ABOVE}  "
          f"trend_filter={USE_TREND_FILTER}  exits={EXIT_MODE}")
    print(f"  starting capital  : ${bt.STARTING_CASH:,.2f}")
    print(f"  slippage modelled : {SLIPPAGE_BPS} bps per round trip\n")

    bars_by, rsi_by, hourly_by, atr_by = {}, {}, {}, {}
    for symbol in bt.SYMBOLS:
        bars = bt.fetch_bars(symbol, "5Min", bt.LOOKBACK_DAYS)
        if len(bars) < 300:
            continue
        bars_by[symbol] = bars
        rsi_by[symbol] = bt.rsi_trend_series([b["c"] for b in bars])
        atr_by[symbol] = bt.atr_pct_series(bars)
        hourly_by[symbol] = bt.hourly_trend_series(
            bt.fetch_bars(symbol, "1Hour", bt.LOOKBACK_DAYS + 10))
        time.sleep(0.3)
    if not bars_by:
        print("FAIL: no market data.", file=sys.stderr)
        return 1

    # out-of-sample slice only
    ob, orr, oa = {}, {}, {}
    for sym, bars in bars_by.items():
        lo = int(len(bars) * IN_SAMPLE_FRAC)
        ob[sym], orr[sym], oa[sym] = bars[lo:], rsi_by[sym][lo:], atr_by[sym][lo:]

    saved = (bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE)
    try:
        bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE = BUY_BELOW, SELL_ABOVE
        res = bt.simulate(ob, orr, hourly_by, oa, EXIT_MODE,
                          use_trend_filter=USE_TREND_FILTER)
    finally:
        bot.RSI_BUY_BELOW, bot.RSI_SELL_ABOVE = saved

    trades = res["trades"]
    if not trades:
        print("No trades in the out-of-sample window.", file=sys.stderr)
        return 1

    # Apply the slippage the backtest omitted, per round trip.
    cost_per_trade = bt.STARTING_CASH / bot.MAX_POSITIONS * (SLIPPAGE_BPS / 10000.0)
    pnls = [t["pnl"] - cost_per_trade for t in trades]

    days = bt.LOOKBACK_DAYS * (1 - IN_SAMPLE_FRAC)
    trades_per_day = len(pnls) / days
    gross = sum(t["pnl"] for t in trades)
    net = sum(pnls)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    print(f"  OUT-OF-SAMPLE ACTUALS ({days:.0f} days, {len(pnls)} trades)")
    print(f"    gross P&L            ${gross:+,.2f}")
    print(f"    net of slippage      ${net:+,.2f}")
    print(f"    profit factor (net)  {pf:.3f}")
    print(f"    trades/day           {trades_per_day:.1f}")
    print(f"    daily return         {net / bt.STARTING_CASH / days * 100:+.4f}%")

    # ── capital required for the target ──────────────────────────────────
    daily_ret = net / bt.STARTING_CASH / days
    print(f"\n  CAPITAL REQUIRED FOR ${DAILY_TARGET:,.0f}/DAY")
    if daily_ret <= 0:
        print("    Not achievable at any capital - the net edge is <= 0.")
    else:
        need = DAILY_TARGET / daily_ret
        print(f"    at the measured {daily_ret*100:.4f}%/day: ${need:,.0f}")
        print(f"    you currently have:                       ${bt.STARTING_CASH:,.0f}")
        print(f"    shortfall factor:                         {need / bt.STARTING_CASH:,.0f}x")

    # ── bootstrap: the range, not the average ────────────────────────────
    n_per_path = int(round(trades_per_day * 21))      # ~1 trading month
    random.seed(7)
    finals, dds, ruin = [], [], 0
    for _ in range(N_PATHS):
        eq, peak, mdd = bt.STARTING_CASH, bt.STARTING_CASH, 0.0
        for _ in range(n_per_path):
            eq += random.choice(pnls)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
            if eq <= 0:
                ruin += 1
                break
        finals.append(eq)
        dds.append(mdd)
    finals.sort()

    def pct(p):
        return finals[min(int(len(finals) * p), len(finals) - 1)]

    losing = sum(1 for f in finals if f < bt.STARTING_CASH) / len(finals) * 100
    print(f"\n  ONE MONTH AHEAD - {N_PATHS:,} bootstrapped paths, "
          f"{n_per_path} trades each")
    print(f"    5th percentile   ${pct(0.05):>9,.2f}  ({(pct(0.05)/bt.STARTING_CASH-1)*100:+.2f}%)")
    print(f"    25th             ${pct(0.25):>9,.2f}  ({(pct(0.25)/bt.STARTING_CASH-1)*100:+.2f}%)")
    print(f"    median           ${pct(0.50):>9,.2f}  ({(pct(0.50)/bt.STARTING_CASH-1)*100:+.2f}%)")
    print(f"    75th             ${pct(0.75):>9,.2f}  ({(pct(0.75)/bt.STARTING_CASH-1)*100:+.2f}%)")
    print(f"    95th percentile  ${pct(0.95):>9,.2f}  ({(pct(0.95)/bt.STARTING_CASH-1)*100:+.2f}%)")
    print(f"\n    probability of ending the month DOWN : {losing:.1f}%")
    print(f"    median worst drawdown along the way  : {statistics.median(dds)*100:.1f}%")
    print(f"    95th-percentile drawdown             : {sorted(dds)[int(len(dds)*0.95)]*100:.1f}%")

    hit = sum(1 for f in finals
              if (f - bt.STARTING_CASH) / 21 >= DAILY_TARGET) / len(finals) * 100
    print(f"\n    paths averaging >= ${DAILY_TARGET:,.0f}/day : {hit:.2f}%")

    print("\n" + "=" * 68)
    print("  Bootstrapping assumes future trades resemble this sample and are")
    print("  independent. Real markets cluster losses, so the true left tail")
    print("  is fatter than shown. Treat every figure here as optimistic.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
