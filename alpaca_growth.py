"""Why Alpaca is not growing, and which lever would actually make it faster.

THE QUESTION THIS ANSWERS

"Get Alpaca growing again, faster than last time." There are exactly two
ways to make an account grow faster: more profit per dollar that trades,
or more dollars trading. They have opposite prerequisites, and pulling the
wrong one is how this account has lost money before:

  - More dollars trading (raise PROP_MAX_RISK_PERCENT) multiplies whatever
    the edge already is. On a positive edge that is growth. On a zero or
    negative edge it is a faster loss.
  - More profit per dollar (change what it trades, or how) is the only
    fix when the edge itself is the problem.

Measured 2026-09-30 from the live snapshot: equity $978.91, cash $489.09,
open positions ~$489 - deployed at exactly the 50% cap, with half the
account idle BECAUSE of that cap. Lifetime profit -$0.80. Whether the cap
is the right lever depends entirely on a number nothing reported for
Alpaca until now: net edge per closed trade.

HOW

Nothing here is Alpaca-specific arithmetic. It hands the real ClosedTrade
rows to capital_kpis.compute() and capital_kpis.bottleneck() - the SAME
functions that judge the crypto side - so the two venues can never be held
to different rules. What this adds is the one fact only Alpaca has: a
single account-wide risk cap, and whether it is the thing keeping the cash
idle.

It changes nothing and places nothing. The cap is an operator decision;
this only says whether the evidence supports moving it.
"""
from __future__ import annotations

import capital_kpis

# A position below this cannot be opened anyway (prop_bot.MIN_POSITION_NOTIONAL),
# so remaining room smaller than it means the cap is effectively full.
DEFAULT_MIN_POSITION_USD = 50.0

# How much of the realized sample one symbol may account for before it is
# named as carrying (or sinking) the whole result.
CONCENTRATION_PCT = 50.0


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def by_symbol(trades):
    """Per-symbol realized result, worst total first.

    The worst-first order is deliberate: the fastest way to raise edge per
    trade on a small universe is usually to stop trading the symbol that
    loses, not to find a new one that wins.
    """
    agg = {}
    for t in trades or ():
        if not hasattr(t, "get"):
            continue
        pnl = _f(t.get("pnl"))
        if pnl is None:
            continue
        sym = t.get("symbol") or t.get("product_id") or "?"
        a = agg.setdefault(sym, {"symbol": sym, "trades": 0, "net_usd": 0.0, "wins": 0})
        a["trades"] += 1
        a["net_usd"] += pnl
        if pnl > 0:
            a["wins"] += 1
    out = []
    for a in agg.values():
        out.append({
            "symbol": a["symbol"],
            "trades": a["trades"],
            "net_usd": round(a["net_usd"], 2),
            "avg_per_trade_usd": round(a["net_usd"] / a["trades"], 4),
            "win_rate_pct": round(a["wins"] / a["trades"] * 100, 1),
        })
    out.sort(key=lambda r: (r["net_usd"], r["symbol"]))
    return out


def cap_state(*, equity, deployed, max_risk_pct, min_position_usd=DEFAULT_MIN_POSITION_USD):
    """Is the account-wide risk cap what is holding the cash back?

    Returns None fields rather than a guess when equity is unreadable.
    """
    eq = _f(equity)
    dep = _f(deployed) or 0.0
    pct = _f(max_risk_pct)
    if not eq or eq <= 0 or pct is None:
        return {"cap_usd": None, "deployed_usd": round(dep, 2), "room_usd": None,
                "cap_binding": None, "max_risk_pct": pct}
    cap = eq * pct
    room = cap - dep
    return {
        "max_risk_pct": pct,
        "cap_usd": round(cap, 2),
        "deployed_usd": round(dep, 2),
        "room_usd": round(room, 2),
        "deployed_pct_of_equity": round(dep / eq * 100, 1),
        # Binding = no room left for even one minimum-size position.
        "cap_binding": room < float(min_position_usd),
    }


def growth_lever(verdict, kpis, cap, symbols):
    """One recommended next move, with the reason, derived from the verdict.

    Never recommends raising the cap on anything but a measured positive
    edge with enough sample behind it.
    """
    code = verdict[0]
    worst = next((s for s in symbols if s["net_usd"] < 0), None)

    if code == "NOT_TRADING":
        return ("WAIT_FOR_TRADES",
                "No closed Alpaca trades in this window, so there is no edge to scale. "
                "Raising the risk cap now would size up an unmeasured strategy.")
    if code == "TOO_EARLY":
        return ("KEEP_CAP_GATHER_SAMPLE",
                f"{verdict[1]} Leave the cap where it is until the sample can tell a real "
                f"edge from luck - otherwise more capital scales noise.")
    if code == "NO_EDGE":
        msg = ("The strategy loses (or breaks even) per trade, so raising the cap would "
               "make the account shrink faster, not grow. Fix the edge first.")
        if worst:
            msg += (f" Biggest drag: {worst['symbol']} at ${worst['net_usd']:+.2f} over "
                    f"{worst['trades']} trade(s) - excluding it is the cheapest edge "
                    f"improvement available.")
        return ("FIX_EDGE_FIRST", msg)

    edge = kpis.get("net_edge_per_trade_usd")
    if cap.get("cap_binding"):
        return ("RAISE_RISK_CAP",
                f"Edge is positive ({edge:+.4f}/trade over {kpis['trades']} trades) and the "
                f"{cap['max_risk_pct']*100:.0f}% cap is full (${cap['deployed_usd']:,.2f} of "
                f"${cap['cap_usd']:,.2f}). The idle cash cannot trade until "
                f"PROP_MAX_RISK_PERCENT is raised. That scales losses by the same factor - "
                f"worst realized drawdown so far was ${kpis.get('max_drawdown_usd', 0):,.2f}.")
    if code in ("CAPITAL_IDLE", "CAPITAL_OUTSIDE"):
        return ("DEPLOY_WITHIN_CAP",
                f"Edge is positive and there is still ${cap.get('room_usd') or 0:,.2f} of room "
                f"under the cap - the limit is entry signals, not the cap.")
    return ("HOLD_COURSE", verdict[1])


def diagnose(trades, *, equity, cash, deployed, max_risk_pct,
             min_position_usd=DEFAULT_MIN_POSITION_USD,
             min_trades=capital_kpis.MIN_TRADES_FOR_RATE):
    """Everything needed to decide the next move on Alpaca, in one payload."""
    kpis = capital_kpis.compute(
        trades, allocated_usd=deployed, free_cash_usd=cash,
        account_total_usd=equity, min_trades=min_trades)
    verdict = capital_kpis.bottleneck(kpis)
    cap = cap_state(equity=equity, deployed=deployed, max_risk_pct=max_risk_pct,
                    min_position_usd=min_position_usd)
    symbols = by_symbol(trades)
    lever = growth_lever(verdict, kpis, cap, symbols)

    concentration = None
    total = kpis.get("net_usd") or 0.0
    if symbols and abs(total) > 0:
        top = max(symbols, key=lambda s: abs(s["net_usd"]))
        share = abs(top["net_usd"]) / sum(abs(s["net_usd"]) for s in symbols) * 100
        if share >= CONCENTRATION_PCT:
            concentration = (f"{top['symbol']} accounts for {share:.0f}% of the realized "
                             f"result - the rest of the universe is close to noise.")

    return {
        "kpis": kpis,
        "bottleneck": verdict[0],
        "bottleneck_detail": verdict[1],
        "cap": cap,
        "by_symbol": symbols,
        "concentration": concentration,
        "lever": lever[0],
        "lever_detail": lever[1],
    }
