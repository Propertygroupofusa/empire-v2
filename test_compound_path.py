"""Tests for the "how long" answer.

The failure that matters is a confident date. The rate this compounds
was measured over 27 days on capital that changed 13x in one morning, so
a single number would be a guess wearing a decimal point - and a plan
built on it would be months old before anyone noticed.
"""
import ast

import pytest

import compound_path as cp

LIVE = dict(account_usd=11554.0, deployed_usd=1933.69, target_usd_per_hour=20.0,
            earned_usd=25.25, days_measured=27.59,
            deployed_low_usd=150.0, deployed_high_usd=1933.69, trades=86)


# --------------------------------------- the two phases are separate

def test_placing_and_compounding_are_never_reported_as_one_number():
    """One is days of work, the other is a year or more. Reporting them
    together is how a plan gets made that feels fast and is not."""
    p = cp.plan(**LIVE)
    assert "phase_1_place" in p and "phase_2_compound" in p
    assert "action, not a wait" in p["phase_1_place"]["time"]


def test_phase_one_is_sized_from_capital_that_already_exists():
    p = cp.plan(**LIVE)
    assert p["undeployed_usd"] == pytest.approx(11554.0 - 1933.69, abs=0.01)
    assert p["phase_1_place"]["to_usd"] == 11554.0


def test_phase_one_earns_nothing_extra_per_dollar_and_says_so():
    """Placing money does not raise the rate. It stops most of the dollars
    being left out, which is a different claim."""
    p = cp.plan(**LIVE)
    assert "earns nothing EXTRA per dollar" in p["phase_1_place"]["detail"]


def test_phase_two_cannot_be_hurried_except_with_money():
    p = cp.plan(**LIVE)
    assert "hurried except by adding money" in p["phase_2_compound"]["detail"]


# ------------------------------------ the answer is a range, not a date

def test_both_ends_of_the_rate_are_reported():
    """Dividing by today's deployed understates the rate several-fold;
    dividing by the old figure overstates what a bigger fleet will do."""
    p = cp.plan(**LIVE)
    assert p["rate_optimistic_pct_per_day"] > p["rate_pessimistic_pct_per_day"]
    assert p["phase_2_compound"]["days_optimistic"] < p["phase_2_compound"]["days_pessimistic"]


def test_the_short_answer_quotes_a_span_never_a_single_date():
    p = cp.plan(**LIVE)
    s = p["the_short_answer"]
    assert " to " in s and "days of compounding" in s


def test_it_is_labelled_a_projection_not_a_promise():
    assert cp.plan(**LIVE)["is_a_projection_not_a_promise"] is True


# ------------------------------------------------ the honest warning

def test_compounding_past_the_measured_window_carries_a_warning():
    """A 0.61%/day return sustained for a year is a 7.5x account, and
    rates like that almost never survive being scaled."""
    p = cp.plan(**LIVE)
    w = p["honest_warning"]
    assert "never survive being scaled" in w
    assert "ceiling that has never been tested" in w


def test_a_short_horizon_gets_no_scare_warning():
    p = cp.plan(**{**LIVE, "target_usd_per_hour": 0.05})
    assert "inside the window" in p["honest_warning"]


# ------------------------------------------------------- refusals

def test_a_thin_sample_refuses_to_produce_a_timeline():
    p = cp.plan(**{**LIVE, "trades": 5})
    assert p["available"] is False
    assert "month of luck projected forward is not a plan" in p["reason"]


def test_a_target_already_reached_takes_no_time():
    assert cp.days_to_grow(100.0, 50.0, 0.01) == 0.0


@pytest.mark.parametrize("bad", [0, None, -1, "x"])
def test_a_zero_or_missing_rate_never_divides(bad):
    assert cp.days_to_grow(100.0, 200.0, bad if isinstance(bad, float) else None) is None
    assert cp.daily_rate(10.0, 5.0, bad if isinstance(bad, (int, float)) else None) is None


def test_arithmetic_is_right_on_a_case_that_can_be_checked_by_hand():
    #  doubling at 1% a day is ln(2)/ln(1.01) = 69.66 days
    assert cp.days_to_grow(100.0, 200.0, 0.01) == pytest.approx(69.66, abs=0.05)


def test_it_touches_no_venue_and_no_database():
    tree = ast.parse(open("compound_path.py").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [x.name for x in n.names] + [getattr(n, "module", None)]
            for nm in names:
                assert not any(b in (nm or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), nm


def test_everything_survives_json():
    import json
    p = cp.plan(**LIVE)
    assert json.loads(json.dumps(p)) == p
