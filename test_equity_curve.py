"""Placing equity in its own history - and refusing to overreach."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import equity_curve as ec  # noqa: E402

DAY = 86400
BASE = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())


def H(values, start=BASE, step=DAY):
    return {"timestamp": [start + i * step for i in range(len(values))],
            "equity": list(values)}


# ── the verdict is three-valued ────────────────────────────────────────

def test_a_true_low_is_called_true():
    out = ec.assess_multi_month_low(H([1200 - i for i in range(90)]))
    assert out["verdict"] == "TRUE"
    assert out["days_below_current"] == 0


def test_a_false_low_is_called_false_with_the_count():
    vals = [1000] * 89 + [980]
    vals[10] = 800
    vals[40] = 900
    out = ec.assess_multi_month_low(H(vals))
    assert out["verdict"] == "FALSE"
    assert out["days_below_current"] == 2
    assert "NOT a" in out["verdict_detail"]


def test_a_short_window_is_unknown_not_a_verdict():
    """Three weeks of data cannot speak to 'months'. Answering anyway is
    how a guess becomes a fact."""
    out = ec.assess_multi_month_low(H([1000 - i for i in range(21)]))
    assert out["verdict"] == "UNKNOWN"
    assert "needs at least" in out["verdict_detail"]


def test_unknown_says_it_is_not_evidence_against_the_claim():
    out = ec.assess_multi_month_low(H([1000 - i for i in range(21)]))
    assert "not evidence that the claim is wrong" in out["verdict_detail"]


def test_an_unreadable_history_is_unknown():
    out = ec.assess_multi_month_low({"timestamp": [], "equity": []})
    assert out["verdict"] == "UNKNOWN"
    assert out["readable"] is False


def test_the_months_threshold_is_configurable_and_enforced():
    hist = H([1000 - i for i in range(40)])
    assert ec.assess_multi_month_low(hist, min_days=60)["verdict"] == "UNKNOWN"
    assert ec.assess_multi_month_low(hist, min_days=30)["verdict"] == "TRUE"


# ── gaps stay gaps ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, 0, 0.0, -5, "abc"])
def test_an_unusable_point_is_dropped_not_read_as_zero(bad):
    """A single zero would sit below every other day and make today look
    like a high-water mark."""
    vals = [1000, 1010, bad, 1020, 990]
    out = ec.summarise(H(vals))
    assert out["window"]["points"] == 4
    assert out["window"]["dropped_points"] == 1
    assert out["low"]["equity"] == 990.0


def test_dropped_points_are_flagged_to_the_reader():
    out = ec.summarise(H([1000, None, 1010]))
    assert "thinner than it looks" in out["caveat_gaps"]


def test_a_history_of_only_gaps_is_unreadable():
    out = ec.summarise(H([None, 0, -1]))
    assert out["readable"] is False
    assert out["dropped_points"] == 3


def test_mismatched_array_lengths_do_not_crash():
    out = ec.summarise({"timestamp": [BASE, BASE + DAY, BASE + 2 * DAY],
                        "equity": [1000, 1010]})
    assert out["window"]["points"] == 2
    assert out["window"]["dropped_points"] == 1


def test_none_history_is_unreadable_not_an_exception():
    assert ec.summarise(None)["readable"] is False


# ── the window is always stated ────────────────────────────────────────

def test_the_span_is_reported_with_every_answer():
    out = ec.summarise(H([1000] * 30))
    assert out["window"]["span_days"] == 29.0
    assert out["window"]["first"] == "2026-07-01"


def test_the_caveat_names_the_window_bounds():
    out = ec.summarise(H([1000] * 30))
    assert "says nothing about equity before that" in out["caveat"]
    assert out["window"]["first"] in out["caveat"]


def test_points_are_sorted_even_if_the_feed_is_not():
    hist = {"timestamp": [BASE + 2 * DAY, BASE, BASE + DAY],
            "equity": [1020, 1000, 1010]}
    out = ec.summarise(hist)
    assert out["window"]["first"] == "2026-07-01"
    assert out["window"]["last"] == "2026-07-03"


# ── the live account beats the last daily bar ──────────────────────────

def test_the_live_equity_overrides_the_last_bar():
    """A bar from this morning is a stale denominator for 'where are we
    now'."""
    out = ec.summarise(H([1000, 1010, 1020]), current_equity=900)
    assert out["current_equity"] == 900.0
    assert out["current_is_live_account"] is True
    assert out["days_below_current"] == 0


