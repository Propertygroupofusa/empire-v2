"""How hard each dollar works, not just how much was made.

WHY THESE AND NOT "PROFIT"

"$19.61 realized" answers one question and hides four. It cannot tell you
whether the figure is small because the edge is thin, because the capital
is tiny, because the money sits idle, or because the wins are being given
back. Those have completely different fixes and only the first one is a
strategy problem.

So this reports the account the way a desk would: profit per dollar
deployed, how many times that dollar was recycled, what share of it never
moved at all, and what the wins looked like against the losses.

THE ONE THAT MATTERS MOST, AND THE TRAP IN IT

Capital velocity - total exposure cycled divided by average capital
allocated - is the headline of the proposal this was built from, and it is
worth measuring. But velocity is a MULTIPLIER ON EDGE, not a substitute
for one, and this account has the proof: at a 0.9% grid step the fleet
turned over five times as much capital as at 3.75% and went from +65.4% to
-71.1%. Same coins, same window, five times the velocity, and the sign
flipped.

So velocity is never reported alone here. It is always reported beside
net edge per trade, because velocity x edge is the only product that means
anything, and a system optimising velocity while its edge is negative is
optimising the speed at which it loses.

WHAT IT REFUSES TO DO

It will not annualise a two-week sample. It will not report a rate from
fewer trades than can support one. Every figure that needs a minimum
sample carries the count beside it, and returns None with a reason rather
than a confident number built on nine trades.
"""
from __future__ import annotations

import statistics
from datetime import datetime

# Below this, a rate is arithmetic rather than evidence.
MIN_TRADES_FOR_RATE = 30
MIN_TRADES_FOR_FACTOR = 20

# Enough trades is not enough time. Forty trades inside one hour divided by
# the span gave 960 trades a day, and with a zero span the floor below it
# produced 3e10 - a number with no upper bound presented as a rate. A window
# shorter than this cannot carry a per-day figure at any trade count.
MIN_SPAN_DAYS = 0.5


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _rows(trades):
    """Usable closed trades, oldest first. Anything unreadable is dropped
    and counted, never guessed at."""
    out, skipped = [], 0
    for t in trades or ():
        if not hasattr(t, "get"):
            skipped += 1
            continue
        pnl = _num(t.get("pnl"))
        if pnl is None:
            skipped += 1
            continue
        out.append({
            "pnl": pnl,
            "qty": _num(t.get("qty")),
            "entry": _num(t.get("entry_price")),
            "exit": _num(t.get("exit_price")),
            "opened_at": t.get("opened_at"),
            "closed_at": t.get("closed_at"),
            "product_id": t.get("product_id"),
            "exit_reason": t.get("exit_reason"),
        })
    out.sort(key=lambda r: str(r["closed_at"] or ""))
    return out, skipped


def _parse(ts):
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# AN INHERITED EXIT IS NOT ONE OF THIS STRATEGY'S TRADES, AND HERE IT GATES
# THE MONEY.
#
# net_edge_per_trade_usd is net / n over every row, and bottleneck() returns
# NO_EDGE the moment it is <= 0. "Where the next dollar should go" then
# refuses EVERY lever on that verdict, by design: more capital onto a losing
# strategy is the same loss, larger and sooner.
#
# On 2026-10-04 four ZEC closes tagged adopted_exit did exactly that:
#
#   net   +$135.58  ->  -$175.66
#   edge  +$0.6917  ->  -$0.88 per trade
#   the binding cause flipped LOW_VELOCITY -> NO_EDGE
#
# and the allocator locked itself shut on a reading taken from coin the grid
# never bought, priced against an adoption-day mark nobody paid. That is the
# one place in this system where a mispriced row stops being a display fault
# and starts deciding where money goes. The grid's own 196 round trips were
# unchanged throughout.
#
# Same rule as loss_study.analyse and get_grid_performance_metrics: the grid
# chose neither end of an inherited position, so it cannot be evidence about
# how the grid trades - in either direction. An inherited GAIN is excluded
# too, and would otherwise unlock the levers just as falsely.
#
# NOT HIDDEN: the count and the booked total are published, and
# exclude_inherited=False measures the raw book.
ADOPTED_EXIT_REASON = "adopted_exit"


