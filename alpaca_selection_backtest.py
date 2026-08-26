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


async def _fetch_bars(session, symbol: str, days: int, end: str = None):
    """Real historical 15-min bars from Alpaca's market-data API (IEX feed -
    free tier, same one prop_bot.py's own get_higher_tf_trend already
    uses). Returns (closes: list[float], None) or (None, reason).

    `end` (optional, ISO string) lets a caller fetch a window that ends in
    the past rather than right now - e.g. "the 30 days before the most
    recent 30 days" - without changing the default single-window behavior
    every existing caller relies on (end=None still means "up to now",
    byte-for-byte the same request this function has always made)."""
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
    start = (end_dt - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=15Min&start={start}&limit=10000&feed=iex"
    if end:
        url += f"&end={end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
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


# ============================================================================
# MOMENTUM ENTRY / TRAILING-STOP EXIT COMPARISON (shadow mode, additive only)
# ============================================================================
# Real account owner request: everything built so far (and everything
# live) is mean-reversion - buy weakness (RSI oversold), take a small
# quick profit. The account owner asked directly for the opposite idea:
# buy STRENGTH (a symbol already moving up with real momentum) and let a
# winner run with a trailing stop instead of a small fixed target - "the
# trend is your friend" instead of "buy the dip." This never touches live
# trading or places an order - it replays real historical Alpaca bars
# under a genuinely different entry/exit rule set, alongside the existing
# real mean-reversion replay on the SAME bars, so the two are directly,
# fairly comparable with real evidence before any real money is touched.
MOMENTUM_RSI_ENTRY = 55.0  # buy strength (RSI above this), not weakness (mean-reversion buys RSI < 40)
MOMENTUM_SMA_PERIOD = 20  # price must be above this many bars' average - confirms a real uptrend, not just a single spike
MOMENTUM_TRAIL_PCT = 0.03  # exits when price pulls back this much from its OWN peak since entry - not a fixed target off entry
MOMENTUM_MAX_HOLD_BARS = 96  # 96 * 15min = 24 real hours - a backstop only, not the primary exit (momentum trades are meant to run)


def _compute_sma(closes: list, period: int):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _replay_symbol_momentum(closes: list, spend_per_trade: float = SPEND_PER_TRADE) -> list:
    """Momentum entry/trailing-exit replay: buys strength (RSI elevated AND
    price above its own MOMENTUM_SMA_PERIOD-bar average - confirms a real
    uptrend, not just one noisy spike) instead of mean-reversion's
    RSI-oversold weakness, and exits via a trailing stop measured off the
    real PEAK price reached since entry, not a small fixed percentage off
    entry - lets a real winning move run further, only cutting it once it
    genuinely reverses from its own high. MOMENTUM_MAX_HOLD_BARS is a
    backstop only (24 real hours), unlike mean-reversion's much tighter
    2-hour default - a real momentum trade is meant to be held longer."""
    trades = []
    position = None  # {"entry": float, "peak": float, "entry_idx": int}

    for i in range(50, len(closes)):
        window = closes[max(0, i - 50):i + 1]
        rsi = _compute_rsi(window)
        sma = _compute_sma(closes[:i + 1], MOMENTUM_SMA_PERIOD)
        price = closes[i]
        if rsi is None or sma is None:
            continue

        if position is None:
            if rsi > MOMENTUM_RSI_ENTRY and price > sma:
                position = {"entry": price, "peak": price, "entry_idx": i}
            continue

        position["peak"] = max(position["peak"], price)
        trailing_stop = position["peak"] * (1 - MOMENTUM_TRAIL_PCT)
        age_bars = i - position["entry_idx"]

        if price <= trailing_stop or age_bars >= MOMENTUM_MAX_HOLD_BARS:
            pnl_pct = (price - position["entry"]) / position["entry"]
            trades.append({"pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct})
            position = None

    if position is not None:
        price = closes[-1]
        pnl_pct = (price - position["entry"]) / position["entry"]
        trades.append({"pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct})

    return trades


async def run_momentum_vs_mean_reversion_comparison(contract_codes=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """Fetches real Alpaca history ONCE per symbol, then replays BOTH the
    existing real mean-reversion strategy (_replay_symbol(), completely
    unchanged - the exact same real function run_full_backtest() uses) and
    the new momentum variant (_replay_symbol_momentum()) against the SAME
    real bars, so the two are directly, fairly comparable - never a
    reimplementation of the live mean-reversion rules, and never touches
    live trading or places a real order."""
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

    rows = []
    mr_total, mom_total = 0.0, 0.0
    mr_trades_total, mom_trades_total = 0, 0
    mr_wins_total, mom_wins_total = 0, 0

    for ticker, closes in per_symbol.items():
        mr_trades = _replay_symbol(closes, symbol=ticker)
        mom_trades = _replay_symbol_momentum(closes)

        mr_pnl = round(sum(t["pnl_usd"] for t in mr_trades), 2)
        mom_pnl = round(sum(t["pnl_usd"] for t in mom_trades), 2)
        mr_wins = len([t for t in mr_trades if t["pnl_usd"] > 0])
        mom_wins = len([t for t in mom_trades if t["pnl_usd"] > 0])

        rows.append({
            "product_id": ticker,
            "mean_reversion": {
                "num_trades": len(mr_trades),
                "win_rate": round(mr_wins / len(mr_trades) * 100, 1) if mr_trades else 0.0,
                "total_pnl": mr_pnl,
            },
            "momentum": {
                "num_trades": len(mom_trades),
                "win_rate": round(mom_wins / len(mom_trades) * 100, 1) if mom_trades else 0.0,
                "total_pnl": mom_pnl,
            },
        })
        mr_total += mr_pnl
        mom_total += mom_pnl
        mr_trades_total += len(mr_trades)
        mom_trades_total += len(mom_trades)
        mr_wins_total += mr_wins
        mom_wins_total += mom_wins

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND_PER_TRADE,
        "symbols_tested": len(unique_tickers),
        "symbols_with_results": len(per_symbol),
        "skipped": skipped,
        "totals": {
            "mean_reversion": {
                "num_trades": mr_trades_total,
                "win_rate": round(mr_wins_total / mr_trades_total * 100, 1) if mr_trades_total else 0.0,
                "total_pnl": round(mr_total, 2),
            },
            "momentum": {
                "num_trades": mom_trades_total,
                "win_rate": round(mom_wins_total / mom_trades_total * 100, 1) if mom_trades_total else 0.0,
                "total_pnl": round(mom_total, 2),
            },
        },
        "symbol_breakdown": rows,
    }


# ============================================================================
# MULTI-WINDOW momentum-vs-mean-reversion comparison - is one strategy
# consistently better, or did a single 30-day sample just get lucky/unlucky?
# ============================================================================
# The account owner ran run_momentum_vs_mean_reversion_comparison() above for
# real and got the OPPOSITE result from the run that originally justified
# switching the live bot to momentum months earlier: mean-reversion won this
# time ($54.58/353 trades vs momentum's $41.57/69 trades). A single 30-day
# window flipping isn't itself proof the earlier decision was wrong - the
# same "don't act on one noisy run" discipline already used elsewhere in
# this codebase (the crypto side's auto-exclusion layer requires several
# consecutive bad runs, not one, before it acts) applies here too. This
# answers the real question directly: run the identical comparison across
# SEVERAL back-to-back real historical windows and see which strategy wins
# more consistently, instead of trusting whichever window happened to be
# fetched most recently. Shadow-mode only - never touches live trading.
async def run_momentum_vs_mean_reversion_multi_window(
    contract_codes=None, window_days: int = BACKTEST_DAYS, num_windows: int = 3, max_concurrent: int = 6
) -> dict:
    """Runs run_momentum_vs_mean_reversion_comparison()'s exact same real
    replay logic across `num_windows` consecutive, non-overlapping real
    historical windows (most recent window first, then the window
    immediately before it, and so on) - e.g. the default 3x30 covers the
    real last 90 days as three real independent 30-day samples. Each
    window is a fully independent real fetch+replay, not a rolling
    average, so a strategy that wins 3 windows out of 3 is real, repeated
    evidence - not one lucky sample."""
    codes = contract_codes or list(FUTURES.keys())
    tickers = [(code, FUTURES[code]["symbol"]) for code in codes]
    seen = set()
    unique_tickers = []
    for _code, ticker in tickers:
        if ticker not in seen:
            seen.add(ticker)
            unique_tickers.append(ticker)

    semaphore = asyncio.Semaphore(max_concurrent)
    now = datetime.now(timezone.utc)
    windows = []

    async with aiohttp.ClientSession() as session:
        for w in range(num_windows):
            window_end = now - timedelta(days=w * window_days)
            window_end_iso = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            per_symbol = {}
            skipped = []

            async def _one(ticker, window_end_iso=window_end_iso, per_symbol=per_symbol, skipped=skipped):
                async with semaphore:
                    closes, err = await _fetch_bars(session, ticker, window_days, end=window_end_iso)
                    if closes is None:
                        skipped.append({"product_id": ticker, "reason": err})
                        return
                    per_symbol[ticker] = closes

            await asyncio.gather(*[_one(t) for t in unique_tickers])

            mr_total, mom_total = 0.0, 0.0
            mr_trades_total, mom_trades_total = 0, 0
            mr_wins_total, mom_wins_total = 0, 0
            for ticker, closes in per_symbol.items():
                mr_trades = _replay_symbol(closes, symbol=ticker)
                mom_trades = _replay_symbol_momentum(closes)
                mr_total += sum(t["pnl_usd"] for t in mr_trades)
                mom_total += sum(t["pnl_usd"] for t in mom_trades)
                mr_trades_total += len(mr_trades)
                mom_trades_total += len(mom_trades)
                mr_wins_total += len([t for t in mr_trades if t["pnl_usd"] > 0])
                mom_wins_total += len([t for t in mom_trades if t["pnl_usd"] > 0])

            windows.append({
                "window_index": w,
                "window_start": (window_end - timedelta(days=window_days)).strftime("%Y-%m-%d"),
                "window_end": window_end.strftime("%Y-%m-%d"),
                "symbols_with_results": len(per_symbol),
                "skipped": skipped,
                "mean_reversion": {
                    "num_trades": mr_trades_total,
                    "win_rate": round(mr_wins_total / mr_trades_total * 100, 1) if mr_trades_total else 0.0,
                    "total_pnl": round(mr_total, 2),
                },
                "momentum": {
                    "num_trades": mom_trades_total,
                    "win_rate": round(mom_wins_total / mom_trades_total * 100, 1) if mom_trades_total else 0.0,
                    "total_pnl": round(mom_total, 2),
                },
            })

    mr_wins_windows = sum(1 for wnd in windows if wnd["mean_reversion"]["total_pnl"] > wnd["momentum"]["total_pnl"])
    mom_wins_windows = sum(1 for wnd in windows if wnd["momentum"]["total_pnl"] > wnd["mean_reversion"]["total_pnl"])
    mr_total_all = round(sum(wnd["mean_reversion"]["total_pnl"] for wnd in windows), 2)
    mom_total_all = round(sum(wnd["momentum"]["total_pnl"] for wnd in windows), 2)

    return {
        "window_days": window_days,
        "num_windows": num_windows,
        "spend_per_trade": SPEND_PER_TRADE,
        "windows": windows,
        "summary": {
            "mean_reversion_windows_won": mr_wins_windows,
            "momentum_windows_won": mom_wins_windows,
            "mean_reversion_total_pnl": mr_total_all,
            "momentum_total_pnl": mom_total_all,
        },
    }


# ============================================================================
# COMBINED DUAL-STRATEGY BACKTEST - momentum AND mean-reversion running
# TOGETHER, sharing one real capital pool (shadow mode, additive only)
# ============================================================================
# Real account owner question, after seeing the momentum-vs-mean-reversion
# comparison above: "they together looks like it'll make a whole lot more
# money... are we putting them together?" run_momentum_vs_mean_reversion_
# comparison() above replays each ruleset INDEPENDENTLY, each with its own
# always-available $150/trade - the right way to answer "which ruleset is
# better," but not the right way to answer "would running both AT ONCE make
# more money," since a real account sharing one pool of cash can't spend the
# same dollar twice. This answers that second question directly: merges
# every symbol's real bars onto ONE shared timeline, in true chronological
# order (not per-symbol independently), and runs BOTH entry gates against a
# SINGLE real cash pool - a new signal can only open a position if there's
# genuinely enough free capital left, same as a real account actually
# working both strategies at once.
#
# Two real numbers come out of this, not one:
# - "unconstrained": the pool is set absurdly large so capital never binds -
#   the closest real answer to "if money were never the limit." Even this
#   isn't a naive sum of the two strategies' standalone totals above: since
#   momentum only enters when RSI > 55 and mean-reversion only enters when
#   RSI < 40, the SAME symbol can never be claimed by both at once, but two
#   real bots independently trading the same real symbol at genuinely
#   different times would still just be one real position in one real
#   account - this replay picks whichever signal claims a flat symbol
#   first, the same as real trading actually would.
# - "constrained": a real, modest shared pool (COMBINED_POOL_USD, enough
#   for a few real concurrent positions at once) - the honest answer given
#   the account's actual real scale, where a strong momentum signal and a
#   strong mean-reversion signal showing up on different symbols at the
#   same real moment genuinely do compete for the same real dollars.
COMBINED_POOL_USD = 3 * SPEND_PER_TRADE  # a real, modest shared pool - room for ~3 concurrent positions, matching the account's actual real trade size
UNCONSTRAINED_POOL_USD = 10_000_000.0  # effectively unlimited - isolates the "same-symbol overlap" effect from the "shared-cash" effect


async def _fetch_bars_with_times(session, symbol: str, days: int):
    """Same real historical 15-min bars as _fetch_bars, but keeps each bar's
    real UTC timestamp too - needed to replay multiple symbols on one
    shared timeline (see run_combined_dual_strategy_backtest), which plain
    array-index alignment can't guarantee stays in sync across symbols with
    slightly different real trading-session gaps. Returns
    (bars: list[(timestamp_str, close)], None) or (None, reason)."""
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
            return [(b["t"], b["c"]) for b in bars], None
    except asyncio.TimeoutError:
        return None, "Alpaca API timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


def _simulate_combined(events: list, pool_usd: float) -> dict:
    """Pure, stateless replay over a pre-built, already-time-sorted event
    stream [(timestamp_str, ticker, close), ...] spanning every symbol -
    no I/O, safe to call more than once (constrained vs unconstrained)
    against the identical fetched data. Real momentum and mean-reversion
    entry/exit logic here mirrors _replay_symbol_momentum()/_replay_symbol()
    exactly, just driven off real elapsed wall-clock time between entry and
    now (position_age_seconds) instead of a bar-index count, since bar
    spacing can't be assumed uniform once multiple symbols are interleaved."""
    history = {}
    cash = pool_usd
    open_positions = {}  # ticker -> {strategy, entry, entry_time, spend, peak/peak_pnl_pct}
    trades = []
    max_concurrent = 0

    for t, ticker, price in events:
        closes = history.setdefault(ticker, [])
        closes.append(price)
        if len(closes) < 51:
            continue
        rsi = _compute_rsi(closes[-51:])
        if rsi is None:
            continue
        now = datetime.fromisoformat(t.replace("Z", "+00:00"))

        pos = open_positions.get(ticker)
        if pos is not None:
            age_seconds = (now - pos["entry_time"]).total_seconds()
            if pos["strategy"] == "momentum":
                pos["peak"] = max(pos["peak"], price)
                exited = price <= pos["peak"] * (1 - MOMENTUM_TRAIL_PCT) or age_seconds >= MOMENTUM_MAX_HOLD_BARS * BAR_MINUTES * 60
            else:
                should_exit, _reason, _exit_type, new_peak = should_exit_position(
                    symbol=ticker, entry_price=pos["entry"], current_price=price,
                    current_rsi=rsi, position_age_seconds=age_seconds, direction="long",
                    stop_loss_pct=STOP_LOSS_PCT, min_profit_target_pct=MIN_PROFIT_TARGET_PCT,
                    rsi_profit_threshold_long=RSI_PROFIT_THRESHOLD_LONG,
                    peak_pnl_pct=pos["peak_pnl_pct"],
                    breakeven_trigger_pct=BREAKEVEN_TRIGGER_PCT, max_giveback_pct=MAX_GIVEBACK_PCT,
                )
                pos["peak_pnl_pct"] = new_peak
                exited = should_exit
            if exited:
                pnl_usd = pos["spend"] * (price - pos["entry"]) / pos["entry"]
                cash += pos["spend"] + pnl_usd
                trades.append({"product_id": ticker, "strategy": pos["strategy"], "pnl_usd": round(pnl_usd, 2)})
                del open_positions[ticker]
            continue  # a symbol that just exited doesn't also re-enter on the same bar

        sma = _compute_sma(closes, MOMENTUM_SMA_PERIOD)
        if sma is not None and rsi > MOMENTUM_RSI_ENTRY and price > sma and cash >= SPEND_PER_TRADE:
            open_positions[ticker] = {"strategy": "momentum", "entry": price, "peak": price, "entry_time": now, "spend": SPEND_PER_TRADE}
            cash -= SPEND_PER_TRADE
        elif rsi < RSI_LONG_THRESHOLD and cash >= SPEND_PER_TRADE:
            open_positions[ticker] = {"strategy": "mean_reversion", "entry": price, "peak_pnl_pct": 0.0, "entry_time": now, "spend": SPEND_PER_TRADE}
            cash -= SPEND_PER_TRADE
        max_concurrent = max(max_concurrent, len(open_positions))

    # mark-to-market any still-open positions at each symbol's own last close
    for ticker, pos in open_positions.items():
        price = history[ticker][-1]
        pnl_usd = pos["spend"] * (price - pos["entry"]) / pos["entry"]
        trades.append({"product_id": ticker, "strategy": pos["strategy"], "pnl_usd": round(pnl_usd, 2)})

    wins = [tr for tr in trades if tr["pnl_usd"] > 0]
    mom_trades = [tr for tr in trades if tr["strategy"] == "momentum"]
    mr_trades = [tr for tr in trades if tr["strategy"] == "mean_reversion"]
    return {
        "pool_usd": pool_usd,
        "max_concurrent_positions": max_concurrent,
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "total_pnl": round(sum(tr["pnl_usd"] for tr in trades), 2),
        "momentum_trades": len(mom_trades),
        "momentum_pnl": round(sum(tr["pnl_usd"] for tr in mom_trades), 2),
        "mean_reversion_trades": len(mr_trades),
        "mean_reversion_pnl": round(sum(tr["pnl_usd"] for tr in mr_trades), 2),
    }


async def run_combined_dual_strategy_backtest(
    contract_codes=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6, pool_usd: float = COMBINED_POOL_USD,
) -> dict:
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Real answer to "would running momentum AND mean-reversion together make
    more money than either alone." Fetches real Alpaca history ONCE per
    symbol (with real timestamps this time, not just closes), merges every
    symbol's bars into one real chronological timeline, then replays that
    single timeline twice via _simulate_combined() - once against a real,
    modest shared pool (constrained) and once against an effectively
    unlimited one (unconstrained) - so both the realistic answer and the
    theoretical ceiling come back from one real backtest run."""
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
                bars, err = await _fetch_bars_with_times(session, ticker, days)
                if bars is None:
                    skipped.append({"product_id": ticker, "reason": err})
                    return
                per_symbol[ticker] = bars

        await asyncio.gather(*[_one(t) for t in unique_tickers])

    events = [(t, ticker, c) for ticker, bars in per_symbol.items() for t, c in bars]
    events.sort(key=lambda e: e[0])

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND_PER_TRADE,
        "symbols_tested": len(unique_tickers),
        "symbols_with_results": len(per_symbol),
        "skipped": skipped,
        "constrained": _simulate_combined(events, pool_usd),
        "unconstrained": _simulate_combined(events, UNCONSTRAINED_POOL_USD),
    }


# ============================================================================
# ENTRY-SIGNAL A/B/C/D TEST (shadow mode, additive only)
# ============================================================================
# Real, well-reasoned pushback from the account owner (relaying a second
# tool's proposal) on the live momentum entry: RSI > 55 AND price > SMA20
# is binary - it can't tell a fresh breakout from a stock that's already
# run hard and is due to snap back. Rather than guess whether a richer
# gate actually helps, this replays the SAME real historical bars under 4
# entry variants that progressively layer on real filters, holding the
# EXISTING live exit (should_exit_position_momentum's trailing-stop logic,
# via _replay_symbol_momentum's same trail/backstop math) completely
# constant across all four - so this isolates entry-signal quality
# specifically, not a mix of entry+exit+sizing changes at once. ATR-based
# position sizing and a "higher highs" confirmation were both raised in
# the wider proposal but deliberately left OUT of this pass - sizing
# changes the dollar-risk basis per trade (would muddy whether an
# improvement came from better timing or just smaller risk), and "higher
# highs" wasn't in the account owner's own concrete A-D list, only the
# earlier conceptual sketch. Both are natural next tests once this
# narrower question is answered. Never touches live trading, places no
# order - same shadow-mode-only posture as every other comparison tool
# in this codebase.
SMA_SLOPE_LOOKBACK_BARS = 4  # ~1 real hour on 15-min bars - short enough that a real slope reading is timely, long enough that it isn't just noise
MAX_EXTENSION_PCT = 0.03  # overextension filter: price can't be more than this far above its own real SMA20 to still count as a fresh entry, not a stretched one

ENTRY_VARIANTS = {
    "A - current (RSI>55 + price>SMA20)": {
        "require_rsi_rising": False, "require_sma_rising": False, "require_not_overextended": False,
    },
    "B - momentum+ (+ RSI rising)": {
        "require_rsi_rising": True, "require_sma_rising": False, "require_not_overextended": False,
    },
    "C - momentum+trend (+ SMA20 rising)": {
        "require_rsi_rising": True, "require_sma_rising": True, "require_not_overextended": False,
    },
    "D - momentum+trend+overextension filter": {
        "require_rsi_rising": True, "require_sma_rising": True, "require_not_overextended": True,
    },
}


def _replay_symbol_momentum_variant(bars_with_times: list, filters: dict, spend_per_trade: float = SPEND_PER_TRADE) -> list:
    """Same real entry/exit shape as _replay_symbol_momentum(), but the
    entry gate is parameterized so the same replay loop can express every
    row of ENTRY_VARIANTS without four near-duplicate functions. The exit
    (trailing stop off the real peak since entry, 24h backstop) is
    IDENTICAL for every variant - only ever the entry gate changes.
    Returns trades carrying real entry_time/exit_time (not just pnl), so
    the caller can compute a real equity curve, holding time, and
    drawdown - metrics the plain pnl-only replay functions don't need."""
    times = [t for t, _c in bars_with_times]
    closes = [c for _t, c in bars_with_times]
    trades = []
    position = None  # {"entry": float, "peak": float, "entry_idx": int, "entry_time": str}

    for i in range(50, len(closes)):
        window = closes[max(0, i - 50):i + 1]
        rsi = _compute_rsi(window)
        sma = _compute_sma(closes[:i + 1], MOMENTUM_SMA_PERIOD)
        price = closes[i]
        if rsi is None or sma is None:
            continue

        if position is None:
            if not (rsi > MOMENTUM_RSI_ENTRY and price > sma):
                continue
            if filters["require_rsi_rising"]:
                prev_window = closes[max(0, i - 1 - 50):i]
                rsi_prev = _compute_rsi(prev_window)
                if rsi_prev is None or not (rsi > rsi_prev):
                    continue
            if filters["require_sma_rising"]:
                sma_prev = _compute_sma(closes[:i + 1 - SMA_SLOPE_LOOKBACK_BARS], MOMENTUM_SMA_PERIOD)
                if sma_prev is None or not (sma > sma_prev):
                    continue
            if filters["require_not_overextended"]:
                if (price - sma) / sma > MAX_EXTENSION_PCT:
                    continue
            position = {"entry": price, "peak": price, "entry_idx": i, "entry_time": times[i]}
            continue

        position["peak"] = max(position["peak"], price)
        trailing_stop = position["peak"] * (1 - MOMENTUM_TRAIL_PCT)
        age_bars = i - position["entry_idx"]

        if price <= trailing_stop or age_bars >= MOMENTUM_MAX_HOLD_BARS:
            pnl_pct = (price - position["entry"]) / position["entry"]
            trades.append({
                "pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct,
                "entry_time": position["entry_time"], "exit_time": times[i],
            })
            position = None

    if position is not None:
        price = closes[-1]
        pnl_pct = (price - position["entry"]) / position["entry"]
        trades.append({
            "pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct,
            "entry_time": position["entry_time"], "exit_time": times[-1],
        })

    return trades


def _summarize_trades(trades: list) -> dict:
    """Real per-variant statistics per the account owner's explicit request
    to judge these on more than total profit alone: win rate, profit
    factor, average trade, a real dollar max drawdown off a real
    chronological equity curve, Sharpe/Sortino computed from the real
    per-trade return series (not annualized - a real per-trade ratio, not
    dressed up as more precise than it is), real average holding time from
    real entry/exit timestamps, and the longest real losing streak in
    real chronological order. Fee/slippage modeling and a market-regime
    breakdown were both raised in the wider proposal but aren't built here -
    every other backtest tool in this codebase has the same real gap
    (no fee model), and regime classification is a genuinely separate,
    larger feature, not a metric this function can produce on its own."""
    if not trades:
        return {
            "num_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "roi_pct_of_spend": 0.0,
            "avg_trade_pct": 0.0, "profit_factor": None, "max_drawdown_usd": 0.0,
            "sharpe": None, "sortino": None, "avg_holding_hours": None, "max_losing_streak": 0,
        }

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    if gross_loss > 0:
        profit_factor = round(gross_win / gross_loss, 2)
    elif gross_win > 0:
        profit_factor = None  # no real losses to divide by - not a real, finite ratio
    else:
        profit_factor = None

    returns = [t["pnl_pct"] for t in trades]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    stdev = variance ** 0.5
    sharpe = round(mean_r / stdev, 3) if stdev > 0 else None

    downside = [r for r in returns if r < 0]
    if downside:
        down_dev = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5
        sortino = round(mean_r / down_dev, 3) if down_dev > 0 else None
    else:
        sortino = None

    sorted_trades = sorted(trades, key=lambda t: t["exit_time"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_streak = streak = 0
    for t in sorted_trades:
        equity += t["pnl_usd"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t["pnl_usd"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    holding_hours = [
        (datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00")) - datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))).total_seconds() / 3600.0
        for t in trades
    ]

    return {
        "num_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "roi_pct_of_spend": round(total_pnl / SPEND_PER_TRADE * 100, 1),
        "avg_trade_pct": round(mean_r * 100, 2),
        "profit_factor": profit_factor,
        "max_drawdown_usd": round(max_dd, 2),
        "sharpe": sharpe,
        "sortino": sortino,
        "avg_holding_hours": round(sum(holding_hours) / len(holding_hours), 1),
        "max_losing_streak": max_streak,
    }


async def run_entry_signal_ab_test(contract_codes=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE ONLY - never touches live trading, places no order.
    Fetches real Alpaca history ONCE per symbol (with real timestamps),
    then replays every row of ENTRY_VARIANTS against the identical real
    bars via _replay_symbol_momentum_variant() - the exit rule is held
    fixed across all four so the comparison isolates entry-signal
    quality, not a mix of entry+exit changes. Returns per-variant totals
    (a real, multi-metric summary, not just total P&L) plus a per-symbol
    P&L breakdown across all four variants."""
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
                bars, err = await _fetch_bars_with_times(session, ticker, days)
                if bars is None:
                    skipped.append({"product_id": ticker, "reason": err})
                    return
                per_symbol[ticker] = bars

        await asyncio.gather(*[_one(t) for t in unique_tickers])

    variant_trades = {name: [] for name in ENTRY_VARIANTS}
    per_symbol_pnl = {name: {} for name in ENTRY_VARIANTS}

    for ticker, bars in per_symbol.items():
        for name, filters in ENTRY_VARIANTS.items():
            trades = _replay_symbol_momentum_variant(bars, filters)
            variant_trades[name].extend(trades)
            per_symbol_pnl[name][ticker] = round(sum(t["pnl_usd"] for t in trades), 2)

    variant_summaries = {name: _summarize_trades(trades) for name, trades in variant_trades.items()}

    symbol_breakdown = []
    for ticker in per_symbol:
        row = {"product_id": ticker}
        for name in ENTRY_VARIANTS:
            row[name] = per_symbol_pnl[name].get(ticker, 0.0)
        symbol_breakdown.append(row)

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND_PER_TRADE,
        "symbols_tested": len(unique_tickers),
        "symbols_with_results": len(per_symbol),
        "skipped": skipped,
        "variant_names": list(ENTRY_VARIANTS.keys()),
        "variants": variant_summaries,
        "symbol_breakdown": symbol_breakdown,
    }
