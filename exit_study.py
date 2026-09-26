"""When is a filled slice allowed to exit? Nothing else varies.

The live sell path gates on the BRANCH before it looks at any slice:

    price >= branch.reference_price * (1 + grid_pct)

Only once that fires does _pick_profitable_slice_to_sell choose among the
open slices. So per-slice economics decide WHICH slice sells, never
WHETHER selling is allowed. On live NEAR, 2026-09-26, a slice entered at
4.8447 cleared its own costs at 4.9111 and was waiting on a branch gate
at 4.9658 - 1.13% of dead time on a slice already in profit.

THE ONLY VARIABLE IS THE EXIT RULE. Entry rule, slice size, starting
capital, level cap, candles, coins, window and stop distance are byte
identical across configurations. The stop is passed in per coin and never
differs between configs; re-anchoring follows one rule everywhere
(reference moves to every fill, buy and sell alike, exactly as the live
branch does). The reference PATH still diverges between configs because
the fills diverge - that is a consequence of the variable under test, not
a second variable, and it is the mechanism by which a shallower exit
slows re-entry.

    A  branch gate, sells the oldest slice when the gate fires
       (the pre-2026 behaviour, kept as the naive baseline)
    B  asymmetric: 2.50% entry, each slice exits at its own 1.67% target
    C  exact production logic: branch gate AND the slice must be net
       positive, which is what _pick_profitable_slice_to_sell enforces

COST MODEL, and which half is measured:

    0.70%  round-trip fee        MEASURED. maker both legs at the
                                 account's real 0.35%/leg.
    0.67%  adverse selection     AN ASSUMPTION, not an observed fee. It
                                 is the fleet's own cost-model padding
                                 for filling on the wrong side of a move.
                                 Reported as its own line everywhere so
                                 it can be argued with or set to zero:
                                 --adverse 0.
    1.37%  total drag            the sum, which is what the live gate
                                 prices against.

Judged on net P&L, capital velocity and drawdown - never on trade count.

    python3 exit_study.py --days 60 --adverse 0.67
"""

import argparse
import asyncio
import datetime
import statistics

DEFAULT_LEVELS = 3
DEFAULT_SLICE_USD = 30.77
DEFAULT_STEP = 0.025
DEFAULT_HURDLE = 0.0167

MEASURED_FEE_PCT = 0.70
ASSUMED_ADVERSE_PCT = 0.67

# The live adaptive stops, 2026-09-26. Passed explicitly and identically
# to every configuration - a stop that differed between configs would make
# this a comparison of two changes at once.
LIVE_ADAPTIVE_STOPS = {
    "BTC-USD": 0.0453, "NEAR-USD": 0.1849, "BONK-USD": 0.1460,
    "ONDO-USD": 0.1255, "FLOKI-USD": 0.1114, "TIA-USD": 0.1385,
}

# Held longer than this and a slice is stale: capital locked with no
# prospect of release inside the window it was sized for.
STALE_HOURS = 14 * 24


def replay(closes, highs, lows, *, step, mode, hurdle, stop_pct,
           levels=DEFAULT_LEVELS, bar_hours=1.0):
    """One coin, one configuration. Returns the TRADE LEDGER, not a score.

    Every metric downstream is computed from these rows, so a metric can
    never disagree with the trades it came from.

    Within a bar the order is stop, then sell, then buy - the least
    favourable ordering, so a bar that could have done either does the
    worse one.
    """
    if not closes or len(closes) < 2:
        return None

    ref = closes[0]
    open_slices = []          # [(entry_price, entry_bar)]
    trades = []               # closed round trips
    peak_open = 0

    for i in range(1, len(closes)):
        hi, lo = highs[i], lows[i]

        # 1. STOP - identical rule in every configuration
        for entry, bar in list(open_slices):
            level = entry * (1 - stop_pct)
            if lo <= level:
                trades.append({"entry": entry, "exit": level, "bars": i - bar,
                               "reason": "stop"})
                open_slices.remove((entry, bar))

        # 2. SELL - the only thing that differs between configurations
        if mode == "branch_oldest":
            gate = ref * (1 + step)
            if open_slices and hi >= gate:
                entry, bar = open_slices[0]
                trades.append({"entry": entry, "exit": gate, "bars": i - bar,
                               "reason": "target"})
                open_slices.pop(0)
                ref = gate
        elif mode == "branch_profitable":
            gate = ref * (1 + step)
            if open_slices and hi >= gate:
                for entry, bar in list(open_slices):
                    if gate > entry:          # net check applied below, on cost
                        trades.append({"entry": entry, "exit": gate,
                                       "bars": i - bar, "reason": "target"})
                        open_slices.remove((entry, bar))
                        ref = gate
                        break
        else:                                  # per-slice target
            for entry, bar in list(open_slices):
                target = entry * (1 + hurdle)
                if hi >= target:
                    trades.append({"entry": entry, "exit": target,
                                   "bars": i - bar, "reason": "target"})
                    open_slices.remove((entry, bar))
                    ref = target

        # 3. BUY - identical rule in every configuration
        buy = ref * (1 - step)
        if len(open_slices) < levels and lo <= buy:
            open_slices.append((buy, i))
            ref = buy

        peak_open = max(peak_open, len(open_slices))

    last = closes[-1]
    end_bar = len(closes) - 1
    return {
        "trades": trades,
        "open": [{"entry": e, "bars": end_bar - b} for e, b in open_slices],
        "last": last,
        "peak_open": peak_open,
        "bars": len(closes) - 1,
        "bar_hours": bar_hours,
    }


