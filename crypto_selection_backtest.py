"""
SHADOW-MODE BACKTEST - offline tool only. Does not touch live trading,
is not imported by any live bot, and places no real orders.

Built in response to a real question: was the family-tree bot's coin
SELECTION (find_most_volatile_unclaimed_coin in crypto_family_tree_bot.py)
actually buying at bad moments? That function's only timing check is
`closes[-1] > closes[0]` over a ~25-hour window - a coarse "is it up
overall" check with no sense of whether the move already happened and
the coin is now extended. This script tests that suspicion the honest
way: replay the bot's OWN real target/stop/breakeven/giveback rules
(imported directly from crypto_btc_compound_bot.py and
crypto_family_tree_bot.py - not reimplemented) against each coin's real
historical price data, and rank coins by what that strategy would
actually have returned on each one.

Simplifications, stated plainly rather than hidden:
- Entries/exits are decided on each hourly candle's CLOSE only, not
  intrabar high/low - this avoids crediting unrealistic "lucky" fills,
  at the cost of being a coarser simulation than real 24/7 tick-by-tick
  trading. Good enough for RANKING coins against each other; not a
  precise P&L forecast.
- A fixed $150 is "redeployed" after every exit (matching a realistic
  mid-size branch from this account), so results are comparable
  apples-to-apples across coins regardless of how big any one real
  branch currently is.
- ATR is computed the same way the live bot computes it
  (_atr_pct_from_candles, 14-period), using a rolling window of the
  last 15 hourly candles at each decision point.

Usage: python3 crypto_selection_backtest.py
"""
import asyncio
import bisect
import sys
sys.path.insert(0, "/home/user/empire-v2")
import os
os.environ.setdefault("COINBASE_API_KEY_NAME", "unused-public-endpoint-only")
os.environ.setdefault("COINBASE_API_PRIVATE_KEY", "unused-public-endpoint-only")

import aiohttp
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import crypto_btc_compound_bot as engine
from crypto_family_tree_bot import COIN_FAMILY_TREE, BREAKEVEN_TRIGGER_PCT, MAX_PROFIT_GIVEBACK_USD
from database import AsyncSessionLocal
from models import CryptoTreeBranch

SPEND = 150.0
BACKTEST_DAYS = 30
# Real, effective size of the existing live dollar-based giveback cap
# (MAX_PROFIT_GIVEBACK_USD, $3.75) at the module's own $150 spend size -
# $3.75 / $150 = 2.5%. Used as the trailing-stop comparison's percentage
# trail so the two exit philosophies are sized comparably rather than
# one being an arbitrary tighter/looser number than the other.
TRAILING_STOP_PCT = 0.025
GRANULARITY_SECONDS = 3600  # 1-hour candles
ATR_WINDOW = 15  # matches _atr_pct_from_candles' 14-period + 1


