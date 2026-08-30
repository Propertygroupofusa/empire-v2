"""
SHADOW-MODE BACKTEST - offline tool only. Does not touch live trading,
is not imported by any live bot, and places no real orders.

Built in response to a real question: was the family-tree bot's coin
SELECTION (find_most_volatile_unclaimed_coin in crypto_family_tree_bot.py)
actually buying at bad moments? That function's only timing check is
`closes[-1] > closes[0]` over a ~25-hour window - a coarse "is it up
overall" check with no sense of whether the move already happened and
the coin is now extended. This script tests that suspicion the honest
way: replay the bot's OWN real target/stop/breakeven/trailing-stop rules
(imported directly from crypto_btc_compound_bot.py and
crypto_family_tree_bot.py - not reimplemented) against each coin's real
historical price data, and rank coins by what that strategy would
actually have returned on each one.

`backtest_one_coin()`'s exit mechanics were updated to match the live
bot's real exit_mode="trailing_stop" rule (STOP with a breakeven ratchet,
then a real trailing stop once target is first reached) after it was
found to still be replaying a retired TARGET/STOP/GIVEBACK rule - see
`backtest_one_coin()`'s own docstring for the full real history.

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
import crypto_grid_bot as grid_engine  # only for its own real constants (TARGET_NET_MARGIN_PCT etc.) - no circular import, crypto_grid_bot never imports this module
from crypto_family_tree_bot import COIN_FAMILY_TREE, BREAKEVEN_TRIGGER_PCT
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


async def fetch_candles_window(session, product_id, start, end, min_candles=ATR_WINDOW + 5, last_error_out=None):
    """Paginated pull of real Coinbase historical candles (public,
    unauthenticated endpoint - same one the live bot's own _fetch_candles
    uses) between two explicit real UTC datetimes. Factored out of
    fetch_historical_candles() below so a caller anchored on a real past
    EVENT (not "the last N days from right now") can reuse the identical
    real pagination logic - see run_stop_hit_reversal_backtest(), which
    needs a real forward window starting at each real stop-loss's own
    closed_at, not the module's usual now-minus-days window.

    Real bug found from the account owner's own observation - Strategy
    Lab was skipping 31 of 36 real coins with a blanket "not enough
    historical data" message. Reading this function's own original code
    turned up the real, likely cause: with several coins fetched
    CONCURRENTLY (see max_concurrent on every real caller) and this
    function's own real 3-page pagination for a 30-day window, a burst
    of simultaneous requests to Coinbase's real public, unauthenticated
    candles endpoint is a real, plausible way to trip its rate limit -
    and the old code treated ANY non-200 response (silently, including a
    real 429) as "give up on this page, return whatever came back so
    far," which for a well-established coin like ETH-USD or LTC-USD is
    almost certainly a rate-limit artifact, not genuinely missing real
    history. Not yet confirmed live (this sandbox has no live network
    access to Coinbase to reproduce it directly) - real evidence needs
    the account owner re-running Strategy Lab after this ships.

    Fixed two ways: a real 429 now gets a bounded number of retries with
    a short real backoff (a genuine rate limit typically clears in well
    under a second) before giving up on that page - a non-429 failure
    (e.g. a real 404 for an invalid product) is NOT retried, since
    retrying an identical request could never fix it. And the real
    reason a coin's fetch ultimately failed is now captured into
    last_error_out[product_id] when a dict is passed in (optional,
    defaults to None - every existing caller that doesn't pass this is
    completely unaffected), so a future skip can say WHY instead of a
    blanket "not enough historical data" hiding a real rate-limit
    problem."""
    all_candles = []
    cursor = start
    last_error = None
    while cursor < end:
        page_end = min(cursor + timedelta(seconds=GRANULARITY_SECONDS * 299), end)
        url = (
            f"https://api.exchange.coinbase.com/products/{product_id}/candles"
            f"?granularity={GRANULARITY_SECONDS}&start={cursor.isoformat()}&end={page_end.isoformat()}"
        )
        page_data = None
        for attempt in range(3):
            try:
                async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
                    if r.status == 429:
                        last_error = f"HTTP 429 rate limited"
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if r.status != 200:
                        last_error = f"HTTP {r.status}"
                        break
                    page_data = await r.json()
                    last_error = None
                    break
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:100]}"
                print(f"  [{product_id}] fetch error for page starting {cursor.date()}: {e}")
                await asyncio.sleep(0.5 * (attempt + 1))
        if page_data:
            all_candles.extend(page_data)
        elif last_error:
            break  # this page never came through even after retries - stop rather than silently pretend the window is complete
        cursor = page_end
        await asyncio.sleep(0.15)  # be polite to the public endpoint

    if len(all_candles) < min_candles:
        if last_error_out is not None:
            last_error_out[product_id] = last_error or f"only {len(all_candles)} of {min_candles} required real candles came back"
        return None
    all_candles.sort(key=lambda c: c[0])  # oldest first
    closes = [float(c[4]) for c in all_candles]
    highs = [float(c[2]) for c in all_candles]
    lows = [float(c[1]) for c in all_candles]
    times = [int(c[0]) for c in all_candles]
    return closes, highs, lows, times


async def fetch_historical_candles(session, product_id, days=BACKTEST_DAYS, last_error_out=None):
    """Real, unauthenticated Coinbase candles for the last `days` days
    from right now - the module's original, most common shape. Now a thin
    wrapper around fetch_candles_window() (see above), unchanged in
    return shape/behavior for every existing caller. last_error_out is
    optional and passed straight through - see fetch_candles_window's own
    docstring."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return await fetch_candles_window(session, product_id, start, end, last_error_out=last_error_out)


