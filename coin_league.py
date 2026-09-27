"""The league table: every coin competing for the crown, on a fair number.

WHAT THE CROWN CANNOT BE

Not units. PEPE holds 20,232,619 of them and is worth $87.61 - the
largest unit count in the account and the smallest position on the page.
Unit count is set by a token's supply, nothing else, so a league ranked
on it would crown whichever coin has the most zeros and tell the owner
to buy billions of the cheapest thing available.

Not dollars earned either. A coin with $200 behind it will always out-
earn a coin with $20, which measures the allocation, not the coin.

THE FAIR NUMBER

Return per dollar risked, per round trip, net of fees:

    edge_pct = pnl / (qty x entry_price)

averaged over that coin's own closed trades. A $87 position and a $2,250
position produce directly comparable figures, which is the whole point:
the small coin can genuinely beat the big one and take the crown off it.

THE GUARD THAT MAKES THE COMPETITION REAL

A long grid on a coin that rose will look brilliant. So the crown is
PROVISIONAL while the coin's own price rose across the measured window,
and CONFIRMED only once it earned through a flat or falling one. Without
that, the league crowns whatever pumped, the blueprint copies luck, and
every other coin is told to imitate a rising market.

That is the same asymmetry beta_check.py applies to configurations,
applied here per coin: a coin that LOST while its price rose really
lost, and a coin that WON while its price rose has not proven anything
yet.

THE BLUEPRINT

The leader's settings - grid step, levels, average hold - are what a
challenger copies. That is the transferable part: not "be BONK", but
"trade the way the coin that is winning trades", which any coin can do,
including a weak one, because the settings are free to adopt.

A challenger is never told to copy a PROVISIONAL crown.
"""
from __future__ import annotations

import statistics

# A coin ranks once it has this many closed round trips. Below it the
# coin is still qualifying - shown, never ranked, because a 100% win rate
# over two trades would take the crown off a coin with eighty.
MIN_TRADES_TO_RANK = 5

# And it cannot hold the crown until it has this many.
MIN_TRADES_FOR_CROWN = 20

# A window move smaller than this is flat enough that a win is not
# obviously the market's doing.
FLAT_BAND_PCT = 2.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _coin(product_id):
    return str(product_id or "").split("-")[0].upper() or None


def scorecard(trades, *, product_id=None, deployed_usd=None,
              window_return_pct=None, config=None,
              min_trades=MIN_TRADES_TO_RANK):
    """One coin's card, built only from its own closed round trips."""
    rows = []
    for t in trades or ():
        if not hasattr(t, "get"):
            continue
        pnl = _num(t.get("pnl"))
        qty, entry = _num(t.get("qty")), _num(t.get("entry_price"))
        if pnl is None:
            continue
        risked = abs(qty * entry) if (qty is not None and entry is not None) else None
        rows.append({"pnl": pnl, "risked": risked if (risked or 0) > 0 else None,
                     "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at")})

    n = len(rows)
    net = round(sum(r["pnl"] for r in rows), 4) if rows else 0.0
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]

    # THE FAIR NUMBER. Only trades whose risked capital is known can
    # produce it - a trade with no size is dropped rather than averaged
    # in at zero, which would drag a good coin down for a missing field.
    pcts = [r["pnl"] / r["risked"] * 100 for r in rows if r["risked"]]
    edge_pct = round(statistics.mean(pcts), 4) if pcts else None

    gross_win, gross_loss = sum(wins), abs(sum(losses))
    factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else None

    w = _num(window_return_pct)
    if w is None:
        regime = "UNKNOWN"
    elif w > FLAT_BAND_PCT:
        regime = "ROSE"
    elif w < -FLAT_BAND_PCT:
        regime = "FELL"
    else:
        regime = "FLAT"

    return {
        "coin": _coin(product_id),
        "product_id": product_id,
        "trades": n,
        "sized_trades": len(pcts),
        "net_usd": net,
        "edge_pct_per_trade": edge_pct,
        "net_edge_per_trade_usd": round(net / n, 4) if n else None,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": factor,
        "avg_win_usd": round(statistics.mean(wins), 4) if wins else None,
        "avg_loss_usd": round(statistics.mean(losses), 4) if losses else None,
        "deployed_usd": _num(deployed_usd),
        "window_return_pct": w,
        "regime": regime,
        "config": config or {},
        "ranked": n >= min_trades and edge_pct is not None,
        "crown_eligible": n >= MIN_TRADES_FOR_CROWN and edge_pct is not None
                          and edge_pct > 0,
        "why_not_ranked": (
            None if (n >= min_trades and edge_pct is not None) else
            (f"{n} closed round trip(s); a coin ranks at {min_trades}. A perfect record "
             f"over two trades would take the crown off a coin with eighty."
             if n < min_trades else
             "no trade carried both a quantity and an entry price, so the per-dollar "
             "figure cannot be computed and the coin cannot be compared fairly")),
    }


