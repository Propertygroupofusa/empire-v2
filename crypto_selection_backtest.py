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
from models import CryptoTreeBranch, CryptoGridBranch, CryptoCoinTradeHistory

SPEND = 150.0
BACKTEST_DAYS = 30
# Real, global throttle on Coinbase's public candles endpoint - found from
# a real, live 429 pileup the account owner hit directly: 15 of 33 coins
# skipped on a real Strategy Lab run even after the per-page retry below
# already existed. Root cause the per-tool asyncio.Semaphore(max_concurrent)
# alone couldn't fix: every real backtest tool in this file (Strategy Lab,
# every Grid Bot comparison, the full backtest, etc.) opens its OWN
# semaphore scoped only to that one call - nothing coordinates real HTTP
# requests ACROSS tools, or across a single tool's own multi-page
# pagination happening inside several concurrent coin fetches at once. A
# real 6-coin-concurrent tool, each paginating 3-4 pages, can genuinely
# burst well past Coinbase's real per-IP rate limit for its public,
# unauthenticated endpoint - especially since this same Railway
# deployment's live trading bots are hitting Coinbase concurrently in the
# background too, sharing the identical real outbound IP. This semaphore
# is acquired around every single real HTTP request this module makes to
# that endpoint (both fetch_candles_window's page loop and
# _fetch_1min_candles_window's own copy below), so no matter how many
# coins or tools are running at once, at most 2 real requests are ever in
# flight - a real, process-wide throttle, not just a per-tool one.
_CANDLE_HTTP_SEMAPHORE = asyncio.Semaphore(2)
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
        for attempt in range(5):
            try:
                async with _CANDLE_HTTP_SEMAPHORE:
                    async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
                        if r.status == 429:
                            last_error = f"HTTP 429 rate limited"
                            await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))
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
                await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))
        if page_data:
            all_candles.extend(page_data)
        elif last_error:
            break  # this page never came through even after retries - stop rather than silently pretend the window is complete
        cursor = page_end
        await asyncio.sleep(0.2)  # be polite to the public endpoint

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
#
# RESTORED after being found accidentally deleted (along with its
# dashboard button/table/promote-row and its POST route) by an unrelated
# later commit that added crypto_grid_bot.py - the account owner asked
# directly why they couldn't find a "push a button to go live" option for
# this on the dashboard, and the honest answer traced back through git
# history to this real regression, not something they were missing on
# the page. The live promote mechanism itself
# (get_live_trailing_stop_pct/set_live_trailing_stop_pct in
# crypto_family_tree_bot.py, and the /family-tree-status/set-trailing-
# stop-pct route) was never touched by that deletion and kept working the
# whole time - only the tool that RUNS the real sweep and the button that
# CALLS that promote endpoint were gone, so a value could still be
# promoted via a raw API call but there was never a dashboard button to
# do it with.
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


def _make_combined_live_entry_gate(closes, times, btc_times_sorted, btc_closes_sorted, lookback_hours: int = 25):
    """Returns an entry_gate(i) closure that composes EVERY entry filter
    currently live in `crypto_family_tree_bot.py`'s
    `find_most_volatile_unclaimed_coin()` and `run_branch_cycle()` into
    ONE combined gate - RSI-overbought exclusion
    (`engine.ENTRY_MAX_RSI`), BTC-relative-strength, the real hourly
    SMA20/SMA50 higher-timeframe trend, and the new RSI(30)+support-zone
    entry-timing filter - so a real entry is only allowed when ALL FOUR
    real, already-individually-validated filters agree. Each of these
    has been backtested and promoted to live ONE AT A TIME; this is the
    first replay of what they actually do TOGETHER, which is how the
    live bot genuinely applies them today.

    Real, honest scope note: this tests per-coin ENTRY TIMING discipline
    only - `find_most_volatile_unclaimed_coin()`'s own job (picking WHICH
    coin among several live candidates) is a different mechanism this
    single-coin replay framework (`backtest_one_coin`) can't express.
    Every sub-gate already fails OPEN on missing real history; composing
    them with a plain AND preserves that - a candle with too little
    history for one sub-check still passes through it, never
    manufacturing a block out of an absence of data."""
    btc_gate = _make_btc_relative_strength_gate(closes, times, lookback_hours, btc_times_sorted, btc_closes_sorted)
    trend_gate = _make_higher_tf_trend_gate(closes)
    sr_gate = _make_support_resistance_gate(closes)

    def gate(i):
        rsi = engine._rsi_from_closes(closes[:i + 1])
        if rsi is not None and rsi >= engine.ENTRY_MAX_RSI:
            return False
        if not btc_gate(i):
            return False
        if not trend_gate(i):
            return False
        if not sr_gate(i):
            return False
        return True
    return gate