def backtest_one_coin(closes, highs, lows, entry_gate=None, spend=None, trail_pct=None):
    """Replays the REAL live rules candle-by-candle: a real ATR-based
    entry, a hard STOP-LOSS with a breakeven ratchet, and - once price
    first reaches the real ATR-based target - a real percentage TRAILING
    STOP behind the highest price seen since entry. This is the same exit
    mechanics as crypto_family_tree_bot.py's own live run_branch_cycle()
    under exit_mode="trailing_stop", the ONLY live exit mode today
    (QUICK_PROFIT and the old GIVEBACK-cap exit were both removed
    outright earlier this session - see CLAUDE.md). Reaching TARGET is
    never an immediate exit here, only arms the trail - exactly matching
    the live bot.

    THIS WAS PREVIOUSLY STALE, fixed after the account owner asked
    directly why a real 1,674-trade "Baseline (A)" sample on Strategy Lab
    showed -$1,301.59: until this fix, this function replayed a retired
    TARGET/STOP/GIVEBACK rule (and its own GIVEBACK check never even had
    the fee-net-positive guard the live giveback exit was given before
    being removed entirely) - meaning "Baseline (A)", and every
    CryptoBacktestRun row this function's real caller (run_full_backtest)
    persists (the same table the automatic coin-exclusion layer and the
    top-15 ROI rotation both read to decide what the live tree can even
    trade), were being computed against an exit rule the live bot hasn't
    run in a while. Some real, unknown share of that real loss was an
    artifact of testing dead code, not a reflection of the bot as it
    actually trades today - fixed by request, not guessed at.

    Returns a dict of results, or None if there wasn't enough data to
    trade at all.

    `spend` (optional) overrides the fixed SPEND module constant for this
    one coin - used by run_full_backtest_with_real_allocations() below to
    simulate each coin's REAL current branch dollars instead of the flat
    $150 every coin gets by default. `spend=None` (the default, every
    existing caller) falls back to the module-level SPEND constant.

    entry_gate, if given, is called as entry_gate(i) at each flat decision
    point (i = index into closes/highs/lows) and must return True to allow
    a new entry there - used by run_btc_relative_strength_comparison()
    and run_higher_tf_trend_comparison() below to add a real entry-timing
    filter without duplicating this whole replay loop. None (the default)
    means always enter the moment flat.

    `trail_pct` (optional) overrides the module's own TRAILING_STOP_PCT
    default (2.5%) - the same real, live-tunable width
    run_trailing_stop_pct_sweep_comparison() already tests candidates
    for. This module has no live access to read whatever width is
    currently promoted on the real deployed dashboard, so the module
    constant is a reasonable default, not a guaranteed match to the exact
    live value at any given moment."""
    # `spend <= 0` (not just None) falls back to the default too - real,
    # confirmed live bug: a coin whose only real branch is sitting at a
    # genuine $0.00 balance (POL-USD, SOL-USD in production) passed
    # spend=0.0 straight through, which isn't None so the check below
    # never caught it, and total_pnl / spend crashed with a real
    # ZeroDivisionError the instant the replay produced even one trade -
    # surfaced as a raw HTTP 500 on "Run Backtest With Real Allocations".
    spend = spend if spend is not None and spend > 0 else SPEND
    trail_pct = trail_pct if trail_pct is not None and trail_pct > 0 else TRAILING_STOP_PCT
    trades = []
    entries_skipped_by_gate = 0
    i = ATR_WINDOW
    n = len(closes)

    position = None  # dict: entry, qty, target, stop, peak_price, profit_activated

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
                "peak_price": price,
                "profit_activated": False,
            }
            i += 1
            continue

        if price > position["peak_price"]:
            position["peak_price"] = price

        # Real breakeven ratchet (same trigger the live bot uses).
        if position["stop"] < position["entry"] and price >= position["entry"] * (1 + BREAKEVEN_TRIGGER_PCT):
            position["stop"] = position["entry"]

        # Real trailing-stop mechanics, identical to the live bot: reaching
        # target only ARMS the trail, it never exits by itself.
        if not position["profit_activated"] and price >= position["target"]:
            position["profit_activated"] = True
        if position["profit_activated"]:
            trailing_stop_price = position["peak_price"] * (1 - trail_pct)
            effective_stop = max(position["stop"], trailing_stop_price)
        else:
            effective_stop = position["stop"]

        exit_reason = None
        if price <= effective_stop:
            exit_reason = "TRAILING STOP" if position["profit_activated"] else "STOP"

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


PARTIAL_EXIT_FRACTION = 0.5  # sell half at the first real target, per the account owner's own explicit request ("take most of your profits... take partials... and trailing the stop") - a common real professional convention (scale out, let the rest run), not a number this codebase already had


