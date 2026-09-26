"""Does a per-slice exit earn more than the branch-level gate?

The live sell path gates on the BRANCH before it looks at any slice:

    price >= branch.reference_price * (1 + grid_pct)

Only once that fires does _pick_profitable_slice_to_sell choose among the
open slices. So per-slice economics decide WHICH slice sells, never
WHETHER selling is allowed. On live NEAR, 2026-09-26, a slice entered at
4.8447 cleared its own costs at 4.9111 and was waiting on a branch gate
at 4.9658 - 1.13% of dead time on a slice already in profit.

Three exit modes, replayed on the fleet's own six coins over 60 days of
hourly candles:

    A  branch gate at the grid step      (what runs today)
    B  each slice its own target at the grid step
    C  each slice its own target at a 1.67% cost-plus hurdle

crossed with three stop policies, because the stop turned out to dominate
both. Results at a 0.70% round-trip fee, $30.77 a slice:

    exit mode              adaptive   fixed 8%      none
    A branch gate 2.50%      -1.77$     -8.01$   +44.36$
    B per-slice   2.50%      +0.03$     -9.14$   +45.97$
    C per-slice   1.67%      -9.38$     -9.46$   +25.97$

Three things fall out, and only the first is about exits:

  * B beats A everywhere, by a little. Releasing a slice the moment it
    clears its own costs is worth about $1.80 over 60 days here.
  * C, the cost-plus hurdle, is clearly WORSE despite selling sooner. A
    lower exit drags reference_price down with it, which deepens the next
    buy, which slows re-entry - 65 trips against A's 79. Selling sooner
    bought fewer chances to sell.
  * The stop costs multiples of what the exit mode gains. Same window,
    same everything: no stop earns +$44 where the adaptive stop earns
    -$1.77. Whatever the exit does, it is rounding error beside that.

CAVEAT, stated because it changes how far this generalises: these 60 days
rose on every coin (26% to 222%). A stop looks bad in a rising market by
construction. The earlier down-week replay agreed in direction - the 8%
stop turned a -19.43% median into -28.44% - but no window here tests a
crash, where an unbounded downside is what a stop exists for.

    python3 exit_study.py --fee 1.37 --days 90
"""

import argparse
import asyncio
import datetime

DEFAULT_LEVELS = 3
DEFAULT_SLICE_USD = 30.77
DEFAULT_STEP = 0.025

# The live adaptive stops, 2026-09-26. Passed explicitly so a re-run
# compares against a known configuration rather than whatever is current.
LIVE_ADAPTIVE_STOPS = {
    "BTC-USD": 0.0453, "NEAR-USD": 0.1849, "BONK-USD": 0.1460,
    "ONDO-USD": 0.1255, "FLOKI-USD": 0.1114, "TIA-USD": 0.1385,
}

# A stop this wide cannot be reached inside any window here; it is how
# "no stop" is expressed without a separate code path that could diverge.
NO_STOP = 0.99


def replay(closes, highs, lows, *, step, mode, hurdle, stop_pct, fee_pct,
           levels=DEFAULT_LEVELS):
    """One coin, one configuration. Percentages of a slice, not dollars.

    Within a bar the order is stop, then sell, then buy - the least
    favourable ordering, so a bar that could have done either does the
    worse one. reference moves to every fill, buy and sell alike, exactly
    as the live branch does.
    """
    if not closes or len(closes) < 2:
        return None
    ref = closes[0]
    slices, realized, trips, stops = [], 0.0, 0, 0

    for i in range(1, len(closes)):
        hi, lo = highs[i], lows[i]

        for entry in list(slices):
            level = entry * (1 - stop_pct)
            if lo <= level:
                realized += ((level / entry) - 1) * 100 - fee_pct
                slices.remove(entry)
                stops += 1
                trips += 1

        if mode == "branch":
            gate = ref * (1 + step)
            if slices and hi >= gate:
                for entry in list(slices):
                    if ((gate / entry) - 1) * 100 - fee_pct > 0:
                        realized += ((gate / entry) - 1) * 100 - fee_pct
                        slices.remove(entry)
                        trips += 1
                        ref = gate
                        break
        else:
            for entry in list(slices):
                target = entry * (1 + hurdle)
                if hi >= target:
                    realized += ((target / entry) - 1) * 100 - fee_pct
                    slices.remove(entry)
                    trips += 1
                    ref = target

        buy = ref * (1 - step)
        if len(slices) < levels and lo <= buy:
            slices.append(buy)
            ref = buy

    last = closes[-1]
    unrealized = sum(((last / e) - 1) * 100 for e in slices)
    return {
        "trips": trips, "stops": stops,
        "realized_pct": round(realized, 4),
        "open": len(slices),
        # Reported beside realized, always. Realized alone is the figure
        # that misread the retired book for two weeks.
        "unrealized_pct": round(unrealized, 4),
        "total_pct": round(realized + unrealized, 4),
    }


