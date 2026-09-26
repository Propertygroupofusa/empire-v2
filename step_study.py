"""How often would a grid fire at step X, and would the money be worth it?

Built 2026-09-26 after the account owner asked for the fleet to "trigger
money faster". The intuition is that a tighter step trades more and
therefore earns more. Measured on the live fleet's own six coins over 60
days, it does the opposite:

    step     trips/60d    net @ 0.70% maker RT
    2.50%       30            +$16.62
    1.50%       55            +$13.54
    1.00%       77            +$ 7.11
    0.75%      105            +$ 1.62
    0.50%      137            -$ 8.43
    0.25%      185            -$25.62

Six times the activity, and the result goes from +$16.62 to -$25.62. The
fee is a FIXED toll per round trip: halving the step halves the gross and
leaves the toll where it is. Below step == fee, every completed trip is a
loss.

This module exists so that arithmetic is re-runnable rather than
remembered - fees change, coins change, and the answer should be measured
again rather than quoted from a chat log.

    python3 step_study.py                      # the live fleet, 60 days
    python3 step_study.py --days 30 --fee 0.0
    python3 step_study.py --coins BTC-USD,ONDO-USD

COMPOUNDING COMES AFTER PROFIT, never before - the account owner's own
rule, and already law in the code: evaluate_adaptive_fleet_stages() will
not open a later stage until realized P&L reaches its threshold, and one
unmet stage blocks the whole tail behind it. Nothing in this study sizes
up a fleet; it only prices what a step would have earned.

NOT a backtest of profit. It counts oscillation and prices it, assuming
every round trip COMPLETES. In the real book winners close and losers sit
open, so these figures are a ceiling, not a forecast. The output says so.
"""

import argparse
import asyncio
import datetime

# The fleet's six coins as of 2026-09-26. Passed explicitly rather than
# read from the database so the study runs anywhere, including against a
# fleet that no longer exists.
DEFAULT_COINS = ["BTC-USD", "NEAR-USD", "BONK-USD", "ONDO-USD", "FLOKI-USD", "TIA-USD"]

DEFAULT_STEPS = [0.025, 0.015, 0.010, 0.0075, 0.005, 0.0025]

# Maker on both legs at the account's real 0.35%/leg. The study takes the
# round-trip figure so a taker fill (1.50%) or a zero-fee tier can be
# priced by passing --fee.
DEFAULT_ROUND_TRIP_FEE_PCT = 0.70

# Spread alone still costs something even at a zero fee tier. Priced so a
# "free" column cannot read as literally free.
SPREAD_ONLY_PCT = 0.05


def net_per_trip_pct(step_pct, round_trip_fee_pct):
    """What one completed round trip keeps, as a percent of the slice."""
    return round(float(step_pct) - float(round_trip_fee_pct), 4)


def verdict(net_pct):
    """Plain words for the number, so a table cannot be skimmed wrongly."""
    if net_pct <= 0:
        return "LOSES on every trip"
    if net_pct < 0.5:
        return "thin"
    return "clears"


def breakeven_step_pct(round_trip_fee_pct):
    """The step below which every completed trip loses money."""
    return round(float(round_trip_fee_pct), 4)


def max_fee_for_step(step_pct, min_net_pct=0.0):
    """The highest round-trip fee at which this step still clears.

    The inverse of the table, and the more useful direction. A step that
    loses money is not a broken step - it is a step being run at the wrong
    fee, and this says which fee would fix it. On the live fleet the two
    losing rows need:

        0.50% step  ->  fee at or under 0.50%   (it pays 0.70%)
        0.25% step  ->  fee at or under 0.25%   (it pays 0.70%)

    Neither is reachable on maker-both-legs at 0.35%/leg. Both are
    reachable on a zero-fee tier, where only the spread is left.
    """
    return round(float(step_pct) - float(min_net_pct), 4)


def project_usd(trips, step_pct, round_trip_fee_pct, slice_usd):
    """Ceiling on what `trips` completed round trips would have paid.

    slice_usd is the money that turns over on one trip - a branch's
    allocation divided by its level count, NOT the branch allocation.
    Uniform across coins here, which is an approximation when branches are
    funded unevenly; it moves every row by the same factor, so the RANKING
    between steps is unaffected and that ranking is the whole question.
    """
    return round(int(trips) * net_per_trip_pct(step_pct, round_trip_fee_pct) / 100.0
                 * float(slice_usd), 2)


def best_step(rows, round_trip_fee_pct, slice_usd):
    """The step that made the most money, not the one that traded most.

    Returns (step_pct, usd). Ties go to the WIDER step: fewer fills for the
    same money is less exposure and less that can sit open.
    """
    best = None
    for step, trips in sorted(rows.items(), reverse=True):
        usd = project_usd(trips, step * 100, round_trip_fee_pct, slice_usd)
        if best is None or usd > best[1]:
            best = (step, usd)
    return best