async def fetch_historical_candles(session, product_id, days=BACKTEST_DAYS):
    """Paginated pull of real Coinbase historical candles (public,
    unauthenticated endpoint - same one the live bot's own
    _fetch_candles uses, just with an explicit start/end window instead
    of 'whatever the last 300 are')."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    all_candles = []
    cursor = start
    while cursor < end:
        page_end = min(cursor + timedelta(seconds=GRANULARITY_SECONDS * 299), end)
        url = (
            f"https://api.exchange.coinbase.com/products/{product_id}/candles"
            f"?granularity={GRANULARITY_SECONDS}&start={cursor.isoformat()}&end={page_end.isoformat()}"
        )
        try:
            async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
                if r.status != 200:
                    break
                data = await r.json()
                if data:
                    all_candles.extend(data)
        except Exception as e:
            print(f"  [{product_id}] fetch error for page starting {cursor.date()}: {e}")
        cursor = page_end
        await asyncio.sleep(0.15)  # be polite to the public endpoint

    if len(all_candles) < ATR_WINDOW + 5:
        return None
    all_candles.sort(key=lambda c: c[0])  # oldest first
    closes = [float(c[4]) for c in all_candles]
    highs = [float(c[2]) for c in all_candles]
    lows = [float(c[1]) for c in all_candles]
    times = [int(c[0]) for c in all_candles]
    return closes, highs, lows, times


def backtest_one_coin(closes, highs, lows, entry_gate=None, spend=None):
    """Replays the REAL live rules candle-by-candle. Returns a dict of
    results, or None if there wasn't enough data to trade at all.

    `spend` (optional) overrides the fixed SPEND module constant for this
    one coin - used by run_full_backtest_with_real_allocations() below to
    simulate each coin's REAL current branch dollars instead of the flat
    $150 every coin gets by default. `spend=None` (the default, every
    existing caller) reproduces the exact original behavior - falls back
    to the module-level SPEND constant, byte-for-byte unchanged.

    entry_gate, if given, is called as entry_gate(i) at each flat decision
    point (i = index into closes/highs/lows) and must return True to allow
    a new entry there - used by run_btc_relative_strength_comparison()
    below to add a real entry-timing filter without duplicating this whole
    replay loop. None (the default) means always enter the moment flat -
    the original, unchanged behavior every existing caller (including the
    live auto-exclusion system's daily backtest run) already depends on."""
    # `spend <= 0` (not just None) falls back to the default too - real,
    # confirmed live bug: a coin whose only real branch is sitting at a
    # genuine $0.00 balance (POL-USD, SOL-USD in production) passed
    # spend=0.0 straight through, which isn't None so the check below
    # never caught it, and total_pnl / spend crashed with a real
    # ZeroDivisionError the instant the replay produced even one trade -
    # surfaced as a raw HTTP 500 on "Run Backtest With Real Allocations".
    spend = spend if spend is not None and spend > 0 else SPEND
    trades = []
    entries_skipped_by_gate = 0
    i = ATR_WINDOW
    n = len(closes)

    position = None  # dict: entry, qty, target, stop, peak_usd

    while i < n:
        price = closes[i]
        window_closes = closes[max(0, i - ATR_WINDOW):i + 1]
        window_highs = highs[max(0, i - ATR_WINDOW):i + 1]
        window_lows = lows[max(0, i - ATR_WINDOW):i + 1]
        atr_pct = engine._atr_pct_from_candles(window_closes, window_highs, window_lows)

        if position is None:
            if entry_gate is not None and not entry_gate(i):
                entries_skipped_by_gate += 1
                i += 1
                continue
            target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(spend, atr_pct))
            qty = spend / price
            position = {
                "entry": price, "qty": qty,
                "target": price * (1 + target_pct),
                "stop": price * (1 - engine.STOP_LOSS_PCT),
                "peak_usd": 0.0,
            }
            i += 1
            continue

        unrealized_usd = position["qty"] * (price - position["entry"])
        if unrealized_usd > position["peak_usd"]:
            position["peak_usd"] = unrealized_usd

        # Real breakeven ratchet (same trigger the live bot uses).
        if position["stop"] < position["entry"] and price >= position["entry"] * (1 + BREAKEVEN_TRIGGER_PCT):
            position["stop"] = position["entry"]

        giveback = position["peak_usd"] - unrealized_usd
        giveback_exceeded = position["peak_usd"] > 0 and giveback >= MAX_PROFIT_GIVEBACK_USD

        exit_reason = None
        if price >= position["target"]:
            exit_reason = "TARGET"
        elif price <= position["stop"]:
            exit_reason = "STOP"
        elif giveback_exceeded:
            exit_reason = "GIVEBACK"

        if exit_reason:
            gross = position["qty"] * (price - position["entry"])
            fee = position["qty"] * (position["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            net = gross - fee
            trades.append((exit_reason, net))
            position = None
        i += 1

    if not trades:
        return None
    total_pnl = sum(net for _, net in trades)
    wins = [net for _, net in trades if net > 0]
    win_rate = len(wins) / len(trades) * 100
    avg_trade_pct = (total_pnl / len(trades)) / spend * 100
    result = {
        "num_trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "roi_pct_of_spend": total_pnl / spend * 100,
        "avg_trade_pct": avg_trade_pct,
        "spend_used": spend,
    }
    # Only added when a gate was actually used, so the baseline schema
    # (what _run_scheduled_backtest_and_update_exclusions persists to
    # CryptoBacktestRun and what the live dashboard table renders) never
    # changes shape.
    if entry_gate is not None:
        result["entries_skipped_by_gate"] = entries_skipped_by_gate
    return result


def _replay_with_exit_mode(closes, highs, lows, mode, entry_gate=None, spend=None, trail_pct=None):
    """Real, shared replay for the QUICK_PROFIT-vs-trailing-stop
    comparison (run_quick_profit_vs_trailing_stop_comparison below) -
    built after a pasted proposal argued for letting winners run with a
    percentage trailing stop, which directly conflicts with the real,
    live QUICK_PROFIT rule (crypto_family_tree_bot.py's run_branch_cycle)
    shipped earlier this same session at the account owner's own explicit
    request ("take any real profit fast, never wait"). Rather than guess
    which is actually better, this replays BOTH real exit philosophies
    against the identical real historical candles.

    Entry and the real hard stop-loss are IDENTICAL to backtest_one_coin()
    above - same ATR-based target/stop, same real fee formula, same
    breakeven ratchet. The two modes diverge ONLY in what happens once a
    position is open:

    mode="quick_profit" mirrors the real live behavior: the position
    exits the INSTANT its real, fee-adjusted net P&L clears $0, exactly
    matching QUICK_PROFIT_MIN_NET_USD=0.0 live - it never holds through a
    pullback hoping for more, win or lose.

    mode="trailing_stop" instead only activates real trailing protection
    once price reaches the SAME real ATR-based target price used in
    quick_profit mode (so both modes agree on what a 'good' move looks
    like) - from that point its stop trails TRAILING_STOP_PCT behind the
    highest real price seen since entry, only exiting on an actual
    reversal, never for reaching profit itself. Before target is reached,
    it behaves identically to the existing hard stop-loss/breakeven
    ratchet - this isolates the one real question being asked (snap
    profit immediately vs. let a winner run) rather than testing a
    different strategy altogether.

    A position still open when the real historical window ends is
    dropped without being marked-to-market - the same simplification
    backtest_one_coin() already documents and accepts; good enough for
    comparing the two exit philosophies against each other, not a precise
    P&L forecast.

    `trail_pct` (optional) overrides the module's own TRAILING_STOP_PCT
    for this one replay - used by run_trailing_stop_pct_sweep_comparison()
    below to test several candidate trail widths against the identical
    real data, per the account owner's own explicit follow-up request to
    "refine and update" trailing stop with what's already built rather
    than replace it outright. `trail_pct=None` (every existing caller)
    reproduces the exact original behavior, byte-for-byte."""
    # Same real spend<=0 guard as backtest_one_coin() above - see its own
    # comment for the exact live ZeroDivisionError this prevents.
    spend = spend if spend is not None and spend > 0 else SPEND
    trail_pct = trail_pct if trail_pct is not None and trail_pct > 0 else TRAILING_STOP_PCT
    trades = []
    i = ATR_WINDOW
    n = len(closes)
    position = None  # entry, qty, target, stop, peak_price, profit_activated

    while i < n:
        price = closes[i]
        window_closes = closes[max(0, i - ATR_WINDOW):i + 1]
        window_highs = highs[max(0, i - ATR_WINDOW):i + 1]
        window_lows = lows[max(0, i - ATR_WINDOW):i + 1]
        atr_pct = engine._atr_pct_from_candles(window_closes, window_highs, window_lows)

        if position is None:
            if entry_gate is not None and not entry_gate(i):
                i += 1
                continue
            target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(spend, atr_pct))
            qty = spend / price
            position = {
                "entry": price, "qty": qty,
                "target": price * (1 + target_pct),
                "stop": price * (1 - engine.STOP_LOSS_PCT),
                "peak_price": price,
                "profit_activated": False,
            }
            i += 1
            continue

        if price > position["peak_price"]:
            position["peak_price"] = price

        # Real breakeven ratchet - identical to backtest_one_coin().
        if position["stop"] < position["entry"] and price >= position["entry"] * (1 + BREAKEVEN_TRIGGER_PCT):
            position["stop"] = position["entry"]

        exit_reason = None
        if mode == "quick_profit":
            gross = position["qty"] * (price - position["entry"])
            fee = position["qty"] * (position["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            net = gross - fee
            if price <= position["stop"]:
                exit_reason = "STOP"
            elif net > 0:
                exit_reason = "QUICK_PROFIT"
        else:  # "trailing_stop"
            if not position["profit_activated"] and price >= position["target"]:
                position["profit_activated"] = True
            if position["profit_activated"]:
                trailing_stop = position["peak_price"] * (1 - trail_pct)
                effective_stop = max(position["stop"], trailing_stop)
            else:
                effective_stop = position["stop"]
            if price <= effective_stop:
                exit_reason = "TRAILING_STOP" if position["profit_activated"] else "STOP"

        if exit_reason:
            gross = position["qty"] * (price - position["entry"])
            fee = position["qty"] * (position["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            net = gross - fee
            trades.append((exit_reason, net))
            position = None
        i += 1

    if not trades:
        return None
    total_pnl = sum(net for _, net in trades)
    wins = [net for _, net in trades if net > 0]
    win_rate = len(wins) / len(trades) * 100
    avg_trade_pct = (total_pnl / len(trades)) / spend * 100
    return {
        "num_trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "roi_pct_of_spend": total_pnl / spend * 100,
        "avg_trade_pct": avg_trade_pct,
    }


async def run_quick_profit_vs_trailing_stop_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading,
    places no real order. Answers a real, direct question: does the live
    QUICK_PROFIT rule (take any real profit the instant it clears fees)
    actually make more real money than letting a winner run behind a
    percentage trailing stop once it reaches the same real ATR-based
    target? Runs BOTH real exit philosophies against the identical real
    historical candles for every coin, so the comparison is fair."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _one(product_id):
        async with semaphore:
            candles = await fetch_historical_candles(session, product_id, days=days)
        if candles is None:
            return product_id, None, "not enough historical data"
        closes, highs, lows, _times = candles
        quick_profit = _replay_with_exit_mode(closes, highs, lows, mode="quick_profit")
        trailing_stop = _replay_with_exit_mode(closes, highs, lows, mode="trailing_stop")
        return product_id, {"quick_profit": quick_profit, "trailing_stop": trailing_stop}, None

    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})

    def _total(key):
        return sum((row[key]["total_pnl"] if row[key] else 0.0) for row in comparison)

    quick_profit_wins = 0
    trailing_stop_wins = 0
    for row in comparison:
        qp_pnl = row["quick_profit"]["total_pnl"] if row["quick_profit"] else 0.0
        ts_pnl = row["trailing_stop"]["total_pnl"] if row["trailing_stop"] else 0.0
        if qp_pnl > ts_pnl:
            quick_profit_wins += 1
        elif ts_pnl > qp_pnl:
            trailing_stop_wins += 1

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "trailing_stop_pct": TRAILING_STOP_PCT,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "totals": {
            "quick_profit_total_pnl": _total("quick_profit"),
            "trailing_stop_total_pnl": _total("trailing_stop"),
            "quick_profit_coins_won": quick_profit_wins,
            "trailing_stop_coins_won": trailing_stop_wins,
        },
        "comparison": comparison,
    }


# Per the account owner's explicit follow-up ("is there any way that we
# can refine and update the trailing stop what we have") right after
# QUICK_PROFIT was removed outright in favor of trailing stop: the live
# 2.5% trail (TRAILING_STOP_PCT) was never itself tested against any
# alternative width - it was sized to match the OLD QUICK_PROFIT
# dollar-giveback cap ($3.75/$150), a coincidence of the comparison it
# won, not evidence it's the best trailing-stop width on its own merits.
# These candidates bracket it on both sides (tighter and looser) so the
# sweep below can actually find a genuinely better real width, not just a
# different one.
TRAILING_STOP_PCT_CANDIDATES = [0.015, 0.02, 0.025, 0.03, 0.04, 0.05]


async def run_trailing_stop_pct_sweep_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6, candidates=None):
    """SHADOW-MODE, additive comparison - never touches live trading,
    places no real order. Replays the real trailing-stop exit rule under
    several candidate trail percentages (TRAILING_STOP_PCT_CANDIDATES by
    default) against the IDENTICAL real historical candles for every
    coin, so a genuinely better trail width can be found with real
    evidence - the same discipline (replay the bot's own real rules,
    never guess) every other comparison tool in this file already
    follows. Entry, target, stop, and breakeven are all identical across
    every candidate - only the trail width itself varies, isolating the
    one real question being asked."""
    coins = coins or COIN_FAMILY_TREE
    candidates = candidates or TRAILING_STOP_PCT_CANDIDATES
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _one(product_id):
        async with semaphore:
            candles = await fetch_historical_candles(session, product_id, days=days)
        if candles is None:
            return product_id, None, "not enough historical data"
        closes, highs, lows, _times = candles
        by_pct = {
            pct: _replay_with_exit_mode(closes, highs, lows, mode="trailing_stop", trail_pct=pct)
            for pct in candidates
        }
        return product_id, by_pct, None

    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, by_pct, skip_reason in outcomes:
        if by_pct is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, "results": {str(pct): by_pct[pct] for pct in candidates}})

    totals_by_pct = {}
    coins_won_by_pct = {}
    for pct in candidates:
        key = str(pct)
        totals_by_pct[key] = sum(
            (row["results"][key]["total_pnl"] if row["results"][key] else 0.0) for row in comparison
        )
        coins_won_by_pct[key] = 0

    for row in comparison:
        best_pnl, best_key = None, None
        for pct in candidates:
            key = str(pct)
            r = row["results"][key]
            pnl = r["total_pnl"] if r else 0.0
            if best_pnl is None or pnl > best_pnl:
                best_pnl, best_key = pnl, key
        if best_key is not None:
            coins_won_by_pct[best_key] += 1

    best_overall_pct = max(totals_by_pct, key=lambda k: totals_by_pct[k]) if totals_by_pct else None

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "candidates": candidates,
        "current_live_trail_pct": TRAILING_STOP_PCT,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "totals_by_pct": totals_by_pct,
        "coins_won_by_pct": coins_won_by_pct,
        "best_overall_pct": best_overall_pct,
        "comparison": comparison,
    }