def compute(trades, *, allocated_usd, free_cash_usd=0.0,
            account_total_usd=None, min_trades=MIN_TRADES_FOR_RATE,
            exclude_inherited=True):
    """Every KPI, each with the sample behind it.

    `allocated_usd` is the capital the bot actually has, not the account
    total - profit per dollar deployed is meaningless against money that
    was never available to the strategy.

    exclude_inherited drops adopted_exit rows, for the reason above this
    function. Pass False to measure the raw book including them.
    """
    rows_all, skipped = _rows(trades)
    inherited = [r for r in rows_all
                 if (r.get("exit_reason") or "") == ADOPTED_EXIT_REASON]
    rows = ([r for r in rows_all if r not in inherited]
            if exclude_inherited else rows_all)
    n = len(rows)
    alloc = _num(allocated_usd)
    free = _num(free_cash_usd) or 0.0
    total = _num(account_total_usd)

    net = round(sum(r["pnl"] for r in rows), 4) if rows else 0.0
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    # A round trip that closed flat is not a loss. Counting it as one pulled
    # the average loss toward zero and made the stop look gentler than it is.
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    scratches = sum(1 for r in rows if r["pnl"] == 0)

    # --- span and rates -------------------------------------------------
    first = _parse(rows[0]["closed_at"]) if rows else None
    last = _parse(rows[-1]["closed_at"]) if rows else None
    days = None
    if first and last:
        days = max((last - first).total_seconds() / 86400.0, 1e-9)

    enough_span = days is not None and days >= MIN_SPAN_DAYS

    def rate(value, per_days):
        if value is None or not enough_span or n < min_trades:
            return None
        return round(value / per_days, 4)

    # --- exposure cycled -> velocity ------------------------------------
    exposure = 0.0
    sized = 0
    for r in rows:
        if r["qty"] is not None and r["entry"] is not None:
            exposure += abs(r["qty"] * r["entry"])
            sized += 1
    velocity = (round(exposure / alloc, 3)
                if alloc and alloc > 0 and sized == n and n else None)

    # --- hold time -------------------------------------------------------
    holds = []
    for r in rows:
        a, b = _parse(r["opened_at"]), _parse(r["closed_at"])
        if a and b and b >= a:
            holds.append((b - a).total_seconds() / 3600.0)

    # --- drawdown on the realised curve ----------------------------------
    peak = run = 0.0
    maxdd = 0.0
    for r in rows:
        run += r["pnl"]
        peak = max(peak, run)
        maxdd = min(maxdd, run - peak)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    factor = (round(gross_win / gross_loss, 3)
              if gross_loss > 0 and n >= MIN_TRADES_FOR_FACTOR else None)

    idle_pct = (round(free / (alloc + free) * 100, 2)
                if (alloc is not None and (alloc + free) > 0) else None)
    # A snapshot can show allocated + free above the account total for a few
    # seconds after a fill. A negative "share outside" is not a small number,
    # it is a nonsense one, so it floors at zero rather than being reported.
    outside_pct = (round(max(total - (alloc or 0) - free, 0.0) / total * 100, 2)
                   if total and total > 0 else None)

    edge = round(net / n, 4) if n else None
    per_1000 = (round(net / alloc * 1000, 2) if alloc and alloc > 0 else None)

    return {
        "trades": n,
        "unreadable_rows": skipped,
        # What was set aside before any of these numbers was taken, and the
        # cash it booked. None of it is hidden; it is simply not evidence
        # about how this grid trades.
        "inherited_excluded": len(inherited) if exclude_inherited else 0,
        "inherited_excluded_usd": (round(sum(r["pnl"] for r in inherited), 2)
                                   if exclude_inherited and inherited else 0.0),
        "inherited_note": (
            (f"{len(inherited)} inherited position(s) closed for "
             f"${sum(r['pnl'] for r in inherited):,.2f} as booked are excluded "
             f"from every figure here. The grid chose neither end of them, so "
             f"they are not evidence about its edge - and net_edge_per_trade_usd "
             f"is what bottleneck() turns into NO_EDGE, which refuses every "
             f"capital lever at once.")
            if (exclude_inherited and inherited) else None),
        "days_span": round(days, 2) if days else None,

        "net_usd": net,
        "profit_per_1000_deployed_usd": per_1000,
        "per_1000_note": (
            "CUMULATIVE profit against a POINT-IN-TIME denominator - the capital deployed "
            "right now, not the average deployed across the window. Read it as a rough "
            "scale figure, never as a return: a return needs capital sampled over time, "
            "and this ledger does not carry that."),
        "net_edge_per_trade_usd": edge,

        "capital_velocity": velocity,
        "exposure_cycled_usd": round(exposure, 2) if sized == n else None,
        "velocity_note": (
            "Exposure cycled divided by capital allocated. It is a MULTIPLIER on edge, "
            "never a substitute: at a 0.9% grid step this fleet turned over five times "
            "the capital it does at 3.75% and went from +65.4% to -71.1%. Read it beside "
            "net_edge_per_trade_usd or not at all."),

        "trades_per_day": rate(float(n), days),
        "net_per_day_usd": rate(net, days),
        "avg_hold_hours": round(statistics.mean(holds), 2) if holds else None,
        "hold_samples": len(holds),

        "idle_capital_pct": idle_pct,
        "outside_any_branch_pct": outside_pct,

        "max_drawdown_usd": round(maxdd, 2),
        "profit_factor": factor,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "scratch_trades": scratches,
        "avg_win_usd": round(statistics.mean(wins), 4) if wins else None,
        "avg_loss_usd": round(statistics.mean(losses), 4) if losses else None,

        "allocated_usd": alloc,
        "free_cash_usd": round(free, 2),
        "account_total_usd": total,

        "sample_warning": _warning(n, min_trades, days, enough_span),
    }


