"""
One-off backtest: does gating crypto bot entries to the London trading
session (07:00-17:00 UTC) improve on the current 24/7 RSI-only approach?

Not part of the app - run manually (this repo's sandbox has no outbound
network access to market-data APIs, so this is designed to run under
GitHub Actions, which does). Fetches ~30 days of real 5-minute candles
for BTC-USD and ETH-USD from Coinbase's public candles endpoint (same
data source crypto_coinbase_bot.py uses in production), then replays
the bot's exact RSI/sizing/profit-target logic twice per symbol:

  A) UNRESTRICTED - entries allowed any time (today's actual behavior)
  B) LONDON-ONLY  - entries only allowed 07:00-17:00 UTC (exits are
                     risk management, so they're never gated in either
                     variant - only NEW positions are session-restricted)

Prints trade count, win rate, total P&L, and avg P&L/trade for both
variants on both symbols so the two can be compared head to head.
"""
import sys
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

SYMBOLS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 300  # 5 minutes, matches crypto_coinbase_bot.py's timeframe
LOOKBACK_DAYS = 30
MAX_CANDLES_PER_REQ = 300

RSI_BUY_BELOW = 45.0
RSI_SELL_ABOVE = 50.0
PROFIT_TARGET_DOLLARS = 0.50
STARTING_CASH = 100.0
MIN_POSITION_NOTIONAL = 5.0

LONDON_START_UTC_HOUR = 7
LONDON_END_UTC_HOUR = 17


def fetch_candles(product_id: str, days: int):
    """Paginate Coinbase's public candles endpoint backward from now.
    Each candle row: [time, low, high, open, close, volume]."""
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
            print(f"  ! HTTP {e.code} fetching {product_id} [{cursor_start}, {cursor_end}]: {e.read()[:200]}", file=sys.stderr)
            rows = []
        except Exception as e:
            print(f"  ! error fetching {product_id} [{cursor_start}, {cursor_end}]: {e}", file=sys.stderr)
            rows = []

        all_rows.extend(rows)
        cursor_end = cursor_start
        time.sleep(0.4)  # stay well under Coinbase's public rate limit

    # dedupe + sort ascending by time
    by_time = {row[0]: row for row in all_rows}
    ordered = sorted(by_time.values(), key=lambda r: r[0])
    return ordered  # [time, low, high, open, close, volume]


def compute_rsi_series(closes):
    """Same 14-period RSI as crypto_coinbase_bot.py's _compute_rsi, but
    returns a value for every bar (rolling) instead of just the latest."""
    rsis = [None] * len(closes)
    for i in range(14, len(closes)):
        window = closes[i - 14:i + 1]
        gains = [max(window[j] - window[j - 1], 0) for j in range(1, len(window))]
        losses = [max(window[j - 1] - window[j], 0) for j in range(1, len(window))]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsis[i] = round(100 - (100 / (1 + rs)), 1)
    return rsis


def in_london_session(ts: int) -> bool:
    hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    return LONDON_START_UTC_HOUR <= hour < LONDON_END_UTC_HOUR


def run_variant(rows, rsis, session_gated: bool):
    """Single-position, long-only simulation matching
    crypto_coinbase_bot.py's exit rules (profit target OR RSI > 50) and
    entry rule (RSI < 45), replayed bar-by-bar over history."""
    position = None  # {"entry": price, "qty": qty}
    trades = []

    for i in range(14, len(rows)):
        ts, low, high, open_, close, vol = rows[i]
        rsi = rsis[i]
        if rsi is None:
            continue

        if position is not None:
            unrealized = (close - position["entry"]) * position["qty"]
            if unrealized >= PROFIT_TARGET_DOLLARS or rsi > RSI_SELL_ABOVE:
                trades.append(unrealized)
                position = None
            continue

        if rsi >= RSI_BUY_BELOW:
            continue
        if session_gated and not in_london_session(ts):
            continue

        qty = STARTING_CASH / close
        if qty * close < MIN_POSITION_NOTIONAL:
            continue
        position = {"entry": close, "qty": qty}

    if position is not None:
        final_close = rows[-1][4]
        trades.append((final_close - position["entry"]) * position["qty"])

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
    for product_id in SYMBOLS:
        print(f"\n=== {product_id} — fetching {LOOKBACK_DAYS}d of {GRANULARITY}s candles ===")
        rows = fetch_candles(product_id, LOOKBACK_DAYS)
        if len(rows) < 100:
            print(f"  ! only got {len(rows)} candles, skipping (insufficient data)")
            continue
        print(f"  got {len(rows)} candles spanning "
              f"{datetime.fromtimestamp(rows[0][0], tz=timezone.utc).date()} to "
              f"{datetime.fromtimestamp(rows[-1][0], tz=timezone.utc).date()}")

        closes = [r[4] for r in rows]
        rsis = compute_rsi_series(closes)

        unrestricted = run_variant(rows, rsis, session_gated=False)
        london_only = run_variant(rows, rsis, session_gated=True)

        summarize("A) unrestricted (now)", unrestricted)
        summarize("B) London-only entries", london_only)


if __name__ == "__main__":
    main()
