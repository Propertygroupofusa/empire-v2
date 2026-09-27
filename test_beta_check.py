"""Tests for the thing that asks whether the coin just went up.

The failure this guards is the most expensive one available: promoting a
backtest that measured the market's return and called it expectancy.
Every failed "growth idea" in this account had that shape, and the tell
was in the data before the money went in.
"""
import ast

import pytest

import beta_check as bc

# The real 21-day window from the live horizon study.
WINDOW = {"BONK-USD": 19.269, "ONDO-USD": 16.52, "BTC-USD": -2.61}

# The real measured rung expectancies, all-in.
RUNG_1_0_AT_6H = {"BONK-USD": -1.0302, "ONDO-USD": -1.2015, "BTC-USD": -1.4965}
RUNG_3_0_AT_72H = {"BONK-USD": 0.8793, "ONDO-USD": 0.5, "BTC-USD": -2.6095}


# ----------------------------------------------------- the real data

def test_the_tight_fast_rung_is_ruled_out_on_the_live_numbers():
    """1.0%@6h lost on BONK (+19.27% window) and ONDO (+16.52%). A
    long-only rung that loses while its coin rises really loses."""
    v = bc.verdict(RUNG_1_0_AT_6H, WINDOW)
    assert v["verdict"] == "ROBUST_NEGATIVE"
    assert v["can_act_on_it"] is True
    assert "no regime excuse" in v["detail"]


def test_the_wide_slow_rung_is_not_ruled_in_on_the_live_numbers():
    """3.0%@72h made money on the two that rose and lost on the one that
    fell. That is the shape of beta, not of an edge."""
    v = bc.verdict(RUNG_3_0_AT_72H, WINDOW)
    assert v["verdict"] == "FAILED_THE_FALL"
    assert "describe the market" in v["detail"]


def test_btcs_zero_fill_rung_returns_exactly_the_window():
    """0% fill and -2.61% expectancy IS the window return - nothing ever
    opened, so the figure marks a leg that never existed."""
    v = bc.verdict({"BONK-USD": 0.8793, "ONDO-USD": 0.5, "BTC-USD": -2.61}, WINDOW)
    btc = next(p for p in v["pairs"] if p["instrument"] == "BTC-USD")
    assert btc["result_pct"] == pytest.approx(btc["window_return_pct"], abs=0.01)


# ------------------------------------------------------ the asymmetry

def test_a_positive_result_with_nothing_falling_is_never_actionable():
    v = bc.verdict({"A": 1.0, "B": 1.2, "C": 0.9},
                   {"A": 19.0, "B": 16.0, "C": 8.0})
    assert v["can_act_on_it"] is False
    assert v["verdict"] in ("UNPROVEN_POSITIVE", "BETA_NOT_EDGE")
    assert "not yet evidence" in v["detail"]


def test_a_result_that_tracks_the_return_one_for_one_is_named_beta():
    v = bc.verdict({"A": 19.0, "B": 16.0, "C": 8.0},
                   {"A": 19.0, "B": 16.0, "C": 8.0})
    assert v["verdict"] == "BETA_NOT_EDGE"
    assert v["correlation"] == pytest.approx(1.0)
    assert "wearing a strategy's name" in v["detail"]


def test_a_negative_result_in_a_rising_market_is_actionable_immediately():
    v = bc.verdict({"A": -0.5, "B": -1.2, "C": -0.1},
                   {"A": 19.0, "B": 16.0, "C": 8.0})
    assert v["verdict"] == "ROBUST_NEGATIVE" and v["can_act_on_it"] is True


def test_winning_while_the_instrument_fell_is_the_sample_worth_waiting_for():
    v = bc.verdict({"A": 1.0, "B": 0.8, "C": 0.6},
                   {"A": 19.0, "B": -4.0, "C": -9.0})
    assert v["verdict"] == "EDGE_SURVIVES_A_FALL"
    assert v["can_act_on_it"] is True


