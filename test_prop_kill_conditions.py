"""The prop bot's kill switch, including the sign bug it was carrying.

THIS SUITE WAS ADAPTED, NOT COPIED. The version it came from was written
against a shape this codebase does not have, and would have passed for
the wrong reason:

  * it called check_kill_conditions(daily_pnl=..., buying_power=...). The
    real signature is (buying_power, equity, daily_loss,
    open_position_count) - four arguments, and `daily_pnl` is not one of
    them. Every call would have raised TypeError.
  * it asserted `result is False`. The real function returns a TUPLE,
    (should_halt, reason) - and a non-empty tuple is truthy, so an
    `is False` assertion can never hold whatever the code does.
  * it patched 'bot_mandates.APEX_MANDATE'. prop_bot does
    `from bot_mandates import APEX_MANDATE`, so the name it reads lives in
    ITS OWN module namespace. Patching the source module rebinds a name
    nothing looks at, and the test would have run against the real live
    config - max_daily_loss=5 - while believing it was using a 5000 mock.
    With daily_loss=-6000 that still returns True, so the test would have
    PASSED, proving nothing.
  * it mocked 'prop_bot.halt_all_trading', which does not exist. The
    function halts nothing: it returns a verdict and its caller decides.
    That is the better design and it needs no mock at all.

The one thing the original was RIGHT about is the bug it was reaching
for, and that bug was real and unfixed.
"""
from unittest.mock import patch

import pytest

import prop_bot


def mandate(max_daily_loss=5000.0, critical_buying_power=1000.0, max_open_positions=6):
    return {"capital": {"max_daily_loss": max_daily_loss,
                        "critical_buying_power": critical_buying_power,
                        "max_open_positions": max_open_positions}}


def check(**kw):
    args = {"buying_power": 5000.0, "equity": 5000.0,
            "daily_loss": 0.0, "open_position_count": 0}
    args.update(kw)
    return prop_bot.check_kill_conditions(**args)


# ── the patch target the original got wrong ──────────────────────────────

def test_patching_the_source_module_does_not_reach_the_function():
    """Pinned deliberately. prop_bot imported the NAME, so rebinding it in
    bot_mandates changes nothing the function reads - and a suite built on
    that target silently tests the live config."""
    import bot_mandates
    with patch.object(bot_mandates, "APEX_MANDATE", mandate(max_daily_loss=999999.0)):
        halt, _ = check(daily_loss=-100000.0)
    assert halt is True, "the live config was still in force, not the mock"


# ── safe conditions ──────────────────────────────────────────────────────

def test_safe_conditions_do_not_halt():
    with patch.object(prop_bot, "APEX_MANDATE", mandate()):
        halt, reason = check(daily_loss=-2000.0, buying_power=5000.0)
    assert halt is False
    assert reason is None


# ── the daily loss limit, both sign conventions ──────────────────────────

def test_a_loss_past_the_limit_halts_with_a_positive_config():
    with patch.object(prop_bot, "APEX_MANDATE", mandate(max_daily_loss=5000.0)):
        halt, reason = check(daily_loss=-6000.0)
    assert halt is True
    assert "Daily loss limit hit" in reason


def test_a_profit_never_halts_when_the_limit_is_written_negative():
    """THE BUG. `daily_loss < -capital["max_daily_loss"]` with the limit
    stored as -5000 becomes `daily_loss < 5000`, which is true for a
    PROFIT. The bot would halt a winning day and log "Daily loss limit
    hit: $1000.00" while the account was up."""
    with patch.object(prop_bot, "APEX_MANDATE", mandate(max_daily_loss=-5000.0)):
        halt, reason = check(daily_loss=1000.0)
    assert halt is False, f"halted on a $1,000 PROFIT: {reason}"


def test_a_real_loss_still_halts_when_the_limit_is_written_negative():
    """The fix must not disarm the limit in the process of fixing it."""
    with patch.object(prop_bot, "APEX_MANDATE", mandate(max_daily_loss=-5000.0)):
        halt, reason = check(daily_loss=-6000.0)
    assert halt is True
    assert "Daily loss limit hit" in reason


def test_exactly_at_the_limit_does_not_halt():
    for limit in (5000.0, -5000.0):
        with patch.object(prop_bot, "APEX_MANDATE", mandate(max_daily_loss=limit)):
            halt, _ = check(daily_loss=-5000.0)
        assert halt is False, f"limit written as {limit} halted exactly at the line"


def test_the_reason_quotes_the_magnitude_not_the_raw_config():
    """A halt reading "<= -$-5000" sends the reader to the wrong place."""
    with patch.object(prop_bot, "APEX_MANDATE", mandate(max_daily_loss=-5000.0)):
        _, reason = check(daily_loss=-6000.0)
    assert "-$-" not in reason
    assert "5000" in reason


# ── buying power, which fails the mirror image ───────────────────────────

def test_buying_power_below_the_floor_halts():
    with patch.object(prop_bot, "APEX_MANDATE", mandate(critical_buying_power=1000.0)):
        halt, reason = check(buying_power=500.0)
    assert halt is True
    assert "Buying power critical" in reason


def test_a_negative_floor_does_not_silently_stop_guarding():
    """The mirror of the loss bug: `buying_power < -150` can never fire,
    so the guard is present and does nothing. That is the worse of the
    two failures - one halts wrongly, this one never halts at all."""
    with patch.object(prop_bot, "APEX_MANDATE", mandate(critical_buying_power=-1000.0)):
        halt, reason = check(buying_power=500.0)
    assert halt is True, "the buying-power guard stopped guarding"
    assert "Buying power critical" in reason


# ── the other two conditions, so the fix did not disturb them ────────────

def test_equity_below_the_survival_level_halts():
    with patch.object(prop_bot, "APEX_MANDATE", mandate()):
        halt, reason = check(equity=700.0)
    assert halt is True
    assert "survival level" in reason


def test_too_many_open_positions_halts():
    with patch.object(prop_bot, "APEX_MANDATE", mandate(max_open_positions=4)):
        halt, reason = check(open_position_count=5)
    assert halt is True
    assert "Too many open positions" in reason


def test_the_first_breached_condition_is_the_one_reported():
    """Order matters for the operator: the loss limit is checked first and
    is the more actionable of the two."""
    with patch.object(prop_bot, "APEX_MANDATE", mandate()):
        _, reason = check(daily_loss=-6000.0, buying_power=1.0)
    assert "Daily loss limit hit" in reason


# ── the live configuration, as actually deployed ─────────────────────────

def test_the_real_shipped_config_behaves_sanely():
    import bot_mandates
    cap = bot_mandates.APEX_MANDATE["capital"]
    assert abs(cap["max_daily_loss"]) > 0
    assert abs(cap["critical_buying_power"]) > 0
    halt, _ = check(daily_loss=abs(cap["max_daily_loss"]) * 2,
                    buying_power=abs(cap["critical_buying_power"]) * 2,
                    equity=5000.0)
    assert halt is False, "a profitable day on the live config must not halt"


def test_the_function_halts_nothing_itself():
    """It returns a verdict; the caller decides. Pinned because the suite
    this came from mocked a halt_all_trading that does not exist, and
    adding one would move the decision into the check."""
    assert not hasattr(prop_bot, "halt_all_trading")
