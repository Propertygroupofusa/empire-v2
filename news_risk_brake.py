"""A news brake for the grid: pause NEW BUYS during a one-way run. Test it first.

THE IDEA, AND WHY IT IS SHAPED THIS WAY

The obvious use of a news feed is to predict direction and trade it. At
this account's fees that is arithmetically dead, and the numbers are not
close. Measured from 349 real hourly BTC candles on 2026-09-25:

    median 60-minute move        0.15%
    90th percentile              0.54%
    99th percentile              1.37%
    taker round trip             1.50%

Only 3 hours in 349 (0.9%) moved further than the round-trip fee. At the
median hour a PERFECT directional call still nets -1.35%. No win rate
fixes that, because the problem is not accuracy - the fee is ten times
the typical move.

But a grid does not lose money by picking the wrong direction. It loses
money when price runs ONE WAY and fills every level with no bounce to
sell into. The retired family tree's single biggest loss bucket was
$224.04 across 94 "STOP HIT" exits - a mean-reverting strategy being fed
into a trend.

That is the thing news is actually good for: not "which way next hour",
but "something just happened, stop opening new positions for a while".
It never has to beat the fee, because it never opens a trade.

WHY THIS TESTS PRICE AND NOT HEADLINES

A news signal's only value is detecting a one-way run EARLIER than price
does. So the mechanic has to be worth something when driven by price
first. If pausing buys during a measured downtrend does not improve the
grid, then pausing them slightly sooner on a headline cannot either, and
the whole newsroom is a nicer-looking way to lose the same money.

So: build the brake, drive it from price, and measure. If it helps, a
news feed is an upgrade to the trigger. If it does not, this is where the
idea stops - before any money or any feed subscription is spent on it.

Nothing here is live. It places no orders and is imported by no bot.
"""

import asyncio
import statistics


# ── the detectors ───────────────────────────────────────────────────────
#
# Each returns a list of booleans, one per candle: True means "do not open
# a NEW position on this bar". Selling is never blocked - an open slice
# must always be able to get out, whatever the signal says.


def brake_on_drawdown(closes, lookback=6, drop_pct=0.02, hold_bars=6):
    """Brake while price is down `drop_pct` over the last `lookback` bars.

    The plainest proxy for "bad news just landed": a sustained one-way
    move down. `hold_bars` keeps the brake on for a while after the
    condition clears, because a grid's danger is not the first bar of a
    run, it is the next several.
    """
    n = len(closes)
    mask = [False] * n
    remaining = 0
    for i in range(n):
        if i >= lookback:
            change = closes[i] / closes[i - lookback] - 1.0
            if change <= -abs(drop_pct):
                remaining = hold_bars
        if remaining > 0:
            mask[i] = True
            remaining -= 1
    return mask


def brake_on_volatility(closes, lookback=24, spike_mult=2.0, hold_bars=6):
    """Brake when recent bar-to-bar movement spikes above its own normal.

    Direction-agnostic. A volatility spike is what a major headline
    actually produces, and a grid filling levels into one is the failure
    mode being guarded against.
    """
    n = len(closes)
    mask = [False] * n
    if n < lookback + 2:
        return mask
    steps = [abs(closes[i] / closes[i - 1] - 1.0) for i in range(1, n)]
    remaining = 0
    for i in range(1, n):
        window = steps[max(0, i - 1 - lookback):i - 1]
        if len(window) >= lookback // 2:
            base = statistics.median(window)
            if base > 0 and steps[i - 1] >= base * spike_mult:
                remaining = hold_bars
        if remaining > 0:
            mask[i] = True
            remaining -= 1
    return mask


def brake_on_consecutive_red(closes, streak=4, hold_bars=6):
    """Brake after `streak` consecutive down bars - a run, not a wobble."""
    n = len(closes)
    mask = [False] * n
    run = 0
    remaining = 0
    for i in range(1, n):
        run = run + 1 if closes[i] < closes[i - 1] else 0
        if run >= streak:
            remaining = hold_bars
        if remaining > 0:
            mask[i] = True
            remaining -= 1
    return mask


