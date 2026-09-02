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


# ============================================================================
# REVERSE MOMENTUM - the honest mirror-image of _replay_symbol_momentum(),
# built at the account owner's explicit request after seeing a real losing
# window (2026-05-31 -> 2026-06-30, momentum -$64.93) and asking to test the
# "reverse" of momentum on real data rather than fabricate/swap the already-
# real numbers on the dashboard (which was declined - that would mean
# showing a result that never actually happened).
#
# Momentum enters once a trend is ALREADY confirmed (RSI already above 55
# AND price already above its own 20-bar average) - it buys strength that's
# established and rides it. Since this real account can't short (see the
# module docstring - every real short attempt fails live with "account is
# not allowed to short"), the honest "reverse" isn't "bet the trend
# continues down" (unexecutable on this account) - it's "buy the moment a
# reversal is just STARTING instead of waiting for it to be confirmed":
# enter the instant RSI crosses UP through REVERSE_MOMENTUM_RSI_ENTRY from
# below (was weak, is turning right now) while price is trading above its
# own SMA20 (real, if early, confirmation the reversal has genuine legs,
# not just one noisy tick) - the mirror of Momentum's own two conditions,
# caught at the RSI crossover moment instead of only after both signals
# have been true for a while.
#
# The EXIT is left byte-for-byte identical to Momentum's own (same trailing
# stop off peak, same max-hold backstop) - same discipline the existing
# entry-signal A/B/C/D test already established: isolate entry-signal
# quality alone, don't let a different exit muddy the real comparison.
REVERSE_MOMENTUM_RSI_ENTRY = 45.0  # crossing UP through this from below = weakness fading, not strength already confirmed (Momentum's own 55 mirrored around neutral)
REVERSE_MOMENTUM_SMA_PERIOD = 20   # same period as Momentum's own SMA, for a fair, directly comparable real signal


