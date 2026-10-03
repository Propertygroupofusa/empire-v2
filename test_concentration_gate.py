"""The 20% ceiling, enforced on new buys only.

Live fleet, 2026-09-28: ZEC-USD held 32.6% and XRP-USD 31.1% of the real
cost basis - 63.7% between them - with zero completed round trips each,
while the ceiling was written down and never consulted.
"""
import ast
import pathlib

import pytest

import capital_velocity
import concentration_gate as gate

# The real fleet, by cost basis, on the day the gate was written.
FLEET = {
    "ZEC-USD": 2341.45, "XRP-USD": 2240.54, "ETH-USD": 438.94, "XLM-USD": 400.00,
    "SHIB-USD": 303.39, "BCH-USD": 231.89, "PEPE-USD": 205.14, "LINK-USD": 200.82,
    "SOL-USD": 152.66, "ALGO-USD": 128.33, "ACH-USD": 110.71, "QNT-USD": 108.84,
    "HBAR-USD": 108.92, "LTC-USD": 75.90, "NEAR-USD": 71.50, "TIA-USD": 23.38,
    "ONDO-USD": 23.34, "BTC-USD": 16.92, "FLOKI-USD": 10.81, "JASMY-USD": 0.0,
}


def test_there_is_exactly_one_ceiling_number():
    """Two disagreeing copies of a constant is the bug class that keeps
    recurring here. The gate must not restate it."""
    assert gate.MAX_SINGLE_COIN_SHARE is capital_velocity.MAX_SINGLE_COIN_SHARE
    src = pathlib.Path(gate.__file__).read_text()
    tree = ast.parse(src)
    assigned = [
        t.id
        for node in tree.body if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    ]
    assert "MAX_SINGLE_COIN_SHARE" not in assigned, "the ceiling must be imported, not re-declared"


def test_the_two_real_offenders_are_refused():
    for coin in ("ZEC-USD", "XRP-USD"):
        allow, reason = gate.concentration_verdict(coin, FLEET, 100.0)
        assert allow is False, coin
        assert "ceiling" in reason


def test_the_real_small_earners_are_untouched():
    for coin in ("NEAR-USD", "JASMY-USD", "QNT-USD", "ONDO-USD"):
        allow, _ = gate.concentration_verdict(coin, FLEET, 60.0)
        assert allow is True, coin


def test_a_buy_that_would_cross_the_line_is_stopped_before_it_does():
    """The ceiling binds on the share AFTER the buy - catching the coin on
    the way through, not one cycle too late."""
    fleet = {"A-USD": 190.0, "B-USD": 810.0}
    assert gate.concentration_verdict("A-USD", fleet, 5.0)[0] is True    # -> 19.4%
    allow, reason = gate.concentration_verdict("A-USD", fleet, 100.0)    # -> 26.4%
    assert allow is False
    assert "through the 20% ceiling" in reason


def test_exactly_at_the_ceiling_is_allowed_not_refused():
    fleet = {"A-USD": 200.0, "B-USD": 800.0}
    allow, _ = gate.concentration_verdict("A-USD", fleet, 0.0)
    assert allow is True, "20.0% is at the ceiling, not over it"


def test_an_unreadable_fleet_fails_CLOSED_and_says_so():
    """REVERSED 2026-10-01. This test used to assert fail-OPEN.

    The reasoning then was "unknown is not a breach". That is true of a
    measurement and false of a ceiling. The book goes unreadable exactly
    when Coinbase rate-limits - 05:21Z that morning: HTTP 429 fetching USD,
    then "[GRID] account book unreadable - concentration not checked this
    cycle" - which is to say the 20% rule switched itself off under load,
    which is when it was most needed. ZEC and XRP reached 26.6% each and
    between them carried 89% of the account's unrealised loss.

    The two errors are not symmetric. A refused buy costs one cycle and the
    grid buys dips, so there is another one. An unmeasured buy is unbounded.
    """
    allow, reason = gate.concentration_verdict("ZEC-USD", None, 100.0)
    assert allow is False
    assert "unreadable" in reason
    assert "REFUSING" in reason


def test_the_429_scenario_end_to_end():
    """The live failure, as a fixture: ZEC already over, book unreadable."""
    readable = {"ZEC": 2272.62, "XRP": 2240.84, "USD": 1276.40, "XLM": 469.69}
    allow, _ = gate.concentration_verdict("ZEC-USD", readable, 500.0)
    assert allow is False, "a readable book already refuses this"
    # Same buy, same instant, only the account read failed.
    allow, _ = gate.concentration_verdict("ZEC-USD", None, 500.0)
    assert allow is False, ("a rate limit must not be a way around the ceiling - "
                            "this is the whole bug")


def test_a_first_buy_is_still_possible_when_nothing_is_held():
    """Failing closed must not make the fleet unable to ever start.

    An EMPTY book is readable and says the account holds nothing; that is a
    different thing from a book that could not be read at all, and only the
    second one refuses.
    """
    allow, _ = gate.concentration_verdict("ZEC-USD", {}, 100.0)
    assert allow is True


def test_an_empty_fleet_is_unknown_not_zero():
    assert gate.coin_share("ZEC-USD", {}, 0.0) is None
    assert gate.coin_share("ZEC-USD", {"A-USD": 0.0}, 0.0) is None


def test_a_first_buy_into_an_empty_fleet_is_allowed():
    """Share-after is 100%, but refusing here would mean the fleet could
    never place its first order. Only a non-empty fleet has a ceiling."""
    allow, _ = gate.concentration_verdict("A-USD", {}, 50.0)
    assert allow is True


def test_the_gate_never_recommends_selling():
    """One-directional by construction: the only outputs are allow/refuse
    on a buy. Nothing in the refusal text may suggest realizing a loss."""
    _, reason = gate.concentration_verdict("ZEC-USD", FLEET, 100.0)
    assert "Nothing is sold" in reason
    for word in ("sell it", "trim", "liquidate", "close the position"):
        assert word not in reason.lower()


def test_a_coin_the_fleet_does_not_hold_is_measured_on_the_buy_alone():
    """Retargeted 2026-09-28 when the book changed, not weakened.

    This asserted 50 / (total + 50) - correct on the old coin-only cost
    basis book, where the money spent landed in both the numerator and
    the denominator. On the account book it does not: buying coin with
    cash from the same account moves dollars from the USD row to the
    coin row and leaves the total exactly where it was. So the
    denominator is the total, unchanged.
    """
    allow, _ = gate.concentration_verdict("NEW-USD", FLEET, 50.0)
    assert allow is True
    share = gate.coin_share("NEW-USD", FLEET, 50.0)
    assert share == pytest.approx(50.0 / sum(FLEET.values()))


def test_garbage_in_is_unknown_not_a_crash():
    assert gate.coin_share("A-USD", {"A-USD": "not-a-number"}, 0.0) is None
    # Garbage still must not CRASH - but it is now refused rather than
    # allowed, for the same reason an unreadable book is.
    allow, reason = gate.concentration_verdict("A-USD", "not-a-map", 10.0)
    assert allow is False
    assert "unreadable" in reason
