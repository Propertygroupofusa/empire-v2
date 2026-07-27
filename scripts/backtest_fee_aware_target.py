"""
One-off backtest: verify the fee-aware PROFIT_TARGET_PCT fix in
crypto_coinbase_bot.py is actually net-positive against real Coinbase
fees, before it ever goes near production. Imports the real module's
constants and RSI function directly (not a reimplementation) so this
backtest can't drift from the actual bot logic.

Not part of the app - throwaway, run under GitHub Actions (this sandbox
has no outbound network access to market-data APIs). Fetches ~30 days of
real 5-minute BTC-USD/ETH-USD candles from Coinbase's public endpoint
(same source the bot uses in production) and replays the bot's exact
entry/exit rules bar-by-bar, deducting the same real round-trip fee this
account actually pays on every trade.
"""
import sys
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

sys.path.insert(0, ".")
import crypto_coinbase_bot as bot  # the real, unmodified production module

SYMBOLS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 300
LOOKBACK_DAYS = 30
MAX_CANDLES_PER_REQ = 300
STARTING_CASH = bot.MAX_ALLOCATION


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


def compute_rsi_series(closes):
    """Same window/period as bot._compute_rsi, computed for every bar."""
    rsis = [None] * len(closes)
    for i in range(14, len(closes)):
        rsis[i] = bot._compute_rsi(closes[i - 14:i + 1])
    return rsis


def fee(notional: float) -> float:
    return notional * bot.TAKER_FEE_RATE


def run_backtest(rows, rsis):
    """Mirrors run_crypto_cycle's Pass-1/Pass-2 exit/entry logic exactly,
    using the real module's RSI_BUY_BELOW/RSI_SELL_ABOVE/PROFIT_TARGET_PCT/
    MIN_POSITION_NOTIONAL constants - single position at a time, long only,
    with real round-trip fees deducted on both legs."""
    position = None
    trades = []

    for i in range(14, len(rows)):
        ts, low, high, open_, close, vol = rows[i]
        rsi = rsis[i]
        if rsi is None:
            continue

        if position is not None:
            unrealized_pct = (close - position["entry"]) / position["entry"]
            target_hit = unrealized_pct >= bot.PROFIT_TARGET_PCT
            stop_hit = unrealized_pct <= -bot.STOP_LOSS_PCT
            rsi_exit = rsi > bot.RSI_SELL_ABOVE and unrealized_pct > bot.ROUND_TRIP_FEE_PCT
            if target_hit or rsi_exit or stop_hit:
                gross = (close - position["entry"]) * position["qty"]
                net = gross - position["entry_fee"] - fee(position["qty"] * close)
                trades.append(net)
                position = None
            continue

        if rsi >= bot.RSI_BUY_BELOW:
            continue

        qty = STARTING_CASH / close
        notional = qty * close
        if notional < bot.MIN_POSITION_NOTIONAL:
            continue
        position = {"entry": close, "qty": qty, "entry_fee": fee(notional)}

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
    print(f"Testing the ACTUAL crypto_coinbase_bot.py module as it stands on this branch:")
    print(f"  RSI_BUY_BELOW={bot.RSI_BUY_BELOW}  RSI_SELL_ABOVE={bot.RSI_SELL_ABOVE}")
    print(f"  PROFIT_TARGET_PCT={bot.PROFIT_TARGET_PCT*100:.2f}%  STOP_LOSS_PCT={bot.STOP_LOSS_PCT*100:.2f}%  ROUND_TRIP_FEE_PCT={bot.ROUND_TRIP_FEE_PCT*100:.2f}%")
    print(f"  MAX_ALLOCATION=${bot.MAX_ALLOCATION:.2f}  MIN_POSITION_NOTIONAL=${bot.MIN_POSITION_NOTIONAL:.2f}\n")

    for product_id in SYMBOLS:
        print(f"=== {product_id} — fetching {LOOKBACK_DAYS}d of {GRANULARITY}s candles ===")
        rows = fetch_candles(product_id, LOOKBACK_DAYS)
        if len(rows) < 100:
            print(f"  ! only got {len(rows)} candles, skipping")
            continue
        print(f"  got {len(rows)} candles spanning "
              f"{datetime.fromtimestamp(rows[0][0], tz=timezone.utc).date()} to "
              f"{datetime.fromtimestamp(rows[-1][0], tz=timezone.utc).date()}")

        closes = [r[4] for r in rows]
        rsis = compute_rsi_series(closes)
        trades = run_backtest(rows, rsis)
        summarize("fee-aware target", trades)
        print()


if __name__ == "__main__":
    main()
