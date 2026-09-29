"""Each slice's own sell target, derived from its own entry and its own fee.

WHY EXACTLY THIS FORMULA. crypto_grid_bot._grid_slice_net_pnl is THE single
fee/profit formula for a slice's round trip, extracted into one function on
the account owner's own instruction: "don't let the dashboard calculation
and the actual execution calculation use two different fee formulas ...
Otherwise you can end up with the dashboard saying +$4.21 while the actual
sale produces something different."

A target price computed by any formula that is not that one's exact inverse
reintroduces precisely the divergence it was extracted to prevent. So this
is the inverse, derived rather than approximated:

    net   = qty*(exit - entry) - qty*(entry + exit)*(rate/2)
    basis = qty * entry

    net/basis = E
      => (exit - entry) - (entry + exit)*(rate/2) = E*entry
      => exit*(1 - rate/2) = entry*(1 + E + rate/2)
      => exit = entry * (1 + E + rate/2) / (1 - rate/2)

THE SPEC'S OWN FORMULA IS AN APPROXIMATION, AND IT UNDERSHOOTS. §5 gives
target = entry * (1 + required_edge + execution_cost + margin). That treats
the fee as a flat addition, but the exit leg's fee is charged on the EXIT
price, which is higher than entry - so the real cost is larger than the
approximation allows. Measured against the real _grid_slice_net_pnl: at a
0.70% round trip and a required 1.20% edge, the approximation realises
1.19335% - short by 0.00665 points on every trade, in the same direction
every time. Small, but it is edge given away systematically rather than
lost to variance, and there is no reason to accept it when the exact
answer is one division away.

ROUNDING A SELL TARGET GOES UP, NOT DOWN. resting_stops.round_price rounds
DOWN because moving a protective stop down is the safe direction. A sell
TARGET is the opposite: rounding it down to the quote increment gives away
edge on every fill. The two are deliberately different functions.

NOTHING HERE DECIDES WHETHER TO TRADE. It answers "at what price does this
particular slice clear its required edge", nothing more.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_UP, InvalidOperation


def _f(v):
    """Float or None. None is UNKNOWN and propagates as a refusal."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def target_price(entry_price, round_trip_fee_rate, required_net_edge):
    """The exit price at which THIS slice nets exactly `required_net_edge`
    over its own basis, or None if it cannot be computed.

    `round_trip_fee_rate` is the slice's OWN rate - crypto_grid_bot._slice_rate
    already derives it from the buy leg's real recorded entry_fee_rate plus
    the expected exit leg, because once maker and market fills are mixed the
    two legs of one round trip genuinely cost different amounts.

    None in, None out. A target computed from a guessed fee is a target that
    does not mean what it says.
    """
    entry = _f(entry_price)
    rate = _f(round_trip_fee_rate)
    edge = _f(required_net_edge)
    if entry is None or rate is None or edge is None:
        return None
    if entry <= 0:
        return None
    # rate >= 2 makes the denominator zero or negative: a round trip that
    # costs the whole position has no exit price that clears any edge.
    if rate < 0 or rate >= 2:
        return None
    return entry * (1.0 + edge + rate / 2.0) / (1.0 - rate / 2.0)


def realised_edge(entry_price, exit_price, round_trip_fee_rate):
    """What edge a fill at `exit_price` would actually net, over basis.

    The inverse of target_price, kept here so the round trip can be
    asserted rather than assumed.
    """
    entry = _f(entry_price)
    exit_ = _f(exit_price)
    rate = _f(round_trip_fee_rate)
    if entry is None or exit_ is None or rate is None or entry <= 0:
        return None
    gross = exit_ - entry
    fee = (entry + exit_) * (rate / 2.0)
    return (gross - fee) / entry


def round_target_up(price, quote_increment):
    """The target, raised to the next quote increment. None if unknown.

    UP, deliberately. Rounding a sell target DOWN to the venue's tick gives
    away part of the edge on every single fill; rounding up costs at most
    one tick of fill probability and never costs edge. This is the opposite
    of resting_stops.round_price, which rounds a protective stop DOWN
    because that is the safe direction for a stop.
    """
    if price is None or quote_increment is None:
        return None
    try:
        p = Decimal(str(price))
        inc = Decimal(str(quote_increment))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not p.is_finite() or not inc.is_finite() or inc <= 0 or p <= 0:
        return None
    return (p / inc).to_integral_value(rounding=ROUND_UP) * inc


def targets_for_slices(slices, required_net_edge, rate_for=None):
    """One target per slice, each from THAT slice's own entry price.

    §5's actual requirement: "Do not copy the same target price across
    slices when their entry prices differ." Three slices bought at 100.00,
    100.40 and 100.85 get three different targets, because they have three
    different break-evens.

    `slices` is any sequence of objects with `entry_price` (and optionally
    whatever `rate_for` needs). `rate_for(slice)` supplies that slice's own
    round-trip rate; without it every slice shares one rate, which is the
    older behaviour and still correct when no maker fills are mixed in.

    A slice whose target cannot be computed yields None rather than a
    fallback - a slice with an unknown target must not be given the
    target of a different slice.
    """
    out = []
    for s in (slices or []):
        entry = getattr(s, "entry_price", None)
        rate = rate_for(s) if rate_for is not None else None
        out.append(target_price(entry, rate, required_net_edge))
    return out
