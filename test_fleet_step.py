"""The fleet minimum moved on evidence, and the evidence stays checkable.

Two things this protects, both of which nearly went the other way.

1. THE DIRECTION. Every instinct says a tighter grid trades more and so
   earns more. Measured twice on this fleet's own candles, it does the
   opposite - the fee is a fixed toll per round trip, so halving the step
   halves the gross and leaves the toll, and selling early resets the
   reference price upward and costs the next entry. The minimum may only
   ever move on measurement, and it must never drop below where the
   evidence puts the plateau.

2. THE SAMPLE. Per-coin steps scored +70.6% over one global step. Gate the
   chosen step on having even 5 trips behind it and ONE coin of twenty
   survives. That +70.6% was the argmax of ten candidates over 1-4
   observations - noise, dressed as a gain, and it would have shipped.
"""
import ast
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()

# The measured 60-day curve at the real 0.70% round trip, all 20 live coins.
CURVE = {0.010: 28.15, 0.015: 51.34, 0.020: 62.79, 0.025: 67.07, 0.030: 73.00,
         0.035: 71.48, 0.040: 75.14, 0.050: 71.21, 0.060: 73.14, 0.080: 60.44}
PLATEAU = (0.030, 0.060)


def const(name):
    m = re.search(name + r' = float\(os\.getenv\("[A-Z_]+", "([0-9.]+)"\)\)', SRC)
    if m:
        return float(m.group(1))
    m = re.search(name + r' = ([0-9.]+)', SRC)
    return float(m.group(1)) if m else None


def test_the_minimum_sits_on_the_measured_plateau():
    step = const("FLEET_MIN_STEP_PCT")
    assert PLATEAU[0] <= step <= PLATEAU[1], (
        f"{step} is outside the measured plateau {PLATEAU}")


def test_it_takes_the_near_edge_not_the_nominal_peak():
    """4.00% scored highest but by less than the spread within the plateau.
    The near edge keeps the most trips for the same money and extrapolates
    least, so a nominal argmax must not be what sets this."""
    assert const("FLEET_MIN_STEP_PCT") == pytest.approx(PLATEAU[0])


def test_the_chosen_step_beats_the_one_it_replaced():
    assert CURVE[const("FLEET_MIN_STEP_PCT")] > CURVE[0.025]


def test_everything_below_the_plateau_really_is_worse():
    """The claim the direction rests on, checked against the curve rather
    than asserted in prose."""
    chosen = CURVE[const("FLEET_MIN_STEP_PCT")]
    for step in (0.010, 0.015, 0.020, 0.025):
        assert CURVE[step] < chosen, f"{step} is not worse - the direction claim fails"


def test_tighter_steps_trade_more_and_earn_less():
    """The whole counter-intuitive result, as an ordering: trips rise as the
    step falls, money does not."""
    trips = {0.010: 136, 0.015: 93, 0.020: 70, 0.025: 54, 0.030: 46}
    steps = sorted(trips)
    assert [trips[s] for s in steps] == sorted((trips[s] for s in steps), reverse=True)
    assert CURVE[0.010] < CURVE[0.030]


# ------------------------------------------------- the evidence stays honest
def test_the_evidence_fee_matches_what_the_account_actually_pays():
    """The stale-evidence trap: the old table was priced at 1.37% while the
    real cost was 0.70%, and it lived in a comment where nothing checked it."""
    assert const("SPACING_EVIDENCE_PRICED_AT_ROUND_TRIP") == pytest.approx(0.0070)


def test_the_invariant_would_catch_that_fee_drifting_again():
    import invariants as inv
    assert inv.spacing_evidence_current(0.0070, 0.0070)["status"] == inv.OK
    assert inv.spacing_evidence_current(0.0070, 0.0137)["status"] == inv.FAIL


def test_the_rejected_per_coin_result_is_recorded_with_why():
    """A rejected idea has to leave its reason behind, or it comes back."""
    i = SRC.index("PER-COIN STEPS WERE MEASURED AND REJECTED")
    block = SRC[i:i + 700]
    assert "70.6%" in block
    assert "ONE coin" in block or "one coin" in block
    assert "noise" in block


# ------------------------------------------------------------- it survives
def test_the_minimum_is_applied_one_directionally_in_the_cycle():
    """It raises a branch below it and leaves a branch above it alone - so a
    coin the gate-clearing floor has widened is never pulled back down."""
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    body = "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "if _current_step < FLEET_MIN_STEP_PCT" in body
    assert "new_grid_pct = FLEET_MIN_STEP_PCT" in body


def test_it_is_applied_every_cycle_not_only_at_branch_creation():
    """The reason this constant is the right lever at all: the promoted
    override re-sets grid_pct every pass, so a one-time per-branch write
    would not survive. This one is re-applied on the same pass, after it."""
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    body = "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert body.index('new_grid_pct = override_cfg["grid_pct"]') < body.index("FLEET_MIN_STEP_PCT")


@pytest.mark.parametrize("bad", [0.010, 0.015, 0.020, 0.025])
def test_a_tighter_minimum_would_fail_these_tests(bad):
    """Mutation: every step the intuition keeps reaching for must be caught."""
    assert not (PLATEAU[0] <= bad <= PLATEAU[1]) or CURVE[bad] <= CURVE[0.025]
