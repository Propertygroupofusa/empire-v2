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


def test_an_unreadable_fleet_fails_open_and_says_so():
    allow, reason = gate.concentration_verdict("ZEC-USD", None, 100.0)
    assert allow is True
    assert "unreadable" in reason


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
    assert gate.concentration_verdict("A-USD", "not-a-map", 10.0)[0] is True