def test_one_loser_among_risers_is_not_a_robust_negative():
    """ROBUST_NEGATIVE needs EVERY riser to have lost. A mixed result is
    not the same claim and must not borrow its confidence."""
    v = bc.verdict({"A": -0.5, "B": 1.2, "C": 0.3},
                   {"A": 19.0, "B": 16.0, "C": 8.0})
    assert v["verdict"] != "ROBUST_NEGATIVE"


# -------------------------------------------------------- refusals

def test_two_instruments_are_not_a_cross_section():
    v = bc.verdict({"A": 1.0, "B": -1.0}, {"A": 19.0, "B": -3.0})
    assert v["verdict"] == "TOO_FEW_INSTRUMENTS" and v["can_act_on_it"] is False


def test_instruments_that_all_did_the_same_thing_decide_nothing():
    v = bc.verdict({"A": 1.0, "B": 1.1, "C": 0.9},
                   {"A": 10.0, "B": 10.5, "C": 11.0})
    assert v["verdict"] == "NO_REGIME_CONTRAST" and v["can_act_on_it"] is False


def test_a_missing_window_return_drops_the_pair_rather_than_assuming_flat():
    """Treating an unknown return as 0% would invent a falling market and
    hand out an EDGE_SURVIVES_A_FALL that nothing measured."""
    v = bc.verdict({"A": 1.0, "B": 1.0, "C": 1.0}, {"A": 19.0, "B": 16.0})
    assert v["verdict"] == "TOO_FEW_INSTRUMENTS"
    assert {p["instrument"] for p in v["pairs"]} == {"A", "B"}


@pytest.mark.parametrize("junk", [None, "x", float("nan"), float("inf")])
def test_unreadable_numbers_are_dropped_not_coerced(junk):
    v = bc.verdict({"A": junk, "B": 1.0, "C": 1.0, "D": 1.0},
                   {"A": 19.0, "B": 16.0, "C": 8.0, "D": -3.0})
    assert "A" not in {p["instrument"] for p in v["pairs"]}


def test_nothing_at_all_refuses_rather_than_crashing():
    for junk in (None, {}, []):
        assert bc.verdict(junk, junk)["can_act_on_it"] is False


# ------------------------------------------------------------ scan

def test_the_scan_separates_what_can_be_ruled_out_from_what_cannot():
    out = bc.scan({"1.0%@6h": RUNG_1_0_AT_6H, "3.0%@72h": RUNG_3_0_AT_72H}, WINDOW)
    assert "1.0%@6h" in out["rule_out"]
    assert out["rule_in"] == []
    assert "None has been shown a falling market" in out["detail"]


def test_a_surviving_config_is_ranked_above_a_ruled_out_one():
    out = bc.scan({"bad": {"A": -1.0, "B": -1.0, "C": -1.0},
                   "good": {"A": 1.0, "B": 0.5, "C": 0.4}},
                  {"A": 19.0, "B": -4.0, "C": -9.0})
    assert out["configs"][0]["config"] == "good"
    assert out["rule_in"] == ["good"]


def test_the_scan_changes_nothing():
    assert bc.scan({"x": RUNG_1_0_AT_6H}, WINDOW)["is_a_measurement_not_a_change"] is True


# -------------------------------------------- structural guards

def test_it_touches_no_venue_and_no_database():
    tree = ast.parse(open("beta_check.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), n


def test_no_verdict_is_actionable_and_positive_without_a_falling_sample():
    """The one invariant that matters: nothing gets ruled IN on a rising
    market alone, however good the number looks."""
    for res in ({"A": 5.0, "B": 4.0, "C": 3.0}, {"A": 0.1, "B": 0.2, "C": 0.3}):
        v = bc.verdict(res, {"A": 19.0, "B": 16.0, "C": 8.0})
        assert not (v["can_act_on_it"] and v["verdict"].startswith("EDGE"))


def test_every_verdict_survives_json():
    import json
    out = bc.scan({"1.0%@6h": RUNG_1_0_AT_6H, "3.0%@72h": RUNG_3_0_AT_72H}, WINDOW)
    assert json.loads(json.dumps(out)) == out
