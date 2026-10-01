"""How big the losses are, why they happen, and what would make them smaller.

THE ASK, AND THE HONEST PART OF IT

"Eventually we just take profit and never take a loss at all."

A grid that never sells at a loss already exists - it is called holding.
The sell path refuses to sell at a loss on purpose
(_pick_profitable_slice_to_sell returns None rather than force one), and
the ONLY thing that ever books a loss here is the stop. So "no losses"
is not a target to work toward; it is one setting away, and it is the
wrong setting.

Turn the stop off and a slice that falls 40% is simply held, forever,
waiting for a +2.5% that has to come from a much lower price. The loss
does not disappear - it stops being counted and starts being inventory.
That is precisely how a 0.9% grid step took this fleet from +65.4% to
-71.1%: nothing was ever realised as a loss, and everything was stuck.

So the measurable goal is not zero losses. It is:

    make the average loss small relative to the average win,
    and make sure every loss taken was worth taking.

Today: avg win $0.4267, avg loss $0.3826, profit factor 3.70. The wins
are barely larger than the losses and the fleet is profitable on
FREQUENCY of winning (75.9%), not on size. That is the fragile shape -
one bad stretch of win rate and it inverts.

WHAT THIS MEASURES

  - the split between losses taken by the STOP and losses taken any
    other way, because they have different fixes
  - the size distribution, so "too big" stops being a feeling
  - what each candidate stop distance WOULD have done, replayed against
    the recorded max adverse excursion on the SAME entries

That last one is the point. mae_pct records how far a slice ever went
against its entry, so "would a 5% stop have beaten the 8% one?" is
answerable on the real closed book instead of by re-running a backtest
that also changes which trades happened. It refuses to answer below a
sample that can support it.
"""
from __future__ import annotations

import statistics
from datetime import datetime

# Below this, a stop comparison is arithmetic on anecdotes.
MIN_TRADES_FOR_STOP_SWEEP = 25

# Distances worth asking about, as fractions.
CANDIDATE_STOPS = (0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12)

# A loss bigger than this multiple of the average win is the shape that
# turns a 75%-win strategy negative.
BIG_LOSS_MULTIPLE = 2.0


def _parse(ts):
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _rows(trades, config_epoch=None):
    epoch = _parse(config_epoch)
    out = []
    for t in trades or ():
        if not hasattr(t, "get"):
            continue
        pnl = _num(t.get("pnl"))
        if pnl is None:
            continue
        qty, entry = _num(t.get("qty")), _num(t.get("entry_price"))
        risked = abs(qty * entry) if (qty is not None and entry is not None) else None
        out.append({
            "pnl": pnl,
            "risked": risked if (risked or 0) > 0 else None,
            "pct": (pnl / risked * 100) if risked else None,
            "exit_reason": t.get("exit_reason"),
            "mae_pct": _num(t.get("mae_pct")),
            "mfe_pct": _num(t.get("mfe_pct")),
            "product_id": t.get("product_id"),
            "closed_at": t.get("closed_at"),
            # An untimed trade counts as PRE-epoch. It is not evidence
            # about the bot running now, and assuming otherwise flatters
            # it - the same rule the league learned the hard way.
            "on_config": bool(epoch and _parse(t.get("closed_at"))
                              and _parse(t.get("closed_at")) >= epoch),
        })
    return out