async def run_combined_live_entry_filters_backtest(coins=None, days=BACKTEST_DAYS, lookback_hours=25, max_concurrent=6):
    """SHADOW-MODE, additive - never touches live trading, never places a
    real order. Direct answer to the account owner's own question after
    seeing the real Coin Trade History table all red (POL -$392, BTC
    -$35, and every other coin negative): would the family tree, running
    with EVERY entry filter currently wired live (RSI-overbought,
    BTC-relative-strength, higher-timeframe trend, and the new
    RSI(30)+support-zone timing filter) all applied TOGETHER, actually
    have made money over the real last `days` days - the real evidence
    needed before deciding whether to un-retire it. Every one of these
    four filters has already been backtested and promoted to live
    INDIVIDUALLY; none of the existing backtest tools test what they do
    stacked together, which is how the live bot genuinely runs today.

    Runs the exact same real target/stop/breakeven/trailing-stop replay
    twice per coin on identical real historical hourly candles - an
    unfiltered baseline vs. the combined-gated version - so the two are
    directly, fairly comparable. Real extra cost versus the plain
    baseline backtest: one additional real BTC-USD history fetch (shared
    across every coin, not re-fetched per coin), same as the standalone
    BTC-relative-strength comparison already pays."""
    coins = [p for p in (coins or COIN_FAMILY_TREE) if p != "BTC-USD"]
    semaphore = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        btc_candles = await fetch_historical_candles(session, "BTC-USD", days=days)
        if btc_candles is None:
            return {"error": "could not fetch real BTC-USD history to compare against"}
        btc_closes, _btc_highs, _btc_lows, btc_times = btc_candles

        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, times = candles
            baseline = backtest_one_coin(closes, highs, lows)
            gate = _make_combined_live_entry_gate(closes, times, btc_times, btc_closes, lookback_hours=lookback_hours)
            filtered = backtest_one_coin(closes, highs, lows, entry_gate=gate)
            return product_id, {"baseline": baseline, "with_combined_filters": filtered}, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})

    def _sort_key(row):
        filtered = row["with_combined_filters"]
        return filtered["roi_pct_of_spend"] if filtered else -999.0
    comparison.sort(key=_sort_key, reverse=True)

    baseline_total = round(sum((row["baseline"] or {}).get("total_pnl", 0.0) for row in comparison), 2)
    filtered_total = round(sum((row["with_combined_filters"] or {}).get("total_pnl", 0.0) for row in comparison), 2)

    return {
        "backtest_days": days,
        "lookback_hours": lookback_hours,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "baseline_total_pnl": baseline_total,
        "with_combined_filters_total_pnl": filtered_total,
        "better": "with_combined_filters" if filtered_total > baseline_total else "baseline",
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
                      grid_pct=STRATEGY_LAB_GRID_PCT, num_levels=STRATEGY_LAB_GRID_LEVELS,
                      entry_gate=None):
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
      actual grid bot has.

    `entry_gate(i)` (optional) - same real per-index callback interface
    backtest_one_coin() already uses (see _make_higher_tf_trend_gate) -
    when given, gates NEW BUYS only; a real sell is NEVER gated, matching
    every other "existing protection never pauses on a filter" rule in
    this codebase. A gated-out dip leaves `reference` completely
    unchanged (mirrors crypto_grid_bot.py's own live behavior - reference
    only ever updates on a REAL fill), so the identical dip is simply
    re-evaluated next candle rather than being silently skipped forever."""
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
            if entry_gate is None or entry_gate(i):
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


# "swing_trading" (D) was removed here per the account owner's own direct
# call after a real 30-day/68-trade sample came back the worst of the
# four candidates by a wide margin (29.4% win rate, -$133.00 total) -
# real evidence, not a guess. _replay_swing_trading() itself is left
# defined below (unused by this dict) rather than deleted, in case it's
# ever worth revisiting with different real parameters later - it just no
# longer runs as part of Strategy Lab.
# "grid_bot" (C) previously replayed at the OLD fixed 1%/10-level
# STRATEGY_LAB_GRID_PCT convention - after the account owner questioned a
# real 572-trade/$51.13 result as implausibly low, tracing the per-trade
# math confirmed the number itself was correct (thin, expected margins at
# 1% spacing against a real 0.8% round-trip fee), but also surfaced that
# this entry no longer matched what's actually live: Grid Bot's real
# spacing has since moved to the average-swing dynamic formula
# (_live_matching_grid_pct - identical to crypto_grid_bot.compute_avg_swing_grid_pct).
# Repointed here so Strategy Lab's own "Grid Bot" comparison stays an
# honest match to today's real live behavior, not a stale snapshot of an
# earlier, tighter default - same correction already applied once before
# to "Baseline (A)" when its own exit rule drifted out of sync with live.
STRATEGY_LAB_STRATEGIES = {
    "baseline": lambda closes, highs, lows, spend: backtest_one_coin(closes, highs, lows, spend=spend),
    "hourly_momentum": lambda closes, highs, lows, spend: _replay_hourly_momentum(closes, highs, lows, spend=spend),
    "grid_bot": lambda closes, highs, lows, spend: _replay_grid_bot(
        closes, highs, lows, spend=spend,
        grid_pct=_live_matching_grid_pct(closes, highs, lows),
        num_levels=STRATEGY_LAB_GRID_LEVELS,
    ),
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


def _average_hourly_swing_pct(closes, highs, lows) -> float:
    """Real "average swing" for one coin - the mean real True Range (as a
    % of price) across EVERY hourly candle in the given window, not just
    a snapshot at the most recent one. Deliberately a different figure
    from engine._atr_pct_from_candles (which reports the real ATR at only
    the LAST 14 candles, the live "right now" volatility reading every
    other real entry filter in this codebase already uses) - this
    backtest's own question is "what does this coin typically do across
    the whole real test window," which needs the real mean, not a single
    live snapshot. Reuses the identical real True-Range formula (max of
    high-low, |high-prev_close|, |low-prev_close|) so the two stay
    consistent, just averaged over a different real span."""
    n = len(closes)
    if n < 2:
        return 0.0
    true_ranges_pct = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        if closes[i]:
            true_ranges_pct.append(tr / closes[i])
    if not true_ranges_pct:
        return 0.0
    return sum(true_ranges_pct) / len(true_ranges_pct)


def _live_matching_grid_pct(closes, highs, lows) -> float:
    """The real, exact grid_pct today's live Grid Bot would compute for
    this coin - identical formula to crypto_grid_bot.compute_avg_swing_grid_pct()
    (this coin's own real average hourly swing x AVG_SWING_SPACING_MULTIPLIER,
    floored at the real fee-safe minimum TARGET_NET_MARGIN_PCT +
    ROUND_TRIP_FEE_RATE), computed from the same historical candles a
    replay already has in hand rather than a second live fetch. Shared
    here so every real "how does this compare to what's actually live"
    check in this file (Strategy Lab's own Grid Bot entry,
    run_grid_level_spacing_comparison's live-default candidate) uses the
    identical real number, never two slightly different guesses at it."""
    fee_safe_floor = max(grid_engine.MIN_DYNAMIC_GRID_PCT, grid_engine.TARGET_NET_MARGIN_PCT + engine.ROUND_TRIP_FEE_RATE)
    avg_swing_pct = _average_hourly_swing_pct(closes, highs, lows)
    return max(fee_safe_floor, avg_swing_pct * grid_engine.AVG_SWING_SPACING_MULTIPLIER)


# Real candidate grid spacings, each a multiple of a coin's OWN real
# average swing over the test window - per the account owner's direct
# question: "what is the average swing of coins... do you think it
# should stay at 1% or we should change it and see what the average of
# coins moving is and set it around that rate." Unlike
# GRID_FEE_TIER_RATIOS above (one spacing shared by every coin, tied to
# the account's own fee tier), every candidate here computes a
# DIFFERENT real grid_pct per coin, sized off that coin's own real
# behavior - a calm coin gets a tighter grid, a choppy one gets a wider
# one. 1.0x is the literal "match the average swing exactly" version of
# the account owner's own question; 0.5x/1.5x/2.0x bracket it so the
# real answer isn't just one untested guess.
GRID_ATR_SPACING_MULTIPLIERS = {
    "0.5x avg swing": 0.5,
    "1.0x avg swing": 1.0,
    "1.5x avg swing": 1.5,
    "2.0x avg swing": 2.0,
}
# Real baseline label for today's live, fixed default - always included
# so every multiplier is directly, fairly compared against what's
# actually live right now, same "always include today's real default"
# discipline as every other Grid Bot comparison in this file.
GRID_ATR_BASELINE_LABEL = "fixed 1% (today's live default)"


async def run_grid_atr_spacing_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading,
    never places an order. Direct answer to the account owner's own
    question: would setting Grid Bot's spacing to match each coin's own
    real average price swing (instead of today's one-size-fits-all fixed
    1%) actually help? Computes each coin's real average hourly swing %
    (_average_hourly_swing_pct) from the SAME real historical candles the
    replay itself uses - no separate live fetch needed - then replays the
    existing, already-validated _replay_grid_bot at grid_pct = that
    coin's own real average swing × each of GRID_ATR_SPACING_MULTIPLIERS
    (floored at grid_engine.MIN_DYNAMIC_GRID_PCT, the same real floor the
    fee-tier dynamic-spacing feature already uses, so a near-zero real
    swing can never produce a degenerate, effectively-always-triggering
    grid), alongside today's real fixed 1% baseline on the identical real
    candles - so every candidate is directly, fairly comparable per coin
    AND in aggregate.

    Real, honest note worth being explicit about, unlike the fee-tier
    comparison above: every coin gets a DIFFERENT real grid_pct under
    each swing-based candidate (that's the whole point), so there's no
    single "the grid_pct for this candidate" figure the way the fee-tier
    table has one - each per-coin row reports its own real average swing
    and the real grid_pct that was actually used for it."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, None, "not enough historical data"
            closes, highs, lows, _times = candles
            avg_swing_pct = _average_hourly_swing_pct(closes, highs, lows)
            per_candidate = {GRID_ATR_BASELINE_LABEL: {"grid_pct_used": STRATEGY_LAB_GRID_PCT, "result": _replay_grid_bot(closes, highs, lows, grid_pct=STRATEGY_LAB_GRID_PCT)}}
            for name, multiplier in GRID_ATR_SPACING_MULTIPLIERS.items():
                grid_pct = max(grid_engine.MIN_DYNAMIC_GRID_PCT, avg_swing_pct * multiplier)
                per_candidate[name] = {"grid_pct_used": grid_pct, "result": _replay_grid_bot(closes, highs, lows, grid_pct=grid_pct)}
            return product_id, avg_swing_pct, per_candidate, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    candidate_names = [GRID_ATR_BASELINE_LABEL] + list(GRID_ATR_SPACING_MULTIPLIERS.keys())
    totals = {name: 0.0 for name in candidate_names}
    trade_counts = {name: 0 for name in candidate_names}
    win_counts = {name: 0 for name in candidate_names}
    for product_id, avg_swing_pct, per_candidate, skip_reason in outcomes:
        if per_candidate is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        row = {"product_id": product_id, "avg_swing_pct": round(avg_swing_pct * 100, 3)}
        for name in candidate_names:
            entry = per_candidate[name]
            result = entry["result"]
            row[name] = {"grid_pct_used": round(entry["grid_pct_used"] * 100, 3), **(result or {})} if result else {"grid_pct_used": round(entry["grid_pct_used"] * 100, 3), "no_trades": True}
            if result is not None:
                totals[name] += result["total_pnl"]
                trade_counts[name] += result["num_trades"]
                win_counts[name] += round(result["win_rate"] / 100 * result["num_trades"])
        comparison.append(row)

    summary = {
        name: {
            "total_pnl": round(totals[name], 2),
            "num_trades": trade_counts[name],
            "win_rate": round(win_counts[name] / trade_counts[name] * 100, 1) if trade_counts[name] else None,
        }
        for name in candidate_names
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]
    avg_swings = [row["avg_swing_pct"] for row in comparison]

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "candidate_names": candidate_names,
        "average_real_swing_across_all_coins_pct": round(sum(avg_swings) / len(avg_swings), 3) if avg_swings else None,
        "skipped": skipped,
        "summary": summary,
        "best_candidate": best,
        "comparison": comparison,
    }


# Real candidates directly testing a pasted third-party critique's own
# "fewer, bigger slices" proposal - "reduce grid levels 6→3, increase
# take-profit 0.8%→2.0-2.5%, position size $41→$82/slice" - against
# today's real live setup (up to 10 levels, spacing sized per-coin off
# real average swing at 1.5x, already validated and shipped earlier this
# session). Each candidate is a (num_levels, grid_pct) pair; grid_pct is
# a FIXED percentage here (not swing-based) since that's what the critique
# itself proposed - a flat 2.0%/2.5% target, not "1.5x whatever this coin
# happens to do." Real, honest framing: fewer levels means fewer, larger
# real slices competing for the same total spend - each slice risks more
# dollars per cycle, and a wider grid_pct means fewer real fills overall
# (a real tradeoff the critique's own "same 68% win rate, but wins 2-3x
# larger" claim never actually backed with a replay - this is that
# missing replay).
GRID_LEVEL_SPACING_CANDIDATES = {
    "3_levels_2.0pct": {"num_levels": 3, "grid_pct": 0.020},
    "3_levels_2.5pct": {"num_levels": 3, "grid_pct": 0.025},
    "5_levels_2.0pct": {"num_levels": 5, "grid_pct": 0.020},
}
GRID_LEVEL_SPACING_LIVE_DEFAULT_LABEL = "live default (10 levels, 1.5x avg-swing spacing)"


async def run_grid_level_spacing_comparison(coins=None, days=BACKTEST_DAYS, max_concurrent=6, candidates=None):
    """SHADOW-MODE, additive comparison - never touches live trading,
    never places an order. Direct, real answer to a pasted third-party
    critique's specific "fewer, bigger slices" proposal (reduce grid
    levels 6→3, widen take-profit to 2.0-2.5%, and its own unbacked claim
    this would turn "-$1.57 into +$15-25 on the same capital"): replays
    the existing, already-validated _replay_grid_bot at each candidate's
    real (num_levels, grid_pct) pair, and at today's real live default
    (num_levels=STRATEGY_LAB_GRID_LEVELS, grid_pct = each coin's own real
    average hourly swing x AVG_SWING_SPACING_MULTIPLIER, fee-safe floored
    - the identical real formula crypto_grid_bot.compute_avg_swing_grid_pct
    itself uses, computed here from the SAME real historical candles the
    replay itself uses rather than a second live fetch) - all against the
    identical real historical Coinbase candles per coin, so every
    candidate is directly, fairly comparable to what's genuinely live
    today, not just to each other."""
    coins = coins or COIN_FAMILY_TREE
    candidates = dict(candidates) if candidates else dict(GRID_LEVEL_SPACING_CANDIDATES)
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, None, "not enough historical data"
            closes, highs, lows, _times = candles
            avg_swing_pct = _average_hourly_swing_pct(closes, highs, lows)
            live_grid_pct = _live_matching_grid_pct(closes, highs, lows)
            per_candidate = {
                GRID_LEVEL_SPACING_LIVE_DEFAULT_LABEL: {
                    "num_levels": STRATEGY_LAB_GRID_LEVELS, "grid_pct_used": live_grid_pct,
                    "result": _replay_grid_bot(closes, highs, lows, grid_pct=live_grid_pct, num_levels=STRATEGY_LAB_GRID_LEVELS),
                }
            }
            for name, cfg in candidates.items():
                per_candidate[name] = {
                    "num_levels": cfg["num_levels"], "grid_pct_used": cfg["grid_pct"],
                    "result": _replay_grid_bot(closes, highs, lows, grid_pct=cfg["grid_pct"], num_levels=cfg["num_levels"]),
                }
            return product_id, avg_swing_pct, per_candidate, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    candidate_names = [GRID_LEVEL_SPACING_LIVE_DEFAULT_LABEL] + list(candidates.keys())
    totals = {name: 0.0 for name in candidate_names}
    trade_counts = {name: 0 for name in candidate_names}
    win_counts = {name: 0 for name in candidate_names}
    for product_id, avg_swing_pct, per_candidate, skip_reason in outcomes:
        if per_candidate is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        row = {"product_id": product_id, "avg_swing_pct": round(avg_swing_pct * 100, 3)}
        for name in candidate_names:
            entry = per_candidate[name]
            result = entry["result"]
            row[name] = {
                "num_levels": entry["num_levels"], "grid_pct_used": round(entry["grid_pct_used"] * 100, 3),
                **(result or {"no_trades": True}),
            }
            if result is not None:
                totals[name] += result["total_pnl"]
                trade_counts[name] += result["num_trades"]
                win_counts[name] += round(result["win_rate"] / 100 * result["num_trades"])
        comparison.append(row)

    summary = {
        name: {
            "total_pnl": round(totals[name], 2),
            "num_trades": trade_counts[name],
            "win_rate": round(win_counts[name] / trade_counts[name] * 100, 1) if trade_counts[name] else None,
        }
        for name in candidate_names
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]

    return {
        "backtest_days": days,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "candidate_names": candidate_names,
        "live_default_label": GRID_LEVEL_SPACING_LIVE_DEFAULT_LABEL,
        "skipped": skipped,
        "summary": summary,
        "best_candidate": best,
        "comparison": comparison,
    }


async def run_grid_higher_tf_trend_comparison(coins=None, days=BACKTEST_DAYS, sma_short=20, sma_long=50, max_concurrent=6):
    """SHADOW-MODE, additive comparison - never touches live trading,
    never places an order. Direct answer to the account owner's own
    follow-up question, after seeing narrower grid spacing lose money by
    repeatedly buying into real declines: would gating Grid Bot's own
    NEW-SLICE buys on the same real higher-timeframe trend filter already
    validated for the family tree's own entries (SMA20 > SMA50 on hourly
    candles - see _make_higher_tf_trend_gate, the exact same real gate
    function run_higher_tf_trend_comparison() already uses for that
    strategy) reduce the real losses this specific failure mode causes?

    Real mechanism this targets: a grid buys every qualifying dip with no
    regard for the broader trend, so a sustained real decline piles up
    several open slices at descending prices; when a partial bounce
    finally triggers a sell, the OLDEST (FIFO) slice - bought before the
    decline, at a real higher price - can still be sold at a real loss
    even though the grid's own local rule was satisfied. Blocking new
    buys while the higher timeframe is confirmed-declining should mean
    fewer slices get stacked up during exactly the moves that produce
    this pattern.

    Replays the existing, already-validated _replay_grid_bot TWICE per
    coin on the identical real historical candles - once as today's live
    baseline (no gate), once with new buys gated on the real trend filter
    - real sells are NEVER gated in either run, matching every other
    "existing protection never pauses" rule in this file. Always at
    today's real live grid_pct (1%) and num_levels (10) - this isolates
    the trend-gate question specifically, not spacing (already tested
    separately in run_grid_atr_spacing_comparison)."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        async def _one(product_id):
            async with semaphore:
                candles = await fetch_historical_candles(session, product_id, days=days)
            if candles is None:
                return product_id, None, "not enough historical data"
            closes, highs, lows, _times = candles
            baseline = _replay_grid_bot(closes, highs, lows)
            gate = _make_higher_tf_trend_gate(closes, sma_short=sma_short, sma_long=sma_long)
            filtered = _replay_grid_bot(closes, highs, lows, entry_gate=gate)
            return product_id, {"baseline": baseline, "with_trend_filter": filtered}, None

        outcomes = await asyncio.gather(*(_one(pid) for pid in coins))

    comparison = []
    skipped = []
    names = ["baseline", "with_trend_filter"]
    totals = {name: 0.0 for name in names}
    trade_counts = {name: 0 for name in names}
    win_counts = {name: 0 for name in names}
    for product_id, result, skip_reason in outcomes:
        if result is None:
            skipped.append({"product_id": product_id, "reason": skip_reason})
            continue
        comparison.append({"product_id": product_id, **result})
        for name in names:
            r = result[name]
            if r is None:
                continue
            totals[name] += r["total_pnl"]
            trade_counts[name] += r["num_trades"]
            win_counts[name] += round(r["win_rate"] / 100 * r["num_trades"])

    summary = {
        name: {
            "total_pnl": round(totals[name], 2),
            "num_trades": trade_counts[name],
            "win_rate": round(win_counts[name] / trade_counts[name] * 100, 1) if trade_counts[name] else None,
        }
        for name in names
    }
    best = max(summary.items(), key=lambda kv: kv[1]["total_pnl"])[0]

    return {
        "backtest_days": days,
        "sma_short": sma_short,
        "sma_long": sma_long,
        "spend_per_trade": SPEND,
        "coins_tested": len(coins),
        "coins_with_results": len(comparison),
        "skipped": skipped,
        "summary": summary,
        "best_candidate": best,
        "comparison": comparison,
    }