DETECTORS = {
    "drawdown_2pct_6h": lambda c: brake_on_drawdown(c, 6, 0.02, 6),
    "drawdown_3pct_12h": lambda c: brake_on_drawdown(c, 12, 0.03, 12),
    "volatility_2x": lambda c: brake_on_volatility(c, 24, 2.0, 6),
    "volatility_3x": lambda c: brake_on_volatility(c, 24, 3.0, 12),
    "four_red_bars": lambda c: brake_on_consecutive_red(c, 4, 6),
    # The control. A brake that fires on a coin flip, at roughly the same
    # rate as the real ones, is the only way to tell a real signal from
    # "pausing sometimes happens to help". Any detector that cannot beat
    # this is not a signal.
    "RANDOM_control": None,
}


def _random_mask(closes, fire_rate, seed=1234):
    """A brake that fires at the same rate as a real one, but at random."""
    import random
    rng = random.Random(seed)
    return [rng.random() < fire_rate for _ in closes]


async def run_news_brake_backtest(coins=None, days=90, spend=None,
                                  buy_pct=0.02, num_levels=3,
                                  max_concurrent=4, control_draws=25) -> dict:
    """Does pausing NEW BUYS during a one-way run actually help the grid?

    Replays the same grid twice per coin on the same real candles - once
    untouched, once with each brake - and reports the difference. Uses
    _replay_grid_bot_v2, which already models the two live guards (never
    sell at a loss, pause buys only), so the answer is about the brake and
    nothing else.

    Fees are the measured taker round trip, not the 0.8% this file used to
    assume.
    """
    import crypto_selection_backtest as bt

    coins = coins or ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD",
                      "NEAR-USD", "ETC-USD", "BCH-USD", "ARB-USD"]
    spend = spend if spend else bt.SPEND
    sem = asyncio.Semaphore(max_concurrent)

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async def one(product_id):
            async with sem:
                err = {}
                # fetch_historical_candles returns (closes, highs, lows,
                # times) - four parallel lists, NOT a list of candles.
                # Iterating it as candle rows yields exactly 4 "candles"
                # and a silently useless backtest.
                fetched = await bt.fetch_historical_candles(
                    session, product_id, days=days, last_error_out=err)
                if not fetched:
                    return product_id, {"error": err.get("error") or "no candles"}
                closes, highs, lows, _times = fetched
                if len(closes) < 50:
                    return product_id, {"error": f"only {len(closes)} candles"}

                base = bt._replay_grid_bot_v2(
                    closes, highs, lows, spend=spend,
                    buy_pct=buy_pct, num_levels=num_levels)
                if base is None:
                    return product_id, {"error": "baseline replay produced nothing"}

                out = {"candles": len(closes), "baseline": base, "brakes": {}}
                for name, make in DETECTORS.items():
                    if name == "RANDOM_control":
                        continue
                    mask = make(closes)
                    res = bt._replay_grid_bot_v2(
                        closes, highs, lows, spend=spend, buy_pct=buy_pct,
                        num_levels=num_levels, brake_mask=mask)
                    if res is None:
                        continue
                    res["fire_rate"] = round(sum(mask) / len(mask), 4)
                    res["delta_usd"] = round(res["total_pnl"] - base["total_pnl"], 2)
                    out["brakes"][name] = res

                # A PROPER control. The first version drew one random
                # mask at the AVERAGE fire rate of all detectors - 34%,
                # while the winning detector fired at 19%. Comparing a
                # brake against a differently-sized brake measures how
                # much you paused, not whether the signal meant anything.
                #
                # So: for each detector, draw `control_draws` random masks
                # at THAT detector's own fire rate, and report the
                # distribution. A real signal has to beat most of them,
                # not just one lucky draw.
                for name, r in list(out["brakes"].items()):
                    rate = r["fire_rate"]
                    ctrl_deltas = []
                    for seed in range(control_draws):
                        c = bt._replay_grid_bot_v2(
                            closes, highs, lows, spend=spend, buy_pct=buy_pct,
                            num_levels=num_levels,
                            brake_mask=_random_mask(closes, rate, seed=seed * 7919 + 13))
                        if c is not None:
                            ctrl_deltas.append(c["total_pnl"] - base["total_pnl"])
                    if ctrl_deltas:
                        ctrl_deltas.sort()
                        beat = sum(1 for d in ctrl_deltas if r["delta_usd"] > d)
                        r["control_median_delta"] = round(
                            ctrl_deltas[len(ctrl_deltas) // 2], 2)
                        r["control_best_delta"] = round(ctrl_deltas[-1], 2)
                        r["beat_n_of_controls"] = f"{beat}/{len(ctrl_deltas)}"
                        r["percentile_vs_random"] = round(beat / len(ctrl_deltas), 3)
                return product_id, out

        pairs = await asyncio.gather(*[one(c) for c in coins])

    per_coin = dict(pairs)
    usable = {k: v for k, v in per_coin.items() if "error" not in v}

    summary = {}
    for name in list(DETECTORS.keys()):
        deltas, skipped, fires = [], 0, []
        for v in usable.values():
            r = v["brakes"].get(name)
            if not r:
                continue
            deltas.append(r["delta_usd"])
            skipped += r.get("braked_buys_skipped", 0)
            fires.append(r.get("fire_rate", 0))
        if not deltas:
            continue
        pcts = [v["brakes"][name].get("percentile_vs_random")
                for v in usable.values()
                if v["brakes"].get(name) and v["brakes"][name].get("percentile_vs_random") is not None]
        ctrl_med = [v["brakes"][name].get("control_median_delta")
                    for v in usable.values()
                    if v["brakes"].get(name) and v["brakes"][name].get("control_median_delta") is not None]
        summary[name] = {
            "total_delta_usd": round(sum(deltas), 2),
            "coins_improved": sum(1 for d in deltas if d > 0),
            "coins_worsened": sum(1 for d in deltas if d < 0),
            "coins_unchanged": sum(1 for d in deltas if d == 0),
            "coins_tested": len(deltas),
            "buys_skipped": skipped,
            "avg_fire_rate": round(sum(fires) / len(fires), 4) if fires else None,
            # How often this detector beat a RANDOM brake that paused
            # exactly as much. 0.5 is chance. This is the number that
            # decides whether there is a signal at all.
            "avg_percentile_vs_random": round(sum(pcts) / len(pcts), 3) if pcts else None,
            "control_median_total": round(sum(ctrl_med), 2) if ctrl_med else None,
        }

    # Rank by whether the signal BEAT A RANDOM BRAKE THAT PAUSED AS MUCH,
    # not by raw dollars. A detector that pauses more will often make more
    # on a falling window for no reason other than pausing more.
    ranked = sorted(summary.items(),
                    key=lambda kv: (-(kv[1].get("avg_percentile_vs_random") or 0),
                                    -kv[1]["total_delta_usd"]))

    verdict = "no detector produced a usable result"
    if ranked:
        name, st = ranked[0]
        pct = st.get("avg_percentile_vs_random")
        d = st["total_delta_usd"]
        n = st["coins_tested"]
        if n < 5:
            verdict = (f"NOT ENOUGH COINS. Only {n} produced a usable replay, which is too "
                       f"thin to conclude anything. Best so far is {name} at ${d:+.2f}.")
        elif pct is None:
            verdict = "the random control did not run, so nothing here is interpretable"
        elif pct < 0.6:
            verdict = (f"NO SIGNAL. The best detector ({name}) beat a random brake that "
                       f"paused exactly as often only {pct * 100:.0f}% of the time - chance "
                       f"is 50%. Its ${d:+.2f} comes from pausing, not from knowing. A news "
                       f"feed would make it pause sooner, which is not the missing piece.")
        elif d <= 0:
            verdict = (f"{name} beat random {pct * 100:.0f}% of the time but still LOST "
                       f"${d:+.2f} overall. Better than chance at losing money is not worth "
                       f"building a newsroom for.")
        else:
            verdict = (f"{name}: ${d:+.2f} across {n} coins, beating a rate-matched random "
                       f"brake {pct * 100:.0f}% of the time (chance is 50%). That is a real "
                       f"but modest edge on a small sample - worth forward-testing before "
                       f"any feed is paid for, not worth wiring live today.")

    return {
        "coins_tested": len(usable),
        "coins_failed": {k: v["error"] for k, v in per_coin.items() if "error" in v},
        "days": days,
        "buy_pct": buy_pct,
        "num_levels": num_levels,
        "round_trip_fee_rate": bt.BACKTEST_ROUND_TRIP_FEE_RATE,
        "summary": summary,
        "ranked": [n for n, _ in ranked],
        "control_draws_per_detector": control_draws,
        "verdict": verdict,
        "per_coin": per_coin,
        "note": ("The brake pauses NEW BUYS only and never blocks a sell. It is driven "
                 "by price here on purpose: a news feed's only value is spotting the same "
                 "one-way run sooner, so if the mechanic does not pay when driven by "
                 "price, a headline cannot rescue it."),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run_news_brake_backtest()), indent=1, default=str)[:4000])