def test_without_a_live_equity_the_last_bar_is_used_and_labelled():
    out = ec.summarise(H([1000, 1010, 1020]))
    assert out["current_equity"] == 1020.0
    assert out["current_is_live_account"] is False


def test_a_live_equity_changes_the_verdict():
    vals = [1000] * 90
    assert ec.assess_multi_month_low(H(vals), current_equity=900)["verdict"] == "TRUE"
    assert ec.assess_multi_month_low(H(vals), current_equity=1100)["verdict"] == "FALSE"


# ── the supporting numbers ─────────────────────────────────────────────

def test_the_low_and_high_carry_their_dates():
    vals = [1000] * 90
    vals[5] = 700
    vals[50] = 1500
    out = ec.summarise(H(vals))
    assert out["low"] == {"equity": 700.0, "on": "2026-07-06"}
    assert out["high"] == {"equity": 1500.0, "on": "2026-08-20"}


def test_the_percentile_places_today_in_the_distribution():
    vals = list(range(900, 1000))          # 100 points, 900..999
    out = ec.summarise(H(vals), current_equity=950)
    assert out["percentile_in_window"] == pytest.approx(50.0, abs=1.0)


def test_days_since_a_lower_reading_finds_the_most_recent_one():
    vals = [1000] * 90
    vals[-10] = 800
    out = ec.summarise(H(vals), current_equity=900)
    assert out["days_since_a_lower_reading"] == pytest.approx(9.0)


def test_days_since_a_lower_reading_is_none_at_a_true_low():
    out = ec.summarise(H([1000] * 90), current_equity=500)
    assert out["days_since_a_lower_reading"] is None


def test_the_two_day_counts_add_up_to_the_window():
    vals = [1000 + (i % 7) for i in range(90)]
    out = ec.summarise(H(vals), current_equity=1003)
    assert (out["days_below_current"] + out["days_at_or_above_current"]
            == out["window"]["points"])


# ── the endpoint ───────────────────────────────────────────────────────

import ast as _ast  # noqa: E402

_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "routers", "trading_dashboard.py")
with open(_ROUTER, encoding="utf-8") as _fh:
    _SRC = _fh.read()
_TREE = _ast.parse(_SRC)


def _endpoint():
    for n in _ast.walk(_TREE):
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and n.name == "get_equity_curve":
            return n
    raise AssertionError("get_equity_curve not found")


def test_the_route_exists():
    assert '@router.get("/alpaca-overview/equity-curve")' in _SRC


def test_the_endpoint_places_nothing():
    src = _ast.get_source_segment(_SRC, _endpoint())
    for forbidden in ("session.post", "session.delete", "place_order",
                      "close_position", "/v2/orders"):
        assert forbidden not in src, f"a read-only endpoint must not use {forbidden}"


def test_the_endpoint_passes_the_live_equity_not_the_last_bar():
    src = _ast.get_source_segment(_SRC, _endpoint())
    assert "_fetch_alpaca_account(" in src
    assert "assess_multi_month_low(history, live_equity)" in src


def test_the_endpoint_does_not_reimplement_the_verdict():
    src = _ast.get_source_segment(_SRC, _endpoint())
    assert "equity_curve.assess_multi_month_low(" in src
    assert "days_below" not in src, "the endpoint is recomputing the verdict"


def test_an_unreadable_equity_becomes_none_not_zero():
    """float(None) raises; a bare except that set 0.0 would make the
    account look like an all-time low."""
    src = _ast.get_source_segment(_SRC, _endpoint())
    assert "live_equity = None" in src
    assert "live_equity = 0" not in src