def aggregate(per_coin, slice_usd=DEFAULT_SLICE_USD):
    """Sum a configuration across coins, in dollars."""
    rows = [r for r in per_coin if r]
    if not rows:
        return None
    realized = sum(r["realized_pct"] for r in rows)
    unrealized = sum(r["unrealized_pct"] for r in rows)
    return {
        "coins": len(rows),
        "trips": sum(r["trips"] for r in rows),
        "stops": sum(r["stops"] for r in rows),
        "open": sum(r["open"] for r in rows),
        "realized_usd": round(realized * slice_usd / 100, 2),
        "unrealized_usd": round(unrealized * slice_usd / 100, 2),
        "total_usd": round((realized + unrealized) * slice_usd / 100, 2),
    }


async def fetch(coins, days, fetcher=None, session=None):
    if fetcher is None:
        import crypto_selection_backtest as CSB
        fetcher = CSB.fetch_candles_window
    own = session is None
    if own:
        import aiohttp
        session = aiohttp.ClientSession()
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    out = {}
    try:
        for pid in coins:
            try:
                got = await fetcher(session, pid, start, end, granularity=3600)
            except Exception:
                got = None
            # A coin that will not load is LEFT OUT rather than counted as
            # a flat, costless one - a zero row flatters every config equally
            # and quietly shrinks the differences being measured.
            if got and len(got) >= 3 and got[0]:
                out[pid] = got
    finally:
        if own:
            await session.close()
    return out


def run_matrix(data, *, fee_pct, step=DEFAULT_STEP, slice_usd=DEFAULT_SLICE_USD,
               stops=None):
    """{(exit_label, stop_label): aggregate}."""
    stops = stops or {
        "adaptive": LIVE_ADAPTIVE_STOPS,
        "fixed 8%": {p: 0.08 for p in data},
        "none": {p: NO_STOP for p in data},
    }
    modes = [("A branch gate 2.50%", "branch", step),
             ("B per-slice 2.50%", "slice", step),
             ("C per-slice 1.67%", "slice", 0.0167)]
    out = {}
    for label, mode, hurdle in modes:
        for sname, table in stops.items():
            rows = [replay(c[0], c[1], c[2], step=step, mode=mode, hurdle=hurdle,
                           stop_pct=table.get(pid, NO_STOP), fee_pct=fee_pct)
                    for pid, c in data.items()]
            out[(label, sname)] = aggregate(rows, slice_usd)
    return out


def render(matrix, *, fee_pct, days, slice_usd=DEFAULT_SLICE_USD):
    lines = [f"{days} days - 2.50% buy step - {DEFAULT_LEVELS} levels - "
             f"${slice_usd:,.2f}/slice - {fee_pct:.2f}% round-trip fee", ""]
    lines.append(f"{'exit mode':22} {'stop':10} {'trips':>6} {'stops':>6} "
                 f"{'realized':>10} {'open':>5} {'unreal':>9} {'TOTAL':>10}")
    for (label, sname), a in matrix.items():
        if not a:
            continue
        lines.append(f"{label:22} {sname:10} {a['trips']:6} {a['stops']:6} "
                     f"{a['realized_usd']:9,.2f}$ {a['open']:5} "
                     f"{a['unrealized_usd']:8,.2f}$ {a['total_usd']:9,.2f}$")
    best = max((a["total_usd"], k) for k, a in matrix.items() if a)
    lines.append("")
    lines.append(f"Best: {best[1][0]} with stop '{best[1][1]}' at ${best[0]:,.2f}.")
    lines.append("Every trip is assumed to fill at its trigger price. Winners close "
                 "and losers sit open, so unrealized is printed beside realized and "
                 "neither is a forecast.")
    return "\n".join(lines)


async def run(coins=None, days=60, fee_pct=0.70):
    coins = coins or list(LIVE_ADAPTIVE_STOPS)
    data = await fetch(coins, days)
    return render(run_matrix(data, fee_pct=fee_pct), fee_pct=fee_pct, days=days)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--fee", type=float, default=0.70)
    p.add_argument("--coins", type=str, default=None)
    a = p.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",")] if a.coins else None
    print(asyncio.run(run(coins, a.days, a.fee)))


if __name__ == "__main__":
    main()
