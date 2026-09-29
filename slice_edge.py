"""How much net edge a slice must clear before a target is worth setting.

THE SPEC OPENS §6 WITH THE WARNING THAT MATTERS: "Do NOT simply increase TP
to make the system appear more profitable." So this module is mostly a set
of floors and a refusal, not a model.

WHAT IT DOES NOT DO, ON PURPOSE. It does not invent a volatility-scaling
formula for the target edge. §6 lists a dozen inputs that "may" feed one -
volatility, spread, realised fill rate, trade duration, inventory age,
order-book conditions - and a scaler built from them would be an
unvalidated claim about this market dressed as arithmetic. There is no
evidence yet that raising the ask above the floor improves NET outcome
rather than just lowering the fill rate, and a wider target that never
fills is a slower engine, not a better one. The hook is here, off, with
the reason written down; turning it on is a decision that should follow
evidence rather than precede it.

WHAT IT DOES. It enforces the floor that already exists and refuses when
the market cannot clear it:

  - The edge fed to slice_target.target_price is NET of fees already -
    that is what the inverse of _grid_slice_net_pnl computes - so any
    positive edge is profitable in the narrow sense. The floor is
    therefore about whether a trade is WORTH doing, not whether it loses:
    GRID_PARKED_MIN_NET_PCT (1.0%) is the account's own answer and is
    never silently undercut here.
  - An adverse-movement allowance can raise that floor but never lower it.
  - If the expected move cannot clear the floor, the answer is DO NOT
    TRADE, not a smaller target. §6 says so explicitly, and the owner's
    standing rule is that no threshold is ever lowered to manufacture
    activity.

The buy side is NOT re-derived here. _net_edge_gate_ok already prices
spread, depth, the real round trip and adverse selection against the live
book, fails closed, and persists its whole diagnostic per slice in
entry_gate_json. This is the sell side's floor only.
"""
from __future__ import annotations

from dataclasses import dataclass

# Decisions
# ─────────────────────────────────────────────────────────────────────────
# AUDITED 2026-09-29: DO NOT WIRE THIS INTO THE BUY PATH.
#
# §6 is ALREADY SERVED, and by something strictly better.
# crypto_grid_bot._net_edge_gate_ok prices, against the LIVE BOOK in the
# instant before the order: the spread actually paid, the depth the slice
# would trade through, one completed step against the real round trip plus
# adverse selection priced off the coin's own volatility, and book
# pressure - that last one in OBSERVE mode until its own logs justify
# enforcing it. It fails closed, and it persists its full reasoning to
# entry_gate_json.
#
# This module takes numbers as ARGUMENTS. It cannot see a book. Calling it
# from the buy path would be writing a second, weaker net-edge gate beside
# a working one, which is the specific thing this codebase forbids - and
# two gates that disagree is how the exit_reason bug happened.
#
# IT IS NOT PENDING WORK. It is not waiting to be wired. Anyone who finds
# it unconsumed and assumes the wiring was forgotten should read this note
# instead, which is why the note is here rather than only in a commit.
#
# WHAT IT IS STILL GOOD FOR: the one idea it holds that nothing else does
# is the ADAPTIVE WIDENER below - raising the required edge toward what
# the market is actually offering. That hook is deliberately OFF
# (allow_adaptive_widening=False) because turning it on is an unvalidated
# claim about the market, and a wider target that never fills is a slower
# engine, not a better one. If evidence ever justifies it, this is where
# it lives. Until then this module is a recorded piece of reasoning, not a
# component.
# ─────────────────────────────────────────────────────────────────────────

SET_TARGET = "SET_TARGET"
DO_NOT_TRADE = "DO_NOT_TRADE"
REFUSED = "REFUSED"

# Reasons
OK = "OK"
BELOW_FLOOR = "BELOW_FLOOR"
MARKET_CANNOT_SUPPORT = "MARKET_CANNOT_SUPPORT"
FLOOR_UNKNOWN = "FLOOR_UNKNOWN"
EDGE_UNKNOWN = "EDGE_UNKNOWN"


def _f(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def minimum_profitable_edge(configured_floor_pct, adverse_margin_pct=0.0):
    """The smallest net edge this account is willing to sell for.

    `configured_floor_pct` is the account's own threshold - in practice
    GRID_PARKED_MIN_NET_PCT, 1.0%. It is REQUIRED: defaulting it would
    mean inventing a floor, and a floor invented here is a threshold
    lowered without anyone deciding to.

    The adverse margin can only raise it. Passing a negative margin cannot
    be used to sneak under the configured floor.
    """
    floor = _f(configured_floor_pct)
    if floor is None or floor < 0:
        return None
    margin = _f(adverse_margin_pct) or 0.0
    if margin < 0:
        margin = 0.0
    return floor + margin


@dataclass(frozen=True)
class EdgePlan:
    decision: str
    reason: str
    detail: str
    target_edge: float | None = None
    minimum_edge: float | None = None
    requested_edge: float | None = None
    expected_move_pct: float | None = None

    @property
    def should_set_target(self) -> bool:
        return self.decision == SET_TARGET


def plan_edge(*, configured_floor_pct, requested_edge=None,
              adverse_margin_pct=0.0, expected_move_pct=None,
              allow_adaptive_widening=False):
    """The net edge to require of this slice, or why none should be set.

    `expected_move_pct` is what the market plausibly offers. When it is
    given and cannot clear the floor, the answer is DO_NOT_TRADE - never a
    reduced target. When it is absent the floor still applies; an unknown
    move is not evidence that the market can support anything.

    `allow_adaptive_widening` is the §6 hook, default off. Even when on it
    can only RAISE the requirement toward the expected move, never lower
    it, and it is capped by that move so the target is not set somewhere
    the market has shown no sign of reaching.
    """
    minimum = minimum_profitable_edge(configured_floor_pct, adverse_margin_pct)
    if minimum is None:
        return EdgePlan(
            decision=REFUSED, reason=FLOOR_UNKNOWN,
            detail=("the account's own minimum net edge could not be read. "
                    "Refusing rather than inventing one - a floor invented "
                    "here is a threshold lowered without anyone deciding to."))

    req = _f(requested_edge)
    if requested_edge is not None and req is None:
        return EdgePlan(
            decision=REFUSED, reason=EDGE_UNKNOWN,
            detail="the requested edge was not a readable number.",
            minimum_edge=minimum)

    # The floor binds. A request under it is raised to it, never honoured.
    edge = minimum if req is None or req < minimum else req

    move = _f(expected_move_pct)
    if move is not None and move < minimum:
        return EdgePlan(
            decision=DO_NOT_TRADE, reason=MARKET_CANNOT_SUPPORT,
            detail=(f"the expected move of {move} cannot clear the {minimum} "
                    f"floor. The answer is no trade, not a smaller target - "
                    f"lowering the ask to fit the market is how a threshold "
                    f"gets moved to manufacture activity."),
            minimum_edge=minimum, requested_edge=req, expected_move_pct=move)

    if allow_adaptive_widening and move is not None and move > edge:
        # Raise toward what the market is actually offering, capped by it.
        # Never beyond: a target the market has shown no sign of reaching
        # is a slower engine, not a better one.
        edge = move

    return EdgePlan(
        decision=SET_TARGET, reason=OK,
        detail=(f"requiring {edge} net, at or above the {minimum} floor."),
        target_edge=edge, minimum_edge=minimum, requested_edge=req,
        expected_move_pct=move)
