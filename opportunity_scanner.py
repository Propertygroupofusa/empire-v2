"""Eight coins competing for capital, scored continuously, most refused.

WHAT THIS CHANGES

The fleet already refuses bad trades - _net_edge_gate_ok() prices every dip
against fees, spread, depth and adverse selection before it is allowed to
buy. What it could not do is COMPARE. The gate only ran on the one coin whose
dip trigger happened to fire, so eight coins were never scored against each
other and capital never went to the best of them; each branch simply held its
own earmark and waited its turn.

This scores all eight every cycle, gives each a state, and ranks the ones
that qualify. The ranking is on net edge per unit of risk, not raw net edge,
so a coin is not preferred merely for carrying a wider step.

THE FIVE STATES

    TRADE    the economics clear AND price has reached this branch's buy
             trigger - actionable right now
    WATCH    the economics clear but price has not reached the trigger yet
    HOLD     this branch already has an open slice
    REJECT   the economics do not clear - fees, spread, depth or net edge
    BLOCKED  something is wrong or unreadable: the book would not load, the
             drawdown breaker is on, the master switch is off

REJECT and BLOCKED are different on purpose. REJECT is an answer - the coin
was measured and is not worth trading right now. BLOCKED is the absence of an
answer. Collapsing them would let a venue outage read as "nothing looked
good", which is the single most expensive confusion available here.

NO TRADE IS A RESULT

Every coin sitting in REJECT at once is a correct, complete outcome, not a
failure to find something. The fleet has logged 152 consecutive refusals
against a 2.00% step that genuinely cannot clear a 2.17% round trip, and
every one of them was right. Nothing in this module lowers a standard because
too little has happened - there is no hourly target, no "trade something"
fallback, and no path that spends capital because capital is idle. An hourly
earnings figure belongs on a dashboard as an observation; the moment it
reaches the execution path it becomes a reason to take a trade the arithmetic
already refused.

WHAT THIS DELIBERATELY DOES NOT DO

It does not move money. Ranking is published; reallocation is a separate,
DB-toggled decision (see ALLOCATION_KEY) that defaults OFF, because changing
which branch gets funded is a live-capital change and it should be switched
on deliberately rather than inherited from a scanner that was only meant to
look.
"""

import logging

from sqlalchemy import select

from database import get_session_factory
from models import TradingBotState

log = logging.getLogger(__name__)

# Whether the ranking is allowed to actually steer capital, rather than only
# report. Defaults OFF and fails closed - same pattern every other real-money
# switch in this codebase uses.
ALLOCATION_KEY = "grid_competitive_allocation"

TRADE, WATCH, HOLD, REJECT, BLOCKED = "TRADE", "WATCH", "HOLD", "REJECT", "BLOCKED"

ICON = {TRADE: "🟢", WATCH: "🟡", HOLD: "🔵", REJECT: "🔴", BLOCKED: "⚫"}

# Order the states are shown in: what can act, then what might, then what is
# already committed, then what was measured and refused, then what could not
# be measured at all.
STATE_ORDER = {TRADE: 0, WATCH: 1, HOLD: 2, REJECT: 3, BLOCKED: 4}


async def is_competitive_allocation_active() -> bool:
    """Whether the ranking may steer capital. Fails closed."""
    try:
        async with get_session_factory()() as db:
            result = await db.execute(
                select(TradingBotState).where(TradingBotState.bot_name == ALLOCATION_KEY))
            row = result.scalar_one_or_none()
            return bool(row and row.base_capital and row.base_capital >= 1.0)
    except Exception as e:
        log.warning(f"[SCAN] allocation switch unreadable ({e}) - staying report-only")
        return False


async def set_competitive_allocation_active(active: bool) -> bool:
    async with get_session_factory()() as db:
        result = await db.execute(
            select(TradingBotState).where(TradingBotState.bot_name == ALLOCATION_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=ALLOCATION_KEY,
                                   base_capital=1.0 if active else 0.0))
        else:
            row.base_capital = 1.0 if active else 0.0
        await db.commit()
    log.warning(f"[SCAN] competitive allocation set to {active}")
    return active


def _edge_per_risk(row):
    """Net edge divided by what is risked to earn it.

    Ranking on raw net edge would hand capital to whichever coin has the
    widest step, which is not the same thing as the best opportunity - a
    wider step earns more per completed round trip and completes less often.
    """
    if not row:
        return None
    edge = row.get("net_edge_pct")
    risk = row.get("stop_pct") or row.get("target_pct")
    if edge is None or not risk:
        return None
    return edge / risk


