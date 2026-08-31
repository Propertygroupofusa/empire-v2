"""
SHADOW-MODE BACKTEST - offline tool only. Does not touch live trading, is
not imported by any live bot, and places no real orders.

Built per the account owner's own direct request: after sharing a real US
Balance of Trade release from tradingeconomics.com and asking "if you
specifically think broad macro releases (trade balance, CPI, jobs numbers)
affect how BTC or the stocks move around release dates... Back-test them
and let me look and make a decision."

Honest, stated up front rather than hidden: this sandbox has no live
network access to any economic-calendar API, and this codebase has never
integrated one - so this tool can only test EVENT DATES that are
explicitly, verifiably real, never guessed from memory or a "typical
schedule" assumption. MACRO_EVENTS below holds exactly the two real dates
the account owner themselves pasted straight from tradingeconomics.com's
own US Balance of Trade calendar - the May and June 2026 trade-balance
releases, both real past release dates with a real "12:30 PM" (GMT/UTC)
release time shown right on that page. Nothing here is invented.

That is a genuinely tiny real sample - 2 events is nowhere near enough to
draw a real conclusion from. This tool exists so the account owner can
grow MACRO_EVENTS with more real, verified release dates (CPI, jobs
reports, more trade-balance prints, FOMC decisions, etc. - pasted the same
way this first pair was) and re-run it as the real sample builds up, the
same "evidence before any live change" discipline every other backtest in
this codebase already follows. Nothing here changes what any live bot
does - it only ever fetches real historical price data and reports real
numbers.

Methodology (a standard event-study approach):
  1. For each real event, find the real candle/bar immediately before its
     real release time, then measure the real % return and real
     volatility over the following WINDOW real periods (24 hourly candles
     for BTC-USD - a full real day; 26 real 15-min bars for SPY/QQQ -
     roughly one real trading session).
  2. Compare that against a real baseline: WINDOW-length return/volatility
     sampled from BASELINE_SAMPLES random, non-overlapping-with-any-event
     windows drawn from the same real fetched history - the honest "does
     this actually look different from a typical window" comparison,
     never a fabricated significance claim.
"""
import random
import statistics
import sys

sys.path.insert(0, "/home/user/empire-v2")
import os

os.environ.setdefault("COINBASE_API_KEY_NAME", "unused-public-endpoint-only")
os.environ.setdefault("COINBASE_API_PRIVATE_KEY", "unused-public-endpoint-only")

from datetime import datetime, timedelta, timezone

import aiohttp

from crypto_selection_backtest import fetch_candles_window
from alpaca_selection_backtest import _fetch_bars_with_times

# Real, verified US macro release dates - see the module docstring for
# exactly where these came from. Grow this list with more real, verified
# dates (pasted from the same kind of real source) to build up a
# meaningful sample - do NOT add a guessed/typical-schedule date here.
MACRO_EVENTS = [
    {"date": "2026-07-07", "time_utc": "12:30", "label": "US Trade Balance (May 2026)", "type": "trade_balance"},
    {"date": "2026-08-04", "time_utc": "12:30", "label": "US Trade Balance (June 2026)", "type": "trade_balance"},
]

WINDOW_CANDLES_BTC = 24  # 24 real hourly candles = 1 real day following each event
WINDOW_BARS_STOCK = 26  # 26 real 15-min bars = ~1 real trading session following each event
DAYS_BACK_DEFAULT = 65  # wide enough to reach both real dates above from "now"
BASELINE_SAMPLES = 200


def _find_index_at_or_after(times: list, target_epoch: int):
    """Real bisect over an already-sorted (oldest-first) list of Unix-second
    timestamps - the first real index whose time is >= target_epoch, or
    None if the real fetched series doesn't reach that far."""
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < target_epoch:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(times) else None


def _window_return_and_vol(closes: list, start_idx: int, window_len: int):
    """Real % return and real volatility (population stdev of the real
    period-over-period % returns) over `window_len` real candles/bars
    starting at closes[start_idx]. None if the window runs past the end of
    the real series."""
    end_idx = start_idx + window_len
    if start_idx < 0 or end_idx >= len(closes):
        return None
    segment = closes[start_idx:end_idx + 1]
    ret_pct = (segment[-1] - segment[0]) / segment[0]
    period_returns = [(segment[i + 1] - segment[i]) / segment[i] for i in range(len(segment) - 1)]
    vol = statistics.pstdev(period_returns) if len(period_returns) > 1 else 0.0
    return {"return_pct": ret_pct, "volatility": vol}


