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
import sys
sys.path.insert(0, "/home/user/empire-v2")
import os
os.environ.setdefault("COINBASE_API_KEY_NAME", "unused-public-endpoint-only")
os.environ.setdefault("COINBASE_API_PRIVATE_KEY", "unused-public-endpoint-only")

import aiohttp
from datetime import datetime, timedelta, timezone

import crypto_btc_compound_bot as engine
from crypto_family_tree_bot import COIN_FAMILY_TREE, BREAKEVEN_TRIGGER_PCT, MAX_PROFIT_GIVEBACK_USD

SPEND = 150.0
BACKTEST_DAYS = 30
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
    return closes, highs, lows


def backtest_one_coin(closes, highs, lows):
    """Replays the REAL live rules candle-by-candle. Returns a dict of
    results, or None if there wasn't enough data to trade at all."""
    trades = []
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
            target_pct = max(engine.pick_target_pct(atr_pct), engine.min_profit_target_pct(SPEND, atr_pct))
            qty = SPEND / price
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
    avg_trade_pct = (total_pnl / len(trades)) / SPEND * 100
    return {
        "num_trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "roi_pct_of_spend": total_pnl / SPEND * 100,
        "avg_trade_pct": avg_trade_pct,
    }


async def _backtest_one_coin_with_semaphore(session, product_id, semaphore):
    async with semaphore:
        candles = await fetch_historical_candles(session, product_id)
    if candles is None:
        return product_id, None, "not enough historical data"
    closes, highs, lows = candles
    result = backtest_one_coin(closes, highs, lows)
    if result is None:
        return product_id, None, "no trades triggered in this window"
    return product_id, result, None


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