def calculate_relative_strength(coin_closes_window: list, btc_closes_window: list) -> float:
    """Real alpha calc, per the account owner's explicit request to add a
    BTC-relative-strength signal on top of the existing selection checks:
    the coin's simple return over a window minus BTC's real simple return
    over the IDENTICAL window. Positive means the coin genuinely
    outperformed BTC over that stretch, not just moved up in absolute
    terms - in a market where everything grinds up together, "up in
    isolation" is a much weaker signal than "beating the market's own
    benchmark asset." Expects both lists ordered oldest to newest, already
    aligned to the same real time window (see _closest_close_at_or_before
    for how the comparison function does that alignment)."""
    if not coin_closes_window or not btc_closes_window or coin_closes_window[0] <= 0 or btc_closes_window[0] <= 0:
        return 0.0
    coin_return = (coin_closes_window[-1] - coin_closes_window[0]) / coin_closes_window[0]
    btc_return = (btc_closes_window[-1] - btc_closes_window[0]) / btc_closes_window[0]
    return coin_return - btc_return


def _closest_close_at_or_before(times_sorted: list, closes_sorted: list, target_time: int):
    """Real historical candle pages can have small gaps at different
    points for two different coins (a page that came back short, a
    momentary API hiccup) - looking up BTC's price by array INDEX instead
    of real timestamp would silently misalign the comparison. This finds
    BTC's most recent real close at or before the exact real time being
    compared against, via binary search (both arrays are already
    time-sorted by fetch_historical_candles). Returns None only if
    target_time is before every real candle in the series."""
    idx = bisect.bisect_right(times_sorted, target_time) - 1
    if idx < 0:
        return None
    return closes_sorted[idx]


