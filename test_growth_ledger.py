"""Tests for the series that answers "is it actually moving".

The failure that costs the most here is not a wrong delta. It is a
missing reading drawn as a zero - an unread census rendered as $0.00
becomes a crash on the chart that never happened, and someone sells
into it.
"""
import ast
from datetime import datetime, timedelta

import pytest

import growth_ledger as gl

T0 = datetime(2026, 9, 27, 0, 0, 0)


def snap(minutes, **over):
    r = {"captured_at": T0 + timedelta(minutes=minutes),
         "trades": 83, "net_usd": 19.61, "net_edge_per_trade_usd": 0.2363,
         "profit_factor": 3.697, "win_rate_pct": 75.9,
         "allocated_usd": 143.11, "claimed_usd": 553.89, "free_cash_usd": 892.30,
         "account_total_usd": 11397.11, "outside_any_branch_pct": 90.92,
         "idle_capital_pct": 86.18, "capital_velocity": 10.495,
         "branch_count": 6, "open_slices": 6, "bottleneck": "CAPITAL_OUTSIDE"}
    r.update(over)
    return r


# ---------------------------------------------------------- refusals

def test_no_readings_says_so_instead_of_drawing_a_flat_line():
    s = gl.summarise([])
    assert s["available"] is False and s["points"] == 0


def test_one_reading_is_a_point_not_a_curve():
    s = gl.summarise([snap(0)])
    assert s["points"] == 1
    assert s["earned"]["change"] is None
    assert "single point" in s["headline"]


def test_two_readings_a_minute_apart_are_noise_not_a_move():
    d = gl.delta([snap(0), snap(1, net_usd=19.62)], "net_usd")
    assert d["change"] is None
    assert "noise and rounding" in d["why"]


def test_a_missing_reading_is_never_treated_as_a_zero():
    """THE expensive one. An unread census stored as NULL must not become
    a $11,397 fall on the chart."""
    rows = [snap(0), snap(30, account_total_usd=None), snap(60)]
    d = gl.delta(rows, "account_total_usd")
    assert d["change"] == 0.0            # compared the two it could read
    assert d["from"] == 11397.11 and d["to"] == 11397.11


def test_a_field_no_reading_carries_is_withheld_not_zeroed():
    rows = [snap(0, capital_velocity=None), snap(60, capital_velocity=None)]
    d = gl.delta(rows, "capital_velocity")
    assert d["change"] is None and "needs two" in d["why"]


def test_rows_without_a_timestamp_are_dropped_not_guessed_at():
    rows = [snap(0), {"net_usd": 50.0}, {"captured_at": "garbage", "net_usd": 99.0}]
    assert len(gl.rows(rows)) == 1


def test_readings_are_ordered_oldest_first_whatever_order_they_arrive_in():
    rs = gl.rows([snap(60), snap(0), snap(30)])
    assert [r["captured_at"] for r in rs] == [T0, T0 + timedelta(minutes=30),
                                              T0 + timedelta(minutes=60)]


# -------------------------------------------------------- the deltas

def test_a_real_move_is_measured_between_two_real_readings():
    d = gl.delta([snap(0), snap(120, net_usd=25.61)], "net_usd")
    assert d["change"] == pytest.approx(6.0)
    assert d["window_minutes"] == pytest.approx(120.0)
    assert d["per_hour"] == pytest.approx(3.0)


def test_a_delta_is_anchored_on_a_reading_not_a_wall_clock_time():
    """The window may contain no reading near its start. Anchoring on the
    oldest reading INSIDE it keeps both ends things that were measured."""
    rows = [snap(0, net_usd=10.0), snap(60 * 40, net_usd=20.0),
            snap(60 * 41, net_usd=21.0)]
    d = gl.delta(rows, "net_usd", hours=24.0)
    assert d["from"] == 20.0 and d["to"] == 21.0


def test_a_window_with_only_one_reading_falls_back_to_the_whole_series():
    rows = [snap(0, net_usd=10.0), snap(60 * 40, net_usd=20.0)]
    d = gl.delta(rows, "net_usd", hours=1.0)
    assert d["from"] == 10.0 and d["to"] == 20.0


def test_nothing_moving_is_reported_as_nothing_moving():
    s = gl.summarise([snap(0), snap(60), snap(120)])
    assert s["earned"]["change"] == 0.0
    assert s["is_moving"] is False
    assert "nothing moved" in s["headline"]


# ------------------------------------- earned and placed stay apart