# ============================================================================
# Grid Bot AUTO-ROTATION effectiveness backtest
# ============================================================================
# Real, direct answer to "does moving a flat branch's idle real cash to a
# better-ranked coin actually help, or would it have done just as well
# staying put" - the exact mechanism behind crypto_grid_9 disappearing
# (it reallocated its own idle real cash into crypto_grid_5 via
# move_cash_between_grid_branches(), drained to ~$0, and - by the already
# -documented "an emptied-out branch doesn't linger" design - was deleted).
# No backtest existed for this before now; every other grid comparison in
# this file (spacing, ATR, higher-tf-trend) tests a SINGLE coin's own
# entry/exit rules, never whether MOVING capital between coins over time
# beats leaving it parked.
#
# Real, honest simplification stated plainly: the live rotation sweep
# (crypto_grid_bot.pick_best_ranked_coin_for_grid) ranks candidates using
# real BACKTESTED ROI (from the CryptoBacktestRun table, itself the
# output of a separate, already-running daily backtest) blended with live
# BTC-relative-strength - a signal that can't be cleanly replayed at an
# arbitrary point in the past without a circular "backtest inside a
# backtest" dependency. This tool substitutes the one piece of that real
# ranking signal that IS honestly replayable purely from real historical
# candles at any past moment: BTC-relative-strength alone (a coin's own
# real trailing ROTATION_RANK_LOOKBACK_HOURS return minus BTC-USD's real
# return over the identical window - the same real comparison
# calculate_relative_strength() already validates elsewhere in this
# file). A real, defensible proxy for "which coin currently looks best,"
# not a byte-for-byte replay of the live ranking function.
# ============================================================================