async def score_one(session, branch, *, grid_mod, engine, scanner,
                    fee_round_trip, price=None):
    """One coin's full picture: economics, distance to its trigger, state.

    Never raises. A coin that cannot be scored comes back BLOCKED with the
    reason, which is a different answer from "not worth trading" and is kept
    distinct everywhere downstream.
    """
    pid = branch.get("product_id") if isinstance(branch, dict) else branch.product_id
    get = (lambda k, d=None: branch.get(k, d)) if isinstance(branch, dict) \
        else (lambda k, d=None: getattr(branch, k, d))

    out = {
        "product_id": pid,
        "bot_name": get("bot_name"),
        "state": BLOCKED,
        "reason": "not scored",
        "net_edge_pct": None,
        "edge_per_risk": None,
        "spread_pct": None,
        "grid_pct": get("grid_pct"),
        "open_slices": get("open_slices") or 0,
        "allocated_usd": get("allocated_usd"),
        "distance_to_buy_pct": None,
        "price": price,
        "reference_price": get("reference_price"),
    }

    try:
        step = out["grid_pct"]
        ref = out["reference_price"]

        if out["open_slices"]:
            out["state"] = HOLD
            out["reason"] = (f"{out['open_slices']} open slice"
                             f"{'' if out['open_slices'] == 1 else 's'} - "
                             f"this branch is already committed")

        bid, ask, bid_depth, ask_depth = await engine.get_book_top_and_depth(session, pid)
        if bid is None or ask is None:
            out["state"] = BLOCKED
            out["reason"] = "order book unreadable - not the same as nothing looking good"
            return out

        mid = (bid + ask) / 2.0
        out["price"] = price if price is not None else mid

        swing = await engine.get_average_hourly_swing_pct(session, pid)
        slice_usd = (out["allocated_usd"] or 0.0)
        levels = get("num_levels") or 10
        if levels:
            slice_usd = slice_usd / levels

        ok, reason, detail = scanner.evaluate_grid_step(
            pid, step, swing,
            best_bid=bid, best_ask=ask,
            bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
            slice_usd=slice_usd, fee_round_trip=fee_round_trip,
        )
        out["reason"] = reason
        out["net_edge_pct"] = (detail or {}).get("net_edge_pct")
        out["spread_pct"] = (detail or {}).get("spread_pct")
        out["edge_per_risk"] = _edge_per_risk(detail)

        # How far price still has to fall to reach this branch's buy trigger.
        # Distinguishes "qualified and ready" from "qualified but not yet".
        if ref and step and out["price"]:
            trigger = ref * (1 - step)
            out["buy_trigger"] = trigger
            out["distance_to_buy_pct"] = max(0.0, (out["price"] / trigger - 1.0)) * 100.0

        if out["state"] == HOLD:
            return out
        if not ok:
            out["state"] = REJECT
            return out
        out["state"] = TRADE if (out["distance_to_buy_pct"] is not None
                                 and out["distance_to_buy_pct"] <= 0.0) else WATCH
        if out["state"] == WATCH:
            out["reason"] = (f"economics clear ({reason}) but price is "
                             f"{out['distance_to_buy_pct']:.2f}% above the buy trigger")
        return out
    except Exception as e:
        out["state"] = BLOCKED
        out["reason"] = f"could not score: {type(e).__name__}: {e}"
        return out


def rank(rows):
    """Actionable first, best edge-per-risk first within that.

    Only TRADE rows compete - a coin whose price has not reached its trigger
    cannot be bought at any ranking, and a coin that was refused should never
    rise above one that was not merely because its number is larger.
    """
    def key(r):
        epr = r.get("edge_per_risk")
        return (STATE_ORDER.get(r["state"], 9),
                -(epr if epr is not None else -9e9),
                r["product_id"] or "")
    return sorted(rows, key=key)


def summarise(rows, free_cash=None, reserve=None, slice_usd=None):
    """What the scan concluded, in the terms the decision is actually made in."""
    counts = {s: 0 for s in (TRADE, WATCH, HOLD, REJECT, BLOCKED)}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    ranked = rank(rows)
    actionable = [r for r in ranked if r["state"] == TRADE]
    spendable = None
    if free_cash is not None:
        spendable = max(0.0, free_cash - (reserve or 0.0))
    fundable = int(spendable // slice_usd) if (spendable and slice_usd) else 0

    if counts[BLOCKED] == len(rows) and rows:
        verdict = ("Nothing could be scored this cycle - every coin is BLOCKED. "
                   "That is a system or venue problem, NOT a market with no "
                   "opportunities in it.")
    elif not actionable:
        best = next((r for r in ranked if r["state"] in (WATCH, REJECT)), None)
        verdict = (f"No coin qualifies right now. Holding cash is the correct "
                   f"outcome, not a failure to find a trade."
                   + (f" Closest: {best['product_id']} - {best['reason']}" if best else ""))
    else:
        top = actionable[0]
        verdict = (f"{len(actionable)} coin{'' if len(actionable) == 1 else 's'} qualify. "
                   f"Next slice goes to {top['product_id']} "
                   f"(net edge {(top['net_edge_pct'] or 0) * 100:+.3f}%, "
                   f"{top['edge_per_risk']:+.2f} per unit risk).")
        if fundable == 0:
            verdict += (" No slice can be funded: free cash is under the reserve, "
                        "so the ranking is advice, not an action.")

    return {
        "rows": ranked,
        "counts": counts,
        "actionable": [r["product_id"] for r in actionable],
        "next_slice_goes_to": actionable[0]["product_id"] if actionable else None,
        "slices_fundable_now": fundable,
        "spendable_usd": round(spendable, 2) if spendable is not None else None,
        "verdict": verdict,
        "note": ("Ranked on net edge per unit of risk, so a coin is not preferred "
                 "merely for carrying a wider step. Every coin in REJECT was "
                 "measured and refused; every coin in BLOCKED could not be "
                 "measured at all. Nothing here lowers a standard because the "
                 "fleet has been quiet - an hourly earnings figure is an "
                 "observation, never an input to a buy."),
    }