def _warning(n, min_trades, days, enough_span):
    """Why the rates are withheld, in the words that say which fix applies.

    Two different shortages read the same on the dashboard - too few trades
    and too little clock - and they are not the same problem. One is solved
    by waiting, the other by trading more often.
    """
    if n < min_trades:
        return (f"{n} trades. Rates are withheld below {min_trades} because a figure "
                f"from this many is arithmetic, not evidence.")
    if days is None:
        return (f"{n} trades with no readable timestamps, so nothing here can be "
                f"expressed per day.")
    if not enough_span:
        return (f"{n} trades but only {days:.3f} days of them. A per-day figure from a "
                f"window under {MIN_SPAN_DAYS} days is an extrapolation, not a rate, so "
                f"the rates are withheld.")
    return None


def bottleneck(k):
    """Which of the four reasons the number is small. One answer, ranked.

    Small profit has four different causes with four different fixes, and
    naming the wrong one sends the next month of work in the wrong
    direction. This picks the binding one rather than listing symptoms.
    """
    if not k or not k.get("trades"):
        return ("NOT_TRADING",
                "No closed trades at all. Nothing else can be diagnosed until there are.")
    if k.get("sample_warning"):
        return ("TOO_EARLY", k["sample_warning"])

    edge = k.get("net_edge_per_trade_usd")
    if edge is not None and edge <= 0:
        return ("NO_EDGE",
                f"{edge:+.4f} per trade after fees. More capital or more velocity would "
                f"scale a loss - the strategy has to clear its costs before anything else "
                f"is worth changing.")

    idle = k.get("idle_capital_pct")
    outside = k.get("outside_any_branch_pct")
    if outside is not None and outside > 50:
        why = (f"{outside:.1f}% of the account sits outside every branch. The edge is "
               f"positive but it is being applied to a fraction of the money - this is a "
               f"capital placement problem, not a strategy one.")
        # Naming only the coin understates what can be done TODAY. Unallocated
        # cash needs no sale to put to work, and saying "move coin into
        # branches" while $892 sits in the wallet points at the harder half.
        cash = k.get("free_cash_usd")
        if idle is not None and idle > 40 and cash:
            why += (f" ${cash:,.2f} of that is already unallocated CASH inside the bot "
                    f"({idle:.1f}% of its own capital) - that part needs no sale to put "
                    f"to work, only rungs to put it on.")
        return ("CAPITAL_OUTSIDE", why)
    if idle is not None and idle > 40:
        return ("CAPITAL_IDLE",
                f"{idle:.1f}% of the bot's own capital is unallocated cash. It is inside "
                f"the system and still not working.")

    vel = k.get("capital_velocity")
    if vel is not None and vel < 1.0:
        return ("LOW_VELOCITY",
                f"Each dollar was cycled {vel:.2f} times. The edge is positive and the "
                f"capital is placed - it simply is not being recycled often enough.")

    return ("HEALTHY",
            "Positive edge, capital deployed, capital recycling. Size is now the only lever.")