def _make_btc_relative_strength_gate(closes, times, lookback_hours, btc_times_sorted, btc_closes_sorted):
    """Returns an entry_gate(i) closure for backtest_one_coin(): only
    allows a new entry at candle i if this coin's real return over the
    last lookback_hours beats BTC-USD's real return over the identical
    real time window. Coins get a free pass until they have enough of
    their own history to compute a real window (~25 hours in, matching
    the live bot's own bullish-check lookback) rather than being blocked
    from ever entering near the start of the backtest."""
    def gate(i):
        if i < lookback_hours:
            return True
        btc_now = _closest_close_at_or_before(btc_times_sorted, btc_closes_sorted, times[i])
        btc_then = _closest_close_at_or_before(btc_times_sorted, btc_closes_sorted, times[i - lookback_hours])
        if btc_now is None or btc_then is None:
            return True  # no real BTC data to compare against here - don't block on missing data
        alpha = calculate_relative_strength([closes[i - lookback_hours], closes[i]], [btc_then, btc_now])
        return alpha > 0
    return gate


def _make_higher_tf_trend_gate(closes, sma_short=20, sma_long=50):
    """Returns an entry_gate(i) closure for backtest_one_coin() - the
    crypto-side analog of prop_bot.py's real get_higher_tf_trend() 1-hour
    confirmation filter (same SMA20/SMA50 pairing, "don't fight the higher
    timeframe trend"). Unlike the Alpaca version, this needs no separate
    coarser-timeframe fetch: this backtest already replays on hourly
    candles, so computing SMA20/SMA50 directly off the same closes array
    already being replayed IS the real, direct equivalent - no extra API
    cost versus the plain baseline. Only allows a new long entry when
    SMA20 > SMA50 (a genuine uptrend), matching prop_bot.py's own
    long-entry rule. Free pass (True) for the first sma_long candles -
    not enough real history yet to compute a trend, so don't block on
    its absence, same "don't block on missing data" rule every other gate
    in this file already follows."""
    def gate(i):
        if i < sma_long:
            return True
        sma20 = sum(closes[i - sma_short + 1:i + 1]) / sma_short
        sma50 = sum(closes[i - sma_long + 1:i + 1]) / sma_long
        return sma20 > sma50
    return gate