def score(results, *, slice_usd, fee_pct, adverse_pct, levels=DEFAULT_LEVELS):
    """Every metric, computed from the trade ledgers."""
    rows = [r for r in results if r]
    if not rows:
        return None

    trades, opens = [], []
    peak_open = bars = 0
    bar_hours = 1.0
    for r in rows:
        trades += r["trades"]
        opens += r["open"]
        peak_open += r["peak_open"]
        bars = max(bars, r["bars"])
        bar_hours = r["bar_hours"]

    cost_pct = fee_pct + adverse_pct
    gross_pct = sum((t["exit"] / t["entry"] - 1) * 100 for t in trades)
    n = len(trades)
    fees_usd = n * fee_pct / 100 * slice_usd
    drag_usd = n * adverse_pct / 100 * slice_usd
    gross_usd = gross_pct / 100 * slice_usd
    net_usd = gross_usd - fees_usd - drag_usd

    wins = sum(1 for t in trades
               if (t["exit"] / t["entry"] - 1) * 100 - cost_pct > 0)
    stops = sum(1 for t in trades if t["reason"] == "stop")

    holds = sorted(t["bars"] * bar_hours for t in trades)
    win_holds = sorted(t["bars"] * bar_hours for t in trades
                       if (t["exit"] / t["entry"] - 1) * 100 - cost_pct > 0)

    # Equity curve from realized trades in close order, for drawdown.
    seq = sorted(trades, key=lambda t: t["bars"])
    eq = 0.0
    peak = 0.0
    maxdd = 0.0
    for t in seq:
        eq += ((t["exit"] / t["entry"] - 1) * 100 - cost_pct) / 100 * slice_usd
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)

    unreal_usd = sum((r["last"] / o["entry"] - 1) * 100 / 100 * slice_usd
                     for r in rows for o in r["open"])
    stale = sum(1 for o in opens if o["bars"] * bar_hours >= STALE_HOURS)

    hours = bars * bar_hours
    days = hours / 24 if hours else 0
    # Capital that could be locked at once, across the whole fleet.
    max_locked = peak_open * slice_usd
    deployed = len(rows) * levels * slice_usd
    turnover = (n * slice_usd) / deployed if deployed else 0

    return {
        "round_trips": n,
        "target_exits": n - stops,
        "stop_exits": stops,
        "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
        "gross_usd": round(gross_usd, 2),
        "fees_usd": round(-fees_usd, 2),
        "adverse_usd": round(-drag_usd, 2),
        "net_usd": round(net_usd, 2),
        "max_drawdown_usd": round(maxdd, 2),
        "avg_hold_h": round(statistics.fmean(holds), 1) if holds else None,
        "median_hold_h": round(statistics.median(holds), 1) if holds else None,
        "median_win_hold_h": round(statistics.median(win_holds), 1) if win_holds else None,
        "turnover_x": round(turnover, 2),
        "usd_per_hour": round(net_usd / hours, 4) if hours else None,
        "usd_per_day": round(net_usd / days, 2) if days else None,
        "max_locked_usd": round(max_locked, 2),
        "open_at_end": len(opens),
        "stale_positions": stale,
        "unrealized_usd": round(unreal_usd, 2),
        "total_usd": round(net_usd + unreal_usd, 2),
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
            # A coin that will not load is LEFT OUT rather than counted as a
            # flat costless row, which would flatter every config equally
            # and shrink the differences being measured.
            if got and len(got) >= 3 and got[0]:
                out[pid] = got
    finally:
        if own:
            await session.close()
    return out


CONFIGS = [
    ("A branch gate, oldest slice", "branch_oldest", DEFAULT_STEP),
    ("B asymmetric per-slice 1.67%", "slice", DEFAULT_HURDLE),
    ("C production: gate + profitable", "branch_profitable", DEFAULT_STEP),
]


def run_matrix(data, *, stops, slice_usd=DEFAULT_SLICE_USD,
               fee_pct=MEASURED_FEE_PCT, adverse_pct=ASSUMED_ADVERSE_PCT,
               step=DEFAULT_STEP, levels=DEFAULT_LEVELS):
    out = {}
    for label, mode, hurdle in CONFIGS:
        rows = [replay(c[0], c[1], c[2], step=step, mode=mode, hurdle=hurdle,
                       stop_pct=stops[pid], levels=levels)
                for pid, c in data.items()]
        out[label] = score(rows, slice_usd=slice_usd, fee_pct=fee_pct,
                           adverse_pct=adverse_pct, levels=levels)
    return out


def render(matrix, *, days, coins, slice_usd, fee_pct, adverse_pct):
    L = []
    L.append(f"{days} days - {len(coins)} coins - 2.50% entry - {DEFAULT_LEVELS} levels - "
             f"${slice_usd:,.2f}/slice - identical stops and entry rule")
    L.append(f"cost model: {fee_pct:.2f}% fee (MEASURED) + {adverse_pct:.2f}% adverse "
             f"selection (ASSUMPTION) = {fee_pct + adverse_pct:.2f}% per round trip")
    L.append("")

    fields = [
        ("round_trips", "completed round trips", "{:>10}"),
        ("target_exits", "  of which target", "{:>10}"),
        ("stop_exits", "  of which stop", "{:>10}"),
        ("win_rate_pct", "win rate %", "{:>10}"),
        ("gross_usd", "gross P&L $", "{:>10}"),
        ("fees_usd", "fees $", "{:>10}"),
        ("adverse_usd", "adverse selection $", "{:>10}"),
        ("net_usd", "NET P&L $", "{:>10}"),
        ("max_drawdown_usd", "max drawdown $", "{:>10}"),
        ("avg_hold_h", "avg hold h", "{:>10}"),
        ("median_hold_h", "median hold h", "{:>10}"),
        ("median_win_hold_h", "median WIN hold h", "{:>10}"),
        ("turnover_x", "capital turnover x", "{:>10}"),
        ("usd_per_hour", "$ per hour", "{:>10}"),
        ("usd_per_day", "$ per day", "{:>10}"),
        ("max_locked_usd", "max locked $", "{:>10}"),
        ("open_at_end", "open at end", "{:>10}"),
        ("stale_positions", f"stale (>{STALE_HOURS // 24}d)", "{:>10}"),
        ("unrealized_usd", "unrealized $", "{:>10}"),
        ("total_usd", "TOTAL $", "{:>10}"),
    ]
    labels = list(matrix)
    L.append(f"{'':24}" + "".join(f"{l[:22]:>24}" for l in labels))
    for key, name, fmt in fields:
        row = f"{name:24}"
        for l in labels:
            v = (matrix[l] or {}).get(key)
            row += f"{('-' if v is None else v):>24}"
        L.append(row)

    L.append("")
    a = matrix.get("C production: gate + profitable")
    b = matrix.get("B asymmetric per-slice 1.67%")
    if a and b:
        L.append("CAPITAL VELOCITY - the whole reason asymmetric exits are on the table")
        if a["median_win_hold_h"] and b["median_win_hold_h"]:
            d = a["median_win_hold_h"] - b["median_win_hold_h"]
            L.append(f"  a winning slice releases its capital {abs(d):.1f}h "
                     f"{'EARLIER' if d > 0 else 'LATER'} under B "
                     f"({b['median_win_hold_h']}h against {a['median_win_hold_h']}h)")
        L.append(f"  round trips      {b['round_trips']} against {a['round_trips']}"
                 f"  ({b['round_trips'] - a['round_trips']:+d})")
        L.append(f"  turnover         {b['turnover_x']}x against {a['turnover_x']}x")
        L.append(f"  NET              ${b['net_usd']:,.2f} against ${a['net_usd']:,.2f}"
                 f"  ({b['net_usd'] - a['net_usd']:+,.2f})")
        verdict = ("more cycles AND more money - the gate was locking capital"
                   if b["round_trips"] > a["round_trips"] and b["net_usd"] > a["net_usd"]
                   else "more cycles and LESS money - faster turnover is not better"
                   if b["round_trips"] > a["round_trips"]
                   else "fewer cycles and less money - the shallower exit slowed re-entry"
                   if b["net_usd"] <= a["net_usd"]
                   else "fewer cycles but more money")
        L.append(f"  VERDICT          {verdict}")

    L.append("")
    L.append("Every trip is assumed to fill at its trigger price. Winners close and "
             "losers sit open, so unrealized is reported beside net and neither is a "
             "forecast. The adverse-selection component is an assumption, not an "
             "observed fee - re-run with --adverse 0 to see the measured-cost-only case.")
    return "\n".join(L)


async def run(coins=None, days=60, fee_pct=MEASURED_FEE_PCT,
              adverse_pct=ASSUMED_ADVERSE_PCT, slice_usd=DEFAULT_SLICE_USD):
    coins = coins or list(LIVE_ADAPTIVE_STOPS)
    data = await fetch(coins, days)
    stops = {p: LIVE_ADAPTIVE_STOPS.get(p, 0.08) for p in data}
    m = run_matrix(data, stops=stops, slice_usd=slice_usd,
                   fee_pct=fee_pct, adverse_pct=adverse_pct)
    return render(m, days=days, coins=list(data), slice_usd=slice_usd,
                  fee_pct=fee_pct, adverse_pct=adverse_pct)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--fee", type=float, default=MEASURED_FEE_PCT)
    p.add_argument("--adverse", type=float, default=ASSUMED_ADVERSE_PCT)
    p.add_argument("--slice", type=float, default=DEFAULT_SLICE_USD)
    p.add_argument("--coins", type=str, default=None)
    a = p.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",")] if a.coins else None
    print(asyncio.run(run(coins, a.days, a.fee, a.adverse, a.slice)))


if __name__ == "__main__":
    main()
