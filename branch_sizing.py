"""Can the fleet afford N new grid branches of $X each? READ-ONLY.

Backs GET /grid-status/sizing-check. Pure: it is handed readings and
returns a report, so it can be tested without fastapi, sqlalchemy or a
wallet, and it cannot move money.

WHY THIS EXISTS. The manual paths - /grid-status/create-branch,
/create-multiple-branches and /quick-buy - all go through
create_grid_branch, whose only cash rule is "free cash covers the amount".
The automatic path, _auto_deploy_idle_free_cash, applies two more:
GRID_CASH_RESERVE_USD stays back for the open branches' remaining levels,
and the grid never spends past its allocator share, which is what protects
the family tree's money in the same wallet. A manual call sized by eye can
break both. This applies all three before anyone presses the button.

Measured 2026-10-04 when this was written: $0.17 free, an $88.00 reserve,
a $0.00 ceiling - every manual branch would have been refused, and the
"double-counted" $3,327.68 that looked free was every branch's own unspent
reserve, earmarked for its next levels.

It also lists every coin that has traded on the fleet and has no branch
now, with its record, so a rebuild is judged on what each coin did rather
than on how many coins there used to be. Keyed on product_id, never
bot_name: branch names get reassigned to other coins.

UNKNOWN IS NEVER ZERO. Any reading that could not be taken leaves every
answer that depends on it as None, never $0.00 and never "fits".
"""
from __future__ import annotations

import math

# Mirrored by value from crypto_grid_bot / rotation_task so this module
# imports nothing heavy. test_branch_sizing.py asserts they still match.
MIN_TRADE_USD = 5.0
DEFAULT_GRID_LEVELS = 10
KEEP_BRANCH_ALIVE_USD = 15.0
OVERRIDE_LEVELS = {
    "3_levels_2.0pct": 3, "3_levels_2.5pct": 3, "5_levels_2.0pct": 5,
    "4_levels_1.5pct": 4, "3_levels_3.0pct": 3,
}


