"""When a coin goes hot, does the move hold or fade? Measured, per coin.

READ-ONLY. Public candles plus /grid-status for the branch list. Places no
order, calls no write endpoint, changes nothing.

THE QUESTION, in the account owner's words: "it's a coin that's hot, yeah,
it's hot, but it's only hot for a second. So we need to get the money and
go ahead. Grabbing cash and dash."

That is a MOMENTUM claim with a MEAN-REVERSION tail: the move is real but
short, so take it fast. The grid is built on the opposite instinct - it
buys dips and waits for a rise. Both cannot be right about the same coin,
and this measures which one the last 60 days actually support.

METHOD. A bar is HOT when the trailing hour is up by at least HOT_PCT.
From that bar, the forward return is measured at 15m, 30m, 1h, 2h and 6h.

  forward return NEGATIVE -> the move fades. Taking it fast beats holding,
                             and the faster the better. Cash and dash.
  forward return POSITIVE -> the move continues. Selling into it leaves
                             money on the table.

Two things are reported beside the mean, because a mean alone cannot carry
a trading decision:

  WIN RATE  the share of hot moments where price was LOWER later. That is
            the share where selling fast was right.
  NET OF FEES  the same figure less the 0.70% round trip actually billed.
            A fade of 0.3% is a real fade and an unprofitable trade.

WHAT THIS IS NOT. It is not a backtest of a strategy and it does not
propose selling anything. It measures one property of these coins so a
decision about speed can be made on evidence instead of on feel.
"""
import statistics as st
import sys

from grid_step_backtest import candles, _get, BASE

GRAN = 900                      # 15-minute bars
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
HOT_PCT = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5
FEE_RT = 0.70                   # percent, the live maker round trip
HORIZONS = [("15m", 1), ("30m", 2), ("1h", 4), ("2h", 8), ("6h", 24)]
LOOKBACK = 4                    # 4 bars = the trailing hour


def study(bars):
    """Every hot moment in one coin, and what happened next."""
    closes = [b[3] for b in bars]
    hot, fwd = 0, {k: [] for k, _ in HORIZONS}
    for i in range(LOOKBACK, len(closes) - 24):
        past = closes[i - LOOKBACK]
        if not past:
            continue
        if (closes[i] / past - 1.0) * 100.0 < HOT_PCT:
            continue
        hot += 1
        for name, n in HORIZONS:
            fwd[name].append((closes[i + n] / closes[i] - 1.0) * 100.0)
    return hot, fwd


def main():
    gs = _get(f"{BASE}/grid-status", timeout=90)
    branches = [b for b in (gs.get("branches") or []) if b.get("product_id")]
    print(f"{len(branches)} branches, {DAYS}d of {GRAN//60}m candles, "
          f"HOT = trailing hour up {HOT_PCT:.1f}% or more, fee {FEE_RT:.2f}%\n")
    head = f"{'coin':11} {'hot':>5} |" + "".join(f"{h:>17}" for h, _ in HORIZONS)
    print(head)
    print(f"{'':11} {'moments':>5} |" + "".join(f"{'mean   fade%':>17}" for _ in HORIZONS))
    print("-" * len(head))

    pooled = {k: [] for k, _ in HORIZONS}
    total_hot = 0
    for b in branches:
        pid = b["product_id"]
        bars, why = candles(pid, days=DAYS, gran=GRAN)
        if not bars:
            print(f"{pid:11} {'--':>5} | SKIPPED - {why}")
            continue
        hot, fwd = study(bars)
        total_hot += hot
        row = f"{pid:11} {hot:>5} |"
        for name, _ in HORIZONS:
            v = fwd[name]
            pooled[name].extend(v)
            if not v:
                row += f"{'--':>17}"
                continue
            m = st.mean(v)
            fade = 100.0 * sum(1 for x in v if x < 0) / len(v)
            row += f"{m:>+8.2f}%{fade:>7.0f}%"
        print(row)

    print("\n" + "=" * len(head))
    print(f"POOLED over {total_hot:,} hot moments across every coin\n")
    print(f"  {'after':>6} {'mean move':>11} {'faded':>8} {'median':>10} "
          f"{'mean net of 0.70% fee':>23}")
    for name, _ in HORIZONS:
        v = pooled[name]
        if not v:
            continue
        m, md = st.mean(v), st.median(v)
        fade = 100.0 * sum(1 for x in v if x < 0) / len(v)
        print(f"  {name:>6} {m:>+10.3f}% {fade:>7.1f}% {md:>+9.3f}% "
              f"{(abs(m) - FEE_RT):>+22.3f}%")

    print("\nHOW TO READ IT")
    print("  A NEGATIVE mean means price was LOWER later - the move faded, and")
    print("  selling into it was right. 'faded' is the share of hot moments")
    print("  where that happened; 50% is a coin flip and says nothing.")
    print("  The last column is what is left after the round trip is paid. A")
    print("  fade smaller than 0.70% is a real fade and a losing trade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