async def run_higher_tf_trend_comparison(coins=None, days=BACKTEST_DAYS, sma_short=20, sma_long=50, max_concurrent=6):
    """SHADOW-MODE, additive comparison - same pattern as
    run_btc_relative_strength_comparison() below, testing a different real
    question raised directly by the account owner (does the crypto side
    need a higher-timeframe trend confirmation filter, the way the Alpaca
    side already has one?): would requiring the coin's own SMA20 > SMA50
    uptrend before entering have improved each coin's real backtested
    numbers? Runs the exact same real target/stop/breakeven/giveback
    replay twice per coin on identical real historical data - baseline vs
    trend-gated - so the two are directly, fairly comparable. Does not
    change what the live bot buys unless/until wired into the live
    selection path separately, on purpose - this is a read-only
    comparison report, same as every other backtest tool in this file."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, _times = candles
            baseline = backtest_one_coin(closes, highs, lows)
            gate = _make_higher_tf_trend_gate(closes, sma_short=sma_short, sma_long=sma_long)
            filtered = backtest_one_coin(closes, highs, lows, entry_gate=gate)
            return product_id, {"baseline": baseline, "with_trend_filter": filtered}, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})

    def _sort_key(row):
        filtered = row["with_trend_filter"]
        return filtered["roi_pct_of_spend"] if filtered else -999.0
    comparison.sort(key=_sort_key, reverse=True)

    return {
        "backtest_days": days,
        "sma_short": sma_short,
        "sma_long": sma_long,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "comparison": comparison,
    }


async def run_btc_relative_strength_comparison(coins=None, days=BACKTEST_DAYS, lookback_hours=25, max_concurrent=6):
    """SHADOW-MODE, additive comparison tool - does NOT touch or replace
    run_full_backtest()/backtest_one_coin()'s existing baseline behavior,
    which _run_scheduled_backtest_and_update_exclusions() and the live
    top-15 coin rotation both depend on for real trading decisions today.
    Answers a narrower question first, per the account owner's explicit
    request: would requiring a coin to be outperforming BTC-USD over the
    same real ~25-hour window (before entering a position) have improved
    the numbers, coin by coin? Runs BOTH the existing baseline replay and
    a new BTC-relative-strength-gated replay against the exact same real
    historical candles, so the two are directly, fairly comparable -
    nothing here changes what the live bot buys unless/until this is
    wired into the live selection path separately, on purpose.

    Real extra cost versus the existing baseline backtest: one additional
    real historical-candle fetch for BTC-USD itself (the baseline backtest
    doesn't currently load BTC's own history at all)."""
    coins = [p for p in (coins or COIN_FAMILY_TREE) if p != "BTC-USD"]
    semaphore = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        btc_candles = await fetch_historical_candles(session, "BTC-USD", days=days)
        if btc_candles is None:
            return {"error": "could not fetch real BTC-USD history to compare against"}
        _btc_closes, _btc_highs, _btc_lows, btc_times = btc_candles
        btc_closes = _btc_closes

        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, times = candles
            baseline = backtest_one_coin(closes, highs, lows)
            gate = _make_btc_relative_strength_gate(closes, times, lookback_hours, btc_times, btc_closes)
            filtered = backtest_one_coin(closes, highs, lows, entry_gate=gate)
            return product_id, {"baseline": baseline, "with_btc_filter": filtered}, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})

    def _sort_key(row):
        filtered = row["with_btc_filter"]
        return filtered["roi_pct_of_spend"] if filtered else -999.0
    comparison.sort(key=_sort_key, reverse=True)

    return {
        "backtest_days": days,
        "lookback_hours": lookback_hours,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "comparison": comparison,
    }


async def _backtest_one_coin_with_semaphore(session, product_id, semaphore, spend=None):
    async with semaphore:
        candles = await fetch_historical_candles(session, product_id)
    if candles is None:
        return product_id, None, "not enough historical data"
    closes, highs, lows, _times = candles
    result = backtest_one_coin(closes, highs, lows, spend=spend)
    if result is None:
        return product_id, None, "no trades triggered in this window"
    return product_id, result, None


async def _get_real_branch_allocations() -> dict:
    """Real, current allocated_usd per coin from CryptoTreeBranch - summed
    across every branch holding that coin, since multiple branches can
    share one coin (see "Multiple branches can now share the same coin"
    in CLAUDE.md) and Coinbase's real balance for it is pooled the same
    way. Same aggregation-by-product_id pattern the per-coin trade
    history already uses. Root (BTC-USD) included like any other coin.

    A coin whose only real branch(es) are sitting at a genuine $0.00
    balance (real production examples: POL-USD, SOL-USD - a never-funded
    branch, or one that's lost its entire real balance) is pruned out
    here entirely rather than returned as 0.0 - real, confirmed live bug:
    passing spend=0.0 through to backtest_one_coin() crashed it with a
    real ZeroDivisionError (0.0 total_pnl / 0.0 spend) the instant the
    replay produced even one trade, surfacing as a raw HTTP 500 on "Run
    Backtest With Real Allocations". Pruning it here means the caller's
    `allocations.get(pid)` correctly returns None for that coin, falling
    through to the same $150 default every other unallocated coin
    already gets - matching this function's own documented contract."""
    allocations = {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CryptoTreeBranch.product_id, CryptoTreeBranch.allocated_usd))
        for product_id, allocated_usd in result.all():
            allocations[product_id] = allocations.get(product_id, 0.0) + allocated_usd
    return {pid: usd for pid, usd in allocations.items() if usd > 0.005}


