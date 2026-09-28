"""A held position must get a stop-loss check even when the bar fetch fails.

THE BUG THIS LOCKS DOWN, found live on 2026-09-28:

    META, 72% of a $983 equity account, went through EVERY cycle with its
    exit rules - stop loss, breakeven ratchet, giveback, max hold -
    never once evaluated. Six consecutive `no_scan_data` refusals in
    three minutes, one per cycle, from the moment the diagnostic that
    reports them shipped.

The cause was a dependency in the wrong direction: the exit pass read
`scans`, which is built from the 15-min-bar/RSI fetch the ENTRY rules
need. When that fetch failed for a symbol the exit pass had nothing, so
it skipped - originally in silence, later loudly, but skipped either way.

None of the four protective rules reads an RSI. They read a PRICE and an
AGE. And the broker already tells us a price for every position it says
we hold, on a request the cycle already makes. So the fix is a second,
independent price source for the protective path.

These tests pin the two halves of that:
  - broker_fallback_scan() hands back a usable price, and refuses rather
    than guessing when it cannot.
  - should_exit_position() survives an RSI of None, and still stops out.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prop_bot  # noqa: E402
from alpaca_mean_reversion import (  # noqa: E402
    should_exit_position,
    should_exit_position_momentum,
)

UTC = timezone.utc


@pytest.fixture(autouse=True)
def clean_cache():
    prop_bot._broker_last_price.clear()
    yield
    prop_bot._broker_last_price.clear()


def _seed(contract, price, age_seconds, now):
    prop_bot._broker_last_price[contract] = (
        price, now - timedelta(seconds=age_seconds))


# ── broker_fallback_scan: gives a price when it has one ────────────────

def test_fresh_broker_mark_becomes_a_usable_scan():
    now = datetime.now(UTC)
    _seed("META", 512.34, 10, now)
    data = prop_bot.broker_fallback_scan("META", now)
    assert data is not None
    assert data["price"] == 512.34


def test_fallback_never_invents_an_rsi():
    """A fabricated RSI would feed a real profit-taking rule a number
    nothing measured. None is the only honest answer."""
    now = datetime.now(UTC)
    _seed("META", 512.34, 10, now)
    data = prop_bot.broker_fallback_scan("META", now)
    assert data["rsi"] is None
    assert data["trend"] == "unknown"


def test_fallback_is_labelled_as_a_broker_mark():
    now = datetime.now(UTC)
    _seed("META", 512.34, 10, now)
    assert prop_bot.broker_fallback_scan("META", now)["source"] == (
        "broker_position_mark")


def test_fallback_reports_how_old_the_price_is():
    now = datetime.now(UTC)
    _seed("META", 512.34, 42, now)
    assert prop_bot.broker_fallback_scan("META", now)["age_seconds"] == \
        pytest.approx(42, abs=1)


# ── broker_fallback_scan: refuses rather than guessing ─────────────────

def test_no_broker_mark_is_a_refusal_not_a_zero():
    now = datetime.now(UTC)
    assert prop_bot.broker_fallback_scan("META", now) is None


def test_a_stale_mark_is_refused():
    """A stalled reconcile must never let an old price decide a real
    exit - a stop computed off a price from ten minutes ago is worse
    than no stop, because it looks like it worked."""
    now = datetime.now(UTC)
    _seed("META", 512.34, prop_bot.BROKER_PRICE_MAX_AGE_SECONDS + 1, now)
    assert prop_bot.broker_fallback_scan("META", now) is None


def test_a_mark_exactly_at_the_age_limit_is_still_accepted():
    now = datetime.now(UTC)
    _seed("META", 512.34, prop_bot.BROKER_PRICE_MAX_AGE_SECONDS, now)
    assert prop_bot.broker_fallback_scan("META", now) is not None


def test_a_mark_from_the_future_is_refused():
    """A negative age means the clocks disagree, not that the price is
    extra fresh."""
    now = datetime.now(UTC)
    _seed("META", 512.34, -30, now)
    assert prop_bot.broker_fallback_scan("META", now) is None


def test_a_naive_timestamp_is_refused_rather_than_raising():
    now = datetime.now(UTC)
    prop_bot._broker_last_price["META"] = (512.34, datetime.now())
    assert prop_bot.broker_fallback_scan("META", now) is None


@pytest.mark.parametrize("bad_price", [0.0, -1.0, None])
def test_a_non_positive_price_is_refused(bad_price):
    """Zero would read as a 100% loss and stop out a healthy position."""
    now = datetime.now(UTC)
    _seed("META", bad_price, 10, now)
    assert prop_bot.broker_fallback_scan("META", now) is None


def test_the_age_limit_is_configurable_for_callers_that_need_tighter():
    now = datetime.now(UTC)
    _seed("META", 512.34, 60, now)
    assert prop_bot.broker_fallback_scan("META", now, max_age_seconds=30) is None
    assert prop_bot.broker_fallback_scan("META", now, max_age_seconds=90) is not None


# ── should_exit_position with no RSI ───────────────────────────────────

def _exit(price, rsi, age=60, entry=100.0, peak=0.0):
    return should_exit_position(
        symbol="META", entry_price=entry, current_price=price,
        current_rsi=rsi, position_age_seconds=age, direction="long",
        max_hold_seconds=7200, stop_loss_pct=0.015,
        min_profit_target_pct=0.02, rsi_profit_threshold_long=60,
        peak_pnl_pct=peak,
    )


def test_a_missing_rsi_does_not_raise():
    """Comparing None to a number raises TypeError, which would kill the
    whole exit evaluation - turning a missing RSI into a missing stop."""
    should_exit, reason, exit_type, _ = _exit(99.5, None)
    assert exit_type == "hold"


def test_the_stop_loss_still_fires_with_no_rsi():
    should_exit, reason, exit_type, _ = _exit(97.0, None)
    assert should_exit is True
    assert exit_type == "stop_loss"


def test_the_max_hold_still_fires_with_no_rsi():
    should_exit, reason, exit_type, _ = _exit(99.9, None, age=99999)
    assert should_exit is True
    assert exit_type == "timeout"


def test_the_profit_target_still_fires_with_no_rsi():
    should_exit, reason, exit_type, _ = _exit(103.0, None)
    assert should_exit is True
    assert exit_type == "profit"


def test_the_rsi_exit_is_skipped_rather_than_guessed_at():
    """The RSI rule is profit-taking, not protection. With no RSI it must
    simply not fire - never fire on an assumed value."""
    should_exit, reason, exit_type, _ = _exit(101.0, None)
    assert exit_type != "rsi_exit"


def test_the_rsi_exit_still_works_when_an_rsi_is_present():
    """The None-handling must not have disabled the rule outright."""
    should_exit, reason, exit_type, _ = _exit(101.0, 75.0)
    assert should_exit is True
    assert exit_type == "rsi_exit"


def test_momentum_exits_never_needed_an_rsi_at_all():
    """The live family's exit function takes no RSI parameter, which is
    why a bar-fetch failure should never have blocked it."""
    import inspect
    params = inspect.signature(should_exit_position_momentum).parameters
    assert not any("rsi" in p for p in params)


def test_momentum_stop_fires_off_a_broker_mark():
    now = datetime.now(UTC)
    _seed("META", 96.0, 5, now)
    data = prop_bot.broker_fallback_scan("META", now)
    should_exit, reason, exit_type, _ = should_exit_position_momentum(
        symbol="META", entry_price=100.0, current_price=data["price"],
        position_age_seconds=60, peak_pnl_pct=0.0,
        max_hold_seconds=7200, trail_pct=0.02,
    )
    assert should_exit is True
