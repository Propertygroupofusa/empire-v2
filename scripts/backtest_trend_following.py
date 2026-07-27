"""
One-off backtest: does a longer-timeframe trend-following approach beat
the 5-minute RSI mean-reversion strategy after real Coinbase fees?

The RSI approach (see crypto_coinbase_bot.py) pays the ~1.2% round-trip
fee on nearly every trade because it trades every few minutes - even
after fixing the fee-blind exits, the best achievable result was
"roughly breakeven," never a real edge. The theory here: trading a much
longer timeframe (1-hour bars, EMA crossover) means paying that same
fee far less often, and aiming to capture multi-hour/multi-day trends
instead of tiny reversions - a fundamentally different risk/reward
shape, not just a re-tuned version of the same idea.

STRATEGY (EMA9/EMA21 crossover, long-only, trailing stop):
  - Entry: EMA9 crosses above EMA21 (bullish crossover) on the 1h close.
  - Exit: EMA9 crosses back below EMA21, OR price drops more than
    TRAIL_PCT below the highest close seen since entry (trailing stop -
    locks in trend profit and cuts losses without a fixed target, since
    trend-following aims to ride winners as far as they go).
  - One position at a time, long-only (matches this account's spot-only
    capability), real round-trip fee deducted on both legs.

Not part of the app - throwaway, run under GitHub Actions (this sandbox
has no outbound network access to market-data APIs). Fetches ~180 days
of real 1-hour BTC-USD/ETH-USD candles from Coinbase's public endpoint
(same source crypto_coinbase_bot.py uses in production) - a longer
window than the RSI backtest used, since trend-following needs to see
real trend/range cycles, not just one month's regime.
"""
import sys
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

SYMBOLS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 3600  # 1 hour
LOOKBACK_DAYS = 180
MAX_CANDLES_PER_REQ = 300

EMA_FAST = 9
EMA_SLOW = 21
TRAIL_PCT = float(__import__("os").environ.get("TREND_TRAIL_PCT", "0.05"))
STARTING_CASH = 100.0
MIN_POSITION_NOTIONAL = 5.0
TAKER_FEE_RATE = 0.006
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2


def fetch_candles(product_id: str, days: int):
    end = int(time.time())
    start = end - days * 86400
    all_rows = []
    cursor_end = end
    step_seconds = GRANULARITY * MAX_CANDLES_PER_REQ

    while cursor_end > start:
        cursor_start = max(start, cursor_end - step_seconds)
        url = (
            f"https://api.exchange.coinbase.com/products/{product_id}/candles"
            f"?granularity={GRANULARITY}&start={cursor_start}&end={cursor_end}"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "empire-v2-backtest"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                rows = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  ! HTTP {e.code} fetching {product_id}: {e.read()[:200]}", file=sys.stderr)
            rows = []
        except Exception as e:
            print(f"  ! error fetching {product_id}: {e}", file=sys.stderr)
            rows = []

        all_rows.extend(rows)
        cursor_end = cursor_start
        time.sleep(0.4)

    by_time = {row[0]: row for row in all_rows}
    return sorted(by_time.values(), key=lambda r: r[0])  # [time, low, high, open, close, volume]


def ema_series(closes, period):
    k = 2 / (period + 1)
    emas = [None] * len(closes)
    ema = None
    for i, c in enumerate(closes):
        if ema is None:
            if i + 1 < period:
                continue
            ema = sum(closes[:period]) / period
            emas[i] = ema
            continue
        ema = c * k + ema * (1 - k)
        emas[i] = ema
    return emas


def fee(notional: float) -> float:
    return notional * TAKER_FEE_RATE


def run_backtest(rows, ema_fast, ema_slow):
    position = None  # {"entry", "qty", "entry_fee", "peak"}
    trades = []

    for i in range(1, len(rows)):
        close = rows[i][4]
        f_now, s_now = ema_fast[i], ema_slow[i]
        f_prev, s_prev = ema_fast[i - 1], ema_slow[i - 1]
        if f_now is None or s_now is None or f_prev is None or s_prev is None:
            continue

        bullish_cross = f_prev <= s_prev and f_now > s_now
        bearish_cross = f_prev >= s_prev and f_now < s_now

        if position is not None:
            position["peak"] = max(position["peak"], close)
            trail_stop_hit = close <= position["peak"] * (1 - TRAIL_PCT)
            if bearish_cross or trail_stop_hit:
                gross = (close - position["entry"]) * position["qty"]
                net = gross - position["entry_fee"] - fee(position["qty"] * close)
                trades.append(net)
                position = None
            continue

        if bullish_cross:
            qty = STARTING_CASH / close
            notional = qty * close
            if notional < MIN_POSITION_NOTIONAL:
                continue
            position = {"entry": close, "qty": qty, "entry_fee": fee(notional), "peak": close}

    if position is not None:
        final_close = rows[-1][4]
        gross = (final_close - position["entry"]) * position["qty"]
        net = gross - position["entry_fee"] - fee(position["qty"] * final_close)
        trades.append(net)

    return trades


def summarize(label, trades):
    n = len(trades)
    if n == 0:
        print(f"    {label:22s} | 0 trades")
        return
    wins = [t for t in trades if t > 0]
    total = sum(trades)
    print(
        f"    {label:22s} | {n:4d} trades | win rate {len(wins)/n*100:5.1f}% | "
        f"total P&L ${total:8.2f} | avg P&L/trade ${total/n:6.3f}"
    )


def main():
    print(f"EMA{EMA_FAST}/EMA{EMA_SLOW} crossover, trailing stop {TRAIL_PCT*100:.1f}%, "
          f"round-trip fee {ROUND_TRIP_FEE_PCT*100:.2f}%\n")
    for product_id in SYMBOLS:
        print(f"=== {product_id} — fetching {LOOKBACK_DAYS}d of {GRANULARITY}s (1h) candles ===")
        rows = fetch_candles(product_id, LOOKBACK_DAYS)
        if len(rows) < EMA_SLOW + 5:
            print(f"  ! only got {len(rows)} candles, skipping")
            continue
        print(f"  got {len(rows)} candles spanning "
              f"{datetime.fromtimestamp(rows[0][0], tz=timezone.utc).date()} to "
              f"{datetime.fromtimestamp(rows[-1][0], tz=timezone.utc).date()}")

        closes = [r[4] for r in rows]
        ema_fast = ema_series(closes, EMA_FAST)
        ema_slow = ema_series(closes, EMA_SLOW)
        trades = run_backtest(rows, ema_fast, ema_slow)
        summarize("trend-following", trades)

        # Buy-and-hold comparison over the same window, same starting cash,
        # same entry/exit fee - the benchmark this strategy actually needs
        # to beat to be worth the added complexity and activity.
        qty = STARTING_CASH / closes[0]
        entry_fee = fee(qty * closes[0])
        exit_fee = fee(qty * closes[-1])
        bh_pnl = (closes[-1] - closes[0]) * qty - entry_fee - exit_fee
        print(f"    {'buy & hold (same window)':22s} | {'1':>4s} trades | {'n/a':>9s}          | "
              f"total P&L ${bh_pnl:8.2f} | avg P&L/trade ${bh_pnl:6.3f}")
        print()


if __name__ == "__main__":
    main()