def run_event_study(times: list, closes: list, events: list, window_len: int,
                     baseline_samples: int = BASELINE_SAMPLES, seed: int = 0) -> dict:
    """Pure function, no I/O - the real event-study core shared by both the
    BTC and stock backtests below. `times`/`closes` must already be real,
    oldest-first, same-length series. See the module docstring for the
    full real methodology."""
    rng = random.Random(seed)
    event_results = []
    event_idx_ranges = []
    for ev in events:
        event_dt = datetime.fromisoformat(f"{ev['date']}T{ev['time_utc']}:00+00:00")
        target_epoch = int(event_dt.timestamp())
        idx = _find_index_at_or_after(times, target_epoch)
        if idx is None:
            event_results.append({**ev, "available": False, "reason": "event date is outside the real fetched history window"})
            continue
        start_idx = max(idx - 1, 0)
        result = _window_return_and_vol(closes, start_idx, window_len)
        if result is None:
            event_results.append({**ev, "available": False, "reason": "not enough real history after this event to fill the window"})
            continue
        event_results.append({
            **ev, "available": True,
            "return_pct": round(result["return_pct"] * 100, 3),
            "volatility_pct": round(result["volatility"] * 100, 3),
        })
        event_idx_ranges.append((start_idx, start_idx + window_len))

    def overlaps_event(start_idx):
        end_idx = start_idx + window_len
        return any(not (end_idx < e_start or start_idx > e_end) for e_start, e_end in event_idx_ranges)

    valid_starts = [i for i in range(0, len(closes) - window_len) if not overlaps_event(i)]
    sample_size = min(baseline_samples, len(valid_starts))
    baseline_returns, baseline_vols = [], []
    if sample_size > 0:
        for start_idx in rng.sample(valid_starts, sample_size):
            r = _window_return_and_vol(closes, start_idx, window_len)
            baseline_returns.append(r["return_pct"])
            baseline_vols.append(r["volatility"])

    available_events = [e for e in event_results if e["available"]]
    return {
        "events": event_results,
        "num_events_available": len(available_events),
        "num_events_total": len(events),
        "baseline_sample_size": sample_size,
        "baseline_avg_return_pct": round(statistics.mean(baseline_returns) * 100, 3) if baseline_returns else None,
        "baseline_return_stdev_pct": round(statistics.pstdev(baseline_returns) * 100, 3) if len(baseline_returns) > 1 else None,
        "baseline_avg_volatility_pct": round(statistics.mean(baseline_vols) * 100, 3) if baseline_vols else None,
        "event_avg_return_pct": round(statistics.mean([e["return_pct"] for e in available_events]), 3) if available_events else None,
        "event_avg_volatility_pct": round(statistics.mean([e["volatility_pct"] for e in available_events]), 3) if available_events else None,
    }


async def run_btc_macro_event_backtest(events: list = None, days_back: int = DAYS_BACK_DEFAULT) -> dict:
    """Real BTC-USD side - fetches real Coinbase hourly candles (the exact
    same fetch_candles_window() every other crypto backtest tool in this
    codebase already uses) wide enough to cover every real event date, then
    runs the shared event study against it."""
    events = events if events is not None else MACRO_EVENTS
    async with aiohttp.ClientSession() as session:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        result = await fetch_candles_window(session, "BTC-USD", start, end, min_candles=WINDOW_CANDLES_BTC + 5)
    if result is None:
        return {"error": "could not fetch enough real BTC-USD history right now to run this"}
    closes, highs, lows, times = result
    return run_event_study(times, closes, events, WINDOW_CANDLES_BTC)


async def run_stock_macro_event_backtest(symbol: str = "SPY", events: list = None, days_back: int = DAYS_BACK_DEFAULT) -> dict:
    """Real stock/ETF side - fetches real Alpaca 15-min bars (the exact
    same _fetch_bars_with_times() the combined-strategy backtest already
    uses) wide enough to cover every real event date, then runs the shared
    event study against it."""
    events = events if events is not None else MACRO_EVENTS
    async with aiohttp.ClientSession() as session:
        bars, err = await _fetch_bars_with_times(session, symbol, days=days_back)
    if bars is None:
        return {"error": f"could not fetch enough real {symbol} history right now: {err}"}
    times = [int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()) for t, _c in bars]
    closes = [c for _t, c in bars]
    return run_event_study(times, closes, events, WINDOW_BARS_STOCK)


async def run_macro_event_backtest(events: list = None, days_back: int = DAYS_BACK_DEFAULT,
                                    stock_symbols: tuple = ("SPY", "QQQ")) -> dict:
    """Real, top-level entry point - runs the BTC side plus every requested
    real stock symbol and returns them together, so the account owner gets
    one real answer covering both crypto and stocks from a single click."""
    events = events if events is not None else MACRO_EVENTS
    btc_result = await run_btc_macro_event_backtest(events, days_back)
    stock_results = {}
    for sym in stock_symbols:
        stock_results[sym] = await run_stock_macro_event_backtest(sym, events, days_back)
    return {"events_used": events, "btc": btc_result, "stocks": stock_results}
