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
from models import CryptoTreeBranch, CryptoCoinTradeHistory

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


async def fetch_candles_window(session, product_id, start, end, min_candles=ATR_WINDOW + 5):
    """Paginated pull of real Coinbase historical candles (public,
    unauthenticated endpoint - same one the live bot's own _fetch_candles
    uses) between two explicit real UTC datetimes. Factored out of
    fetch_historical_candles() below so a caller anchored on a real past
    EVENT (not "the last N days from right now") can reuse the identical
    real pagination logic - see run_stop_hit_reversal_backtest(), which
    needs a real forward window starting at each real stop-loss's own
    closed_at, not the module's usual now-minus-days window."""
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

    if len(all_candles) < min_candles:
        return None
    all_candles.sort(key=lambda c: c[0])  # oldest first
    closes = [float(c[4]) for c in all_candles]
    highs = [float(c[2]) for c in all_candles]
    lows = [float(c[1]) for c in all_candles]
    times = [int(c[0]) for c in all_candles]
    return closes, highs, lows, times


async def fetch_historical_candles(session, product_id, days=BACKTEST_DAYS):
    """Real, unauthenticated Coinbase candles for the last `days` days
    from right now - the module's original, most common shape. Now a thin
    wrapper around fetch_candles_window() (see above), unchanged in
    return shape/behavior for every existing caller."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return await fetch_candles_window(session, product_id, start, end)


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
# These candidates originally bracketed it on both sides (tighter and
# looser) so the sweep could find a genuinely better real width, not just
# a different one.
#
# Revised again per the account owner's own direct read of the real
# per-coin sweep results: the narrower candidates (1.5%/2.0%/2.5%) were
# consistently the worst real performers across almost every coin in the
# table (the most red), while the wider ones (3.0%/4.0%/5.0%) were
# consistently the best (the most green) - a real, visible pattern of
# wider trails outperforming tighter ones on this data (though NOT
# perfectly monotonic per-coin - a few coins like LDO/SUI/ETC stayed
# negative at every width tested, meaning trail width alone can't fix a
# fundamentally bad setup on those). Dropped the three worst-performing
# narrow candidates and added 0.075 (7.5%) to keep testing further in the
# direction the real data was already pointing - 0.05 (5.0%, the
# account owner's own currently-promoted live width) stays in the set so
# it's never silently dropped out from under the live bot.
TRAILING_STOP_PCT_CANDIDATES = [0.03, 0.04, 0.05, 0.075]


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


STRATEGY_LAB_MOMENTUM_TARGET_PCT = 0.025  # midpoint of the pasted proposal's "1.5% to 3%"
STRATEGY_LAB_MOMENTUM_STOP_PCT = 0.015
STRATEGY_LAB_MOMENTUM_MAX_HOLD_HOURS = 48
STRATEGY_LAB_GRID_PCT = 0.01
STRATEGY_LAB_GRID_LEVELS = 10
STRATEGY_LAB_SWING_LOOKBACK_HOURS = 120  # ~5 days of hourly candles, a real proxy for "recent support"
STRATEGY_LAB_SWING_TARGET_PCT = 0.07  # midpoint of the pasted proposal's "5% to 10%"
STRATEGY_LAB_SWING_STOP_PCT = 0.03
STRATEGY_LAB_SWING_MAX_HOLD_HOURS = 168  # 7 days, the proposal's own outer hold window


def _replay_hourly_momentum(closes, highs, lows, spend=None,
                             target_pct=STRATEGY_LAB_MOMENTUM_TARGET_PCT,
                             stop_pct=STRATEGY_LAB_MOMENTUM_STOP_PCT,
                             max_hold_hours=STRATEGY_LAB_MOMENTUM_MAX_HOLD_HOURS):
    """Real replay of the pasted proposal's "Hourly Momentum Trading" idea:
    enter on confirmed intraday strength (RSI above 55 AND the hourly
    SMA20 > SMA50 uptrend - reusing engine._rsi_from_closes and the same
    SMA pairing _make_higher_tf_trend_gate already validates elsewhere in
    this file), exit at a real fixed target/stop instead of the baseline's
    ATR-derived one, since the proposal itself specifies fixed percentage
    moves. A max_hold_hours backstop marks-to-market if neither fires -
    same "don't leave a position open forever" pattern already used by
    the stop-hit-reversal backtest above.

    This is the one proposal idea genuinely close to logic already live
    (RSI + higher-timeframe trend confirmation) - the other two below
    (grid, swing) have no real analog in this codebase today."""
    spend = spend if spend is not None and spend > 0 else SPEND
    trades = []
    i = max(ATR_WINDOW, 50)
    n = len(closes)
    position = None

    while i < n:
        price = closes[i]
        if position is None:
            rsi = engine._rsi_from_closes(closes[:i + 1])
            sma20 = sum(closes[i - 19:i + 1]) / 20
            sma50 = sum(closes[i - 49:i + 1]) / 50
            if rsi is not None and rsi > 55 and sma20 > sma50:
                position = {
                    "entry": price, "qty": spend / price,
                    "target": price * (1 + target_pct), "stop": price * (1 - stop_pct),
                    "entry_i": i,
                }
            i += 1
            continue

        held_hours = i - position["entry_i"]
        exit_reason = None
        if price >= position["target"]:
            exit_reason = "TARGET"
        elif price <= position["stop"]:
            exit_reason = "STOP"
        elif held_hours >= max_hold_hours:
            exit_reason = "TIME"

        if exit_reason:
            gross = position["qty"] * (price - position["entry"])
            fee = position["qty"] * (position["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            trades.append((exit_reason, gross - fee))
            position = None
        i += 1

    return _summarize_strategy_trades(trades, spend)


def _replay_grid_bot(closes, highs, lows, spend=None,
                      grid_pct=STRATEGY_LAB_GRID_PCT, num_levels=STRATEGY_LAB_GRID_LEVELS):
    """Real replay of the pasted proposal's "Automated Grid Bot" idea -
    a genuinely different mechanism from every other strategy in this
    file: instead of one directional position at a time, capital is split
    into `num_levels` real slices, buying a slice every time price closes
    grid_pct below the last reference level and selling the oldest open
    slice every time price closes grid_pct above it - matching the
    proposal's own description ("every time Bitcoin ticks down 1%, buy a
    tiny piece; every time it ticks up 1%, sell that piece").

    Real, stated simplifications (this is a simulation of the CORE
    mechanic, not Coinbase's actual grid-bot product):
    - Decided on each hourly candle's CLOSE only, same limitation as
      backtest_one_coin's own docstring already states for the baseline -
      a real grid bot watches price continuously, so this likely UNDER-
      counts how many real grid fills would actually occur.
    - The proposal claims a real grid bot pays only the lower 0.40% maker
      fee (limit orders resting in the book) rather than the 0.60% taker
      rate a market order pays - this simulation does NOT assume that
      more favorable rate. It reuses the exact same engine.ROUND_TRIP_FEE_RATE
      every other strategy in this file uses, for one honest reason: this
      codebase's own live trading engine places MARKET orders everywhere
      (place_market_buy/place_market_sell), and there's no already-
      validated real maker-fee assumption anywhere in this codebase to
      safely reuse instead. Giving the grid strategy a cheaper, unverified
      fee rate than every other strategy tested here would make this
      comparison structurally unfair in the grid strategy's own favor.
      If Coinbase's real current fee schedule is confirmed to differ
      (see the account owner's own pasted sourcing), this constant should
      be revisited codebase-wide, not just here.
    - num_levels caps how many concurrent real slices can be open at
      once, matching a real fixed capital split - a sustained one-
      directional move will fill every level and then simply stop buying
      (or selling) until price reverses, the same real constraint an
      actual grid bot has."""
    spend = spend if spend is not None and spend > 0 else SPEND
    slice_usd = spend / num_levels
    trades = []
    n = len(closes)
    if n < 2:
        return None
    # No ATR dependency here (unlike every other replay in this file) -
    # a grid only needs a single real prior close to anchor its first
    # reference level, so it doesn't skip a warm-up window the way the
    # ATR-based strategies have to.
    i = 1
    open_slices = []  # FIFO: [{entry, qty}]
    reference = closes[0]

    while i < n:
        price = closes[i]
        if price <= reference * (1 - grid_pct) and len(open_slices) < num_levels:
            open_slices.append({"entry": price, "qty": slice_usd / price})
            reference = price
        elif price >= reference * (1 + grid_pct) and open_slices:
            slot = open_slices.pop(0)
            gross = slot["qty"] * (price - slot["entry"])
            fee = slot["qty"] * (slot["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            trades.append(("GRID_CYCLE", gross - fee))
            reference = price
        i += 1

    # Real, honest mark-to-market for whatever slices are still open at
    # the end of the window - same "don't just discard an open position"
    # principle every other replay tool in this file already follows,
    # tagged distinctly so the caller can see how much of the total was
    # actually realized vs. still sitting open.
    final_price = closes[-1]
    for slot in open_slices:
        gross = slot["qty"] * (final_price - slot["entry"])
        trades.append(("OPEN_AT_WINDOW_END", gross))

    result = _summarize_strategy_trades(trades, spend)
    if result is not None:
        result["open_slices_at_end"] = len(open_slices)
    return result


def _replay_swing_trading(closes, highs, lows, spend=None,
                           lookback_hours=STRATEGY_LAB_SWING_LOOKBACK_HOURS,
                           target_pct=STRATEGY_LAB_SWING_TARGET_PCT,
                           stop_pct=STRATEGY_LAB_SWING_STOP_PCT,
                           max_hold_hours=STRATEGY_LAB_SWING_MAX_HOLD_HOURS):
    """Real replay of the pasted proposal's "Spot Swing Trading" idea -
    buy on a real pullback toward recent support, hold for days for a
    larger real move. "Support" is operationalized as the real rolling
    lookback_hours low (a concrete, defensible proxy for a technical
    support zone, not a claim this matches any specific chart pattern a
    human trader might draw) - entry requires price within 2% of that
    rolling low AND RSI confirming a genuine oversold pullback (reusing
    engine._rsi_from_closes, threshold 40, the same oversold convention
    prop_bot.py's own mean-reversion entry already uses), not just any
    dip. Exit at the proposal's own fixed target/stop, or a
    max_hold_hours backstop (the proposal's own outer "2 to 7 days"
    window) if neither fires."""
    spend = spend if spend is not None and spend > 0 else SPEND
    trades = []
    i = max(ATR_WINDOW, lookback_hours)
    n = len(closes)
    position = None

    while i < n:
        price = closes[i]
        if position is None:
            rolling_low = min(closes[i - lookback_hours:i + 1])
            near_support = price <= rolling_low * 1.02
            rsi = engine._rsi_from_closes(closes[:i + 1])
            if near_support and rsi is not None and rsi < 40:
                position = {
                    "entry": price, "qty": spend / price,
                    "target": price * (1 + target_pct), "stop": price * (1 - stop_pct),
                    "entry_i": i,
                }
            i += 1
            continue

        held_hours = i - position["entry_i"]
        exit_reason = None
        if price >= position["target"]:
            exit_reason = "TARGET"
        elif price <= position["stop"]:
            exit_reason = "STOP"
        elif held_hours >= max_hold_hours:
            exit_reason = "TIME"

        if exit_reason:
            gross = position["qty"] * (price - position["entry"])
            fee = position["qty"] * (position["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            trades.append((exit_reason, gross - fee))
            position = None
        i += 1

    return _summarize_strategy_trades(trades, spend)


def _summarize_strategy_trades(trades, spend):
    """Shared real summary math for the three strategy-lab replays above -
    identical shape to backtest_one_coin()'s own return dict so the
    dashboard can render all four (baseline included) through one table."""
    if not trades:
        return None
    total_pnl = sum(net for _, net in trades)
    wins = [net for _, net in trades if net > 0]
    return {
        "num_trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total_pnl,
        "roi_pct_of_spend": total_pnl / spend * 100,
        "avg_trade_pct": (total_pnl / len(trades)) / spend * 100,
        "spend_used": spend,
    }


STRATEGY_LAB_STRATEGIES = {
    "baseline": lambda closes, highs, lows, spend: backtest_one_coin(closes, highs, lows, spend=spend),
    "hourly_momentum": lambda closes, highs, lows, spend: _replay_hourly_momentum(closes, highs, lows, spend=spend),
    "grid_bot": lambda closes, highs, lows, spend: _replay_grid_bot(closes, highs, lows, spend=spend),
    "swing_trading": lambda closes, highs, lows, spend: _replay_swing_trading(closes, highs, lows, spend=spend),
}


async def run_strategy_lab_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading, never
    places an order. Direct answer to the account owner's own request
    ("I would like to try all of the options just to see what my options
    are... a b c d to see which one I like") after a pasted third-party
    proposal (Spot Swing Trading / Automated Grid Bot / Hourly Momentum
    Trading) that itself contained no real backtest, only illustrative
    arithmetic. Replays the EXISTING live baseline (backtest_one_coin,
    unchanged) alongside all three new strategies above, on the identical
    real historical Coinbase candles per coin, so all four are directly,
    fairly comparable on the same data.

    Real, honest limits stated plainly, not hidden:
    - grid_bot's own fee assumption may be too pessimistic relative to
      Coinbase's real grid-bot product specifically (see _replay_grid_bot's
      own docstring) - or this codebase's existing fee constant may be too
      OPTIMISTIC for every strategy here, since every real order this
      codebase places is a market/taker order, not the maker rate the
      pasted proposal assumed. Worth confirming Coinbase's actual current
      fee schedule directly rather than trusting either source blind.
    - None of these three are wired into live trading. Promoting any of
      them - even hourly_momentum, the closest to what's already live -
      would be a real, separate, deliberate decision once real evidence
      from this comparison exists, the same "evidence before any live
      change" rule every other strategy decision in this file already
      follows. grid_bot and swing_trading also don't fit the live branch
      engine's current single-position-per-branch design at all (a grid
      needs several real concurrent open slices; a multi-day swing hold
      would conflict with the tree's existing ~30s per-cycle exit checks)
      - going live with either would need real architecture work first,
      not just a promote button."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, _times = candles
            per_strategy = {
                name: fn(closes, highs, lows, None) for name, fn in STRATEGY_LAB_STRATEGIES.items()
            }
            return product_id, per_strategy, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    totals = {name: 0.0 for name in STRATEGY_LAB_STRATEGIES}
    trade_counts = {name: 0 for name in STRATEGY_LAB_STRATEGIES}
    win_counts = {name: 0 for name in STRATEGY_LAB_STRATEGIES}
    for product_id, per_strategy, skip_reason in outcomes:
        if per_strategy is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **per_strategy})
        for name, result in per_strategy.items():
            if result is None:
                continue
            totals[name] += result["total_pnl"]
            trade_counts[name] += result["num_trades"]
            win_counts[name] += round(result["win_rate"] / 100 * result["num_trades"])

    summary = {
        name: {
            "total_pnl": round(totals[name], 2),
            "num_trades": trade_counts[name],
            "win_rate": round(win_counts[name] / trade_counts[name] * 100, 1) if trade_counts[name] else None,
        }
        for name in STRATEGY_LAB_STRATEGIES
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]

    def _sort_key(row):
        best_result = row.get(best)
        return best_result["roi_pct_of_spend"] if best_result else -999.0
    comparison.sort(key=_sort_key, reverse=True)

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "summary": summary,
        "best_strategy": best,
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


STOP_HIT_REVERSAL_HOURS_FORWARD = 24
STOP_HIT_REVERSAL_TARGET_PCT = 0.02
STOP_HIT_REVERSAL_STOP_PCT = 0.02
STOP_HIT_REVERSAL_EVENT_LIMIT = 300

# Real, genuinely-recurring structural forced-exit reasons - the branch's
# own floor/drawdown-breach safety nets firing, distinct from a real
# price-based STOP HIT. Unlike PEAK PROFIT GIVEBACK/QUICK PROFIT (legacy
# exit types from the removed QUICK_PROFIT era - can never happen again,
# so a reversal test on them would answer a question about a strategy
# that no longer runs), these two CAN still happen live today, so a real
# "does price recover after this" test on them is genuinely actionable.
FORCED_EXIT_REASONS = ["BRANCH BREACH - forced exit", "EQUITY FLOOR BREACH - forced exit"]


async def _load_real_exit_events(exit_reasons, limit=STOP_HIT_REVERSAL_EVENT_LIMIT, hours_forward=STOP_HIT_REVERSAL_HOURS_FORWARD):
    """Real CryptoCoinTradeHistory rows whose exit_reason is exactly one
    of `exit_reasons` - e.g. ["STOP HIT"] for a genuine, price-driven
    hard-stop exit, or FORCED_EXIT_REASONS for the branch's own real
    floor/drawdown-breach safety nets firing. Deliberately exact-match,
    never a substring match, so this can never accidentally sweep in a
    real legacy exit type (PEAK PROFIT GIVEBACK/QUICK PROFIT, from the
    removed QUICK_PROFIT era) that shares no real relationship with what's
    being tested.

    Only returns events with at least `hours_forward` of real elapsed
    time since closed_at - an event too recent to have that much real
    history yet is skipped rather than scored on a truncated window,
    which would understate its real forward return."""
    cutoff = datetime.utcnow() - timedelta(hours=hours_forward)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CryptoCoinTradeHistory)
            .where(CryptoCoinTradeHistory.exit_reason.in_(exit_reasons))
            .where(CryptoCoinTradeHistory.closed_at <= cutoff)
            .order_by(CryptoCoinTradeHistory.closed_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


def _simulate_reversal_trade(closes, times, start_idx, entry_price, target_pct, stop_pct):
    """Real, simple hypothesis test: what if the tree had immediately
    bought back in at the real stop-loss's own exit price, right after
    getting stopped out? Walks forward from start_idx (the first real
    candle at or after the stop event) looking for the first real close
    that clears a modest real profit target, or a second real hard stop
    protecting the reversal trade itself, whichever comes first; if
    neither fires before the window (the candles passed in) runs out,
    marks-to-market against the real last close instead of leaving the
    hypothetical trade open forever. No fees modeled - stated plainly,
    same as the module's other single-position replay simplifications.

    Real bug fixed here: recovered_to_breakeven originally checked
    EVERY candle including the very first one in the window (the candle
    AT the stop event itself), whose real close is often at or extremely
    near the entry price by construction - falsely counting that as a
    genuine "recovery" before any real forward price movement had even
    happened. Now only counts a candle strictly AFTER the starting one -
    a real recovery has to happen from actual forward movement, not the
    coincidence of the first candle sharing (or nearly sharing) the
    entry price."""
    target_price = entry_price * (1 + target_pct)
    stop_price = entry_price * (1 - stop_pct)
    recovered_to_breakeven = False
    for offset, i in enumerate(range(start_idx, len(closes))):
        price = closes[i]
        if offset > 0 and price >= entry_price:
            recovered_to_breakeven = True
        if price >= target_price:
            return {"exit_reason": "TARGET", "exit_price": price, "pnl_pct": (price - entry_price) / entry_price, "recovered_to_breakeven": True}
        if price <= stop_price:
            return {"exit_reason": "STOP", "exit_price": price, "pnl_pct": (price - entry_price) / entry_price, "recovered_to_breakeven": recovered_to_breakeven}
    # Window ran out with neither hit - mark to the real last close.
    last_price = closes[-1]
    return {
        "exit_reason": "TIME", "exit_price": last_price, "pnl_pct": (last_price - entry_price) / entry_price,
        "recovered_to_breakeven": recovered_to_breakeven,
    }


async def run_stop_hit_reversal_backtest(
    hours_forward=STOP_HIT_REVERSAL_HOURS_FORWARD, target_pct=STOP_HIT_REVERSAL_TARGET_PCT,
    stop_pct=STOP_HIT_REVERSAL_STOP_PCT, event_limit=STOP_HIT_REVERSAL_EVENT_LIMIT, max_concurrent=6,
):
    """SHADOW-MODE, additive - never touches live trading, places no real
    order. Built directly from the account owner's own real question,
    right after the exit-reason breakdown surfaced that most of a real
    losing window's damage wasn't from real price-based stop-losses at
    all (1 real STOP HIT vs 7 legacy PEAK PROFIT GIVEBACK trades and 3
    structural BRANCH/EQUITY FLOOR BREACH forced exits in that specific
    window): "if we figure out a way to make money on it losing... we can
    make money off stops." Tests the real, honest version of that idea -
    does the real coin's price tend to recover after a real STOP HIT, and
    would a hypothetical "buy back in right after the stop" trade have
    been profitable - using the FULL real historical STOP HIT ledger
    (every coin, not just one rolling 20-trade window), not a guess.

    For every real STOP HIT event with enough real elapsed time since it
    closed, fetches that coin's real hourly candles from the event's own
    real closed_at through hours_forward hours later (grouped by
    product_id and fetched once per coin, sliced per event via
    _closest_close_at_or_before - the same real time-alignment technique
    run_btc_relative_strength_comparison() already uses), then:
      - the real forward price return at the end of the window
      - whether the real price ever recovered back to the stop's own
        real exit price within the window
      - a real, simple hypothetical "buy back at the stop-exit price,
        exit at a modest target or a second hard stop" trade
        (_simulate_reversal_trade above)

    Real, honest limitations stated plainly: no fees are modeled on the
    hypothetical reversal trades (a real one would need to clear the
    real round-trip fee on top of target_pct to be a genuine profit);
    this never accounts for whether real free cash would actually have
    been available to take the hypothetical trade; and coins with very
    few real STOP HIT events don't carry the same statistical weight as
    POL-USD's real 80+ trade history. This is diagnostic only - it never
    reads into any live trading decision on its own."""
    events = await _load_real_exit_events(["STOP HIT"], limit=event_limit, hours_forward=hours_forward)
    return await _run_reversal_backtest_for_events(events, hours_forward, target_pct, stop_pct, max_concurrent)


async def run_forced_exit_reversal_backtest(
    hours_forward=STOP_HIT_REVERSAL_HOURS_FORWARD, target_pct=STOP_HIT_REVERSAL_TARGET_PCT,
    stop_pct=STOP_HIT_REVERSAL_STOP_PCT, event_limit=STOP_HIT_REVERSAL_EVENT_LIMIT, max_concurrent=6,
):
    """SHADOW-MODE, additive - never touches live trading, places no real
    order. The direct follow-up to run_stop_hit_reversal_backtest() above,
    per the account owner's own real follow-up question after seeing that
    a real losing window was mostly driven by structural forced exits
    (BRANCH BREACH/EQUITY FLOOR BREACH - a branch's own floor/drawdown-
    breach safety nets firing) rather than genuine STOP HIT price-stops:
    "how is there a way that we can make money off a system like that."

    Deliberately does NOT test the OTHER real exit type in that same
    losing window - PEAK PROFIT GIVEBACK (and QUICK_PROFIT wins) - since
    both are legacy exit types from the removed QUICK_PROFIT era that can
    never happen again on the live bot; a reversal test on a dead exit
    rule would answer a question about a strategy that no longer runs,
    not something actionable today. BRANCH BREACH/EQUITY FLOOR BREACH are
    real, still-live exit types (see FORCED_EXIT_REASONS above), so this
    is a genuinely actionable real test.

    Identical real methodology to run_stop_hit_reversal_backtest() -
    same _simulate_reversal_trade hypothesis, same real historical
    candle fetch, same honest limitations (no fees modeled, real cash
    availability not checked) - only the source exit_reason filter
    differs. See run_stop_hit_reversal_backtest()'s own docstring for
    the full real methodology."""
    events = await _load_real_exit_events(FORCED_EXIT_REASONS, limit=event_limit, hours_forward=hours_forward)
    return await _run_reversal_backtest_for_events(events, hours_forward, target_pct, stop_pct, max_concurrent)


async def _run_reversal_backtest_for_events(events, hours_forward, target_pct, stop_pct, max_concurrent):
    """Shared real reversal-scoring core for both
    run_stop_hit_reversal_backtest() and run_forced_exit_reversal_backtest()
    above - fetches real candles per coin and scores every real event
    identically regardless of which exit_reason(s) produced it."""
    if not events:
        return {"events_tested": 0, "events_skipped_no_data": 0, "per_event": [], "summary": None}

    by_product = {}
    for ev in events:
        by_product.setdefault(ev.product_id, []).append(ev)

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _fetch_for_product(session, product_id, product_events):
        earliest = min(e.closed_at for e in product_events)
        start = earliest.replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        async with semaphore:
            candles = await fetch_candles_window(session, product_id, start, end, min_candles=2)
        return product_id, candles

    # One real shared session for every coin's fetch, semaphore-limited -
    # same pattern every other multi-coin comparison in this file already
    # uses (see run_trailing_stop_pct_sweep_comparison above).
    async with aiohttp.ClientSession() as session:
        fetch_results = await asyncio.gather(*(_fetch_for_product(session, pid, evs) for pid, evs in by_product.items()))

    per_event = []
    skipped_no_data = 0
    for product_id, candles in fetch_results:
        if candles is None:
            skipped_no_data += len(by_product[product_id])
            continue
        closes, _highs, _lows, times = candles
        for ev in by_product[product_id]:
            event_time = int(ev.closed_at.replace(tzinfo=timezone.utc).timestamp())
            start_idx = bisect.bisect_left(times, event_time)
            if start_idx >= len(closes):
                skipped_no_data += 1
                continue
            end_time = event_time + hours_forward * 3600
            end_idx = bisect.bisect_right(times, end_time)
            window_closes = closes[start_idx:end_idx] or [closes[start_idx]]
            entry_price = ev.exit_price
            if not entry_price:
                skipped_no_data += 1
                continue
            forward_return_pct = (window_closes[-1] - entry_price) / entry_price if entry_price else 0.0
            # Real bug fixed here: this used to compute recovery separately
            # as any(c >= entry_price for c in window_closes), which
            # (like the same bug inside _simulate_reversal_trade, now
            # fixed) counted the very first candle in the window - the one
            # AT the stop event itself, whose real close is often at or
            # extremely near the entry price by construction - as a false
            # "recovery" before any genuine forward movement happened.
            # Now uses _simulate_reversal_trade's own fixed, single
            # computation instead of a second, inconsistent one.
            sim = _simulate_reversal_trade(window_closes, times[start_idx:end_idx] or [times[start_idx]], 0, entry_price, target_pct, stop_pct)
            recovered = sim["recovered_to_breakeven"]
            per_event.append({
                "product_id": product_id,
                "original_stop_closed_at": ev.closed_at.isoformat() + "Z",
                "original_stop_pnl": ev.pnl,
                "stop_exit_price": entry_price,
                "forward_return_pct": round(forward_return_pct * 100, 2),
                "recovered_to_breakeven": recovered,
                "reversal_trade_pnl_pct": round(sim["pnl_pct"] * 100, 2),
                "reversal_trade_exit_reason": sim["exit_reason"],
            })

    if not per_event:
        return {"events_tested": 0, "events_skipped_no_data": skipped_no_data, "per_event": [], "summary": None}

    recovered_count = sum(1 for e in per_event if e["recovered_to_breakeven"])
    reversal_wins = sum(1 for e in per_event if e["reversal_trade_pnl_pct"] > 0)
    avg_forward_return_pct = round(sum(e["forward_return_pct"] for e in per_event) / len(per_event), 2)
    avg_reversal_pnl_pct = round(sum(e["reversal_trade_pnl_pct"] for e in per_event) / len(per_event), 2)

    per_event.sort(key=lambda e: e["reversal_trade_pnl_pct"], reverse=True)

    return {
        "events_tested": len(per_event),
        "events_skipped_no_data": skipped_no_data,
        "hours_forward": hours_forward,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "per_event": per_event,
        "summary": {
            "recovered_to_breakeven_count": recovered_count,
            "recovered_to_breakeven_rate_pct": round(100.0 * recovered_count / len(per_event), 1),
            "avg_forward_return_pct": avg_forward_return_pct,
            "reversal_trade_win_count": reversal_wins,
            "reversal_trade_win_rate_pct": round(100.0 * reversal_wins / len(per_event), 1),
            "avg_reversal_trade_pnl_pct": avg_reversal_pnl_pct,
        },
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