def table(trades_by_coin, *, deployed_by_coin=None, window_returns=None,
          configs=None, min_trades=MIN_TRADES_TO_RANK):
    """Every coin, ranked, with the crown awarded only on evidence."""
    deployed = deployed_by_coin or {}
    windows = window_returns or {}
    cfgs = configs or {}

    cards = []
    for pid, trades in (trades_by_coin or {}).items():
        c = _coin(pid)
        cards.append(scorecard(
            trades, product_id=pid,
            deployed_usd=deployed.get(pid, deployed.get(c)),
            window_return_pct=windows.get(pid, windows.get(c)),
            config=cfgs.get(pid, cfgs.get(c)), min_trades=min_trades))

    ranked = [c for c in cards if c["ranked"]]
    qualifying = [c for c in cards if not c["ranked"]]
    ranked.sort(key=lambda c: -c["edge_pct_per_trade"])
    qualifying.sort(key=lambda c: -c["trades"])

    for i, c in enumerate(ranked, 1):
        c["rank"] = i
    for c in qualifying:
        c["rank"] = None

    # THE CROWN. Highest fair edge among coins with a real sample, and
    # PROVISIONAL while its own price was rising - a long grid on a coin
    # that went up looks brilliant for reasons that are not the grid.
    crown = next((c for c in ranked if c["crown_eligible"]), None)
    if crown:
        crown["crown"] = "CONFIRMED" if crown["regime"] in ("FLAT", "FELL") else "PROVISIONAL"
        if crown["crown"] == "CONFIRMED":
            tail = (f", earned through a {crown['regime'].lower()} market - that is the coin "
                    f"to copy.")
        elif crown["regime"] == "ROSE":
            tail = (f", but its price ROSE {crown['window_return_pct']:+.2f}% across the "
                    f"window. A long grid on a rising coin looks brilliant for reasons that "
                    f"are not the grid, so this crown is PROVISIONAL and nothing should be "
                    f"copied from it yet.")
        else:
            # Not knowing the regime is not the same as having survived
            # one. An unmeasured window must never confirm a crown.
            tail = (", but what its own price did across the window is unknown, so its record "
                    "has not been separated from its market. PROVISIONAL until it is.")
        crown["crown_detail"] = (
            f"{crown['coin']} leads at {crown['edge_pct_per_trade']:+.4f}% a round trip over "
            f"{crown['trades']} trades" + tail)

    # A coin that LOST while its own price rose really lost. That finding
    # is robust and is the one thing that can be acted on immediately.
    failing = [c for c in ranked
               if c["regime"] == "ROSE" and (c["edge_pct_per_trade"] or 0) <= 0]

    return {
        "ranked": ranked,
        "still_qualifying": qualifying,
        "coins": len(cards),
        "crown": crown["coin"] if crown else None,
        "crown_status": crown["crown"] if crown else None,
        "crown_detail": (crown["crown_detail"] if crown else
                         (f"No coin has the {MIN_TRADES_FOR_CROWN} closed round trips a crown "
                          f"needs. {len(ranked)} coin(s) rank and the leader is "
                          f"{ranked[0]['coin']} at {ranked[0]['edge_pct_per_trade']:+.4f}%."
                          if ranked else
                          "No coin has enough closed trades to rank yet.")),
        "robustly_failing": [c["coin"] for c in failing],
        "robustly_failing_detail": (
            f"{', '.join(c['coin'] for c in failing)} lost per round trip while its own price "
            f"ROSE. No regime excuse exists for that, so it is the one finding here that can be "
            f"acted on today - before any crown is copied anywhere."
            if failing else None),
        "ranked_on": (
            "Return per dollar risked, per round trip, net of fees - pnl / (qty x entry). "
            "Not units: PEPE holds 20,232,619 of them and is worth $87.61, because unit count "
            "is set by a token's supply and nothing else. Not dollars earned either: that "
            "measures the allocation, not the coin. Per-dollar return is the only number on "
            "which an $87 position can genuinely beat a $2,250 one."),
        "is_a_measurement_not_a_change": True,
    }