def analyse(trades, *, config_epoch=None):
    """The shape of the losses, and which kind they are.

    Split against `config_epoch`, because a loss booked by a
    configuration that has since been replaced is history, not a problem
    to fix. This fleet's 19 losses were ALL taken before the 2026-09-26
    change - including the DOGE trades that prompted the sell-path fix in
    _pick_profitable_slice_to_sell, whose whole job is to refuse a losing
    sale. Reading them as a live loss problem would send someone
    tightening a stop that never fired.
    """
    rows = _rows(trades, config_epoch)
    n = len(rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]

    avg_win = statistics.mean([r["pnl"] for r in wins]) if wins else None
    avg_loss = statistics.mean([r["pnl"] for r in losses]) if losses else None

    by_reason = {}
    for r in losses:
        by_reason[r["exit_reason"] or "unrecorded"] = \
            by_reason.get(r["exit_reason"] or "unrecorded", 0) + 1

    worst = sorted(losses, key=lambda r: r["pnl"])[:5]
    big = ([r for r in losses if avg_win and abs(r["pnl"]) > avg_win * BIG_LOSS_MULTIPLE]
           if avg_win else [])

    loss_pcts = [r["pct"] for r in losses if r["pct"] is not None]
    win_pcts = [r["pct"] for r in wins if r["pct"] is not None]

    on_cfg = [r for r in rows if r["on_config"]]
    losses_on_cfg = [r for r in on_cfg if r["pnl"] < 0]

    # THE RATIO THAT DECIDES FRAGILITY. With wins and losses the same
    # size, the whole edge rests on the win RATE holding - and a win rate
    # is the first thing a regime change moves.
    ratio = round(abs(avg_win / avg_loss), 3) if (avg_win and avg_loss) else None
    breakeven_win_rate = (round(100 / (1 + ratio), 1) if ratio else None)
    actual_win_rate = round(len(wins) / n * 100, 1) if n else None

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": n - len(wins) - len(losses),
        "win_rate_pct": actual_win_rate,

        "avg_win_usd": round(avg_win, 4) if avg_win else None,
        "avg_loss_usd": round(avg_loss, 4) if avg_loss else None,
        "worst_loss_usd": round(min(r["pnl"] for r in losses), 4) if losses else None,
        "total_lost_usd": round(sum(r["pnl"] for r in losses), 2) if losses else 0.0,

        "avg_win_pct": round(statistics.mean(win_pcts), 4) if win_pcts else None,
        "avg_loss_pct": round(statistics.mean(loss_pcts), 4) if loss_pcts else None,
        "worst_loss_pct": round(min(loss_pcts), 4) if loss_pcts else None,

        "win_loss_size_ratio": ratio,
        "breakeven_win_rate_pct": breakeven_win_rate,
        "margin_of_safety_points": (round(actual_win_rate - breakeven_win_rate, 1)
                                    if (actual_win_rate and breakeven_win_rate) else None),

        "trades_on_current_config": len(on_cfg),
        "losses_on_current_config": len(losses_on_cfg),
        "lost_on_current_config_usd": round(sum(r["pnl"] for r in losses_on_cfg), 2),
        "config_epoch": config_epoch,
        "config_split": (
            (f"{len(losses)} loss(es) in the book and {len(losses_on_cfg)} of them were taken "
             f"by the configuration running now. "
             + ("The rest were booked by a bot that has since been replaced - including the "
                "trades that prompted the sell-path fix, whose whole job is to refuse a "
                "losing sale. Reading them as a live problem would send someone tightening "
                "a stop that never fired."
                if len(losses_on_cfg) < len(losses) else
                "Every loss here was taken by the current bot."))
            if config_epoch else
            "No config epoch supplied, so a loss taken by the current bot cannot be told "
            "from one a replaced configuration booked."),

        "losses_by_exit_reason": by_reason,
        "big_losses": len(big),
        "worst_five": [{"pnl": round(r["pnl"], 4), "pct": round(r["pct"], 3) if r["pct"] else None,
                        "product_id": r["product_id"], "exit_reason": r["exit_reason"]}
                       for r in worst],

        "fragility": (
            None if ratio is None else
            ("Wins and losses are nearly the same size, so the entire edge rests on the win "
             f"RATE staying above {breakeven_win_rate}%. It is {actual_win_rate}% today. A win "
             f"rate is the first thing a regime change moves, which makes this the fragile "
             f"shape - profitable on frequency, not on size."
             if ratio < 1.5 else
             f"Wins are {ratio:.2f}x the size of losses, so the strategy survives a win rate "
             f"down to {breakeven_win_rate}%. It is {actual_win_rate}% today.")),
    }