def _replay_partial_then_trail(closes, highs, lows, spend=None, trail_pct=None, partial_fraction=PARTIAL_EXIT_FRACTION):
    """Real replay of the account owner's own proposal: instead of
    trailing the WHOLE position once it reaches the real ATR-based target
    (what mode="trailing_stop" in _replay_with_exit_mode already does),
    sell `partial_fraction` of it right at target - a real, separate
    order that pays its own real round-trip fee - and only trail the real
    REMAINDER behind the peak from there. The hard stop-loss (with the
    same real breakeven ratchet every other replay in this file uses)
    still protects the full remaining qty at every point, whether or not
    a partial has fired yet - a partial exit only ever adds a second,
    tighter protection on top once armed, never removes the first.

    Deliberately a separate function rather than a third mode inside
    _replay_with_exit_mode() - that function's trade-tracking assumes
    exactly one closing leg per position, and forcing a two-leg
    (partial + remainder) position through that shape would have meant
    restructuring already-tested code. Mirrors _replay_grid_bot() and
    friends instead: its own separate replay, same real entry/breakeven/
    fee mechanics, own trade list.

    Each real leg (a partial sale, a remainder sale, or a real mark-to-
    market at the window's end) is its own row in the returned trade
    list - num_trades can be higher than a pure single-exit strategy's
    on the identical data purely because a winning position now closes
    in two real pieces instead of one, not because more positions were
    opened. Same real simplifications as _replay_with_exit_mode: decided
    on each hourly candle's close only, and a position (or a still-
    trailing remainder) still open when the window ends is marked to the
    real last close rather than silently dropped, so a partial that fired
    but never got a chance to finish trailing isn't invisible here."""
    spend = spend if spend is not None and spend > 0 else SPEND
    trail_pct = trail_pct if trail_pct is not None and trail_pct > 0 else TRAILING_STOP_PCT
    trades = []
    i = ATR_WINDOW
    n = len(closes)
    position = None  # entry, qty (remaining), target, stop, peak_price, partial_taken

    def _net_leg(qty, entry, exit_price):
        gross = qty * (exit_price - entry)
        fee = qty * (entry + exit_price) * (engine.ROUND_TRIP_FEE_RATE / 2)
        return gross - fee

    while i < n:
        price = closes[i]
        window_closes = closes[max(0, i - ATR_WINDOW):i + 1]
        window_highs = highs[max(0, i - ATR_WINDOW):i + 1]
        window_lows = lows[max(0, i - ATR_WINDOW):i + 1]
        atr_pct = engine._atr_pct_from_candles(window_closes, window_highs, window_lows)

        if position is None:
            target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(spend, atr_pct))
            qty = spend / price
            position = {
                "entry": price, "qty": qty,
                "target": price * (1 + target_pct),
                "stop": price * (1 - engine.STOP_LOSS_PCT),
                "peak_price": price,
                "partial_taken": False,
            }
            i += 1
            continue

        if price > position["peak_price"]:
            position["peak_price"] = price

        # Real breakeven ratchet - identical to every other replay here.
        if position["stop"] < position["entry"] and price >= position["entry"] * (1 + BREAKEVEN_TRIGGER_PCT):
            position["stop"] = position["entry"]

        # The real hard stop always protects the FULL remaining qty,
        # partial already taken or not - checked before anything else.
        if price <= position["stop"]:
            trades.append(("STOP", _net_leg(position["qty"], position["entry"], price)))
            position = None
            i += 1
            continue

        if not position["partial_taken"]:
            if price >= position["target"]:
                partial_qty = position["qty"] * partial_fraction
                trades.append(("PARTIAL_TARGET", _net_leg(partial_qty, position["entry"], price)))
                position["qty"] -= partial_qty
                position["partial_taken"] = True
        else:
            trailing_stop_price = position["peak_price"] * (1 - trail_pct)
            effective_stop = max(position["stop"], trailing_stop_price)
            if price <= effective_stop:
                trades.append(("TRAIL_REMAINDER", _net_leg(position["qty"], position["entry"], price)))
                position = None
        i += 1

    if position is not None:
        trades.append(("OPEN_AT_WINDOW_END", _net_leg(position["qty"], position["entry"], closes[-1])))

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


async def run_partial_exit_vs_full_trail_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading,
    places no real order. Direct answer to the account owner's own real
    proposal: "take most of your profits... take partials... and
    trailing the stop" - does selling a real partial at the first ATR-
    based target and trailing only the real remainder actually make more
    money than the live rule (trail the WHOLE position, one exit,
    mode="trailing_stop" in _replay_with_exit_mode)? Runs BOTH real exit
    philosophies against the identical real historical candles for every
    coin - same real entry, same real hard stop/breakeven ratchet, same
    real fee on every leg - so the comparison is fair. Never wired into
    live trading; this only informs whether it's worth doing so."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _one(product_id):
        async with semaphore:
            candles = await fetch_historical_candles(session, product_id, days=days)
        if candles is None:
            return product_id, None, "not enough historical data"
        closes, highs, lows, _times = candles
        full_trail = _replay_with_exit_mode(closes, highs, lows, mode="trailing_stop")
        partial_then_trail = _replay_partial_then_trail(closes, highs, lows)
        return product_id, {"full_trail": full_trail, "partial_then_trail": partial_then_trail}, None

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

    full_trail_wins = 0
    partial_wins = 0
    for row in comparison:
        ft_pnl = row["full_trail"]["total_pnl"] if row["full_trail"] else 0.0
        pt_pnl = row["partial_then_trail"]["total_pnl"] if row["partial_then_trail"] else 0.0
        if ft_pnl > pt_pnl:
            full_trail_wins += 1
        elif pt_pnl > ft_pnl:
            partial_wins += 1

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "trailing_stop_pct": TRAILING_STOP_PCT,
        "partial_exit_fraction": PARTIAL_EXIT_FRACTION,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "totals": {
            "full_trail_total_pnl": round(_total("full_trail"), 2),
            "partial_then_trail_total_pnl": round(_total("partial_then_trail"), 2),
            "full_trail_coins_won": full_trail_wins,
            "partial_then_trail_coins_won": partial_wins,
        },
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