def blueprint(league, challenger_coin):
    """What a trailing coin would copy from the leader, and whether it may.

    The transferable part is the SETTINGS, not the coin - grid step,
    levels, hold. Any coin can adopt those, including a weak one, which
    is the whole point of keeping a blueprint at all.
    """
    lead = next((c for c in (league or {}).get("ranked", []) if c.get("crown")), None)
    chall = next((c for c in ((league or {}).get("ranked", []) +
                              (league or {}).get("still_qualifying", []))
                  if c.get("coin") == str(challenger_coin or "").upper()), None)

    if lead is None:
        return {"ok": False, "reason": "NO_CROWN_YET",
                "detail": (f"No coin holds the crown, so there is nothing proven to copy. A "
                           f"crown needs {MIN_TRADES_FOR_CROWN} closed round trips and a "
                           f"positive per-dollar edge.")}
    if chall is None:
        return {"ok": False, "reason": "UNKNOWN_CHALLENGER",
                "detail": f"{challenger_coin} has no scorecard in this league."}
    if lead["crown"] == "PROVISIONAL":
        moved = (f"its price rose {lead['window_return_pct']:+.2f}% across the window"
                 if lead.get("window_return_pct") is not None
                 else "what its own price did across the window is unknown")
        return {"ok": False, "reason": "CROWN_IS_PROVISIONAL",
                "leader": lead["coin"],
                "detail": (f"{lead['coin']} leads, but {moved}, so its record has not been "
                           f"separated from its market. Copying it now would spread a rising "
                           f"market's returns as if they were a method.")}

    gap = None
    if chall.get("edge_pct_per_trade") is not None:
        gap = round(lead["edge_pct_per_trade"] - chall["edge_pct_per_trade"], 4)

    return {
        "ok": True,
        "leader": lead["coin"],
        "challenger": chall["coin"],
        "gap_pct_per_trade": gap,
        "copy": {k: v for k, v in (lead.get("config") or {}).items()},
        "from": {k: v for k, v in (chall.get("config") or {}).items()},
        "detail": (
            f"{lead['coin']} earns {lead['edge_pct_per_trade']:+.4f}% a round trip against "
            f"{chall['coin']}'s "
            + (f"{chall['edge_pct_per_trade']:+.4f}%" if chall.get("edge_pct_per_trade")
               is not None else "unmeasured")
            + (f" - a gap of {gap:+.4f} points. " if gap is not None else ". ")
            + f"The transferable part is the settings, not the coin: any coin can trade the "
              f"way {lead['coin']} trades, including a small one, because settings cost "
              f"nothing to adopt. What cannot be copied is {lead['coin']}'s market."),
        "is_a_plan_not_a_change": True,
    }