def _replay_symbol_reverse_momentum(closes: list, spend_per_trade: float = SPEND_PER_TRADE) -> list:
    """Reverse-Momentum entry/trailing-exit replay: the mirror image of
    _replay_symbol_momentum()'s entry gate - instead of confirming an
    already-established real uptrend (RSI already high, price already above
    its SMA for a while), this catches the real moment RSI crosses UP
    through REVERSE_MOMENTUM_RSI_ENTRY (was weak, is turning right now)
    while price is trading above its own REVERSE_MOMENTUM_SMA_PERIOD-bar
    average - real, executable, long-only. Exit is intentionally identical
    to Momentum's own trailing-stop/backstop logic, so this isolates entry
    timing alone."""
    trades = []
    position = None  # {"entry": float, "peak": float, "entry_idx": int}
    prev_rsi = None

    for i in range(50, len(closes)):
        window = closes[max(0, i - 50):i + 1]
        rsi = _compute_rsi(window)
        sma = _compute_sma(closes[:i + 1], REVERSE_MOMENTUM_SMA_PERIOD)
        price = closes[i]
        if rsi is None or sma is None:
            prev_rsi = rsi
            continue

        if position is None:
            if (prev_rsi is not None and prev_rsi < REVERSE_MOMENTUM_RSI_ENTRY
                    and rsi >= REVERSE_MOMENTUM_RSI_ENTRY and price > sma):
                position = {"entry": price, "peak": price, "entry_idx": i}
            prev_rsi = rsi
            continue

        position["peak"] = max(position["peak"], price)
        trailing_stop = position["peak"] * (1 - MOMENTUM_TRAIL_PCT)
        age_bars = i - position["entry_idx"]

        if price <= trailing_stop or age_bars >= MOMENTUM_MAX_HOLD_BARS:
            pnl_pct = (price - position["entry"]) / position["entry"]
            trades.append({"pnl_usd": spend_per_trade * pnl_pct, "pnl_pct": pnl_pct})
            position = None

        prev_rsi = rsi

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

            mr_total, mom_total, rev_total = 0.0, 0.0, 0.0
            mr_trades_total, mom_trades_total, rev_trades_total = 0, 0, 0
            mr_wins_total, mom_wins_total, rev_wins_total = 0, 0, 0
            for ticker, closes in per_symbol.items():
                mr_trades = _replay_symbol(closes, symbol=ticker)
                mom_trades = _replay_symbol_momentum(closes)
                rev_trades = _replay_symbol_reverse_momentum(closes)
                mr_total += sum(t["pnl_usd"] for t in mr_trades)
                mom_total += sum(t["pnl_usd"] for t in mom_trades)
                rev_total += sum(t["pnl_usd"] for t in rev_trades)
                mr_trades_total += len(mr_trades)
                mom_trades_total += len(mom_trades)
                rev_trades_total += len(rev_trades)
                mr_wins_total += len([t for t in mr_trades if t["pnl_usd"] > 0])
                mom_wins_total += len([t for t in mom_trades if t["pnl_usd"] > 0])
                rev_wins_total += len([t for t in rev_trades if t["pnl_usd"] > 0])

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
                "reverse_momentum": {
                    "num_trades": rev_trades_total,
                    "win_rate": round(rev_wins_total / rev_trades_total * 100, 1) if rev_trades_total else 0.0,
                    "total_pnl": round(rev_total, 2),
                },
            })

    # Three-way "which real strategy actually won this window" - a genuine
    # tie for best (identical total_pnl, e.g. all three at $0 with zero
    # trades) counts toward none of them rather than crediting an arbitrary
    # one.
    mr_wins_windows = mom_wins_windows = rev_wins_windows = 0
    for wnd in windows:
        pnls = {
            "mean_reversion": wnd["mean_reversion"]["total_pnl"],
            "momentum": wnd["momentum"]["total_pnl"],
            "reverse_momentum": wnd["reverse_momentum"]["total_pnl"],
        }
        best_pnl = max(pnls.values())
        leaders = [name for name, pnl in pnls.items() if pnl == best_pnl]
        if len(leaders) > 1:
            continue
        if leaders[0] == "mean_reversion":
            mr_wins_windows += 1
        elif leaders[0] == "momentum":
            mom_wins_windows += 1
        else:
            rev_wins_windows += 1
    mr_total_all = round(sum(wnd["mean_reversion"]["total_pnl"] for wnd in windows), 2)
    mom_total_all = round(sum(wnd["momentum"]["total_pnl"] for wnd in windows), 2)
    rev_total_all = round(sum(wnd["reverse_momentum"]["total_pnl"] for wnd in windows), 2)

    return {
        "window_days": window_days,
        "num_windows": num_windows,
        "spend_per_trade": SPEND_PER_TRADE,
        "windows": windows,
        "summary": {
            "mean_reversion_windows_won": mr_wins_windows,
            "momentum_windows_won": mom_wins_windows,
            "reverse_momentum_windows_won": rev_wins_windows,
            "mean_reversion_total_pnl": mr_total_all,
            "momentum_total_pnl": mom_total_all,
            "reverse_momentum_total_pnl": rev_total_all,
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


# ============================================================================
# NARROW-RANGE BREAKOUT CONTINUATION - the Alpaca-side counterpart to
# crypto_selection_backtest.py's run_narrow_range_breakout_backtest(). Per
# the account owner's own real trading claim: "the best opportunity come
# from a narrow state... if you open above a narrow state... 87% chance
# there are more upside to come... if you open below a narrow state...
# 87% chance to follow through to the downside." That 87% figure is their
# own stated number, not something already verified against this
# account's real data - this replays real historical Alpaca bars to see
# what the actual hit rate comes out to.
#
# Stocks get a MORE LITERAL version of this than crypto could: crypto
# trades 24/7 with no discrete session open, so the crypto-side backtest
# uses a rolling hourly window as its stand-in for "narrow state." Stocks
# genuinely have a real daily open - this groups real bars into real
# trading days, measures each real day's own range, and checks the very
# next real day's actual FIRST bar against the prior (narrow) day's own
# high/low - exactly the account owner's own literal framing.
# ============================================================================

NARROW_DAY_HISTORY_DAYS = 20
NARROW_DAY_PERCENTILE = 0.25
NARROW_BREAKOUT_FOLLOW_BARS = 26  # ~one real trading day of 15-min bars (6.5h * 4/h ≈ 26)


async def _fetch_bars_with_ohlc_and_times(session, symbol: str, days: int):
    """Real historical 15-min OHLC bars + real UTC timestamps - needed
    for real day-range detection (needs high/low) and identifying each
    real trading day's own first bar (needs timestamps), neither of
    which _fetch_bars()/_fetch_bars_with_times() (close-only) carry.
    Returns (bars: list[{"t","o","h","l","c"}], None) or (None, reason)."""
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
            return [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars], None
    except asyncio.TimeoutError:
        return None, "Alpaca API timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


# _group_bars_by_day is now shared with prop_bot.py's own live opening-bar
# trading loop, via opening_bar_signals.py (see that module's docstring for
# why this had to be a relocation, not a copy - a circular import otherwise).
from opening_bar_signals import _group_bars_by_day


def _day_range_pct(day_bars: list):
    """Real trading day range (using every real bar's own high/low that
    day) as a fraction of the day's own real midpoint price. Returns
    (range_pct, day_high, day_low), or None if the day's own real
    midpoint is zero (never happens with real data, defensive only)."""
    day_high = max(b["h"] for b in day_bars)
    day_low = min(b["l"] for b in day_bars)
    mid = (day_high + day_low) / 2
    if not mid:
        return None
    return (day_high - day_low) / mid, day_high, day_low


def _replay_narrow_day_breakout(bars: list, history_days: int = NARROW_DAY_HISTORY_DAYS,
                                 percentile: float = NARROW_DAY_PERCENTILE,
                                 follow_bars: int = NARROW_BREAKOUT_FOLLOW_BARS) -> list:
    """Groups real bars into real trading days, then for each real day
    whose own range is NARROW (bottom `percentile` of its own real range
    history over the preceding `history_days` real trading days, the
    same percentile-relative "tight relative to its OWN recent history"
    approach crypto_selection_backtest.py's version uses - a stock's
    normal daily range varies as much across symbols as a coin's does,
    so this is never one fixed absolute threshold), checks whether the
    very next real trading day's real FIRST bar opens above that narrow
    day's own high (bullish breakout) or below its low (bearish) -
    matching the account owner's own literal framing exactly ("when your
    stock opens in the morning, the first bar..."). A day whose real
    open falls INSIDE the narrow range's own high/low is not a real
    breakout and is skipped, not scored as a miss.

    Checks real follow-through `follow_bars` (26 default, ~one real
    trading day of 15-min bars) real bars forward from that real opening
    bar - flattening every remaining real bar across day boundaries so
    "26 bars forward" means 26 real bars of real trading time, not
    artificially truncated at each day's own close.

    Returns a list of real event dicts: {date, direction ("up"/"down"),
    open_price, forward_price, followed_through (bool),
    forward_return_pct}."""
    days = _group_bars_by_day(bars)
    ranges = [_day_range_pct(day_bars) for _date, day_bars in days]
    events = []
    for i in range(history_days, len(days) - 1):
        current = ranges[i]
        if current is None:
            continue
        current_range_pct, day_high, day_low = current

        hist = [r[0] for r in ranges[i - history_days:i] if r is not None]
        if len(hist) < 10:
            continue
        hist_sorted = sorted(hist)
        idx = min(int(len(hist_sorted) * percentile), len(hist_sorted) - 1)
        threshold = hist_sorted[idx]
        if current_range_pct > threshold:
            continue  # not a genuinely narrow real day

        next_date, next_day_bars = days[i + 1]
        if not next_day_bars:
            continue
        open_price = next_day_bars[0]["o"]
        if open_price > day_high:
            direction = "up"
        elif open_price < day_low:
            direction = "down"
        else:
            continue  # opened INSIDE the narrow range - not a real breakout, don't score it either way

        remaining = [b for _d, db in days[i + 1:] for b in db]
        if len(remaining) <= follow_bars:
            continue  # not enough real forward history to check follow-through
        forward_price = remaining[follow_bars]["c"]
        followed_through = (forward_price > open_price) if direction == "up" else (forward_price < open_price)
        events.append({
            "date": next_date, "direction": direction, "open_price": open_price,
            "forward_price": forward_price, "followed_through": followed_through,
            "forward_return_pct": (forward_price - open_price) / open_price,
        })
    return events


def _summarize_narrow_breakout_events(events: list):
    """Real hit-rate/avg-return summary for a list of breakout events -
    None (not a fabricated 0%) when there are no real events to
    summarize."""
    if not events:
        return None
    hits = sum(1 for e in events if e["followed_through"])
    return {
        "count": len(events),
        "hit_rate": round(hits / len(events), 4),
        "avg_forward_return_pct": round(sum(e["forward_return_pct"] for e in events) / len(events), 4),
    }


async def run_narrow_range_breakout_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE, real historical Alpaca data, per-symbol AND
    aggregate. Never places a real order, never touches live trading -
    purely diagnostic, same posture as every other backtest tool in this
    file. symbols=None (default) tests every real symbol prop_bot.py
    trades (FUTURES.keys())."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        return symbol, _replay_narrow_day_breakout(bars)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    per_symbol = []
    skipped = []
    all_events = []
    for symbol, events in results:
        if events is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        up_events = [e for e in events if e["direction"] == "up"]
        down_events = [e for e in events if e["direction"] == "down"]
        per_symbol.append({
            "product_id": symbol, "total_events": len(events),
            "up_breakouts": _summarize_narrow_breakout_events(up_events),
            "down_breakouts": _summarize_narrow_breakout_events(down_events),
        })
        all_events.extend(events)

    up_all = [e for e in all_events if e["direction"] == "up"]
    down_all = [e for e in all_events if e["direction"] == "down"]
    combined_hit_rate = None
    if all_events:
        combined_hit_rate = round(sum(1 for e in all_events if e["followed_through"]) / len(all_events), 4)

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol,
        "overall": {
            "total_events": len(all_events),
            "up_breakouts": _summarize_narrow_breakout_events(up_all),
            "down_breakouts": _summarize_narrow_breakout_events(down_all),
            "combined_hit_rate": combined_hit_rate,
            "coin_flip_baseline": 0.5,
        },
        "params": {
            "narrow_day_history_days": NARROW_DAY_HISTORY_DAYS,
            "narrow_day_percentile": NARROW_DAY_PERCENTILE,
            "follow_through_bars": NARROW_BREAKOUT_FOLLOW_BARS,
        },
    }


# ============================================================================
# OPENING-BAR ELEPHANT/TAIL BREAKOUT - the Alpaca-side counterpart to
# crypto_selection_backtest.py's run_opening_bar_breakout_backtest(). Per
# the account owner's own fully-specified real trading system: wait for
# the real first 2-minute bar of the trading day; if it's a real
# "Elephant Bar" (oversized green candle) or a real "bottoming Tail" bar
# (long lower-wick rejection - their own answer to what a "Tails" bar
# means), mark its high + one penny; the instant the SECOND bar's real
# price reaches that level (never waiting for bar 2 to close), enter
# $25,000; the stop sits at bar 1's own low; once a real second "push"
# confirms, exit.
#
# Stocks get a MORE LITERAL version than crypto's invented UTC anchor:
# Alpaca's real bars API directly supports a native 2-minute timeframe
# and real, genuine trading-day sessions (9:30am ET), so no synthetic
# aggregation or invented session boundary is needed here - this is the
# account owner's own setup, running on the real thing.
#
# SHADOW-MODE, real historical data, never places a real order. Same
# "evidence before trusting a claimed number" discipline as every other
# backtest tool in this file - the account owner's own claimed 80%+
# follow-through rate is tested here, not assumed.
# ============================================================================

# Elephant/tail detection and the single-entry replay are now shared with
# prop_bot.py's own live opening-bar trading loop, via opening_bar_signals.py
# (a circular import otherwise - prop_bot.py cannot import from this file,
# since this file already imports FUTURES/get_headers from prop_bot.py).
from opening_bar_signals import (
    ELEPHANT_BAR_MIN_SIZE_MULTIPLE, ELEPHANT_BAR_LOOKBACK, TAIL_BAR_MIN_WICK_FRACTION,
    OPENING_BAR_ENTRY_BUFFER_USD, OPENING_BAR_SPEND_USD, PUSH_MIN_PULLBACK_PCT,
    _is_elephant_bar, _is_bottoming_tail_bar, _replay_opening_bar_breakout,
)


async def _fetch_bars_2min_with_ohlc_and_times(session, symbol: str, days: int):
    """Real historical 2-minute OHLC bars + real UTC timestamps -
    Alpaca's real bars API accepts an arbitrary N-minute timeframe
    directly (unlike Coinbase's fixed-granularity public candles), so
    this needs no synthetic aggregation the way the crypto-side version
    does. Returns (bars: list[{"t","o","h","l","c"}], None) or
    (None, reason)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=2Min&start={start}&limit=10000&feed=iex"
    try:
        async with session.get(url, headers=get_headers(), timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                body = (await r.text())[:200]
                return None, f"HTTP {r.status}: {body}"
            data = await r.json()
            bars = data.get("bars", [])
            if len(bars) < 60:
                return None, f"only {len(bars)} bars (need 60+)"
            return [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars], None
    except asyncio.TimeoutError:
        return None, "Alpaca API timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


async def run_opening_bar_breakout_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE, real historical Alpaca 2-minute bars, per-symbol AND
    aggregate. Never places a real order. symbols=None (default) tests
    every real symbol prop_bot.py trades."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        days_grouped = _group_bars_by_day(bars)
        trades = []
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return symbol, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    per_symbol = []
    skipped = []
    all_trades = []
    for symbol, trades in results:
        if trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_symbol.append({
                "product_id": symbol, "num_trades": len(trades),
                "win_rate": round(wins / len(trades), 4),
                "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
            })
        all_trades.extend(trades)

    overall = None
    if all_trades:
        wins = sum(1 for t in all_trades if t["pnl_usd"] > 0)
        overall = {
            "num_trades": len(all_trades),
            "win_rate": round(wins / len(all_trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in all_trades), 2),
            "stop_count": sum(1 for t in all_trades if t["exit_reason"] == "STOP"),
            "push_exit_count": sum(1 for t in all_trades if t["exit_reason"].startswith("PUSH")),
        }

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "days": days,
        },
    }


# ============================================================================
# OPENING-BAR MULTI-ENTRY CONTINUATION - per the account owner's own real
# reference screenshots (Oliver Velez's "Why I Trade Only the First 20
# Minutes of the Market" video, plus their own annotated SWKS chart showing
# a real staircase: big opening bar -> pullback to a level -> breakout
# (entry) -> another pullback -> another breakout (a second entry)). The
# original _replay_opening_bar_breakout() above takes exactly ONE real
# trade per day - it already exits on the FIRST confirmed "push" after
# entry (a real pullback of at least PUSH_MIN_PULLBACK_PCT off a new peak,
# followed by a new high above it), then simply stops watching for the
# rest of the session. The real gap the account owner's own screenshot
# showed: their actual setup keeps trading that SAME established trend
# through MULTIPLE such pullback-and-continuation legs in one session,
# not just the first one.
#
# SHADOW-MODE, real historical data, never places a real order - same
# posture as every other backtest tool in this file. This is additive:
# _replay_opening_bar_breakout()/run_opening_bar_breakout_backtest() above
# are completely unchanged, still the real one-entry-per-day baseline
# every comparison in this file measures against.
# ============================================================================

# The multi-leg continuation replay is now shared with prop_bot.py's own
# live opening-bar trading loop too - see opening_bar_signals.py.
from opening_bar_signals import (
    OPENING_BAR_MAX_ENTRIES_PER_DAY, _replay_one_opening_bar_leg,
    _replay_opening_bar_breakout_multi_entry,
)


async def run_opening_bar_multi_entry_comparison(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6, max_entries_per_day: int = OPENING_BAR_MAX_ENTRIES_PER_DAY) -> dict:
    """Real, direct comparison - today's real one-entry-per-day baseline
    (_replay_opening_bar_breakout, completely unchanged) vs. the new
    multi-entry continuation version, replayed on the IDENTICAL real
    historical Alpaca 2-minute bars (fetched once per symbol, not twice)
    so the two are directly, fairly comparable. SHADOW-MODE, never places
    a real order. symbols=None (default) tests every real symbol
    prop_bot.py trades."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None, None
        days_grouped = _group_bars_by_day(bars)
        single_trades, multi_trades = [], []
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            single_trade = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if single_trade is not None:
                single_trades.append(single_trade)
            multi_trades.extend(_replay_opening_bar_breakout_multi_entry(session_bars, preceding_bars, max_entries_per_day=max_entries_per_day))
        return symbol, single_trades, multi_trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades), "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
            "stop_count": sum(1 for t in trades if t["exit_reason"] == "STOP"),
        }

    per_symbol = []
    skipped = []
    all_single, all_multi = [], []
    for symbol, single_trades, multi_trades in results:
        if single_trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        per_symbol.append({
            "product_id": symbol,
            "single_entry": _summarize(single_trades),
            "multi_entry": _summarize(multi_trades),
        })
        all_single.extend(single_trades)
        all_multi.extend(multi_trades)

    single_overall = _summarize(all_single)
    multi_overall = _summarize(all_multi)
    better = None
    if single_overall and multi_overall:
        better = "multi_entry" if multi_overall["total_pnl"] > single_overall["total_pnl"] else "single_entry"

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol,
        "single_entry_overall": single_overall, "multi_entry_overall": multi_overall,
        "better": better,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD, "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "max_entries_per_day": max_entries_per_day, "days": days,
        },
    }