async def measure(coins, steps, days, session=None, fetcher=None):
    """{step: {coin: trips}} over `days` of real hourly candles.

    A coin whose history will not load is LEFT OUT of every step's counts
    rather than scored zero, matching coin_rotation.measure_universe - a
    fetch failure is not evidence that a coin does not move, and a zero
    would quietly make every tighter step look better than it is.
    """
    import coin_rotation as R
    if fetcher is None:
        import crypto_selection_backtest as CSB
        fetcher = CSB.fetch_candles_window

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)

    own_session = session is None
    if own_session:
        import aiohttp
        session = aiohttp.ClientSession()
    try:
        candles, missing = {}, []
        for pid in coins:
            try:
                got = await fetcher(session, pid, start, end, granularity=3600)
            except Exception:
                got = None
            if not got or len(got) < 3 or not got[1]:
                missing.append(pid)
                continue
            candles[pid] = got
    finally:
        if own_session:
            await session.close()

    out = {}
    for step in steps:
        out[step] = {pid: R.count_round_trips(c[1], c[2], step)
                     for pid, c in candles.items()}
    return out, missing


def render(counts, missing, *, fee_pct, slice_usd, days, free_fee_pct=SPREAD_ONLY_PCT):
    """The three tables, as text."""
    steps = sorted(counts, reverse=True)
    coins = sorted({c for per in counts.values() for c in per})
    lines = []

    lines.append(f"ROUND TRIPS IN {days} DAYS, per coin, by step size\n")
    head = f"{'step':>6} " + " ".join(f"{c.replace('-USD', ''):>6}" for c in coins)
    lines.append(head + f" {'TOTAL':>7} {'/day':>7}")
    totals = {}
    for step in steps:
        per = counts[step]
        tot = sum(per.values())
        totals[step] = tot
        lines.append(f"{step * 100:5.2f}% "
                     + " ".join(f"{per.get(c, 0):6d}" for c in coins)
                     + f" {tot:7d} {tot / days:7.2f}")
    if missing:
        lines.append(f"\nnot scored (history unavailable): {', '.join(missing)}")

    lines.append(f"\n\nNET PER ROUND TRIP at a {fee_pct:.2f}% round-trip fee\n")
    lines.append(f"{'step':>6} {'gross':>7} {'fee':>7} {'net':>8} {'needs fee':>10}  verdict")
    for step in steps:
        net = net_per_trip_pct(step * 100, fee_pct)
        # The fix for a losing row is a lower FEE, not a wider step - say
        # which fee, so the row is a condition rather than a dead end.
        need = max_fee_for_step(step * 100)
        need_txt = f"<={need:5.2f}%" if net <= 0 else "    ok"
        lines.append(f"{step * 100:5.2f}% {step * 100:6.2f}% {fee_pct:6.2f}% "
                     f"{net:7.2f}% {need_txt:>10}  {verdict(net)}")
    lines.append(f"\nBreak-even step at this fee: {breakeven_step_pct(fee_pct):.2f}%. "
                 f"Anything tighter loses money on every completed trip.")
    losing = [s for s in steps if net_per_trip_pct(s * 100, fee_pct) <= 0]
    if losing:
        lines.append(f"The {len(losing)} losing row(s) are not broken steps - they are steps "
                     f"run at the wrong fee. Each needs the round trip at or under its own "
                     f"gross to clear, which at {fee_pct:.2f}% it does not get.")
    lines.append("The live bot refuses these regardless of configuration: the net-edge gate "
                 "prices every buy against the real round trip before placing it, and fails "
                 "closed. A sub-fee step does not trade, it just never fires.")

    lines.append(f"\n\nCEILING over {days} days on a ${slice_usd:,.2f} slice\n")
    lines.append(f"{'step':>6} {'trips':>6} {'@' + f'{fee_pct:.2f}%':>12} "
                 f"{'@' + f'{free_fee_pct:.2f}%':>12}")
    for step in steps:
        lines.append(f"{step * 100:5.2f}% {totals[step]:6d} "
                     f"{project_usd(totals[step], step * 100, fee_pct, slice_usd):11,.2f}$ "
                     f"{project_usd(totals[step], step * 100, free_fee_pct, slice_usd):11,.2f}$")

    bstep, busd = best_step(totals, fee_pct, slice_usd)
    most = max(totals, key=lambda s: totals[s])
    lines.append(f"\nMost money: {bstep * 100:.2f}% at ${busd:,.2f}. "
                 f"Most trades: {most * 100:.2f}% at {totals[most]} trips.")
    if bstep != most:
        lines.append("Those are NOT the same step. Trading more is not earning more - "
                     "the fee is a fixed toll per trip and a tighter step shrinks the "
                     "gross without shrinking the toll.")

    lines.append("\nEvery figure above assumes each round trip COMPLETES. Winners close "
                 "and losers stay open, so the real book lands below this. These are "
                 "ceilings, not forecasts.")
    return "\n".join(lines)


async def run(coins=None, steps=None, days=60, fee_pct=DEFAULT_ROUND_TRIP_FEE_PCT,
              slice_usd=30.77):
    counts, missing = await measure(coins or DEFAULT_COINS, steps or DEFAULT_STEPS, days)
    return render(counts, missing, fee_pct=fee_pct, slice_usd=slice_usd, days=days)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--fee", type=float, default=DEFAULT_ROUND_TRIP_FEE_PCT,
                   help="round-trip fee in percent (0.70 = maker both legs)")
    p.add_argument("--slice", type=float, default=30.77,
                   help="USD that turns over on one trip")
    p.add_argument("--coins", type=str, default=None, help="comma separated product ids")
    p.add_argument("--steps", type=str, default=None, help="comma separated percents")
    a = p.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",")] if a.coins else None
    steps = [float(s) / 100 for s in a.steps.split(",")] if a.steps else None
    print(asyncio.run(run(coins, steps, a.days, a.fee, a.slice)))


if __name__ == "__main__":
    main()
