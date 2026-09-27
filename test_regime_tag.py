"""Tests for the guard between an optimistic measurement and the live gate.

Swapping the measured -0.023% adverse selection into the cost would halve
the bar and turn a great many refusals into trades. That is exactly why
these tests are mostly about refusing.
"""
import ast

import pytest

import regime_tag as rt


def s(adverse, move):
    return {"adverse_pct": adverse, "benchmark_move_pct": move}


RISING_ONLY = [s(-0.02, 3.0) for _ in range(500)]


# ------------------------------------------------------- classify

@pytest.mark.parametrize("move,expect", [
    (5.0, rt.RISING), (1.01, rt.RISING),
    (-5.0, rt.FALLING), (-1.01, rt.FALLING),
    (0.0, rt.FLAT), (1.0, rt.FLAT), (-1.0, rt.FLAT),
    (None, rt.UNKNOWN), ("x", rt.UNKNOWN), (float("nan"), rt.UNKNOWN),
])
def test_regime_boundaries(move, expect):
    assert rt.classify_regime(move) == expect


def test_flat_is_an_admission_not_a_third_opinion():
    assert rt.classify_regime(0.5) == rt.FLAT


# ----------------------------------------------------------- tag

def test_unknown_samples_are_kept_not_dropped():
    by = rt.tag([s(-0.02, None), s(-0.02, 3.0)])
    assert len(by[rt.UNKNOWN]) == 1 and len(by[rt.RISING]) == 1


def test_an_unknown_regime_never_counts_as_falling():
    by = rt.tag([s(-0.02, None) for _ in range(100)])
    assert by[rt.FALLING] == []


def test_samples_without_a_reading_are_skipped():
    by = rt.tag([{"benchmark_move_pct": -5.0}, s(-0.02, -5.0)])
    assert len(by[rt.FALLING]) == 1


# ------------------------------------------------ THE GUARD ITSELF

def test_five_hundred_rising_samples_cannot_replace_the_assumption():
    """The exact situation live: a big, tidy, useless sample."""
    r = rt.summarise(RISING_ONLY)
    assert r["may_replace_assumption"] is False
    assert r["adverse_pct_in_force"] == rt.ASSUMED_ADVERSE_PCT
    assert r["falling_samples_needed"] == rt.MIN_FALLING_SAMPLES


def test_the_reason_names_the_missing_regime():
    r = rt.summarise(RISING_ONLY)
    assert "falling" in r["basis"].lower()


def test_enough_falling_samples_do_replace_it():
    samples = RISING_ONLY + [s(0.4, -4.0) for _ in range(rt.MIN_FALLING_SAMPLES)]
    r = rt.summarise(samples)
    assert r["may_replace_assumption"] is True
    assert r["adverse_pct_in_force"] == pytest.approx(0.4, abs=0.01)


def test_one_short_of_the_threshold_still_refuses():
    samples = RISING_ONLY + [s(0.4, -4.0) for _ in range(rt.MIN_FALLING_SAMPLES - 1)]
    assert rt.summarise(samples)["may_replace_assumption"] is False


def test_the_figure_used_is_the_worst_regime_not_a_blend():
    """A blend is dominated by whichever regime was sampled most."""
    samples = RISING_ONLY + [s(0.9, -4.0) for _ in range(rt.MIN_FALLING_SAMPLES)]
    r = rt.summarise(samples)
    assert r["adverse_pct_in_force"] == pytest.approx(0.9, abs=0.01)
    blend = (500 * -0.02 + rt.MIN_FALLING_SAMPLES * 0.9) / (500 + rt.MIN_FALLING_SAMPLES)
    assert r["adverse_pct_in_force"] != pytest.approx(blend, abs=0.01)


def test_no_samples_at_all_keeps_the_assumption():
    r = rt.summarise([])
    assert r["may_replace_assumption"] is False
    assert r["adverse_pct_in_force"] == rt.ASSUMED_ADVERSE_PCT


def test_falling_samples_are_counted_and_reported():
    samples = [s(0.3, -3.0) for _ in range(7)]
    r = rt.summarise(samples)
    assert r["counts"][rt.FALLING] == 7
    assert r["falling_samples_needed"] == rt.MIN_FALLING_SAMPLES - 7


# --------------------------------------------------------- the cost

def test_a_negative_adverse_figure_cannot_price_below_the_fees():
    """Receipts are the floor. Trading is never cheaper than what it cost."""
    assert rt.round_trip_cost_pct(0.70, -0.50) == 0.70
    assert rt.round_trip_cost_pct(0.70, -5.0) == 0.70


def test_a_positive_adverse_figure_adds_to_the_fees():
    assert rt.round_trip_cost_pct(0.70, 0.67) == pytest.approx(1.37)


def test_the_live_numbers_reproduce():
    assert rt.round_trip_cost_pct(0.70, rt.ASSUMED_ADVERSE_PCT) == pytest.approx(1.37)


def test_missing_adverse_leaves_the_fees_alone():
    assert rt.round_trip_cost_pct(0.70, None) == 0.70


def test_missing_fees_is_unanswerable():
    assert rt.round_trip_cost_pct(None, 0.5) is None


# ------------------------------------------- structural guards

def test_the_threshold_is_not_trivially_small():
    """A guard that a handful of prints can satisfy is not a guard."""
    assert rt.MIN_FALLING_SAMPLES >= 20


def test_it_reaches_nothing():
    tree = ast.parse(open("regime_tag.py").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + [getattr(n, "module", None)]
            for x in names:
                assert not any(b in (x or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy")), x
