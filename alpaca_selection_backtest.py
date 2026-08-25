"""
ALPACA STOCK/ETF SELECTION BACKTEST - the Alpaca-side counterpart to
crypto_selection_backtest.py. Pulls real historical Alpaca bars for every
symbol prop_bot.py/alpaca_swing_bot.py actually trade (SPY, QQQ, DIA, IWM,
GLD, USO, SLV, plus the 1x inverse ETFs SH/PSQ/DOG/RWM) and replays
alpaca_mean_reversion.py's own real target/stop/breakeven-ratchet/
peak-giveback rules against them - same "replay the bot's own real rules
on real history" approach as the crypto side, not a reimplementation.

LONG-ONLY replay, on purpose: validate_dual_direction() can also flag a
SHORT entry (RSI overbought), but prop_bot.py's real Alpaca account has
shorting disabled - confirmed live, every short attempt fails with
"account is not allowed to short" (see get_account_shorting_enabled() in
prop_bot.py). Backtesting the short side would be purely hypothetical
and couldn't inform any real decision this account can actually act on,
so this only replays what's genuinely executable today.

The backtest run itself never places a real order - shadow mode, same as
crypto_selection_backtest.py.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from alpaca_mean_reversion import should_exit_position
from prop_bot import FUTURES, get_headers

log = logging.getLogger("alpaca_selection_backtest")

BACKTEST_DAYS = 30
SPEND_PER_TRADE = 150.0
STOP_LOSS_PCT = 0.015
MIN_PROFIT_TARGET_PCT = 0.02
RSI_LONG_THRESHOLD = 40.0
RSI_PROFIT_THRESHOLD_LONG = 60.0
BREAKEVEN_TRIGGER_PCT = 0.01
MAX_GIVEBACK_PCT = 0.005
BAR_MINUTES = 15  # matches the "15Min" timeframe requested below


async def _fetch_bars(session, symbol: str, days: int):
    """Real historical 15-min bars from Alpaca's market-data API (IEX feed -
    free tier, same one prop_bot.py's own get_higher_tf_trend already
    uses). Returns (closes: list[float], None) or (None, reason)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=15Min&start={start}&limit=10000&feed=iex"
    try:
        async with session.get(url, headers=get_headers(), timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                body = (await r.text())[:200]
                return None, f"HTTP {r.status}: {body}"
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 60:
                return None, f"only {len(bars)} bars (need 60+)"
            return [b["c"] for b in bars], None
    except asyncio.TimeoutError:
        return None, "Alpaca API timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


def _compute_rsi(closes: list, period: int = 14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return 100 - (100 / (1 + rs))


def _replay_symbol(
    closes: list, spend_per_trade: float = SPEND_PER_TRADE, symbol: str = None,
    giveback_pct: float = MAX_GIVEBACK_PCT, profit_target_pct: float = MIN_PROFIT_TARGET_PCT,
) -> list:
    """Long-only replay: enters when 14-period RSI < RSI_LONG_THRESHOLD
    (mirrors validate_dual_direction's long branch), then exits via the
    bot's real should_exit_position() rules - same function the live bot
    calls, not a reimplementation. A position still open at the end of
    the window is marked-to-market at the last close, same convention
    crypto_selection_backtest.py uses.

    `symbol` is passed through to should_exit_position() purely for its
    own log line - previously left as the hardcoded default None, which
    made every backtest-replay exit line print "None (LONG)"/"None
    (SHORT)" instead of the real ticker (e.g. "SPY"), indistinguishable
    from a genuine live-position log line at a glance. Doesn't change the
    returned should_exit/new_peak values at all - purely a diagnostic
    label.

    `giveback_pct`/`profit_target_pct` default to the module's own real
    live constants (MAX_GIVEBACK_PCT/MIN_PROFIT_TARGET_PCT), so every
    existing caller (run_full_backtest) is completely unaffected - these
    only let run_exit_rule_sensitivity_comparison() below replay the
    exact same real history under a looser exit rule, to see whether it
    would have actually made more money."""
    trades = []
    position = None  # {"entry": float, "peak_pnl_pct": float, "entry_idx": int}

    for i in range(50, len(closes)):
        window = closes[max(0, i - 50):i + 1]
        rsi = _compute_rsi(window)
        if rsi is None:
            continue
        price = closes[i]

        if position is None:
            if rsi < RSI_LONG_THRESHOLD:
                position = {"entry": price, "peak_pnl_pct": 0.0, "entry_idx": i}
            continue

        age_seconds = (i - position["entry_idx"]) * BAR_MINUTES * 60
        should_exit, _reason, _exit_type, new_peak = should_exit_position(
            symbol=symbol, entry_price=position["entry"], current_price=price,
            current_rsi=rsi, position_age_seconds=age_seconds, direction="long",
            stop_loss_pct=STOP_LOSS_PCT, min_profit_target_pct=profit_target_pct,
            rsi_profit_threshold_long=RSI_PROFIT_THRESHOLD_LONG,
            peak_pnl_pct=position["peak_pnl_pct"],
            breakeven_trigger_pct=BREAKEVEN_TRIGGER_PCT, max_giveback_pct=giveback_pct,
        )
        position["peak_pnl_pct"] = new_peak
        if should_exit:
            pnl_pct = (price - position["entry"]) / position["entry"]
            trades.append({"pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct})
            position = None

    if position is not None:
        price = closes[-1]
        pnl_pct = (price - position["entry"]) / position["entry"]
        trades.append({"pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct})

    return trades


async def run_full_backtest(contract_codes=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """Real entry point - pulls real Alpaca history concurrently (capped
    by max_concurrent) for every symbol in prop_bot.py's FUTURES universe
    (or a caller-supplied subset of contract codes), replays the real
    exit rules, and ranks by real backtested ROI. Never places an order."""
    codes = contract_codes or list(FUTURES.keys())
    tickers = [(code, FUTURES[code]["symbol"]) for code in codes]

    semaphore = asyncio.Semaphore(max_concurrent)
    results = []
    skipped = []

    async with aiohttp.ClientSession() as session:
        async def _one(ticker):
            async with semaphore:
                closes, err = await _fetch_bars(session, ticker, days)
                if closes is None:
                    skipped.append({"product_id": ticker, "reason": err})
                    return
                trades = _replay_symbol(closes, symbol=ticker)
                if not trades:
                    skipped.append({"product_id": ticker, "reason": "no trades produced (RSI never dipped below threshold)"})
                    return
                wins = [t for t in trades if t["pnl_usd"] > 0]
                total_pnl = sum(t["pnl_usd"] for t in trades)
                avg_trade_pct = sum(t["pnl_pct"] for t in trades) / len(trades) * 100
                results.append({
                    "product_id": ticker,
                    "num_trades": len(trades),
                    "win_rate": round(len(wins) / len(trades) * 100, 1),
                    "total_pnl": round(total_pnl, 2),
                    "roi_pct_of_spend": round(total_pnl / SPEND_PER_TRADE * 100, 1),
                    "avg_trade_pct": round(avg_trade_pct, 2),
                })

        # Dedup tickers (SH/PSQ/DOG/RWM's contract code equals their own
        # ticker, so no collision with MES/MNQ/etc., but stay defensive)
        seen = set()
        unique_tickers = []
        for _code, ticker in tickers:
            if ticker not in seen:
                seen.add(ticker)
                unique_tickers.append(ticker)

        await asyncio.gather(*[_one(t) for t in unique_tickers])

    results.sort(key=lambda r: -r["roi_pct_of_spend"])
    return {
        "backtest_days": days,
        "spend_per_trade": SPEND_PER_TRADE,
        "coins_tested": len(unique_tickers),
        "coins_with_results": len(results),
        "skipped": skipped,
        "ranked": results,
    }


# ============================================================================
# EXIT-RULE SENSITIVITY COMPARISON (shadow mode, additive only)
# ============================================================================
# Real account owner question after 4 months of live Alpaca trading: $980
# in, only ~$29-50 of real profit - is the tight peak-giveback cap (0.5%)
# the reason winners never get room to run toward the real 2% target? This
# replays the SAME real historical bars every symbol already gets in
# run_full_backtest() above, under multiple exit-rule scenarios side by
# side, using the bot's own real should_exit_position() for every scenario
# - never a reimplementation, never touches live trading or places an
# order. Deliberately NOT wired into anything live; purely a decision-
# support comparison, same shadow-mode-only posture as every other
# comparison tool in this codebase (BTC-relative-strength, higher-tf-trend
# on the crypto side).
EXIT_RULE_SCENARIOS = {
    "current (0.5% giveback / 2% target)": {"giveback_pct": MAX_GIVEBACK_PCT, "profit_target_pct": MIN_PROFIT_TARGET_PCT},
    "moderate (1.5% giveback / 3% target)": {"giveback_pct": 0.015, "profit_target_pct": 0.03},
    "loose (2.5% giveback / 4% target)": {"giveback_pct": 0.025, "profit_target_pct": 0.04},
}


async def run_exit_rule_sensitivity_comparison(contract_codes=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """Fetches real Alpaca history ONCE per symbol (not once per scenario -
    same real bars replayed multiple times), then replays every scenario in
    EXIT_RULE_SCENARIOS against it via the bot's real should_exit_position().
    Returns both a per-scenario TOTAL (summed across every symbol - the
    direct answer to "would loosening this have made more real money over
    the last 30 days") and a per-symbol breakdown per scenario."""
    codes = contract_codes or list(FUTURES.keys())
    tickers = [(code, FUTURES[code]["symbol"]) for code in codes]
    seen = set()
    unique_tickers = []
    for _code, ticker in tickers:
        if ticker not in seen:
            seen.add(ticker)
            unique_tickers.append(ticker)

    semaphore = asyncio.Semaphore(max_concurrent)
    per_symbol = {}
    skipped = []

    async with aiohttp.ClientSession() as session:
        async def _one(ticker):
            async with semaphore:
                closes, err = await _fetch_bars(session, ticker, days)
                if closes is None:
                    skipped.append({"product_id": ticker, "reason": err})
                    return
                per_symbol[ticker] = closes

        await asyncio.gather(*[_one(t) for t in unique_tickers])

    scenario_totals = {name: {"total_pnl": 0.0, "num_trades": 0, "num_wins": 0} for name in EXIT_RULE_SCENARIOS}
    symbol_breakdown = []

    for ticker, closes in per_symbol.items():
        row = {"product_id": ticker, "scenarios": {}}
        for name, params in EXIT_RULE_SCENARIOS.items():
            trades = _replay_symbol(
                closes, symbol=ticker,
                giveback_pct=params["giveback_pct"], profit_target_pct=params["profit_target_pct"],
            )
            wins = [t for t in trades if t["pnl_usd"] > 0]
            total_pnl = sum(t["pnl_usd"] for t in trades)
            row["scenarios"][name] = {
                "num_trades": len(trades),
                "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
                "total_pnl": round(total_pnl, 2),
                "roi_pct_of_spend": round(total_pnl / SPEND_PER_TRADE * 100, 1) if trades else 0.0,
            }
            scenario_totals[name]["total_pnl"] += total_pnl
            scenario_totals[name]["num_trades"] += len(trades)
            scenario_totals[name]["num_wins"] += len(wins)
        symbol_breakdown.append(row)

    for name, totals in scenario_totals.items():
        totals["total_pnl"] = round(totals["total_pnl"], 2)
        totals["win_rate"] = round(totals["num_wins"] / totals["num_trades"] * 100, 1) if totals["num_trades"] else 0.0

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND_PER_TRADE,
        "symbols_tested": len(unique_tickers),
        "symbols_with_results": len(per_symbol),
        "skipped": skipped,
        "scenario_totals": scenario_totals,
        "symbol_breakdown": symbol_breakdown,
    }