def _rolling_range_pct(highs: list, lows: list, end_idx: int, lookback: int):
    """Real channel width - the real high/low range over the `lookback`
    real bars ending just before end_idx, as a fraction of that window's
    own real midpoint price. Ported from crypto_selection_backtest.py's
    identical function (this file never cross-imports from that one, per
    this codebase's own established convention) - returns (range_pct,
    window_high, window_low), or None if end_idx doesn't have `lookback`
    real bars of history behind it yet (or the window's own midpoint is
    zero)."""
    if end_idx < lookback:
        return None
    window_high = max(highs[end_idx - lookback:end_idx])
    window_low = min(lows[end_idx - lookback:end_idx])
    mid = (window_high + window_low) / 2
    if not mid:
        return None
    return (window_high - window_low) / mid, window_high, window_low


def _is_narrow_range_at(i: int, highs: list, lows: list, lookback: int, history: int, percentile: float):
    """Real, percentile-relative "narrow state" detection - the direct
    index-based counterpart to crypto_selection_backtest.py's identical
    function, ported here (not imported) for real 2-minute bar arrays.
    "Narrow" = the real range over the last `lookback` real bars is in
    the bottom `percentile` of that same rolling-range measure's own
    real distribution over the preceding `history` bars. Returns
    (is_narrow, window_high, window_low). Returns (False, None, None)
    when there isn't yet enough real history to judge - never guesses
    without at least 10 real historical range samples behind it."""
    current = _rolling_range_pct(highs, lows, i, lookback)
    if current is None or i < lookback + history:
        return False, None, None
    current_range_pct, window_high, window_low = current

    step = max(1, lookback // 2)
    samples = []
    j = i - lookback
    stop = i - lookback - history
    while j > stop:
        r = _rolling_range_pct(highs, lows, j, lookback)
        if r is not None:
            samples.append(r[0])
        j -= step
    if len(samples) < 10:
        return False, None, None
    samples.sort()
    idx = min(int(len(samples) * percentile), len(samples) - 1)
    threshold = samples[idx]
    return current_range_pct <= threshold, window_high, window_low


SMA_STATE_SHORT_PERIOD = 20
SMA_STATE_LONG_PERIOD = 200
SMA_STATE_NARROW_PCT = 0.005
OPENING_BAR_PCTL_LOOKBACK_BARS = 200
OPENING_BAR_PCTL_HISTORY_BARS = 2000
WIDE_STATE_SPEND_USD = OPENING_BAR_SPEND_USD
WIDE_STATE_STOP_PCT = 0.02
WIDE_STATE_MAX_HOLD_BARS = 200


def _sma_at(closes: list, i: int, period: int):
    """Real simple moving average of `period` closes ending AT index i
    (inclusive) - None if index i doesn't have `period` real closes of
    history behind it yet."""
    if i - period + 1 < 0 or i >= len(closes):
        return None
    return sum(closes[i - period + 1:i + 1]) / period


def _sma_state_at(closes: list, i: int, short_period: int = SMA_STATE_SHORT_PERIOD,
                   long_period: int = SMA_STATE_LONG_PERIOD, narrow_pct: float = SMA_STATE_NARROW_PCT):
    """Real 20/200 SMA-convergence "state" detection - the direct Alpaca-
    side counterpart to crypto_selection_backtest.py's identical
    function. Per the account owner's own real trading concept,
    transcribed directly: "moving averages far apart is a wide state...
    a tight narrow state [is] the 20 a little below the 200... know the
    stock's state first and you've got 85% of the game figured out." A
    GENUINELY DIFFERENT real definition of "narrow" than this file's
    existing percentile-range method above - that one looks at how
    tight the PRICE RANGE itself has been; this one looks at how close
    two moving averages of different real speeds currently sit to each
    other. Both are tested side by side in
    run_opening_bar_narrow_state_comparison below.

    `narrow_pct` (0.5% default) is an INVENTED threshold - the account
    owner described "close together" vs. "separated" but gave no real
    number, so this session picked one and is saying so plainly.

    Returns "narrow", "wide_up" (the real 20 SMA has separated ABOVE the
    real 200 SMA by more than narrow_pct), "wide_down" (separated
    BELOW), or None when there isn't yet enough real closes for both
    real SMAs."""
    sma_short = _sma_at(closes, i, short_period)
    sma_long = _sma_at(closes, i, long_period)
    if sma_short is None or sma_long is None or sma_long == 0:
        return None
    gap_pct = (sma_short - sma_long) / sma_long
    if abs(gap_pct) <= narrow_pct:
        return "narrow"
    return "wide_up" if gap_pct > 0 else "wide_down"


async def run_opening_bar_narrow_state_comparison(symbols=None, days: int = BACKTEST_DAYS,
                                                    max_concurrent: int = 6) -> dict:
    """SHADOW-MODE. The direct Alpaca-side counterpart to
    crypto_selection_backtest.py's identical function - compares three
    real narrow-state definitions against the IDENTICAL real Elephant/
    Tail opening-bar trades (_replay_opening_bar_breakout):
      - "baseline": no narrow-state gate - every real qualifying setup,
        exactly run_opening_bar_breakout_backtest's own shipped default.
      - "percentile": gated on _is_narrow_range_at (real range in the
        bottom 25th percentile of its own recent history).
      - "sma": gated on the account owner's own real 20/200 SMA-
        convergence method (_sma_state_at).
    A day where a given method doesn't yet have enough real history to
    have an opinion is excluded from THAT method's bucket only. Never
    places a real order."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        days_grouped = _group_bars_by_day(bars)
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]

        cum = 0
        day_start_idx = []
        for _date, dbars in days_grouped:
            day_start_idx.append(cum)
            cum += len(dbars)

        buckets = {"baseline": [], "percentile": [], "sma": []}
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if trade is None:
                continue
            buckets["baseline"].append(trade)

            idx_before = day_start_idx[i] - 1
            if idx_before < 0:
                continue
            is_narrow_pctl, _, _ = _is_narrow_range_at(
                idx_before, highs, lows,
                lookback=OPENING_BAR_PCTL_LOOKBACK_BARS, history=OPENING_BAR_PCTL_HISTORY_BARS,
                percentile=NARROW_DAY_PERCENTILE,
            )
            if is_narrow_pctl:
                buckets["percentile"].append(trade)

            sma_state = _sma_state_at(closes, idx_before)
            if sma_state == "narrow":
                buckets["sma"].append(trade)

        return symbol, buckets

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades),
            "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
        }

    per_symbol = []
    skipped = []
    overall_buckets = {"baseline": [], "percentile": [], "sma": []}
    for symbol, buckets in results:
        if buckets is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if buckets["baseline"]:
            per_symbol.append({
                "product_id": symbol,
                "baseline": _summarize(buckets["baseline"]),
                "percentile": _summarize(buckets["percentile"]),
                "sma": _summarize(buckets["sma"]),
            })
        for k in overall_buckets:
            overall_buckets[k].extend(buckets[k])

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol,
        "overall": {k: _summarize(v) for k, v in overall_buckets.items()},
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD, "days": days,
            "sma_short_period": SMA_STATE_SHORT_PERIOD, "sma_long_period": SMA_STATE_LONG_PERIOD,
            "sma_narrow_pct": SMA_STATE_NARROW_PCT,
            "percentile_lookback_bars": OPENING_BAR_PCTL_LOOKBACK_BARS,
            "percentile_history_bars": OPENING_BAR_PCTL_HISTORY_BARS,
        },
    }


def _replay_wide_state_contrarian(closes: list, highs: list, lows: list, entry_idx: int, direction: str,
                                   spend: float = WIDE_STATE_SPEND_USD, max_hold_bars: int = WIDE_STATE_MAX_HOLD_BARS,
                                   stop_pct: float = WIDE_STATE_STOP_PCT,
                                   short_period: int = SMA_STATE_SHORT_PERIOD, long_period: int = SMA_STATE_LONG_PERIOD,
                                   narrow_pct: float = SMA_STATE_NARROW_PCT):
    """Real contrarian mean-reversion trade - the direct Alpaca-side
    counterpart to crypto_selection_backtest.py's identical function.
    `direction="long"` (a real wide_down state, betting on reversion UP)
    is executable by prop_bot.py today - it's long-only, but a long
    entry here is exactly what it can already place. `direction="short"`
    (a real wide_up state, betting on reversion DOWN) is diagnostic
    only - prop_bot.py's real shorting is a documented, confirmed
    account-level restriction ("account is not allowed to short"), not a
    bug in this backtest.

    Real exit conditions, checked bar by bar from entry_idx+1: STOP
    (price moves `stop_pct` - 2% default, an INVENTED risk bound -
    further against the bet), REVERSION (the real 20/200 state returns
    to "narrow" - the real win condition), or MAX_HOLD (a real 200-bar
    time backstop, ~6.7 real trading hours on 2-minute bars).

    Returns a real trade dict, or None if entry_idx has no real close to
    enter at."""
    if entry_idx >= len(closes):
        return None
    entry_price = closes[entry_idx]
    stop_price = entry_price * (1 - stop_pct) if direction == "long" else entry_price * (1 + stop_pct)
    qty = spend / entry_price

    def _result(exit_price, exit_reason, exit_idx):
        pnl_usd = qty * (exit_price - entry_price) if direction == "long" else qty * (entry_price - exit_price)
        return {
            "direction": direction, "entry_price": entry_price, "entry_idx": entry_idx,
            "stop_price": round(stop_price, 8), "exit_price": exit_price,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_usd / spend, 4),
        }

    last_idx = min(entry_idx + max_hold_bars, len(closes) - 1)
    for i in range(entry_idx + 1, last_idx + 1):
        if direction == "long":
            if lows[i] <= stop_price:
                return _result(stop_price, "STOP", i)
        else:
            if highs[i] >= stop_price:
                return _result(stop_price, "STOP", i)
        state = _sma_state_at(closes, i, short_period, long_period, narrow_pct)
        if state == "narrow":
            return _result(closes[i], "REVERSION", i)

    return _result(closes[last_idx], "MAX_HOLD", last_idx)


async def run_wide_state_contrarian_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE, real historical 2-minute Alpaca bars - the direct
    Alpaca-side counterpart to crypto_selection_backtest.py's identical
    function. The account owner's own separate "wide state -> contrarian
    reversion" idea (see _replay_wide_state_contrarian's own docstring
    for the real, honest long-vs-short scope note). Never places a real
    order. symbols=None (default) tests every real symbol prop_bot.py
    trades."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        closes = [b["c"] for b in bars]
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]

        trades = []
        i = SMA_STATE_LONG_PERIOD
        prev_state = None
        while i < len(closes):
            state = _sma_state_at(closes, i)
            if state in ("wide_up", "wide_down") and prev_state != state:
                direction = "long" if state == "wide_down" else "short"
                trade = _replay_wide_state_contrarian(closes, highs, lows, i, direction)
                if trade is not None:
                    trades.append(trade)
                    i = trade["exit_idx"] + 1
                    prev_state = None
                    continue
            prev_state = state
            i += 1
        return symbol, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades),
            "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
            "reversion_count": sum(1 for t in trades if t["exit_reason"] == "REVERSION"),
            "stop_count": sum(1 for t in trades if t["exit_reason"] == "STOP"),
        }

    per_symbol = []
    skipped = []
    all_trades = []
    for symbol, trades in results:
        if trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if trades:
            long_trades = [t for t in trades if t["direction"] == "long"]
            short_trades = [t for t in trades if t["direction"] == "short"]
            per_symbol.append({
                "product_id": symbol,
                "long_wide_down": _summarize(long_trades),
                "short_wide_up_diagnostic_only": _summarize(short_trades),
            })
        all_trades.extend(trades)

    all_long = [t for t in all_trades if t["direction"] == "long"]
    all_short = [t for t in all_trades if t["direction"] == "short"]

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol,
        "overall": {
            "long_wide_down": _summarize(all_long),
            "short_wide_up_diagnostic_only": _summarize(all_short),
        },
        "params": {
            "spend_usd": WIDE_STATE_SPEND_USD, "stop_pct": WIDE_STATE_STOP_PCT,
            "max_hold_bars": WIDE_STATE_MAX_HOLD_BARS,
            "sma_short_period": SMA_STATE_SHORT_PERIOD, "sma_long_period": SMA_STATE_LONG_PERIOD,
            "sma_narrow_pct": SMA_STATE_NARROW_PCT, "days": days,
        },
    }


def _is_red_elephant_bar(bar: dict, preceding_bars: list, min_size_multiple: float = ELEPHANT_BAR_MIN_SIZE_MULTIPLE,
                          lookback: int = ELEPHANT_BAR_LOOKBACK) -> bool:
    """The real bearish mirror of _is_elephant_bar - the direct Alpaca-
    side counterpart to crypto_selection_backtest.py's identical
    function. A real RED "power bar" (close < open) whose own range is
    at least `min_size_multiple` the average range of the last real RED
    bars among `preceding_bars` (needs at least 3 real red preceding
    bars to judge against)."""
    if bar["c"] >= bar["o"]:
        return False
    reds = [b for b in preceding_bars[-lookback:] if b["c"] < b["o"]]
    if len(reds) < 3:
        return False
    avg_range = sum(b["h"] - b["l"] for b in reds) / len(reds)
    if avg_range <= 0:
        return False
    return (bar["h"] - bar["l"]) >= min_size_multiple * avg_range


def _is_topping_tail_bar(bar: dict, min_wick_fraction: float = TAIL_BAR_MIN_WICK_FRACTION) -> bool:
    """The real bearish mirror of _is_bottoming_tail_bar - a real long
    UPPER wick rejection candle. Returns False on a real zero-range bar."""
    total_range = bar["h"] - bar["l"]
    if total_range <= 0:
        return False
    upper_wick = bar["h"] - max(bar["o"], bar["c"])
    return (upper_wick / total_range) >= min_wick_fraction


def _replay_opening_bar_breakout_short(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """The real bearish mirror of _replay_opening_bar_breakout - the
    direct Alpaca-side counterpart to crypto_selection_backtest.py's
    identical function. A real RED Elephant Bar or topping Tail bar as
    bar 1, a real SHORT entry the instant bar 2's real price crosses
    bar 1's low MINUS $0.01, a real stop at bar 1's own HIGH, a real
    exit on a second downside "push" or session end.

    DIAGNOSTIC ONLY - prop_bot.py's real shorting is a documented,
    confirmed account-level restriction ("account is not allowed to
    short"), not a bug in this backtest. The real entry trigger fires
    the instant ANY LATER real bar's own low crosses bar 1's low minus
    $0.01, scanning forward as far as needed - abandoned if price rises
    to bar 1's own high first. Returns a real trade dict, or None if no
    real trade fired today."""
    if len(session_bars) < 3:
        return None
    bar1 = session_bars[0]
    is_red_elephant = _is_red_elephant_bar(bar1, preceding_bars)
    is_topping_tail = _is_topping_tail_bar(bar1)
    if not (is_red_elephant or is_topping_tail):
        return None

    trigger_price = bar1["l"] - OPENING_BAR_ENTRY_BUFFER_USD
    stop_price = bar1["h"]
    qualifies_as = "red_elephant" if is_red_elephant else "topping_tail"

    entry_idx = None
    for i in range(1, len(session_bars)):
        b = session_bars[i]
        if b["h"] >= stop_price:
            return None
        if b["l"] <= trigger_price:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    entry_price = trigger_price
    qty = spend / entry_price

    def _result(exit_price, exit_reason, exit_index):
        pnl_usd = qty * (entry_price - exit_price)
        return {
            "qualifies_as": qualifies_as, "entry_price": entry_price, "entry_index": entry_idx,
            "stop_price": stop_price, "exit_price": exit_price, "exit_reason": exit_reason,
            "exit_index": exit_index, "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round((entry_price - exit_price) / entry_price, 4),
        }

    trough = entry_price
    pushes = 1
    in_bounce = False
    for i in range(entry_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["h"] >= stop_price:
            return _result(stop_price, "STOP", i)
        if b["l"] < trough:
            if in_bounce:
                pushes += 1
                in_bounce = False
                if pushes >= 2:
                    return _result(b["l"], f"PUSH_{pushes}", i)
            trough = b["l"]
        elif not in_bounce and trough > 0 and (b["h"] - trough) / trough >= PUSH_MIN_PULLBACK_PCT:
            in_bounce = True

    last = session_bars[-1]
    return _result(last["c"], "SESSION_END", len(session_bars) - 1)


async def run_opening_bar_short_side_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE, DIAGNOSTIC ONLY - the real bearish mirror of the live
    Elephant/Tail system. Never places a real order, and prop_bot.py
    genuinely can't short today regardless. symbols=None (default) tests
    every real symbol prop_bot.py trades."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        days_grouped = _group_bars_by_day(bars)
        trades = []
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout_short(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return symbol, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    per_symbol = []
    skipped = []
    all_trades = []
    for symbol, trades in results:
        if trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_symbol.append({
                "product_id": symbol, "num_trades": len(trades),
                "win_rate": round(wins / len(trades), 4),
                "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
            })
        all_trades.extend(trades)

    overall = None
    if all_trades:
        wins = sum(1 for t in all_trades if t["pnl_usd"] > 0)
        overall = {
            "num_trades": len(all_trades),
            "win_rate": round(wins / len(all_trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in all_trades), 2),
            "stop_count": sum(1 for t in all_trades if t["exit_reason"] == "STOP"),
            "push_exit_count": sum(1 for t in all_trades if t["exit_reason"].startswith("PUSH")),
        }

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "days": days, "diagnostic_only": True,
        },
    }


SCALED_ENTRY_INITIAL_FRACTION = 0.5
SCALED_ENTRY_ADD_FRACTION = 0.25
SCALED_ENTRY_MAX_ADDS = 2


def _replay_opening_bar_breakout_scaled(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD,
                                         max_adds: int = SCALED_ENTRY_MAX_ADDS):
    """The account owner's own real scaling-in mechanic - the direct
    Alpaca-side counterpart to crypto_selection_backtest.py's identical
    function. Half the real spend at the original trigger, up to two
    real quarter-size adds each triggered by a later bar trading through
    the high of the most recent single real red pullback bar + $0.01.
    The real stop and push-based exit are unchanged from the single-shot
    version. Returns a real trade dict, or None if no real trade fired."""
    if len(session_bars) < 3:
        return None
    bar1 = session_bars[0]
    is_elephant = _is_elephant_bar(bar1, preceding_bars)
    is_tail = _is_bottoming_tail_bar(bar1)
    if not (is_elephant or is_tail):
        return None

    trigger_price = bar1["h"] + OPENING_BAR_ENTRY_BUFFER_USD
    stop_price = bar1["l"]
    qualifies_as = "elephant" if is_elephant else "tail"

    entry_idx = None
    for i in range(1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return None
        if b["h"] >= trigger_price:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    entry_price = trigger_price
    initial_spend = spend * SCALED_ENTRY_INITIAL_FRACTION
    add_spend = spend * SCALED_ENTRY_ADD_FRACTION
    qty = initial_spend / entry_price
    total_cost = initial_spend

    def _result(exit_price, exit_reason, exit_index, num_adds):
        avg_entry = total_cost / qty if qty else entry_price
        pnl_usd = qty * (exit_price - avg_entry)
        return {
            "qualifies_as": qualifies_as, "entry_price": round(avg_entry, 8), "entry_index": entry_idx,
            "initial_entry_price": entry_price, "stop_price": stop_price,
            "exit_price": exit_price, "exit_reason": exit_reason, "exit_index": exit_index,
            "num_adds": num_adds, "total_spend": round(total_cost, 2),
            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round((exit_price - avg_entry) / avg_entry, 4),
        }

    peak = entry_price
    pushes = 1
    in_pullback = False
    pending_add_trigger = None
    adds_done = 0
    for i in range(entry_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return _result(stop_price, "STOP", i, adds_done)

        if adds_done < max_adds and pending_add_trigger is not None and b["h"] >= pending_add_trigger:
            qty += add_spend / pending_add_trigger
            total_cost += add_spend
            adds_done += 1
            pending_add_trigger = None

        if b["c"] < b["o"]:
            pending_add_trigger = b["h"] + OPENING_BAR_ENTRY_BUFFER_USD

        if b["h"] > peak:
            if in_pullback:
                pushes += 1
                in_pullback = False
                if pushes >= 2:
                    return _result(b["h"], f"PUSH_{pushes}", i, adds_done)
            peak = b["h"]
        elif not in_pullback and peak > 0 and (peak - b["l"]) / peak >= PUSH_MIN_PULLBACK_PCT:
            in_pullback = True

    last = session_bars[-1]
    return _result(last["c"], "SESSION_END", len(session_bars) - 1, adds_done)


async def run_scaled_entry_comparison_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE - the direct Alpaca-side counterpart to
    crypto_selection_backtest.py's identical function. Replays the
    IDENTICAL real qualifying Elephant/Tail setups two ways: the
    existing single-shot entry vs. the account owner's own real
    half-in-then-two-adds scaling mechanic. Never places a real order."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None, None
        days_grouped = _group_bars_by_day(bars)
        single_shot_trades = []
        scaled_trades = []
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            t1 = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if t1 is not None:
                single_shot_trades.append(t1)
            t2 = _replay_opening_bar_breakout_scaled(session_bars, preceding_bars)
            if t2 is not None:
                scaled_trades.append(t2)
        return symbol, single_shot_trades, scaled_trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades),
            "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
        }

    per_symbol = []
    skipped = []
    all_single = []
    all_scaled = []
    for symbol, single_trades, scaled_trades in results:
        if single_trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if single_trades:
            per_symbol.append({
                "product_id": symbol,
                "single_shot": _summarize(single_trades),
                "scaled": _summarize(scaled_trades),
            })
        all_single.extend(single_trades)
        all_scaled.extend(scaled_trades)

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol,
        "overall": {"single_shot": _summarize(all_single), "scaled": _summarize(all_scaled)},
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD, "days": days,
            "initial_fraction": SCALED_ENTRY_INITIAL_FRACTION, "add_fraction": SCALED_ENTRY_ADD_FRACTION,
            "max_adds": SCALED_ENTRY_MAX_ADDS,
        },
    }


def _replay_red_bar_takeout_breakout(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """The account owner's own real THIRD, lower-conviction setup - the
    direct Alpaca-side counterpart to crypto_selection_backtest.py's
    identical function. Bar 1 needs no special qualification beyond
    being a real, ordinary RED bar that does NOT already qualify as a
    real Elephant Bar or bottoming Tail bar (the two higher-conviction
    setups already tested separately). Same real entry/stop/exit
    mechanics as the other long-side setups. Returns a real trade dict,
    or None if no real trade fired today."""
    if len(session_bars) < 3:
        return None
    bar1 = session_bars[0]
    if bar1["c"] >= bar1["o"]:
        return None
    if _is_elephant_bar(bar1, preceding_bars) or _is_bottoming_tail_bar(bar1):
        return None

    trigger_price = bar1["h"] + OPENING_BAR_ENTRY_BUFFER_USD
    stop_price = bar1["l"]

    entry_idx = None
    for i in range(1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return None
        if b["h"] >= trigger_price:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    entry_price = trigger_price
    qty = spend / entry_price

    def _result(exit_price, exit_reason, exit_index):
        pnl_usd = qty * (exit_price - entry_price)
        return {
            "qualifies_as": "red_bar_takeout", "entry_price": entry_price, "entry_index": entry_idx,
            "stop_price": stop_price, "exit_price": exit_price, "exit_reason": exit_reason,
            "exit_index": exit_index, "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round((exit_price - entry_price) / entry_price, 4),
        }

    peak = entry_price
    pushes = 1
    in_pullback = False
    for i in range(entry_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return _result(stop_price, "STOP", i)
        if b["h"] > peak:
            if in_pullback:
                pushes += 1
                in_pullback = False
                if pushes >= 2:
                    return _result(b["h"], f"PUSH_{pushes}", i)
            peak = b["h"]
        elif not in_pullback and peak > 0 and (peak - b["l"]) / peak >= PUSH_MIN_PULLBACK_PCT:
            in_pullback = True

    last = session_bars[-1]
    return _result(last["c"], "SESSION_END", len(session_bars) - 1)


async def run_red_bar_takeout_backtest(symbols=None, days: int = BACKTEST_DAYS, max_concurrent: int = 6) -> dict:
    """SHADOW-MODE, real historical 2-minute Alpaca bars - the direct
    Alpaca-side counterpart to crypto_selection_backtest.py's identical
    function. Never places a real order. symbols=None (default) tests
    every real symbol prop_bot.py trades."""
    symbol_list = symbols if symbols is not None else list(FUTURES.keys())
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, symbol):
        async with semaphore:
            ticker = FUTURES.get(symbol, {}).get("symbol", symbol)  # real Alpaca-tradable ticker (e.g. SIL -> SLV, MES -> SPY), not the raw contract code
            bars, err = await _fetch_bars_2min_with_ohlc_and_times(session, ticker, days)
        if bars is None:
            last_error[symbol] = err
            return symbol, None
        days_grouped = _group_bars_by_day(bars)
        trades = []
        for i in range(1, len(days_grouped)):
            _date, session_bars = days_grouped[i]
            preceding_bars = days_grouped[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_red_bar_takeout_breakout(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return symbol, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, s) for s in symbol_list))

    per_symbol = []
    skipped = []
    all_trades = []
    for symbol, trades in results:
        if trades is None:
            skipped.append({"product_id": symbol, "reason": last_error.get(symbol, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_symbol.append({
                "product_id": symbol, "num_trades": len(trades),
                "win_rate": round(wins / len(trades), 4),
                "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
            })
        all_trades.extend(trades)

    overall = None
    if all_trades:
        wins = sum(1 for t in all_trades if t["pnl_usd"] > 0)
        overall = {
            "num_trades": len(all_trades),
            "win_rate": round(wins / len(all_trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in all_trades), 2),
            "stop_count": sum(1 for t in all_trades if t["exit_reason"] == "STOP"),
            "push_exit_count": sum(1 for t in all_trades if t["exit_reason"].startswith("PUSH")),
        }

    return {
        "symbols_tested": len(symbol_list), "symbols_with_results": len(per_symbol),
        "skipped": skipped, "per_symbol": per_symbol, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "days": days,
        },
    }