def stop_sweep(trades, *, candidates=CANDIDATE_STOPS, fee_rate_pct=0.70,
               min_trades=MIN_TRADES_FOR_STOP_SWEEP):
    """What each stop distance WOULD have done, on the same entries.

    Replayed against the recorded max adverse excursion, which is why
    those columns exist: "would a 5% stop have beaten the 8% one" is a
    question about the real closed book, not about a backtest that also
    changes which trades happened.

    A trade with no recorded mae cannot be replayed and is counted, never
    assumed. Early in this ledger that is most of them.
    """
    rows = _rows(trades)
    usable = [r for r in rows if r["mae_pct"] is not None and r["pct"] is not None]

    if len(usable) < min_trades:
        return {
            "available": False,
            "usable_trades": len(usable),
            "total_trades": len(rows),
            "reason": (
                f"{len(usable)} of {len(rows)} closed trades carry a recorded max adverse "
                f"excursion, and a stop comparison needs {min_trades}. mae_pct was added on "
                f"2026-09-26, so the ledger is only now starting to collect it - at this "
                f"fleet's rate that is a matter of weeks, not months. Answering now would be "
                f"arithmetic on anecdotes, and a stop chosen that way costs real money on "
                f"every trade after it."),
        }

    out = []
    for stop in candidates:
        cut = kept = 0
        pnl = 0.0
        for r in usable:
            # A slice whose worst excursion reached the stop would have
            # been sold there, at roughly -stop minus the round trip.
            # UNITS. mae_pct is stored as a FRACTION (-0.1269 is -12.69%),
            # CANDIDATE_STOPS are fractions (0.03 is 3%), and r["pct"] is a
            # PERCENT (pnl / risked * 100). This line compared the fraction
            # against `-abs(stop) * 100` - that is -3.0 for a 3% stop - so
            # it asked whether -0.1269 <= -3.0. It never can be.
            #
            # The result: EVERY candidate from 3% to 12% reported 0 stopped
            # out and an identical net, and the panel published "3.0% is
            # best" off a comparison that could not fire. A real -12.69%
            # excursion sat in the data and no stop level saw it.
            if r["mae_pct"] is not None and r["mae_pct"] <= -abs(stop):
                cut += 1
                pnl += -(abs(stop) * 100 + fee_rate_pct) / 100 * (r["risked"] or 0)
            else:
                kept += 1
                pnl += r["pnl"]
        out.append({"stop_pct": round(stop * 100, 2), "would_stop_out": cut,
                    "would_keep": kept, "net_usd": round(pnl, 4)})

    out.sort(key=lambda o: -o["net_usd"])
    best = out[0]
    return {
        "available": True,
        "usable_trades": len(usable),
        "total_trades": len(rows),
        "by_stop": out,
        "best_stop_pct": best["stop_pct"],
        "detail": (
            f"Replayed on {len(usable)} trades that carry a recorded excursion. A "
            f"{best['stop_pct']:.1f}% stop would have netted ${best['net_usd']:,.2f} against "
            f"${out[-1]['net_usd']:,.2f} at the worst candidate. This is a comparison on "
            f"IDENTICAL entries - it does not say those entries were good ones."),
    }


def verdict(analysis, sweep=None):
    """One answer about the losses, and what would actually shrink them."""
    a = analysis or {}
    if not a.get("trades"):
        return ("NO_TRADES", "No closed trades, so there are no losses to size.")

    # BEFORE ANYTHING ELSE: are these even THIS bot's losses? A stop
    # tightened over a replaced configuration's history costs money on
    # every future trade and fixes nothing.
    if (a.get("config_epoch") and a.get("losses")
            and not a.get("losses_on_current_config")):
        return ("EVERY_LOSS_PREDATES_THIS_CONFIG",
                f"All {a['losses']} loss(es) were booked before the {a['config_epoch']} "
                f"change, and the configuration running now has lost nothing across "
                f"{a.get('trades_on_current_config')} trade(s). The worst of them came from "
                f"DOGE and WIF under a sell path that has since been fixed to refuse a losing "
                f"sale. Tightening a stop over this history would cost money on every future "
                f"trade and fix a problem that is already fixed.")

    if not a.get("losses"):
        return ("NO_LOSSES_YET",
                "No closed trade has lost money. That is not a solved problem yet - it is a "
                "sample that has not met a bad stretch.")

    by = a.get("losses_by_exit_reason") or {}
    stopped = by.get("stop_loss", 0)
    other = sum(v for k, v in by.items() if k != "stop_loss")

    if a.get("big_losses"):
        return ("A_FEW_LOSSES_DOMINATE",
                f"{a['big_losses']} loss(es) are more than {BIG_LOSS_MULTIPLE}x the average "
                f"win. Those are what a tighter stop actually addresses - trimming the many "
                f"small losses saves almost nothing and cuts winners short doing it.")

    # SIZE BEFORE CAUSE. "Every loss came from the stop" is descriptive
    # and true of a healthy fleet and a fragile one alike; the ratio is
    # what decides which of those this is, so it is asked first.
    if (a.get("win_loss_size_ratio") or 0) < 1.5:
        return ("PROFITABLE_ON_FREQUENCY_NOT_SIZE",
                a.get("fragility") or "Wins and losses are nearly the same size.")
    if other and not stopped:
        return ("LOSSES_ARE_NOT_FROM_THE_STOP",
                f"{other} loss(es) were not stop exits. The sell path refuses to force a "
                f"losing sale, so these need explaining before any stop is touched - a loss "
                f"from somewhere else is not fixed by moving the stop.")
    if stopped and not other:
        return ("EVERY_LOSS_IS_THE_STOP",
                f"All {stopped} loss(es) were taken by the stop, which means they were chosen, "
                f"not suffered. The only question left is whether the distance is right, and "
                f"that is answered by the excursion sweep - not by opinion.")
    return ("LOSSES_ARE_IN_PROPORTION",
            f"Wins are {a['win_loss_size_ratio']:.2f}x losses at a {a['win_rate_pct']}% win "
            f"rate, against a {a['breakeven_win_rate_pct']}% breakeven. There is no loss-size "
            f"problem to fix here; the constraint is elsewhere.")
