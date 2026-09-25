"""Run the whole 432-variant sweep as a background job, with live progress.

WHY THIS IS A JOB AND NOT A REQUEST

432 variants across 8 coins is 3,456 replays plus the matched-control draws
behind each one. That is minutes, not seconds, and a request that long dies
to a proxy timeout with nothing to show. So the sweep runs in the
background, reports how far it has got, and the page polls - which also
means the ranking can be read while it is still filling in.

WHAT THIS DELIBERATELY DOES NOT DO

It does not decide anything. run_strategy_lab already ranks by
out-of-sample return and then refuses the winner on five separate grounds
(too few trades, overfit, not beating its matched control, under
buy-and-hold, fees dominate). This module runs it and reports it. Nothing
here re-scores, re-weights or promotes a variant, because the moment the
thing that RANKS also gets to JUDGE, the ranking stops being evidence.

THE TRAP THIS SWEEP IS BUILT AROUND

Ranking 3,456 tests by out-of-sample return and taking the top row is still
selection on out-of-sample. Holding out 30% of the data protects a SINGLE
hypothesis; it does not protect the best of thousands, because with
thousands of tries something clears any fixed bar by luck. This lab has the
measurement: on real BTC daily returns the best of 432 ZERO-EDGE strategies
reaches +65.3% out-of-sample at the 95th percentile, and the best of 3,456
reaches +92.0%. That is why every result carries the noise floor for the
width of the search that produced it, and why the top row of this table is
a candidate to forward-test rather than a strategy to fund.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# One job at a time, in memory. A sweep is a read-only measurement over
# public candles, so losing it to a restart costs a re-run and nothing else -
# not worth a table.
_JOB = {
    "state": "idle",        # idle | fetching | running | done | error
    "started_at": None,
    "finished_at": None,
    "done": 0,
    "total": 0,
    "current": None,
    "coins": [],
    "per_coin": {},
    "fleet": None,
    "error": None,
    "params": {},
}
_LOCK = asyncio.Lock()


def snapshot(include_results: bool = True) -> dict:
    """What the page polls. Cheap, and safe to call mid-run."""
    out = {k: v for k, v in _JOB.items() if k not in ("per_coin", "fleet")}
    out["elapsed_seconds"] = (
        round(time.time() - _JOB["started_at"], 1) if _JOB["started_at"] else None)
    out["pct"] = (round(_JOB["done"] / _JOB["total"] * 100, 1)
                  if _JOB["total"] else 0.0)
    if include_results:
        out["per_coin"] = _JOB["per_coin"]
        out["fleet"] = _JOB["fleet"]
    return out


def is_running() -> bool:
    return _JOB["state"] in ("fetching", "running")


async def start(coins, days=730, granularity=86400, in_sample_frac=0.7,
                control_draws=10, fee_round_trip=None,
                sizing="full", fraction=0.25, target_vol=0.02, vol_window=20):
    """Kick off a sweep. Refuses to start a second one over a live one."""
    async with _LOCK:
        if is_running():
            return {"status": "already_running", **snapshot(include_results=False)}
        _JOB.update({
            "state": "fetching", "started_at": time.time(), "finished_at": None,
            "done": 0, "total": 0, "current": None, "coins": list(coins),
            "per_coin": {}, "fleet": None, "error": None,
            "params": {"days": days, "granularity": granularity,
                       "in_sample_frac": in_sample_frac,
                       "control_draws": control_draws,
                       "fee_round_trip": fee_round_trip,
                       "sizing": sizing, "fraction": fraction,
                       "target_vol": target_vol, "vol_window": vol_window},
        })
    asyncio.create_task(_run(coins, days, granularity, in_sample_frac,
                             control_draws, fee_round_trip,
                             sizing, fraction, target_vol, vol_window))
    return {"status": "started", **snapshot(include_results=False)}


async def _fetch_series(coins, days, granularity):
    """Real Coinbase candles per coin. A coin that will not load is REPORTED,
    never quietly dropped - a sweep silently run on 6 of 8 coins is a
    different experiment from the one that was asked for."""
    import aiohttp
    import crypto_selection_backtest as CSB

    series, failures = {}, {}
    end = datetime.now(timezone.utc)
    start_at = end - timedelta(days=days)
    async with aiohttp.ClientSession() as session:
        for coin in coins:
            _JOB["current"] = f"loading {coin}"
            err = {}
            try:
                got = await CSB.fetch_candles_window(
                    session, coin, start_at, end,
                    granularity=granularity, last_error_out=err)
            except Exception as e:
                failures[coin] = f"{type(e).__name__}: {e}"
                continue
            if not got or not got[0] or len(got[0]) < 80:
                failures[coin] = (err.get("error")
                                  or f"only {len(got[0]) if got and got[0] else 0} candles")
                continue
            series[coin] = got
    return series, failures


async def _run(coins, days, granularity, in_sample_frac, control_draws, fee,
               sizing="full", fraction=0.25, target_vol=0.02, vol_window=20):
    import strategy_lab as LAB
    try:
        series, failures = await _fetch_series(coins, days, granularity)
        if not series:
            _JOB.update({"state": "error", "finished_at": time.time(),
                         "error": f"no coin loaded any candles: {failures}"})
            return

        _JOB["total"] = LAB.VARIANT_COUNT * len(series)
        _JOB["state"] = "running"
        _JOB["coin_load_failures"] = failures

        per_coin = {}
        for coin, got in series.items():
            _JOB["current"] = coin
            closes, highs, lows = got[0], got[1], got[2]
            times = got[3] if len(got) > 3 else None
            # run_strategy_lab is CPU-bound and synchronous; a thread keeps
            # the event loop answering the progress poll that this whole
            # design exists to serve.
            res = await asyncio.to_thread(
                LAB.run_strategy_lab, closes, highs, lows, None,
                in_sample_frac, control_draws, fee, coin,
                sizing, fraction, target_vol, vol_window)
            res["split_label"] = _split_label(times, res.get("in_sample_bars"))
            res["granularity_seconds"] = granularity
            per_coin[coin] = res
            _JOB["done"] += LAB.VARIANT_COUNT
            _JOB["per_coin"] = per_coin

        _JOB["current"] = "cross-checking the winner on every coin"
        # run_fleet unpacks (closes, highs, lows); fetch_candles_window
        # returns a FOURTH element, the timestamps. Passing the raw tuples
        # through raised "too many values to unpack" and killed the sweep
        # after every per-coin result was already computed - so the cross-coin
        # check, which is the part that catches a one-lucky-coin winner,
        # never ran.
        _JOB["fleet"] = await asyncio.to_thread(
            LAB.run_fleet, {c: (g[0], g[1], g[2]) for c, g in series.items()},
            None, in_sample_frac, control_draws, fee)
        _JOB.update({"state": "done", "finished_at": time.time(), "current": None})
    except Exception as e:
        log.exception("[LAB] sweep failed")
        _JOB.update({"state": "error", "finished_at": time.time(),
                     "error": f"{type(e).__name__}: {e}"})


def _split_label(times, in_sample_bars):
    """The DATE the out-of-sample period begins.

    Reported as a date rather than a bar index because that is what has to
    be matched when the same strategy is run somewhere else: two engines
    testing different periods will disagree for a reason that has nothing to
    do with the strategy, and a bar count cannot be checked against
    TradingView by eye.
    """
    if not times or not in_sample_bars or in_sample_bars >= len(times):
        return None
    t = times[in_sample_bars]
    try:
        if isinstance(t, (int, float)):
            return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(t, datetime):
            return t.strftime("%Y-%m-%d")
        return str(t)[:10]
    except Exception:
        return None


def _row_verdict(oos, floor, row, bh):
    """Why this row is or is not evidence - in the order that matters.

    Deliberately mirrors run_strategy_lab's own refusals rather than
    inventing a second opinion. A table that ranks by return and a verdict
    that judges by something else would contradict each other on screen.
    """
    import strategy_lab as LAB
    if oos["trades"] < LAB.MIN_OOS_TRADES:
        n = oos["trades"]
        return (f"only {n} trade{'' if n == 1 else 's'} - too few to mean anything "
                f"(needs {LAB.MIN_OOS_TRADES})")
    if floor is not None and oos["total_return_pct"] <= floor:
        return f"under the {floor:+.1f}% that luck reaches at this search width"
    gap = row.get("overfit_gap_pct")
    if gap is not None and gap > LAB.OVERFIT_GAP_LIMIT_PCT:
        return f"in-sample collapsed {gap:.0f} points out-of-sample - fitted, not found"
    p = row.get("percentile_vs_control")
    if p is not None and p < 0.8:
        return f"beat its matched control only {p * 100:.0f}% of the time"
    if bh is not None and oos["total_return_pct"] <= bh:
        return f"under buy-and-hold ({bh:+.1f}%), which paid one fee"
    return "clears every check - worth forward-testing, not funding"


def ranked_rows(coin_filter: str = "", limit: int = 60, sort_by: str = "oos"):
    """Every variant from every completed coin, as one ranked table.

    Sorted by OUT-OF-SAMPLE by default, which is the only ranking worth
    reading - and still a ranking over thousands of tests, which is why
    `noise_floor_p95` rides along on every row rather than living in a
    footnote somewhere the reader has already scrolled past.
    """
    import strategy_lab as LAB
    _sizing = (_JOB.get("params") or {}).get("sizing", "full")
    rows = []
    for coin, res in (_JOB["per_coin"] or {}).items():
        if coin_filter and coin_filter.upper() not in coin.upper():
            continue
        if not res or "ranked" not in res:
            continue
        floor = (res.get("noise_floor") or {}).get("p95")
        for r in res["ranked"]:
            oos, ins = r["out_of_sample"], r["in_sample"]
            rows.append({
                "coin": coin,
                "strategy": r["strategy"],
                "params": r["params"],
                "oos_return_pct": oos["total_return_pct"],
                "in_sample_return_pct": ins["total_return_pct"],
                "oos_win_rate": oos["win_rate"],
                "in_sample_win_rate": ins["win_rate"],
                "oos_trades": oos["trades"],
                "oos_sharpe": oos.get("sharpe"),
                "oos_max_drawdown_pct": oos["max_drawdown_pct"],
                "oos_final_balance": oos.get("final_balance"),
                # WHERE the money came from, not just how much. The pair that
                # matters is break-even vs realised win rate: the gap between
                # them IS the edge, and it is the only thing that explains a
                # 41%-win strategy making money while a 70%-win one loses.
                "avg_win_pct": oos.get("avg_win_pct"),
                "avg_loss_pct": oos.get("avg_loss_pct"),
                "break_even_win_rate_pct": oos.get("break_even_win_rate_pct"),
                "edge_points": oos.get("edge_points"),
                "profit_factor": oos.get("profit_factor"),
                "win_uniformity": oos.get("win_uniformity"),
                "overfit_gap_pct": r["overfit_gap_pct"],
                "beat_control": r.get("beat_control"),
                "percentile_vs_control": r.get("percentile_vs_control"),
                "noise_floor_p95": floor,
                "min_trades_for_a_verdict": LAB.MIN_OOS_TRADES,
                "enough_trades": oos["trades"] >= LAB.MIN_OOS_TRADES,
                # The single most important column, and the one a plain
                # ranking hides: a row can top the table and still be under
                # what pure luck reaches at this search width.
                #
                # Trade count is part of the SAME question, not a separate
                # one. The noise floor is computed for a typical trade count,
                # so comparing a 2-trade result against it is not a
                # comparison - and the first sweep run through this table
                # duly put a 2-trade, +50%, Sharpe-14.2 row on top and
                # labelled it "above luck". It was two lucky moves. A row
                # can only clear this bar by clearing BOTH.
                "above_noise_floor": (None if floor is None
                                      else (oos["total_return_pct"] > floor
                                            and oos["trades"] >= LAB.MIN_OOS_TRADES)),
                "verdict_short": _row_verdict(oos, floor, r, res.get("buy_and_hold_oos_pct")),
                "split_label": res.get("split_label"),
                "buy_and_hold_oos_pct": res.get("buy_and_hold_oos_pct"),
            })

    keys = {
        "oos": lambda r: -(r["oos_return_pct"]),
        # Rows without enough trades sort to the BOTTOM of every quality
        # ranking rather than the top. A Sharpe of 66.0 over 2 trades is
        # arithmetic on a sample of two, and sorting by it hands the top of
        # the table to exactly the rows the verdict refuses.
        "sharpe": lambda r: (not r["enough_trades"],
                             -(r["oos_sharpe"] if r["oos_sharpe"] is not None else -9e9)),
        "oos_quality": lambda r: (not r["enough_trades"], -r["oos_return_pct"]),
        "drawdown": lambda r: (not r["enough_trades"], r["oos_max_drawdown_pct"]),
        "trades": lambda r: -r["oos_trades"],
        "in_sample": lambda r: -(r["in_sample_return_pct"]),
    }
    rows.sort(key=keys.get(sort_by, keys["oos"]))
    return {
        "rows": rows[:limit],
        "total_rows": len(rows),
        "sorted_by": sort_by if sort_by in keys else "oos",
        "coin_filter": coin_filter,
        # Stated on the results themselves rather than left to be remembered.
        # Both of these change what a number means, and neither is visible in
        # the number.
        "sizing": _sizing,
        # The caveat has to follow the run. Printing "these are full-equity
        # figures" over a volatility-sized sweep would be a warning about a
        # distortion that is no longer there - which teaches the reader to
        # ignore the line that matters when it IS there.
        "sizing_caveat": (
            "Every figure here is FULL-EQUITY sizing: each trade risks the whole "
            "balance, and each win compounds into the next position. Nothing is "
            "ever traded that way. Real sizing - a fixed fraction, or a "
            "volatility-scaled one - produces a smaller return AND a much smaller "
            "drawdown, so these totals are an upper bound on the return and on the "
            "risk together, not a forecast of either."
            if _sizing == "full" else
            f"Sized as '{_sizing}', not full equity, so these returns are NOT "
            f"comparable with a full-equity run - the drawdowns are smaller for "
            f"the same reason the returns are. Sizing decides how much of an edge "
            f"you keep and how much drawdown you take collecting it; it cannot "
            f"create one, and a strategy that loses here loses at any size."),
        "execution_caveat": (
            "Fills are at the next bar's CLOSE, with no intrabar stop or target, so "
            "no bar can hit both and force a guess about which came first. An engine "
            "that does model intrabar exits will differ - that is a different "
            "assumption, not a different result."),
        "note": ("Ranked by out-of-sample. Holding back 30% of the data protects ONE "
                 "hypothesis, not the best of thousands - with this many variants "
                 "something clears any fixed bar by luck, which is what "
                 "noise_floor_p95 measures. A row is a candidate to forward-test, "
                 "never a strategy to fund on a backtest."),
    }