ROTATION_RANK_LOOKBACK_HOURS = 25  # matches the "~25-hour" bullish/relative-strength convention used codebase-wide
ROTATION_COOLDOWN_HOURS = 2  # matches crypto_grid_bot.GRID_ROTATION_COOLDOWN_SECONDS (2h) exactly


def _grid_step(price: float, reference: float, open_slices: list, slice_usd: float, grid_pct: float, num_levels: int):
    """One real grid-mechanic decision at a single real price point -
    factored out of _replay_grid_bot's own inner loop (identical real
    buy/sell trigger and fee model, byte-for-byte) so a caller can drive
    it incrementally across a real, possibly coin-switching timeline
    instead of only ever replaying one coin's whole candle array at once.
    Returns (new_reference, new_open_slices, trade_net_or_None)."""
    if price <= reference * (1 - grid_pct) and len(open_slices) < num_levels:
        return price, open_slices + [{"entry": price, "qty": slice_usd / price}], None
    if price >= reference * (1 + grid_pct) and open_slices:
        slot = open_slices[0]
        remaining = open_slices[1:]
        gross = slot["qty"] * (price - slot["entry"])
        fee = slot["qty"] * (slot["entry"] + price) * (engine.ROUND_TRIP_FEE_RATE / 2)
        return price, remaining, gross - fee
    return reference, open_slices, None


def _best_ranked_candidate(candidates: dict, btc_times_sorted: list, btc_closes_sorted: list, at_time: int, lookback_hours: int = ROTATION_RANK_LOOKBACK_HOURS):
    """Real BTC-relative-strength ranking among candidate coins at one
    real point in time (see the section docstring above for why this,
    not the live blended signal, is what's replayable here). Returns the
    product_id with the highest real (coin_return - btc_return) alpha at
    `at_time`, or None if no candidate has enough real history yet to
    judge. `candidates`: {product_id: (times_sorted, closes_sorted)}."""
    btc_now = _closest_close_at_or_before(btc_times_sorted, btc_closes_sorted, at_time)
    btc_then = _closest_close_at_or_before(btc_times_sorted, btc_closes_sorted, at_time - lookback_hours * 3600)
    btc_return = (btc_now - btc_then) / btc_then if (btc_now and btc_then and btc_then > 0) else None

    best_id, best_alpha = None, None
    for product_id, (times_sorted, closes_sorted) in candidates.items():
        now = _closest_close_at_or_before(times_sorted, closes_sorted, at_time)
        then = _closest_close_at_or_before(times_sorted, closes_sorted, at_time - lookback_hours * 3600)
        if now is None or then is None or then <= 0:
            continue
        coin_return = (now - then) / then
        alpha = (coin_return - btc_return) if btc_return is not None else coin_return
        if best_alpha is None or alpha > best_alpha:
            best_alpha, best_id = alpha, product_id
    return best_id