def test_earning_and_placing_are_never_added_together():
    """A balance rose the day the trimmer sold $882.68. Liquidation
    rendered as profit is exactly what keeping these apart prevents."""
    rows = [snap(0, net_usd=19.61, allocated_usd=143.11),
            snap(120, net_usd=19.61, allocated_usd=900.00)]
    s = gl.summarise(rows)
    assert s["earned"]["change"] == 0.0
    assert s["placed"]["change"] == pytest.approx(756.89)
    assert "earned +0.00" in s["headline"]


def test_the_summary_has_no_combined_total_field():
    s = gl.summarise([snap(0), snap(120, net_usd=30.0, allocated_usd=900.0)])
    for key in s:
        assert "total_growth" not in key and "combined" not in key


# ------------------------------------------------- integrity guards

def test_realised_profit_that_falls_is_flagged_not_charted_as_a_loss():
    """P&L only accumulates. A drop is a rewritten ledger or a bad read."""
    rows = [snap(0, net_usd=19.61), snap(60, net_usd=4.00), snap(120, net_usd=20.00)]
    i = gl.integrity(rows)
    assert i["trustworthy"] is False
    assert i["monotonic_breaks"][0]["field"] == "net_usd"
    assert i["monotonic_breaks"][0]["fell_from"] == 19.61


def test_a_falling_trade_count_is_flagged_too():
    rows = [snap(0, trades=83), snap(60, trades=41)]
    assert gl.integrity(rows)["trustworthy"] is False


def test_a_clean_series_is_trustworthy():
    rows = [snap(0, net_usd=19.61), snap(60, net_usd=20.0), snap(120, net_usd=21.5)]
    assert gl.integrity(rows)["trustworthy"] is True


def test_nulls_are_counted_so_a_thin_chart_can_be_explained():
    rows = [snap(0, account_total_usd=None, capital_velocity=None), snap(60)]
    assert gl.integrity(rows)["null_fields"] == 2


# -------------------------------------------- the cause over time

def test_a_bottleneck_that_never_changes_is_reported_as_unchanged():
    s = gl.summarise([snap(0), snap(60), snap(120)])
    assert s["bottleneck_now"] == "CAPITAL_OUTSIDE"
    assert s["bottleneck_changed"] is False


def test_a_bottleneck_that_changed_is_the_headline_of_the_series():
    rows = [snap(0, bottleneck="CAPITAL_OUTSIDE"), snap(60, bottleneck="HEALTHY")]
    s = gl.summarise(rows)
    assert s["bottleneck_changed"] is True
    assert s["bottleneck_first"] == "CAPITAL_OUTSIDE"
    assert s["bottleneck_now"] == "HEALTHY"


# ------------------------------------------------ the writing shape

def test_a_reading_is_built_from_the_kpi_payload_without_renaming_anything():
    import capital_kpis as ck
    k = ck.compute([], allocated_usd=143.11, free_cash_usd=892.30,
                   account_total_usd=11397.11)
    row = gl.from_kpis(k, claimed_usd=553.89, branch_count=6, open_slices=6,
                       bottleneck="CAPITAL_OUTSIDE")
    assert row["allocated_usd"] == 143.11
    assert row["idle_capital_pct"] is not None
    assert row["claimed_usd"] == 553.89
    assert row["bottleneck"] == "CAPITAL_OUTSIDE"


def test_every_field_this_module_writes_is_a_key_the_kpis_produce():
    """A renamed KPI key would otherwise write a column of NULLs for a
    month before anyone noticed the chart had gone flat."""
    import capital_kpis as ck
    produced = set(ck.compute([], allocated_usd=100.0))
    # claimed_usd, branch_count and open_slices come from grid status,
    # not the KPI payload, and are passed in explicitly.
    from_kpis = set(gl.FIELDS) - {"claimed_usd", "branch_count", "open_slices"}
    assert from_kpis <= produced, from_kpis - produced


def test_every_field_the_series_writes_exists_on_the_snapshot_table():
    import models
    cols = {c.name for c in models.CapitalKpiSnapshot.__table__.columns}
    assert set(gl.FIELDS) <= cols, set(gl.FIELDS) - cols
    assert {"captured_at", "bottleneck", "note"} <= cols


# ------------------------------------------- structural guards

def test_the_ledger_touches_no_venue_and_no_database():
    tree = ast.parse(open("growth_ledger.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), n


def test_nothing_here_annualises_a_day_of_readings():
    tree = ast.parse(open("growth_ledger.py").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            assert node.value not in (365, 365.0, 252, 8760), node.value


def test_the_series_survives_json():
    import json
    s = gl.summarise([snap(0), snap(60), snap(120, net_usd=21.0)])
    assert json.loads(json.dumps(s)) == s