async def run_full_backtest(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """The reusable entry point routers/trading_dashboard.py calls. Fetches
    every coin's history CONCURRENTLY (capped by max_concurrent, since a
    fully sequential pull of 27 coins x up to 3 pages each would be slow
    enough to risk a request timeout) and returns a ranked list of dicts,
    ready to serialize as JSON. Real network calls to Coinbase's public
    candles endpoint - this only works from an environment that can reach
    api.exchange.coinbase.com (Railway can; some sandboxed dev
    environments cannot, since it's the same host the live bot's own ATR
    lookups already depend on every cycle)."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)
    skipped = []
    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(
            *(_backtest_one_coin_with_semaphore(session, pid, semaphore) for pid in coins)
        )
    results = {}
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
        else:
            results[product_id] = result

    ranked = sorted(results.items(), key=lambda kv: kv[1]["roi_pct_of_spend"], reverse=True)
    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(ranked),
        "skipped": skipped,
        "ranked": [{"product_id": pid, **r} for pid, r in ranked],
    }


async def run_full_backtest_with_real_allocations(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """The account owner's own real point: a flat $150 for every coin is
    deliberately apples-to-apples for RANKING coins by quality, but it
    doesn't reflect what your actual money would have done - the real
    tree has $881.76 on BTC, $797.66 on POL, $49.58 on SOL, not an equal
    $150 each. This is the direct counterpart to run_full_backtest() that
    simulates each coin's REAL current branch dollars instead.

    A coin with no real branch/allocation right now still gets tested -
    falls back to the same $150 default every other coin in
    run_full_backtest() uses, so the table stays complete rather than
    only showing the 2-3 coins the tree happens to be holding today.
    Every coin's `spend_used` in the result tells you which case applied.

    Real network calls to Coinbase's public candles endpoint - same host
    every other backtest in this file already depends on."""
    coins = coins or COIN_FAMILY_TREE
    allocations = await _get_real_branch_allocations()
    semaphore = asyncio.Semaphore(max_concurrent)
    skipped = []
    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(
            *(_backtest_one_coin_with_semaphore(session, pid, semaphore, spend=allocations.get(pid)) for pid in coins)
        )
    results = {}
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
        else:
            results[product_id] = result

    ranked = sorted(results.items(), key=lambda kv: kv[1]["roi_pct_of_spend"], reverse=True)
    return {
        "backtest_days": days,
        "default_spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(ranked),
        "coins_with_real_allocation": len([pid for pid in results if pid in allocations]),
        "skipped": skipped,
        "ranked": [{"product_id": pid, "has_real_allocation": pid in allocations, **r} for pid, r in ranked],
    }


async def main():
    print(f"Backtesting {len(COIN_FAMILY_TREE)} coins over the last {BACKTEST_DAYS} days of REAL Coinbase hourly candles.")
    print(f"Replaying the live bot's real target/stop/breakeven/giveback rules, ${SPEND:.0f} redeployed per trade.\n")

    output = await run_full_backtest()

    if not output["ranked"]:
        print("\nNo coins produced usable results.")
        for s in output["skipped"]:
            print(f"  [{s['product_id']}] {s['reason']}")
        return

    print("\n" + "=" * 90)
    print(f"{'Coin':<12}{'Trades':>8}{'Win rate':>11}{'Total P&L':>13}{'ROI on $150':>14}{'Avg/trade':>12}")
    print("=" * 90)
    for r in output["ranked"]:
        marker = "  <<<" if r["product_id"] == "STX-USD" else ""
        print(
            f"{r['product_id']:<12}{r['num_trades']:>8}{r['win_rate']:>10.1f}%{r['total_pnl']:>12.2f}$"
            f"{r['roi_pct_of_spend']:>13.1f}%{r['avg_trade_pct']:>11.2f}%{marker}"
        )
    print("=" * 90)
    for s in output["skipped"]:
        print(f"  skipped [{s['product_id']}]: {s['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