def _replay_grid_rotation(candidates: dict, btc_series: tuple, start_coin: str, spend: float = SPEND,
                           grid_pct: float = STRATEGY_LAB_GRID_PCT, num_levels: int = STRATEGY_LAB_GRID_LEVELS,
                           rotation_enabled: bool = True, rotation_cooldown_hours: int = ROTATION_COOLDOWN_HOURS):
    """Real, shared-clock replay of ONE branch's own capital, starting on
    `start_coin`, over every real hourly tick common to the candidate
    pool's own real time window. `candidates`: {product_id: (closes,
    highs, lows, times)} - highs/lows unused here (grid decisions are
    close-only, matching _replay_grid_bot's own documented limitation),
    kept in the tuple only so callers can pass the same fetched data this
    module's other comparisons already use.

    With `rotation_enabled=False` (the baseline), capital never leaves
    `start_coin` - a direct call-through to the SAME real per-tick
    mechanic (_grid_step) _replay_grid_bot's own full-window replay uses,
    just walked one hour at a time instead of over the whole array at
    once - so baseline results here are the real apples-to-apples
    comparison point, not a different mechanic wearing the same name.

    With `rotation_enabled=True`, every real hourly tick where the
    branch is genuinely FLAT (no open slices - real rotation never
    touches an open slice, matching move_cash_between_grid_branches'
    own real "can only move cash from a FLAT branch" rule) and at least
    `rotation_cooldown_hours` have passed since the last real rotation,
    the pool is re-ranked via _best_ranked_candidate(); if a DIFFERENT
    coin currently ranks best, capital "moves" (the reference price
    resets to the new coin's real price at that tick, `open_slices`
    stays empty since nothing was open to carry over - matching the real
    live mechanism exactly).

    Returns a dict shaped like _summarize_strategy_trades()'s own output
    (so both scenarios render through the same real table), plus
    `rotations` (how many real coin switches occurred) and
    `final_coin`."""
    btc_closes, btc_highs, btc_lows, btc_times = btc_series
    btc_pairs = sorted(zip(btc_times, btc_closes))
    btc_times_sorted = [t for t, _ in btc_pairs]
    btc_closes_sorted = [c for t, c in btc_pairs]

    rank_series = {}
    for product_id, (closes, highs, lows, times) in candidates.items():
        pairs = sorted(zip(times, closes))
        rank_series[product_id] = ([t for t, _ in pairs], [c for t, c in pairs])

    if start_coin not in candidates:
        return None
    start_times = candidates[start_coin][3]
    if len(start_times) < 2:
        return None

    slice_usd = spend / num_levels
    trades = []
    current_coin = start_coin
    current_times, current_closes = rank_series[current_coin]
    reference = current_closes[0]
    open_slices = []
    last_rotation_at = None
    rotations = 0

    clock = sorted(set(start_times))
    for t in clock:
        price = _closest_close_at_or_before(current_times, current_closes, t)
        if price is None:
            continue
        reference, open_slices, net = _grid_step(price, reference, open_slices, slice_usd, grid_pct, num_levels)
        if net is not None:
            trades.append(("GRID_CYCLE", net))

        if rotation_enabled and not open_slices:
            cooldown_clear = last_rotation_at is None or (t - last_rotation_at) >= rotation_cooldown_hours * 3600
            if cooldown_clear:
                best_id = _best_ranked_candidate(rank_series, btc_times_sorted, btc_closes_sorted, t)
                if best_id and best_id != current_coin:
                    current_coin = best_id
                    current_times, current_closes = rank_series[current_coin]
                    new_price = _closest_close_at_or_before(current_times, current_closes, t)
                    if new_price is not None:
                        reference = new_price
                        last_rotation_at = t
                        rotations += 1

    final_price = _closest_close_at_or_before(current_times, current_closes, clock[-1])
    for slot in open_slices:
        gross = slot["qty"] * ((final_price or slot["entry"]) - slot["entry"])
        trades.append(("OPEN_AT_WINDOW_END", gross))

    result = _summarize_strategy_trades(trades, spend)
    if result is None:
        result = {"num_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "roi_pct_of_spend": 0.0, "avg_trade_pct": 0.0, "spend_used": spend}
    result["open_slices_at_end"] = len(open_slices)
    result["rotations"] = rotations
    result["final_coin"] = current_coin
    return result


async def run_grid_rotation_effectiveness_backtest(coins=None, days=BACKTEST_DAYS, spend=SPEND, max_concurrent=6):
    """SHADOW-MODE, additive - never touches live trading, never places a
    real order. For each real candidate coin, replays what a single real
    Grid Bot branch STARTING on that coin would have done over the real
    last `days` days two ways: parked the whole time (baseline) vs. free
    to auto-rotate to a better-ranked coin whenever flat (see
    _replay_grid_rotation's own docstring for the real mechanics and its
    one honest simplification - a BTC-relative-strength proxy standing in
    for the live blended ranking signal). Fetches every real candidate's
    full historical series ONCE (shared across every starting-coin
    replay, not re-fetched per coin) - this is O(coins) real API calls,
    not O(coins²)."""
    coins = coins or COIN_FAMILY_TREE
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _fetch(session, product_id):
        async with semaphore:
            candles = await fetch_historical_candles(session, product_id, days=days, last_error_out=last_error)
        return product_id, candles

    async with aiohttp.ClientSession() as session:
        btc_task = _fetch(session, "BTC-USD")
        coin_tasks = [_fetch(session, pid) for pid in coins if pid != "BTC-USD"]
        results = await asyncio.gather(btc_task, *coin_tasks)

    fetched = {pid: candles for pid, candles in results if candles is not None}
    skipped = [{"product_id": pid, "reason": last_error.get(pid, "not enough historical data")} for pid, candles in results if candles is None]
    btc_series = fetched.get("BTC-USD")
    candidates = {pid: c for pid, c in fetched.items() if pid != "BTC-USD"}

    if btc_series is None or len(candidates) < 2:
        return {"error": "not enough real historical data across the candidate pool to run this", "skipped": skipped}

    per_coin = []
    for start_coin in candidates:
        baseline = _replay_grid_rotation(candidates, btc_series, start_coin, spend=spend, rotation_enabled=False)
        with_rotation = _replay_grid_rotation(candidates, btc_series, start_coin, spend=spend, rotation_enabled=True)
        per_coin.append({"product_id": start_coin, "baseline": baseline, "with_rotation": with_rotation})

    def _total(key):
        return round(sum((row[key] or {}).get("total_pnl", 0.0) for row in per_coin), 2)

    baseline_total = _total("baseline")
    rotation_total = _total("with_rotation")
    coins_improved = sum(
        1 for row in per_coin
        if (row["with_rotation"] or {}).get("total_pnl", 0.0) > (row["baseline"] or {}).get("total_pnl", 0.0)
    )

    return {
        "backtest_days": days,
        "spend_per_trade": spend,
        "rotation_rank_lookback_hours": ROTATION_RANK_LOOKBACK_HOURS,
        "rotation_cooldown_hours": ROTATION_COOLDOWN_HOURS,
        "coins_tested": len(coins),
        "coins_with_results": len(per_coin),
        "skipped": skipped,
        "baseline_total_pnl": baseline_total,
        "with_rotation_total_pnl": rotation_total,
        "coins_improved_by_rotation": coins_improved,
        "coins_tested_count": len(per_coin),
        "better": "with_rotation" if rotation_total > baseline_total else "baseline",
        "per_coin": per_coin,
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
    """Real, current allocated_usd per coin, summed across BOTH real
    branch systems this codebase runs - CryptoTreeBranch (the family
    tree) AND CryptoGridBranch (Grid Bot) - since either one, or both at
    once, can hold real deployed capital in a given coin. Originally this
    only read CryptoTreeBranch; fixed after the account owner's own
    direct, repeated correction ("update this to our amount... pulling
    them from my bots") while the family tree sat fully retired ($0, no
    branches) and every real dollar in the account was actually sitting
    in Grid Bot instead - the table was technically working (nothing WAS
    allocated in the tree), but it wasn't answering the real question,
    which is "what would MY actual deployed money have done," regardless
    of which of the two real systems is currently holding it.

    Summed by product_id across BOTH tables together (not reported
    separately) - if a coin ever has real money in both the tree AND Grid
    Bot at once, that's genuinely the real total dollar exposure to that
    coin, and the whole point of this function is to answer "how much of
    MY real money is really riding on this coin right now," not to
    attribute it to one system or the other. Same aggregation-by-
    product_id pattern the per-coin trade history already uses. Root
    (BTC-USD) included like any other coin.

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
        tree_result = await db.execute(select(CryptoTreeBranch.product_id, CryptoTreeBranch.allocated_usd))
        for product_id, allocated_usd in tree_result.all():
            allocations[product_id] = allocations.get(product_id, 0.0) + allocated_usd
        grid_result = await db.execute(select(CryptoGridBranch.product_id, CryptoGridBranch.allocated_usd))
        for product_id, allocated_usd in grid_result.all():
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
    doesn't reflect what your actual real money would have done. This is
    the direct counterpart to run_full_backtest() that simulates each
    coin's REAL current dollars instead - pulled from
    _get_real_branch_allocations(), which reads BOTH real branch systems
    (the family tree AND Grid Bot, summed together) so this always
    reflects wherever your real money actually is right now, not just one
    of the two systems - e.g. with the family tree fully retired ($0) and
    Grid Bot actively holding real capital across several coins, this
    correctly simulates Grid Bot's real dollars, not a table of every
    coin falling back to the $150 default.

    A coin with no real allocation right now (in either real system)
    still gets tested - falls back to the same $150 default every other
    coin in run_full_backtest() uses, so the table stays complete rather
    than only showing whichever few coins real money happens to be in
    today. Every coin's `spend_used` in the result tells you which case
    applied.

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


# ============================================================================
# OPENING-BAR ELEPHANT/TAIL BREAKOUT - per the account owner's own real,
# fully-specified trading system, described directly across several real
# voice messages: pick a coin sitting in a narrow state; wait for its real
# first bar of the session; if that bar is a real "Elephant Bar" (an
# oversized green candle) or a real "bottoming tail" bar (a long lower
# wick - their own answer to what a "Tails" bar means), mark its high +
# one penny; the instant the SECOND bar's price reaches that level (never
# waiting for bar 2 to close), enter $25,000; the stop sits at bar 1's own
# low, for the life of the trade; once a real second "push" (a new higher
# high following a genuine pullback) confirms, start selling.
#
# SHADOW-MODE, real historical data, never places a real order. Same
# "evidence before trusting a claimed number" discipline as the
# narrow-range breakout backtest above - the account owner's own claimed
# 80%+ follow-through rate for this setup is tested here, not assumed.
#
# Crypto has no real discrete session open the way stocks do (see the
# Alpaca-side counterpart in alpaca_selection_backtest.py, which uses the
# real literal trading day) - per the account owner's own explicit "do
# both," this uses an INVENTED stand-in: 13:30 UTC, the real US stock
# market's own regular-session open time, chosen because it's a real,
# meaningful anchor (many crypto traders watch for a real volume/
# volatility pickup around the US open) rather than an arbitrary hour.
# Stated plainly: this is this session's own interpretation of "the
# morning" for a market that has no morning, not a literal transcription
# of the account owner's own words.
#
# Coinbase's public candles endpoint has no native 2-minute granularity
# (only 60/300/900/3600/21600/86400s) - real 1-minute candles are fetched
# and paired into synthetic real 2-minute bars (open of the first minute,
# close of the second, high/low across both) to match the account
# owner's own literal "2-minute bar" spec.
# ============================================================================

ELEPHANT_BAR_MIN_SIZE_MULTIPLE = 1.5
ELEPHANT_BAR_LOOKBACK = 10
TAIL_BAR_MIN_WICK_FRACTION = 0.6
OPENING_BAR_ENTRY_BUFFER_USD = 0.01
OPENING_BAR_SPEND_USD = 25000.0
PUSH_MIN_PULLBACK_PCT = 0.003
OPENING_BAR_SESSION_UTC_HOUR = 13
OPENING_BAR_SESSION_UTC_MINUTE = 30
OPENING_BAR_DAYS = 5  # deliberately much smaller than BACKTEST_DAYS (30) - see _fetch_1min_candles_window's own docstring for the real API-load reason
OPENING_BAR_GRANULARITY_SECONDS = 60


def _is_elephant_bar(bar: dict, preceding_bars: list) -> bool:
    """A real 'Elephant Bar' - a green (bullish) candle whose own real
    range is meaningfully larger (ELEPHANT_BAR_MIN_SIZE_MULTIPLE, 1.5x
    default) than the AVERAGE real range of the preceding real green
    bars (up to ELEPHANT_BAR_LOOKBACK, 10 default) - "sizable... larger
    and taller than the vast majority of the green bars before it," per
    the account owner's own real description. Requires at least 3 real
    preceding green bars to compare against - never guesses with too
    little real evidence. `bar`/`preceding_bars` are dicts with
    o/h/l/c keys."""
    if bar["c"] <= bar["o"]:
        return False
    green_preceding = [b for b in preceding_bars[-ELEPHANT_BAR_LOOKBACK:] if b["c"] > b["o"]]
    if len(green_preceding) < 3:
        return False
    avg_range = sum(b["h"] - b["l"] for b in green_preceding) / len(green_preceding)
    if avg_range <= 0:
        return False
    return (bar["h"] - bar["l"]) >= ELEPHANT_BAR_MIN_SIZE_MULTIPLE * avg_range


def _is_bottoming_tail_bar(bar: dict) -> bool:
    """A real 'bottoming tail' bar - a candle with a long real lower
    wick (rejection of the downside), the account owner's own real
    answer to what a "Tails" bar means: at least TAIL_BAR_MIN_WICK_FRACTION
    (60% default) of the bar's own total real range sits BELOW its own
    real body, signaling price was pushed down hard within the bar and
    then rejected back up before it closed."""
    total_range = bar["h"] - bar["l"]
    if total_range <= 0:
        return False
    body_low = min(bar["o"], bar["c"])
    lower_wick = body_low - bar["l"]
    return (lower_wick / total_range) >= TAIL_BAR_MIN_WICK_FRACTION


def _replay_opening_bar_breakout(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """Real replay of ONE real session's opening-bar breakout setup,
    exactly per the account owner's own described mechanics:

    - Bar 1 (session_bars[0]) must qualify as a real Elephant Bar
      (compared against `preceding_bars`, real history from BEFORE this
      session) or a real bottoming Tail bar - if neither, no real setup
      today, returns None.
    - The real entry trigger is bar 1's own high + $0.01 (a real
      buy-stop). Filled the instant ANY LATER real bar's own HIGH
      reaches that price, scanning forward as far as needed - never
      waiting for that bar to close. Per the account owner's own later,
      more precise example ("it looks like on bar 3... three is the one
      that pokes through"), the qualifying cross is NOT limited to bar
      2 specifically - it's whichever real bar first crosses, however
      many bars that takes. If price instead falls to bar 1's own low
      FIRST, before any bar ever crosses the entry trigger, this
      session's own interpretation is that the setup is abandoned - the
      level meant to protect the trade already failed before a real
      position was ever opened - returns None. If no bar ever crosses
      the trigger at all before the real session ends, also returns
      None.
    - The real stop-loss sits at bar 1's own low, unconditionally, for
      the life of the trade ("protect yourself below bar one... limiting
      your loss... to one bar").
    - Real exit: the hard STOP being hit, OR once a real second "push"
      confirms (a new real higher high following a genuine pullback from
      the running peak - PUSH_MIN_PULLBACK_PCT, 0.3% default - from the
      running peak) - "I'm looking for the stock to meet three pushes...
      after push two I start throwing sell orders." "Push" has no single
      unambiguous definition in raw price data - this operationalizes it
      as a genuine swing high after a real minimum pullback, stated
      plainly as this session's own interpretation, not a literal
      transcription. Real session end with neither firing marks to the
      real last close.

    Returns a real trade dict {qualifies_as, entry_price, entry_index,
    stop_price, exit_price, exit_reason, exit_index, pnl_usd, pnl_pct},
    or None if no real trade fired today."""
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
    qty = spend / entry_price

    def _result(exit_price, exit_reason, exit_index):
        pnl_usd = qty * (exit_price - entry_price)
        return {
            "qualifies_as": qualifies_as, "entry_price": entry_price, "entry_index": entry_idx,
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


def _aggregate_to_2min_bars(one_min_candles: list) -> list:
    """Pairs consecutive real 1-minute candles into synthetic real
    2-minute OHLC bars - open of the first minute, close of the second,
    high/low across both. Assumes real, consecutive (gap-free) 1-minute
    data, matching every real Coinbase candle response this fetches from.
    An odd real trailing candle (no pair) is dropped, not padded with a
    fabricated one. `one_min_candles` are dicts with t/o/h/l/c keys,
    oldest-first."""
    bars = []
    for i in range(0, len(one_min_candles) - 1, 2):
        a, b = one_min_candles[i], one_min_candles[i + 1]
        bars.append({
            "t": a["t"], "o": a["o"], "c": b["c"],
            "h": max(a["h"], b["h"]), "l": min(a["l"], b["l"]),
        })
    return bars


async def _fetch_1min_candles_window(session, product_id: str, days: int = OPENING_BAR_DAYS, last_error_out: dict = None):
    """Real, paginated 1-minute Coinbase candles for the last `days`
    days - deliberately a SMALLER default window (5 days, not this
    module's usual 30) than every other backtest tool here: 1-minute
    granularity over 30 real days is ~43,200 real candles per coin
    (roughly 144 real paginated requests each, at Coinbase's real
    300-candle-per-page limit) - impractical real API load across dozens
    of coins. 5 real days (~7,200 candles, ~24 real requests per coin)
    keeps this practical while still giving several real opening-bar
    setups to evaluate. Returns a real list of {t,o,h,l,c} dicts
    (oldest-first), or None if too little real data came back."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    all_candles = []
    cursor = start
    last_error = None
    while cursor < end:
        page_end = min(cursor + timedelta(seconds=OPENING_BAR_GRANULARITY_SECONDS * 299), end)
        url = (
            f"https://api.exchange.coinbase.com/products/{product_id}/candles"
            f"?granularity={OPENING_BAR_GRANULARITY_SECONDS}&start={cursor.isoformat()}&end={page_end.isoformat()}"
        )
        page_data = None
        for attempt in range(5):
            try:
                async with _CANDLE_HTTP_SEMAPHORE:
                    async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
                        if r.status == 429:
                            last_error = "HTTP 429 rate limited"
                            await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))
                            continue
                        if r.status != 200:
                            last_error = f"HTTP {r.status}"
                            break
                        page_data = await r.json()
                        last_error = None
                        break
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:100]}"
                await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))
        if page_data:
            all_candles.extend(page_data)
        elif last_error:
            break
        cursor = page_end
        await asyncio.sleep(0.2)

    if len(all_candles) < 120:
        if last_error_out is not None:
            last_error_out[product_id] = last_error or f"only {len(all_candles)} of 120 required real 1-min candles came back"
        return None
    all_candles.sort(key=lambda c: c[0])
    return [{"t": int(c[0]), "l": float(c[1]), "h": float(c[2]), "o": float(c[3]), "c": float(c[4])} for c in all_candles]


