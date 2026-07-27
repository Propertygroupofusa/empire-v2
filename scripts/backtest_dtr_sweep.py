"""
One-off backtest: current RSI strategy (unrestricted + London-session-only)
vs. a "DTR range theory" liquidity-sweep reversal strategy, all with real
Coinbase trading fees applied - the earlier London-session backtest found
that the RSI strategy's edge (cents per trade) was smaller than Coinbase's
real per-trade fees, so this run adds fee modeling before comparing
anything else.

Not part of the app - throwaway, run under GitHub Actions (this sandbox
has no outbound network access to market-data APIs). Fetches ~30 days of
real 5-minute BTC-USD/ETH-USD candles from Coinbase's public endpoint
(same source crypto_coinbase_bot.py uses in production).

STRATEGIES:

  A) RSI unrestricted   - today's actual bot logic, entries allowed 24/7
  B) RSI London-only    - same logic, entries gated to 07:00-17:00 UTC
  C) DTR sweep (long)   - "range theory": the 00:00-07:00 UTC ("Asian
                           session") high/low is the day's reference range.
                           During the London window (07:00-11:00 UTC), if
                           price dips below the range low then closes back
                           above it, that's a liquidity sweep - enter long.
                           Stop = the sweep candle's low. Target = the
                           range high. Long-only, matching this account's
                           spot-only capability (same constraint the RSI
                           bot already has).

All three deduct a realistic 0.6% taker fee per side (1.2% round trip) -
Coinbase's real published rate for accounts below the $10k/30-day volume
tier, i.e. this account.
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

ASIAN_START_UTC_HOUR = 0
ASIAN_END_UTC_HOUR = 7
SWEEP_WINDOW_END_UTC_HOUR = 11  # London Killzone-style window for the sweep trigger

TAKER_FEE_RATE = 0.006  # 0.6% per side - Coinbase's real <$10k/30d tier rate


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
        time.sleep(0.4)

    by_time = {row[0]: row for row in all_rows}
    ordered = sorted(by_time.values(), key=lambda r: r[0])
    return ordered  # [time, low, high, open, close, volume]


def compute_rsi_series(closes):
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


def utc_hour(ts: int) -> int:
    return datetime.fromtimestamp(ts, tz=timezone.utc).hour


def utc_date(ts: int):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def fee(notional: float) -> float:
    return notional * TAKER_FEE_RATE


def run_rsi_variant(rows, rsis, session_gated: bool):
    """Same logic as the earlier backtest, now with entry+exit fees
    deducted from realized P&L on every closed trade."""
    position = None
    trades = []

    for i in range(14, len(rows)):
        ts, low, high, open_, close, vol = rows[i]
        rsi = rsis[i]
        if rsi is None:
            continue

        if position is not None:
            gross_unrealized = (close - position["entry"]) * position["qty"]
            if gross_unrealized >= PROFIT_TARGET_DOLLARS or rsi > RSI_SELL_ABOVE:
                notional = position["qty"] * close
                net = gross_unrealized - position["entry_fee"] - fee(notional)
                trades.append(net)
                position = None
            continue

        if rsi >= RSI_BUY_BELOW:
            continue
        if session_gated and not (LONDON_START_UTC_HOUR <= utc_hour(ts) < LONDON_END_UTC_HOUR):
            continue

        qty = STARTING_CASH / close
        notional = qty * close
        if notional < MIN_POSITION_NOTIONAL:
            continue
        position = {"entry": close, "qty": qty, "entry_fee": fee(notional)}

    if position is not None:
        final_close = rows[-1][4]
        gross = (final_close - position["entry"]) * position["qty"]
        net = gross - position["entry_fee"] - fee(position["qty"] * final_close)
        trades.append(net)

    return trades


def run_dtr_sweep_variant(rows):
    """Long-only liquidity-sweep reversal: 00:00-07:00 UTC sets the day's
    range; a dip below the range low followed by a close back above it,
    during the 07:00-11:00 UTC window, triggers a long. Stop = sweep low,
    target = range high. One position at a time, fees on both legs."""
    trades = []
    position = None  # {"entry", "qty", "stop", "target", "entry_fee"}

    day_low = None
    day_high = None
    current_day = None
    swept_below = False

    for i in range(len(rows)):
        ts, low, high, open_, close, vol = rows[i]
        d = utc_date(ts)
        hour = utc_hour(ts)

        if d != current_day:
            current_day = d
            day_low = None
            day_high = None
            swept_below = False

        if ASIAN_START_UTC_HOUR <= hour < ASIAN_END_UTC_HOUR:
            day_low = low if day_low is None else min(day_low, low)
            day_high = high if day_high is None else max(day_high, high)

        # manage open position first (stop/target checked against this bar's range)
        if position is not None:
            hit_stop = low <= position["stop"]
            hit_target = high >= position["target"]
            if hit_stop or hit_target:
                exit_price = position["stop"] if hit_stop else position["target"]
                gross = (exit_price - position["entry"]) * position["qty"]
                net = gross - position["entry_fee"] - fee(position["qty"] * exit_price)
                trades.append(net)
                position = None
            continue

        if day_low is None or day_high is None:
            continue  # no Asian range established yet this day
        if not (LONDON_START_UTC_HOUR <= hour < SWEEP_WINDOW_END_UTC_HOUR):
            continue

        if low < day_low:
            swept_below = True
            continue

        if swept_below and close > day_low:
            qty = STARTING_CASH / close
            notional = qty * close
            if notional < MIN_POSITION_NOTIONAL:
                swept_below = False
                continue
            position = {
                "entry": close, "qty": qty,
                "stop": low,  # this bar's low is the sweep extreme
                "target": day_high,
                "entry_fee": fee(notional),
            }
            swept_below = False

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
    print(f"Taker fee modeled: {TAKER_FEE_RATE*100:.2f}% per side ({TAKER_FEE_RATE*200:.2f}% round trip)\n")
    for product_id in SYMBOLS:
        print(f"=== {product_id} — fetching {LOOKBACK_DAYS}d of {GRANULARITY}s candles ===")
        rows = fetch_candles(product_id, LOOKBACK_DAYS)
        if len(rows) < 100:
            print(f"  ! only got {len(rows)} candles, skipping (insufficient data)")
            continue
        print(f"  got {len(rows)} candles spanning "
              f"{utc_date(rows[0][0])} to {utc_date(rows[-1][0])}")

        closes = [r[4] for r in rows]
        rsis = compute_rsi_series(closes)

        rsi_unrestricted = run_rsi_variant(rows, rsis, session_gated=False)
        rsi_london = run_rsi_variant(rows, rsis, session_gated=True)
        dtr_sweep = run_dtr_sweep_variant(rows)

        summarize("A) RSI unrestricted", rsi_unrestricted)
        summarize("B) RSI London-only", rsi_london)
        summarize("C) DTR sweep (long)", dtr_sweep)
        print()


if __name__ == "__main__":
    main()