def levels_for(amount, override_label=None):
    """crypto_grid_bot._effective_num_levels, by value."""
    base = max(1, min(DEFAULT_GRID_LEVELS, int(amount // MIN_TRADE_USD)))
    cap = OVERRIDE_LEVELS.get(override_label)
    return max(1, min(base, cap)) if cap else base


def _grid_ceiling(cash_report):
    """(ceiling, share_pct, reason) for the grid from allocation_report."""
    for b in (cash_report or {}).get("bots") or []:
        if b.get("bot") == "grid":
            return b.get("ceiling_usd"), b.get("share_pct"), b.get("reason")
    return None, None, "the grid's allocator ceiling could not be read - UNKNOWN"


def deployable_usd(free_cash, reserve, ceiling):
    """_auto_deploy_idle_free_cash's rule: min(free - reserve, ceiling)."""
    if free_cash is None or reserve is None or ceiling is None:
        return None
    return round(max(0.0, min(free_cash - reserve, ceiling)), 2)


def flat_branches(branches, keep_alive=KEEP_BRANCH_ALIVE_USD):
    """Branches with no open slice - the only ones withdraw_from_grid_branch
    will release money from. Each keeps `keep_alive`, because draining a
    branch to zero deletes its row."""
    out = []
    for b in branches or []:
        if (b.get("open_slices") or 0) != 0:
            continue
        alloc = b.get("allocated_usd")
        out.append({
            "product_id": b.get("product_id"), "bot_name": b.get("bot_name"),
            "allocated_usd": alloc,
            "withdrawable_keeping_branch_usd":
                None if alloc is None else round(max(0.0, alloc - keep_alive), 2),
            "locked": bool(b.get("locked")),
        })
    return out


def drained_coins(trades, live_coins):
    """Per-coin record for every coin that traded and has no branch now."""
    per = {}
    for t in trades or []:
        pid = t.get("product_id")
        if not pid or pid in live_coins:
            continue
        p = per.setdefault(pid, {"product_id": pid, "trades": 0, "wins": 0,
                                 "realized_pnl_usd": 0.0, "worst_trade_usd": None,
                                 "last_close": None, "exit_reasons": {}})
        pnl = float(t.get("pnl") or 0.0)
        p["trades"] += 1
        p["wins"] += 1 if pnl > 0 else 0
        p["realized_pnl_usd"] += pnl
        p["worst_trade_usd"] = pnl if p["worst_trade_usd"] is None else min(p["worst_trade_usd"], pnl)
        closed = t.get("closed_at")
        if closed and (p["last_close"] is None or str(closed) > p["last_close"]):
            p["last_close"] = str(closed)
        reason = t.get("exit_reason") or "unrecorded"
        p["exit_reasons"][reason] = p["exit_reasons"].get(reason, 0) + 1
    rows = sorted(per.values(), key=lambda p: -p["realized_pnl_usd"])
    for p in rows:
        p["realized_pnl_usd"] = round(p["realized_pnl_usd"], 2)
        p["win_rate_pct"] = round(100.0 * p["wins"] / p["trades"], 1)
    return rows


def plan(count, amount, free_cash, reserve, ceiling, override_label=None):
    """Would `count` branches of `amount` each pass all three cash rules?"""
    total = round(count * amount, 2)
    lv = levels_for(amount, override_label)
    checks = {
        "free_cash_covers_total": None if free_cash is None else free_cash + 0.01 >= total,
        "leaves_grid_reserve": (None if free_cash is None or reserve is None
                                else free_cash - total >= reserve - 0.01),
        "within_grid_allocator_share": None if ceiling is None else total <= ceiling + 0.01,
    }
    fits = None if any(v is None for v in checks.values()) else all(checks.values())
    dep = deployable_usd(free_cash, reserve, ceiling)
    return {
        "count": count, "amount_per_branch": amount, "total_usd": total,
        "levels_per_branch": lv, "slice_usd": round(amount / lv, 2),
        "slice_meets_min_trade": amount / lv >= MIN_TRADE_USD,
        "checks": checks, "fits": fits,
        "max_branches_at_this_amount":
            None if dep is None else int(math.floor(dep / amount + 1e-9)),
    }


def assess(status, cash_report, history, reserve, backing=None,
           count=None, amount=None):
    """The whole report.

    status       crypto_grid_bot.get_grid_status()
    cash_report  crypto_cash_allocator.allocation_report(free_cash), or None
    history      crypto_grid_bot.get_grid_trade_history(...)
    reserve      GRID_CASH_RESERVE_USD
    backing      slice_backing.assess(...) as /grid-status computes it, or None
    """
    status = status or {}
    branches = status.get("branches") or []
    free = status.get("real_free_cash_usd")
    ceiling, share_pct, ceiling_reason = _grid_ceiling(cash_report)
    deployable = deployable_usd(free, reserve, ceiling)

    flat = flat_branches(branches)
    freeable = round(sum(f["withdrawable_keeping_branch_usd"] or 0.0
                         for f in flat if not f["locked"]), 2)

    if backing and backing.get("readable"):
        unbacked = [{"product_id": u.get("product_id"), "backed_pct": u.get("backed_pct"),
                     "short_usd": u.get("short_usd"), "can_be_sold": u.get("can_be_sold")}
                    for u in backing.get("unbacked") or []]
    else:
        unbacked = None   # UNKNOWN - the wallet was not read, not "all backed"

    history = history or {}
    live_coins = {b.get("product_id") for b in branches}
    drained = drained_coins(history.get("recent_trades"), live_coins)

    report = {
        "is_a_measurement_not_a_change": True,
        "cash": {
            "real_free_cash_usd": free,
            "grid_cash_reserve_usd": reserve,
            "global_reserve_usd": (cash_report or {}).get("global_reserve_usd"),
            "grid_share_pct": share_pct,
            "grid_ceiling_usd": ceiling,
            "grid_ceiling_reason": ceiling_reason,
            "deployable_for_new_branches_usd": deployable,
            "unspent_reserve_inside_branches_usd":
                (status.get("allocation_backing") or {}).get("unspent_reserve_usd"),
            "unspent_reserve_note": ("already earmarked for those branches' next "
                                     "levels - not free cash"),
        },
        "fleet": {
            "branches": len(branches),
            "with_open_slices": status.get("branches_with_open_slices"),
            "total_allocated_usd": status.get("total_allocated_usd"),
            "unrealized_net_usd": status.get("total_unrealized_net_usd"),
            "auto_rotate_active": status.get("auto_rotate_active"),
            "grid_spacing_override": status.get("grid_spacing_override"),
        },
        "freeable_from_flat_branches_usd": freeable,
        "flat_branches": flat,
        "unbacked_branches": unbacked,
        "drained_coins": drained,
        "drained_history_complete": not history.get("recent_trades_truncated"),
        "plan": (plan(count, amount, free, reserve, ceiling,
                      status.get("grid_spacing_override"))
                 if count and amount else None),
    }
    report["detail"] = _detail(report)
    return report


def _money(v):
    if v is None:
        return "UNKNOWN"
    return ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"


def _detail(r):
    c, p = r["cash"], r["plan"]
    s = (f"{_money(c['deployable_for_new_branches_usd'])} is deployable into new branches: "
         f"{_money(c['real_free_cash_usd'])} free, less the "
         f"{_money(c['grid_cash_reserve_usd'])} grid reserve, capped at the grid's "
         f"{_money(c['grid_ceiling_usd'])} share. "
         f"{_money(r['freeable_from_flat_branches_usd'])} more could be withdrawn from "
         f"flat branches.")
    if p:
        verdict = {True: "FITS", False: "DOES NOT FIT", None: "is UNKNOWN"}[p["fits"]]
        s += (f" {p['count']} x {_money(p['amount_per_branch'])} = "
              f"{_money(p['total_usd'])} {verdict}; at most "
              f"{p['max_branches_at_this_amount']} branch(es) at that size right now.")
    return s