def _group_2min_bars_into_sessions(bars: list, session_utc_hour: int = OPENING_BAR_SESSION_UTC_HOUR,
                                    session_utc_minute: int = OPENING_BAR_SESSION_UTC_MINUTE) -> list:
    """Groups real 2-minute bars into real "sessions" anchored at the
    invented stand-in session-open time (13:30 UTC default - the real US
    stock market's own regular-session open) rather than raw UTC
    midnight - crypto has no genuine session boundary, so this defines
    one explicitly rather than pretending midnight UTC means anything
    special. Returns an ordered list of (session_start_iso, session_bars)
    tuples, oldest first - each session runs from one real anchor time to
    the next."""
    if not bars:
        return []
    from datetime import datetime as _dt
    groups = []
    current_bucket = []
    current_key = None
    for bar in bars:
        ts = _dt.fromtimestamp(bar["t"], tz=timezone.utc)
        # Shift the clock back by the anchor offset so grouping by real
        # UTC calendar date naturally buckets each real anchor-to-anchor
        # window together.
        shifted = ts - timedelta(hours=session_utc_hour, minutes=session_utc_minute)
        key = shifted.strftime("%Y-%m-%d")
        if key != current_key:
            if current_bucket:
                groups.append((current_key, current_bucket))
            current_bucket = []
            current_key = key
        current_bucket.append(bar)
    if current_bucket:
        groups.append((current_key, current_bucket))
    return groups


async def run_opening_bar_breakout_backtest(coins=None, days: int = OPENING_BAR_DAYS, max_concurrent: int = 4) -> dict:
    """SHADOW-MODE, real historical data (synthetic real 2-min bars
    aggregated from real 1-min Coinbase candles), per-coin AND
    aggregate. Never places a real order. coins=None (default) tests
    every real coin in COIN_FAMILY_TREE."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        sessions = _group_2min_bars_into_sessions(two_min)
        trades = []
        for i in range(1, len(sessions)):
            _key, session_bars = sessions[i]
            preceding_bars = sessions[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return product_id, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    per_coin = []
    skipped = []
    all_trades = []
    for product_id, trades in results:
        if trades is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_coin.append({
                "product_id": product_id, "num_trades": len(trades),
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
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "session_utc_anchor": f"{OPENING_BAR_SESSION_UTC_HOUR:02d}:{OPENING_BAR_SESSION_UTC_MINUTE:02d}",
            "days": days,
        },
    }


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
    """Real 20/200 SMA-convergence "state" detection - per the account
    owner's own real trading concept, transcribed directly: "moving
    averages far apart is a wide state... a tight narrow state [is] the
    20 a little below the 200... know the stock's state first and
    you've got 85% of the game figured out." A GENUINELY DIFFERENT real
    definition of "narrow" than this file's existing percentile-range
    method (_is_narrow_range_at) - that one looks at how tight the
    PRICE RANGE itself has been; this one looks at how close two moving
    averages of different real speeds currently sit to each other. Both
    are tested side by side in run_opening_bar_narrow_state_comparison
    below, never silently swapped for one another.

    `narrow_pct` (0.5% default) is an INVENTED threshold - the account
    owner described "close together" vs. "separated" but gave no real
    number, so this session picked one and is saying so plainly rather
    than presenting it as a literal transcription.

    Returns "narrow" (the two real SMAs sit within narrow_pct of each
    other), "wide_up" (the real 20 SMA has separated ABOVE the real 200
    SMA by more than narrow_pct), "wide_down" (separated BELOW), or None
    when there isn't yet enough real closes for both real SMAs."""
    sma_short = _sma_at(closes, i, short_period)
    sma_long = _sma_at(closes, i, long_period)
    if sma_short is None or sma_long is None or sma_long == 0:
        return None
    gap_pct = (sma_short - sma_long) / sma_long
    if abs(gap_pct) <= narrow_pct:
        return "narrow"
    return "wide_up" if gap_pct > 0 else "wide_down"