SR_LOOKBACK_HOURS = 72  # 3 real days of hourly candles - a real, stated proxy for "recent chart structure" on the 1hr timeframe requested
SR_RSI_OVERSOLD = 30  # TradingView's own classic RSI threshold, per the account owner's explicit request - deliberately distinct from this codebase's other real RSI conventions (engine.ENTRY_MAX_RSI=65 for overbought, mean-reversion's own RSI<40), tested here as its own real hypothesis rather than silently substituted for either
SR_SUPPORT_PROXIMITY_PCT = 0.02  # same real 2% tolerance _replay_swing_trading's own "near_support" check already uses (price <= rolling_low * 1.02) - reused for consistency, not a new arbitrary number


def _find_support_resistance(closes, i, lookback_hours=SR_LOOKBACK_HOURS):
    """Real, concrete proxy for the two liquidity zones the account owner
    asked about directly - "is there any support area, check for
    previous high or previous breakdown area, any resistance zone" -
    operationalized the same honest way _replay_swing_trading() already
    treats "support" elsewhere in this file (a real rolling low), rather
    than inventing a vaguer, unvalidatable notion of "structure."
    Support = the real lowest low in the lookback window (a previous
    low / previous breakdown level); resistance = the real highest high
    in the same window (a previous high). Returns (support, resistance),
    or (None, None) if there isn't yet lookback_hours of real history."""
    if i < lookback_hours:
        return None, None
    window = closes[i - lookback_hours:i + 1]
    return min(window), max(window)


def _make_support_resistance_gate(closes, rsi_oversold=SR_RSI_OVERSOLD, lookback_hours=SR_LOOKBACK_HOURS, proximity_pct=SR_SUPPORT_PROXIMITY_PCT):
    """Returns an entry_gate(i) closure for backtest_one_coin(), testing
    the account owner's own real proposal directly: RSI 70/30 on the 1hr
    chart, plus real structure/levels, to see if it "boost[s] the
    accuracy." Only allows a new entry when BOTH are real: RSI(14,
    hourly) is oversold (below rsi_oversold) AND price is sitting within
    proximity_pct of a real recent support zone (see
    _find_support_resistance above) - buying an oversold dip that's also
    sitting at a real historical floor, not just any oversold dip
    wherever price happens to be right now. Free pass (True) until
    there's enough real history for both the RSI and the lookback window
    - never blocks on missing data, the same rule every other gate in
    this file already follows."""
    def gate(i):
        if i < lookback_hours:
            return True
        rsi = engine._rsi_from_closes(closes[:i + 1])
        if rsi is None:
            return True  # not enough real history for RSI yet - don't block on missing data
        if rsi >= rsi_oversold:
            return False  # not genuinely oversold - the core RSI signal isn't there
        support, _resistance = _find_support_resistance(closes, i, lookback_hours)
        if support is None:
            return True  # not enough real history for the lookback window yet
        return closes[i] <= support * (1 + proximity_pct)
    return gate