async def run_opening_bar_narrow_state_comparison(coins=None, days: int = OPENING_BAR_DAYS,
                                                    max_concurrent: int = 4) -> dict:
    """SHADOW-MODE. Compares three real narrow-state definitions against
    the IDENTICAL real Elephant/Tail opening-bar trades
    (_replay_opening_bar_breakout) - the only thing that ever changes
    between the three buckets is which sessions are considered
    "genuinely narrow" at the open, never a different trade outcome for
    a trade that qualifies under more than one:
      - "baseline": no narrow-state gate at all - every real qualifying
        Elephant/Tail setup, exactly what run_opening_bar_breakout_backtest's
        own shipped default already returns.
      - "percentile": gated on this file's existing _is_narrow_range_at
        (the real range sitting in the bottom 25th percentile of its own
        recent history), adapted here to real 2-minute bars.
      - "sma": gated on the account owner's own newly-described real
        20/200 SMA-convergence method (_sma_state_at).
    A session where a given method doesn't yet have enough real history
    to have an opinion is excluded from THAT method's bucket only -
    absence of evidence is never treated as "confirmed narrow." Never
    places a real order."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        sessions = _group_2min_bars_into_sessions(two_min)
        highs = [b["h"] for b in two_min]
        lows = [b["l"] for b in two_min]
        closes = [b["c"] for b in two_min]

        cum = 0
        session_start_idx = []
        for _key, sbars in sessions:
            session_start_idx.append(cum)
            cum += len(sbars)

        buckets = {"baseline": [], "percentile": [], "sma": []}
        for i in range(1, len(sessions)):
            _key, session_bars = sessions[i]
            preceding_bars = sessions[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if trade is None:
                continue
            buckets["baseline"].append(trade)

            idx_before = session_start_idx[i] - 1
            if idx_before < 0:
                continue
            is_narrow_pctl, _, _ = _is_narrow_range_at(
                idx_before, highs, lows,
                lookback=OPENING_BAR_PCTL_LOOKBACK_BARS, history=OPENING_BAR_PCTL_HISTORY_BARS,
            )
            if is_narrow_pctl:
                buckets["percentile"].append(trade)

            sma_state = _sma_state_at(closes, idx_before)
            if sma_state == "narrow":
                buckets["sma"].append(trade)

        return product_id, buckets

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades),
            "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
        }

    per_coin = []
    skipped = []
    overall_buckets = {"baseline": [], "percentile": [], "sma": []}
    for product_id, buckets in results:
        if buckets is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if buckets["baseline"]:
            per_coin.append({
                "product_id": product_id,
                "baseline": _summarize(buckets["baseline"]),
                "percentile": _summarize(buckets["percentile"]),
                "sma": _summarize(buckets["sma"]),
            })
        for k in overall_buckets:
            overall_buckets[k].extend(buckets[k])

    return {
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin,
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
    """Real contrarian mean-reversion trade, entered the instant the real
    20/200 SMA state flips into a genuine WIDE state - the account
    owner's own SEPARATE real trading idea from the Elephant/Tail
    breakout-continuation system above: "you become a contrarian trader
    in the wide state... the drop brings you back to narrow." A
    real, honest scope note baked into this function's own two
    directions: `direction="long"` (a real wide_down state - price has
    separated hard BELOW its own 200 SMA - betting on reversion back UP
    toward narrow) is executable by both real live bots today (crypto
    and Alpaca are both long-only in production). `direction="short"`
    (a real wide_up state, betting on reversion back DOWN) is included
    here as pure real diagnostic information only - NEITHER live bot can
    actually short today, a real, confirmed account-level restriction
    already documented elsewhere in this codebase, not a bug in this
    backtest.

    Real exit conditions, checked bar by bar from entry_idx+1:
    - STOP: price moves `stop_pct` (2% default, an INVENTED risk bound -
      the account owner never specified a number - stated plainly as
      this session's own interpretation) further AGAINST the contrarian
      bet, i.e. the wide state keeps extending instead of reverting.
    - REVERSION (the real win condition): the real 20/200 state
      genuinely returns to "narrow" - the account owner's own stated
      target ("the drop brings you back to narrow").
    - MAX_HOLD: a real time backstop (200 bars, ~6.7 real hours on
      2-minute bars) if neither of the above ever fires.

    Returns a real trade dict {direction, entry_price, entry_idx,
    stop_price, exit_price, exit_reason, exit_idx, pnl_usd, pnl_pct}, or
    None if entry_idx has no real close to enter at."""
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


async def run_wide_state_contrarian_backtest(coins=None, days: int = OPENING_BAR_DAYS, max_concurrent: int = 4) -> dict:
    """SHADOW-MODE, real historical 2-minute bars. The account owner's
    own separate "wide state -> contrarian reversion" idea (see
    _replay_wide_state_contrarian's own docstring for the real, honest
    long-vs-short scope note - the short/wide_up leg is diagnostic only,
    neither live bot can actually short today). Never places a real
    order. coins=None (default) tests every real coin in
    COIN_FAMILY_TREE."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        closes = [b["c"] for b in two_min]
        highs = [b["h"] for b in two_min]
        lows = [b["l"] for b in two_min]

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
        return product_id, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

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

    per_coin = []
    skipped = []
    all_trades = []
    for product_id, trades in results:
        if trades is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if trades:
            long_trades = [t for t in trades if t["direction"] == "long"]
            short_trades = [t for t in trades if t["direction"] == "short"]
            per_coin.append({
                "product_id": product_id,
                "long_wide_down": _summarize(long_trades),
                "short_wide_up_diagnostic_only": _summarize(short_trades),
            })
        all_trades.extend(trades)

    all_long = [t for t in all_trades if t["direction"] == "long"]
    all_short = [t for t in all_trades if t["direction"] == "short"]

    return {
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin,
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
    """The real bearish mirror of _is_elephant_bar - a real RED
    "power bar" the account owner described directly: "you can get a
    power bar to the downside... red solid power." A real RED bar
    (close < open) whose own range is at least `min_size_multiple`
    (1.5x default) the average range of the last real RED bars among
    `preceding_bars` (needs at least 3 real red preceding bars to judge
    against, same real minimum-evidence floor as the green version -
    never guesses off too little history)."""
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
    UPPER wick rejection candle (a real failed push higher, price
    rejected back down), the real opposite of the bottoming Tail's long
    lower wick. Returns False on a real zero-range bar (no real
    division by zero, same guard as the bottoming version)."""
    total_range = bar["h"] - bar["l"]
    if total_range <= 0:
        return False
    upper_wick = bar["h"] - max(bar["o"], bar["c"])
    return (upper_wick / total_range) >= min_wick_fraction


def _replay_opening_bar_breakout_short(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """The real bearish mirror of _replay_opening_bar_breakout - per the
    account owner's own direct description of the same system applied
    to the downside: a real RED Elephant Bar or a real topping Tail bar
    as bar 1, a real SHORT entry the instant bar 2's real price crosses
    bar 1's low MINUS $0.01 (never waiting for bar 2 to close), a real
    protective stop at bar 1's own HIGH for the life of the trade, and a
    real exit once a second downside "push" confirms (a new real lower
    low following a genuine real bounce from the running trough) or the
    real session ends.

    DIAGNOSTIC ONLY, by explicit design - neither live bot in this
    codebase can actually place a real short order today (a documented,
    confirmed account-level restriction on the Alpaca side; the crypto
    side has no short mechanism at all). This function exists to let the
    account owner's own full real system be judged in full, not to
    suggest it's tradeable as-is.

    The real entry trigger fires the instant ANY LATER real bar's own
    LOW crosses bar 1's low minus $0.01, scanning forward as far as
    needed (the same real "whichever bar pokes through" correction
    applied to the long-side function above) - abandoned if price
    instead rises to bar 1's own high first, before the trigger is ever
    reached.

    Returns a real trade dict, or None if no real trade fired today."""
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


async def run_opening_bar_short_side_backtest(coins=None, days: int = OPENING_BAR_DAYS, max_concurrent: int = 4) -> dict:
    """SHADOW-MODE, DIAGNOSTIC ONLY - the real bearish mirror of the live
    Elephant/Tail system: a real RED elephant bar or topping Tail bar
    breaking below a real narrow state. Never places a real order, and
    never could - the crypto side has no real short-selling mechanism at
    all today. coins=None (default) tests every real coin in
    COIN_FAMILY_TREE."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        sessions = _group_2min_bars_into_sessions(two_min)
        trades = []
        for i in range(1, len(sessions)):
            _key, session_bars = sessions[i]
            preceding_bars = sessions[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_opening_bar_breakout_short(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return product_id, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    per_coin = []
    skipped = []
    all_trades = []
    for product_id, trades in results:
        if trades is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_coin.append({
                "product_id": product_id, "num_trades": len(trades),
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
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "session_utc_anchor": f"{OPENING_BAR_SESSION_UTC_HOUR:02d}:{OPENING_BAR_SESSION_UTC_MINUTE:02d}",
            "days": days, "diagnostic_only": True,
        },
    }


SCALED_ENTRY_INITIAL_FRACTION = 0.5
SCALED_ENTRY_ADD_FRACTION = 0.25
SCALED_ENTRY_MAX_ADDS = 2


def _replay_opening_bar_breakout_scaled(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD,
                                         max_adds: int = SCALED_ENTRY_MAX_ADDS):
    """The account owner's own real scaling-in mechanic layered onto the
    unchanged long-side entry/stop/exit rules of
    _replay_opening_bar_breakout - transcribed directly: "you want to go
    in and then in and then in... usually two adds... let me put half
    in... that add arrow is one penny above the high of a single red
    bar." This session's own concrete interpretation of that real,
    informally-described mechanic: the initial real fill is HALF of
    `spend` at the same original trigger (bar 1's high + $0.01); every
    time a single real RED bar forms while the position is open, its
    high + $0.01 becomes a real pending add-trigger (a NEWER red bar's
    level replaces an older, still-unfilled one - only ever tracking the
    most recent single red bar, matching "it can't give you more than
    one, it gives you a little one"); the instant a LATER bar's real
    high trades through that level, a real quarter-size add (25% of
    `spend`) fires, up to `max_adds` (2 default) real adds - reaching
    100% of spend if both fire. The real stop (bar 1's own low) and the
    real push-based exit are COMPLETELY UNCHANGED from the single-shot
    version; only entry sizing and the resulting real blended entry
    price/P&L differ.

    Returns a real trade dict (same shape as _replay_opening_bar_breakout,
    plus `initial_entry_price`, `num_adds`, `total_spend`) - `entry_price`
    is the real quantity-weighted BLENDED entry across every real fill,
    the same convention this codebase's own "Add cash" blended-entry math
    already uses elsewhere. Returns None if no real trade fired today."""
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


async def run_scaled_entry_comparison_backtest(coins=None, days: int = OPENING_BAR_DAYS, max_concurrent: int = 4) -> dict:
    """SHADOW-MODE. Replays the IDENTICAL real qualifying Elephant/Tail
    setups two ways on the same real data: the existing single-shot,
    all-at-once entry (_replay_opening_bar_breakout, this file's own
    already-shipped default) vs. the account owner's own real
    half-in-then-two-adds scaling mechanic
    (_replay_opening_bar_breakout_scaled) - so "does scaling in actually
    help" gets a real, direct answer instead of a guess. Never places a
    real order."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        sessions = _group_2min_bars_into_sessions(two_min)
        single_shot_trades = []
        scaled_trades = []
        for i in range(1, len(sessions)):
            _key, session_bars = sessions[i]
            preceding_bars = sessions[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            t1 = _replay_opening_bar_breakout(session_bars, preceding_bars)
            if t1 is not None:
                single_shot_trades.append(t1)
            t2 = _replay_opening_bar_breakout_scaled(session_bars, preceding_bars)
            if t2 is not None:
                scaled_trades.append(t2)
        return product_id, single_shot_trades, scaled_trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    def _summarize(trades):
        if not trades:
            return None
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        return {
            "num_trades": len(trades),
            "win_rate": round(wins / len(trades), 4),
            "total_pnl": round(sum(t["pnl_usd"] for t in trades), 2),
        }

    per_coin = []
    skipped = []
    all_single = []
    all_scaled = []
    for product_id, single_trades, scaled_trades in results:
        if single_trades is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if single_trades:
            per_coin.append({
                "product_id": product_id,
                "single_shot": _summarize(single_trades),
                "scaled": _summarize(scaled_trades),
            })
        all_single.extend(single_trades)
        all_scaled.extend(scaled_trades)

    return {
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin,
        "overall": {"single_shot": _summarize(all_single), "scaled": _summarize(all_scaled)},
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD, "days": days,
            "initial_fraction": SCALED_ENTRY_INITIAL_FRACTION, "add_fraction": SCALED_ENTRY_ADD_FRACTION,
            "max_adds": SCALED_ENTRY_MAX_ADDS,
        },
    }


def _replay_red_bar_takeout_breakout(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """The account owner's own real THIRD, lower-conviction setup -
    transcribed directly: "even if you don't have an elephant or a
    tail... little red bar take outs [work too]." Bar 1 needs no
    special size or shape qualification here beyond being a real,
    ordinary RED bar (close < open) that does NOT already qualify as a
    real Elephant Bar or bottoming Tail bar - those are the two
    higher-conviction setups already tested separately
    (run_opening_bar_breakout_backtest); this is deliberately the
    fallback case, so it never double-counts a session already covered
    by the higher-conviction system.

    The exact same real entry/stop/exit mechanics as the other long-side
    setups: mark bar 1's high, enter the instant a LATER real bar's high
    crosses that level + $0.01 (scanning forward as far as needed,
    matching the account owner's own "bar 3 is the one that pokes
    through" example), stop at bar 1's own low, exit on a second real
    push or session end. Abandoned (returns None) if price falls to bar
    1's own low before the entry trigger is ever reached, or if no real
    bar ever crosses it.

    Returns a real trade dict, or None if no real trade fired today."""
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


async def run_red_bar_takeout_backtest(coins=None, days: int = OPENING_BAR_DAYS, max_concurrent: int = 4) -> dict:
    """SHADOW-MODE, real historical 2-minute bars. The account owner's
    own real third, lower-conviction long-side setup - an ordinary red
    bar 1, not a qualifying Elephant or Tail, whose high still gets
    "taken out" by a later real bar. Never places a real order.
    coins=None (default) tests every real coin in COIN_FAMILY_TREE."""
    coin_list = coins if coins is not None else list(COIN_FAMILY_TREE)
    semaphore = asyncio.Semaphore(max_concurrent)
    last_error = {}

    async def _one(session, product_id):
        async with semaphore:
            one_min = await _fetch_1min_candles_window(session, product_id, days=days, last_error_out=last_error)
        if one_min is None:
            return product_id, None
        two_min = _aggregate_to_2min_bars(one_min)
        sessions = _group_2min_bars_into_sessions(two_min)
        trades = []
        for i in range(1, len(sessions)):
            _key, session_bars = sessions[i]
            preceding_bars = sessions[i - 1][1][-ELEPHANT_BAR_LOOKBACK * 2:]
            trade = _replay_red_bar_takeout_breakout(session_bars, preceding_bars)
            if trade is not None:
                trades.append(trade)
        return product_id, trades

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(_one(session, pid) for pid in coin_list))

    per_coin = []
    skipped = []
    all_trades = []
    for product_id, trades in results:
        if trades is None:
            skipped.append({"product_id": product_id, "reason": last_error.get(product_id, "not enough real historical data")})
            continue
        if trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            per_coin.append({
                "product_id": product_id, "num_trades": len(trades),
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
        "coins_tested": len(coin_list), "coins_with_results": len(per_coin),
        "skipped": skipped, "per_coin": per_coin, "overall": overall,
        "params": {
            "spend_usd": OPENING_BAR_SPEND_USD,
            "entry_buffer_usd": OPENING_BAR_ENTRY_BUFFER_USD,
            "session_utc_anchor": f"{OPENING_BAR_SESSION_UTC_HOUR:02d}:{OPENING_BAR_SESSION_UTC_MINUTE:02d}",
            "days": days,
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