async def run_support_resistance_comparison(coins=None, days=BACKTEST_DAYS, rsi_oversold=SR_RSI_OVERSOLD,
                                              lookback_hours=SR_LOOKBACK_HOURS, proximity_pct=SR_SUPPORT_PROXIMITY_PCT,
                                              max_concurrent=6):
    """SHADOW-MODE, additive comparison - same pattern as
    run_higher_tf_trend_comparison/run_btc_relative_strength_comparison
    above. Direct answer to the account owner's own real proposal (RSI
    70/30 on the 1hr chart plus real support/resistance structure -
    "should help boost the accuracy by 20%"): TESTS that claim against
    real historical data rather than assuming it, the same "evidence
    before any live change" rule every other strategy question in this
    file already follows. Runs the exact same real target/stop/breakeven/
    trailing-stop replay twice per coin on identical real historical
    hourly candles - baseline (unfiltered) vs the new RSI(30)+support-zone
    gate - so the two are directly, fairly comparable. Never wired into
    live trading; this only informs whether it's worth doing so."""
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
            gate = _make_support_resistance_gate(closes, rsi_oversold=rsi_oversold, lookback_hours=lookback_hours, proximity_pct=proximity_pct)
            filtered = backtest_one_coin(closes, highs, lows, entry_gate=gate)
            return product_id, {"baseline": baseline, "with_sr_filter": filtered}, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})

    def _sort_key(row):
        filtered = row["with_sr_filter"]
        return filtered["roi_pct_of_spend"] if filtered else -999.0
    comparison.sort(key=_sort_key, reverse=True)

    return {
        "backtest_days": days,
        "rsi_oversold": rsi_oversold,
        "lookback_hours": lookback_hours,
        "proximity_pct": round(proximity_pct * 100, 1),
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


async def run_strategy_lab_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=3):
    """SHADOW-MODE, additive comparison - never touches live trading, never
    places an order. Direct answer to the account owner's own request
    ("I would like to try all of the options just to see what my options
    are... a b c d to see which one I like") after a pasted third-party
    proposal (Spot Swing Trading / Automated Grid Bot / Hourly Momentum
    Trading) that itself contained no real backtest, only illustrative
    arithmetic. Replays the EXISTING live baseline (backtest_one_coin)
    alongside all three new strategies above, on the identical real
    historical Coinbase candles per coin, so all four are directly, fairly
    comparable on the same data.

    Real, honest limits stated plainly, not hidden:
    - "Baseline (A)" previously replayed a RETIRED exit rule
      (TARGET/STOP/GIVEBACK) instead of the live bot's real
      exit_mode="trailing_stop" - found and fixed after a real
      1,674-trade sample showed -$1,301.59 and the account owner asked
      why. backtest_one_coin() now matches the live rule; see its own
      docstring for the full real history. Not yet confirmed against a
      fresh real run on the deployed dashboard.
    - Real 31-of-36 coins skipped in a real live run, all reporting a
      blanket "not enough historical data" - traced to fetch_candles_window's
      own old behavior of silently giving up on ANY non-200 response
      (very plausibly a real 429 rate-limit from several coins' candle
      fetches firing at once against Coinbase's public endpoint, not
      genuinely missing history for well-established coins). Fixed there
      with a real bounded retry on 429 specifically, and max_concurrent
      here lowered from 6 to 3 to reduce how many real simultaneous
      requests this tool fires at once. skipped[].reason now reports the
      real captured cause (e.g. "HTTP 429 rate limited") instead of the
      old generic message, so if coins are still being skipped after
      this, the real reason is visible instead of guessed at again. Not
      yet confirmed live - needs a real re-run to see if the skip count
      actually drops.
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
    last_errors = {}
    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days, last_error_out=last_errors)
            if candles is None:
                reason = last_errors.get(product_id, "not enough historical data")
                return product_id, None, reason
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


def _replay_grid_bot_with_drawdown_breaker(closes, highs, lows, spend=None,
                                            grid_pct=STRATEGY_LAB_GRID_PCT, num_levels=STRATEGY_LAB_GRID_LEVELS,
                                            drawdown_pct=None):
    """Real replay of the live crypto_grid_bot.py's own drawdown-breaker
    mechanism - a genuinely different function from _replay_grid_bot
    above (not a parameterized variant of it), since testing a breaker
    means tracking real equity/peak/drawdown through the whole replay,
    which the baseline grid replay has no reason to do on its own.

    `drawdown_pct=None` replays with NO breaker at all (the real
    baseline - identical trading behavior to _replay_grid_bot, just
    computed through this function's own equity-tracking loop so the
    "no breaker" candidate is directly, fairly comparable to every real
    threshold candidate on identical code paths). A real float
    (e.g. 0.25) pauses new slice-opening once real equity - allocated
    cash-equivalent basis plus real unrealized P&L across every
    currently-open slice, the EXACT SAME formula
    crypto_grid_bot._grid_branch_real_equity() itself uses - drops that
    fraction below its own real running peak. An existing open slice is
    NEVER blocked from selling by this - matches the live bot's own
    "pause new entries only, never force-sell a healthy position"
    philosophy exactly."""
    spend = spend if spend is not None and spend > 0 else SPEND
    slice_usd = spend / num_levels
    trades = []
    n = len(closes)
    if n < 2:
        return None
    i = 1
    open_slices = []  # FIFO: [{entry, qty}]
    reference = closes[0]
    allocated = spend  # the real cost-basis figure crypto_grid_bot.py's own allocated_usd tracks
    peak_equity = spend
    breaches = 0
    was_breached = False

    while i < n:
        price = closes[i]
        unrealized = sum(s["qty"] * (price - s["entry"]) for s in open_slices)
        equity = allocated + unrealized
        if equity > peak_equity:
            peak_equity = equity
        breached = drawdown_pct is not None and peak_equity > 0 and (peak_equity - equity) / peak_equity >= drawdown_pct
        if breached and not was_breached:
            breaches += 1
        was_breached = breached

        if not breached and price <= reference * (1 - grid_pct) and len(open_slices) < num_levels:
            open_slices.append({"entry": price, "qty": slice_usd / price})
            reference = price
        elif price >= reference * (1 + grid_pct) and open_slices:
            slot = open_slices.pop(0)
            gross = slot["qty"] * (price - slot["entry"])
            fee = slot["qty"] * (slot["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
            pnl = gross - fee
            trades.append(("GRID_CYCLE", pnl))
            allocated += pnl  # matches the live bot's own allocated_usd += pnl on every real sell
            reference = price
        i += 1

    final_price = closes[-1]
    for slot in open_slices:
        gross = slot["qty"] * (final_price - slot["entry"])
        trades.append(("OPEN_AT_WINDOW_END", gross))

    result = _summarize_strategy_trades(trades, spend)
    if result is not None:
        result["open_slices_at_end"] = len(open_slices)
        result["real_breach_count"] = breaches
        result["was_breached_at_window_end"] = was_breached
    return result


GRID_DRAWDOWN_BREAKER_CANDIDATES = [None, 0.15, grid_engine.GRID_DRAWDOWN_BREAKER_PCT, 0.40]


async def run_grid_drawdown_breaker_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6, candidates=None):
    """SHADOW-MODE, additive comparison - never touches live trading,
    never places an order. Direct answer to "does Grid Bot's new real
    drawdown breaker actually help, or does it just cut off branches
    that would have recovered on their own": replays every candidate
    threshold (plus a real no-breaker baseline) against the identical
    real historical Coinbase candles per coin, via
    _replay_grid_bot_with_drawdown_breaker above - never the live-bot
    code path itself, but the exact same real equity/peak/drawdown
    math it uses.

    grid_engine.GRID_DRAWDOWN_BREAKER_PCT (today's real live default,
    25%) is always included in `candidates` even if the caller only
    passes their own custom list, so the live default is always directly
    comparable against whatever else is being tested."""
    coins = coins or COIN_FAMILY_TREE
    candidates = list(candidates) if candidates else list(GRID_DRAWDOWN_BREAKER_CANDIDATES)
    if grid_engine.GRID_DRAWDOWN_BREAKER_PCT not in candidates:
        candidates.append(grid_engine.GRID_DRAWDOWN_BREAKER_PCT)
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, _times = candles
            per_candidate = {
                _candidate_label(c): _replay_grid_bot_with_drawdown_breaker(closes, highs, lows, drawdown_pct=c)
                for c in candidates
            }
            return product_id, per_candidate, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    labels = [_candidate_label(c) for c in candidates]
    totals = {label: 0.0 for label in labels}
    trade_counts = {label: 0 for label in labels}
    breach_counts = {label: 0 for label in labels}
    for product_id, per_candidate, skip_reason in outcomes:
        if per_candidate is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **per_candidate})
        for label, result in per_candidate.items():
            if result is None:
                continue
            totals[label] += result["total_pnl"]
            trade_counts[label] += result["num_trades"]
            breach_counts[label] += result.get("real_breach_count", 0)

    summary = {
        label: {
            "total_pnl": round(totals[label], 2),
            "num_trades": trade_counts[label],
            "real_breach_count": breach_counts[label],
        }
        for label in labels
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "candidates_tested": labels,
        "live_default_label": _candidate_label(grid_engine.GRID_DRAWDOWN_BREAKER_PCT),
        "skipped": skipped,
        "summary": summary,
        "best_candidate": best,
        "comparison": comparison,
    }


def _candidate_label(drawdown_pct):
    return "no_breaker" if drawdown_pct is None else f"{drawdown_pct*100:.0f}pct"


# Real, publicly-documented Coinbase Advanced Trade taker-fee tiers by
# 30-day trailing volume (base -> $10K -> $50K -> $100K -> $1M),
# expressed as a RATIO against the base tier - deliberately not
# hardcoded absolute fee percentages, so this backtest stays anchored to
# this codebase's own existing engine.ROUND_TRIP_FEE_RATE assumption
# (0.8% round trip / 0.4% each way) rather than silently introducing a
# second, different fee number nothing else in this codebase uses. See
# crypto_grid_bot.compute_dynamic_grid_pct's own docstring for why the
# LIVE version of this feature doesn't need this table at all - it reads
# the account's real current fee tier directly from Coinbase's own
# /transaction_summary endpoint. This table exists ONLY because this
# sandbox has no live network access to fetch real historical fee-tier
# data for a real backtest replay - an honest approximation, not a
# fetched real number, stated plainly per this file's own established
# norm for every other estimated constant. Deliberately taker (not
# maker) ratios throughout - every real order this codebase places is a
# MARKET order, so the maker rate a pasted proposal might assume was
# never the real basis this bot trades under.
GRID_FEE_TIER_RATIOS = {
    "base (<$10K vol)": 1.0,
    "tier2 ($10K-$50K vol)": 0.40 / 0.60,
    "tier3 ($50K-$100K vol)": 0.25 / 0.60,
    "tier4 ($100K-$1M vol)": 0.20 / 0.60,
}


async def run_grid_fee_tier_spacing_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading,
    never places an order. Direct answer to "would fee-tier-aware
    dynamic grid spacing (crypto_grid_bot.compute_dynamic_grid_pct) have
    actually helped": replays the EXISTING, already-validated
    _replay_grid_bot at the real grid_pct each fee tier in
    GRID_FEE_TIER_RATIOS would produce (via the identical real formula
    compute_dynamic_grid_pct itself uses -
    grid_engine.TARGET_NET_MARGIN_PCT + round_trip_fee_rate, floored at
    grid_engine.MIN_DYNAMIC_GRID_PCT), against the identical real
    historical candles per coin - so all three tiers are directly, fairly
    comparable. The base tier's own grid_pct is asserted to exactly
    reproduce today's live grid_pct (0.01) - if this feature is ever
    turned on for an account still at the base fee tier, nothing about
    its real trading changes."""
    coins = coins or COIN_FAMILY_TREE
    tier_grid_pcts = {}
    for tier_name, ratio in GRID_FEE_TIER_RATIOS.items():
        round_trip = engine.ROUND_TRIP_FEE_RATE * ratio
        tier_grid_pcts[tier_name] = max(grid_engine.MIN_DYNAMIC_GRID_PCT, grid_engine.TARGET_NET_MARGIN_PCT + round_trip)
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, _times = candles
            per_tier = {
                tier_name: _replay_grid_bot(closes, highs, lows, grid_pct=pct)
                for tier_name, pct in tier_grid_pcts.items()
            }
            return product_id, per_tier, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    tier_names = list(tier_grid_pcts.keys())
    totals = {name: 0.0 for name in tier_names}
    trade_counts = {name: 0 for name in tier_names}
    win_counts = {name: 0 for name in tier_names}
    for product_id, per_tier, skip_reason in outcomes:
        if per_tier is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **per_tier})
        for name, result in per_tier.items():
            if result is None:
                continue
            totals[name] += result["total_pnl"]
            trade_counts[name] += result["num_trades"]
            win_counts[name] += round(result["win_rate"] / 100 * result["num_trades"])

    summary = {
        name: {
            "grid_pct": round(tier_grid_pcts[name] * 100, 3),
            "total_pnl": round(totals[name], 2),
            "num_trades": trade_counts[name],
            "win_rate": round(win_counts[name] / trade_counts[name] * 100, 1) if trade_counts[name] else None,
        }
        for name in tier_names
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "tier_grid_pcts": {k: round(v * 100, 3) for k, v in tier_grid_pcts.items()},
        "skipped": skipped,
        "summary": summary,
        "best_tier": best,
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


def _net_pnl_pct(entry_price, exit_price):
    """Real, fee-adjusted round-trip return - the exact same math every
    other real replay in this file already uses (backtest_one_coin,
    _replay_with_exit_mode, etc: fee = qty*(entry+exit)*(ROUND_TRIP_FEE_RATE/2),
    net = gross - fee), expressed in percentage terms so this function
    doesn't need a real qty/spend to compute it - net_pct = net/spend is
    algebraically identical to this formula regardless of position size.
    Added after the account owner pointed out the reversal backtest
    should model real fees "so we would know how everything going to
    look" - previously this walked away with a raw, fee-free price
    return, overstating every hypothetical reversal trade's real result
    by the full real round-trip cost."""
    gross_pct = (exit_price - entry_price) / entry_price
    fee_pct = (entry_price + exit_price) / entry_price * (engine.ROUND_TRIP_FEE_RATE / 2)
    return gross_pct - fee_pct


def _simulate_reversal_trade(closes, times, start_idx, entry_price, target_pct, stop_pct):
    """Real, simple hypothesis test: what if the tree had immediately
    bought back in at the real stop-loss's own exit price, right after
    getting stopped out? Walks forward from start_idx (the first real
    candle at or after the stop event) looking for the first real close
    that clears a modest real profit target, or a second real hard stop
    protecting the reversal trade itself, whichever comes first; if
    neither fires before the window (the candles passed in) runs out,
    marks-to-market against the real last close instead of leaving the
    hypothetical trade open forever.

    `pnl_pct` is now the REAL, fee-adjusted round-trip return (see
    _net_pnl_pct above) - a real buy-back has to clear the real
    round-trip fee on top of target_pct to count as a genuine win, same
    as every other trade this codebase actually places. target_price/
    stop_price themselves are still set on the raw price move (a real
    order's trigger doesn't care about fees, only its settled P&L does) -
    only the reported pnl_pct changed.

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
            return {"exit_reason": "TARGET", "exit_price": price, "pnl_pct": _net_pnl_pct(entry_price, price), "recovered_to_breakeven": True}
        if price <= stop_price:
            return {"exit_reason": "STOP", "exit_price": price, "pnl_pct": _net_pnl_pct(entry_price, price), "recovered_to_breakeven": recovered_to_breakeven}
    # Window ran out with neither hit - mark to the real last close.
    last_price = closes[-1]
    return {
        "exit_reason": "TIME", "exit_price": last_price, "pnl_pct": _net_pnl_pct(entry_price, last_price),
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

    Real, honest limitations stated plainly: the real round-trip fee IS
    now modeled on the hypothetical reversal trades (_net_pnl_pct, added
    after the account owner asked for it directly - "back testing with
    the fees and everything... so we would know how everything going to
    look"), but this still never accounts for whether real free cash
    would actually have been available to take the hypothetical trade at
    that moment; and coins with very few real STOP HIT events don't carry
    the same statistical weight as POL-USD's real 80+ trade history. This
    is diagnostic only - it never reads into any live trading decision on
    its own."""
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
    same _simulate_reversal_trade hypothesis (now real, fee-adjusted P&L
    via _net_pnl_pct), same real historical candle fetch, same honest
    remaining limitation (real cash availability not checked) - only the
    source exit_reason filter differs. See run_stop_hit_reversal_backtest()'s
    own docstring for the full real methodology."""
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


NARROW_RANGE_LOOKBACK_HOURS = 24
NARROW_RANGE_HISTORY_LOOKBACK_HOURS = 240
NARROW_RANGE_PERCENTILE = 0.25
NARROW_BREAKOUT_FOLLOW_HOURS = 24


def _rolling_range_pct(highs, lows, end_idx, lookback):
    """Real channel width - the real high/low range over the `lookback`
    real candles ending just before end_idx, as a fraction of that
    window's own real midpoint price. Returns (range_pct, window_high,
    window_low), or None if end_idx doesn't have `lookback` real candles
    of history behind it yet (or the window's own midpoint is zero)."""
    if end_idx < lookback:
        return None
    window_high = max(highs[end_idx - lookback:end_idx])
    window_low = min(lows[end_idx - lookback:end_idx])
    mid = (window_high + window_low) / 2
    if not mid:
        return None
    return (window_high - window_low) / mid, window_high, window_low


def _is_narrow_range_at(i, highs, lows, lookback=NARROW_RANGE_LOOKBACK_HOURS,
                         history=NARROW_RANGE_HISTORY_LOOKBACK_HOURS, percentile=NARROW_RANGE_PERCENTILE):
    """Real, percentile-relative "narrow state" detection - per the
    account owner's own real trading concept: the best setups come from
    a genuinely TIGHT/NARROW range, and you go WITH the breakout
    direction out of one (only flipping contrarian after a coin has
    already made a WIDE, extended move - a separate, not-yet-built idea;
    this backtest only tests the narrow-state breakout-continuation
    half). Deliberately PERCENTILE-relative to each coin's own recent
    range history, not a fixed absolute % - a coin's own normal
    volatility varies wildly (BTC vs. a small altcoin), so "narrow" has
    to mean "tight relative to how this coin itself has been trading
    lately," not one fixed number for every coin.

    "Narrow" = the real range over the last `lookback` real candles is
    in the bottom `percentile` (25% default) of that same rolling-range
    measure's own real distribution over the preceding `history` hours.
    Returns (is_narrow, window_high, window_low) - the last two are the
    real narrow range's own boundaries, needed by the replay loop to
    detect the eventual real breakout. Returns (False, None, None) when
    there isn't yet enough real history to judge (both for the current
    window and for building a real percentile distribution to compare
    it against) - never guesses without a real minimum of history
    behind it (at least 10 real historical range samples)."""
    current = _rolling_range_pct(highs, lows, i, lookback)
    if current is None or i < lookback + history:
        return False, None, None
    current_range_pct, window_high, window_low = current

    # Real historical distribution of this same rolling-range measure,
    # sampled every half-lookback (12h default) rather than every single
    # hour - overlapping windows one hour apart are heavily
    # autocorrelated and would just pad the sample count without adding
    # real independent evidence.
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


def _replay_narrow_range_breakout(highs, lows, closes, lookback=NARROW_RANGE_LOOKBACK_HOURS,
                                   history=NARROW_RANGE_HISTORY_LOOKBACK_HOURS,
                                   percentile=NARROW_RANGE_PERCENTILE,
                                   follow_hours=NARROW_BREAKOUT_FOLLOW_HOURS):
    """Walks a real candle series looking for real narrow-range states
    (see _is_narrow_range_at), then scores the FIRST real candle whose
    CLOSE breaks outside that narrow range's own high/low - matching the
    account owner's own framing ("the first bar... opens above/below") -
    and checks whether price genuinely continued in that breakout
    direction `follow_hours` (24 default) later. Only the first real
    breakout after each narrow state is scored, not every candle while
    still narrow, so the same narrow state's eventual breakout is never
    counted more than once.

    Returns a list of real event dicts: {breakout_index, direction
    ("up"/"down"), breakout_price, forward_price, followed_through
    (bool), forward_return_pct}.

    _is_narrow_range_at(i, ...) tells us whether the real window of
    candles STRICTLY BEFORE index i was narrow - it says nothing about
    candle i itself. So the real breakout candle is found by: the moment
    a narrow window is detected ending right before some index i, scan
    FORWARD (i, i+1, i+2, ...) for the first real candle whose CLOSE
    actually lies outside that fixed narrow zone's own high/low - that
    is the real breakout. Once found (or if the series runs out first),
    resume scanning for the NEXT narrow zone starting after it, so the
    same narrow zone's breakout is never scored twice."""
    events = []
    n = len(closes)
    i = lookback + history
    while i < n:
        is_narrow, wh, wl = _is_narrow_range_at(i, highs, lows, lookback, history, percentile)
        if not is_narrow:
            i += 1
            continue
        j = i
        while j < n and wl <= closes[j] <= wh:
            j += 1
        if j >= n:
            break  # ran off the end of real history without a genuine breakout
        direction = "up" if closes[j] > wh else "down"
        breakout_price = closes[j]
        target_j = j + follow_hours
        if target_j < n:
            forward_price = closes[target_j]
            followed_through = (forward_price > breakout_price) if direction == "up" else (forward_price < breakout_price)
            events.append({
                "breakout_index": j, "direction": direction,
                "breakout_price": breakout_price, "forward_price": forward_price,
                "followed_through": followed_through,
                "forward_return_pct": (forward_price - breakout_price) / breakout_price,
            })
        i = j + 1
    return events


def _summarize_breakout_events(events):
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


async def run_narrow_range_breakout_backtest(coins=None, days=BACKTEST_DAYS, max_concurrent=6) -> dict:
    """SHADOW-MODE, real historical data, per-coin AND aggregate - tests
    the account owner's own real trading claim directly: "if you open
    above a narrow state... 87% chance there are more upside to come...
    if you open below a narrow state... 87% chance to follow through to
    the downside." That 87% figure was their own stated number, not
    something already verified against this system's real data - this
    replays real narrow-range breakout events on real historical
    Coinbase candles and reports the REAL hit rate this system's own
    coins actually produced, split by breakout direction, against an
    honest 50% coin-flip baseline (same "state the baseline plainly"
    convention run_directional_signal_backtest() already uses for the
    BTC price-projection panel).

    Never places a real order, never touches live trading - purely
    diagnostic, same posture as every other backtest tool in this file.
    coins=None (default) tests every real coin in COIN_FAMILY_TREE."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            data = await fetch_historical_candles(session, product_id, days=days, last_error_out=last_error)
        if data is None:
            return product_id, None
        closes, highs, lows, _times = data
        return product_id, _replay_narrow_range_breakout(highs, lows, closes)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    per_coin = []
    skipped = []
    all_events = []
    for product_id, events in results:
        if events is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        up_events = [e for e in events if e["direction"] == "up"]
        down_events = [e for e in events if e["direction"] == "down"]
        per_coin.append({
            "product_id": product_id, "total_events": len(events),
            "up_breakouts": _summarize_breakout_events(up_events),
            "down_breakouts": _summarize_breakout_events(down_events),
        })
        all_events.extend(events)

    up_all = [e for e in all_events if e["direction"] == "up"]
    down_all = [e for e in all_events if e["direction"] == "down"]
    combined_hit_rate = None
    if all_events:
        combined_hit_rate = round(sum(1 for e in all_events if e["followed_through"]) / len(all_events), 4)

    return {
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin,
        "overall": {
            "total_events": len(all_events),
            "up_breakouts": _summarize_breakout_events(up_all),
            "down_breakouts": _summarize_breakout_events(down_all),
            "combined_hit_rate": combined_hit_rate,
            "coin_flip_baseline": 0.5,
        },
        "params": {
            "narrow_range_lookback_hours": NARROW_RANGE_LOOKBACK_HOURS,
            "narrow_range_history_lookback_hours": NARROW_RANGE_HISTORY_LOOKBACK_HOURS,
            "narrow_range_percentile": NARROW_RANGE_PERCENTILE,
            "follow_through_hours": NARROW_BREAKOUT_FOLLOW_HOURS,
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
