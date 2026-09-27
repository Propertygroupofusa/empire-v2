"""Tests for the floor that decides how tight a grid may be set.

The existing fee floor certifies 0.90% as safe. Measured out of sample a
0.90% step lost money at 5-minute, 15-minute AND hourly granularity. The
fee calculation is not wrong - it is answering a narrower question than
the one that matters, because it cannot see the rungs that fill and never
close.
"""
import ast
import math

import pytest

import spacing_floor as sf


def bars(closes):
    return [{"close": c, "low": c, "high": c} for c in closes]


def oscillating(n=400, amp=6.0, base=100.0):
    return [base * (1 + amp / 100 * math.sin(i / 3.0)) for i in range(n)]


def falling(n=400, base=100.0, per=-0.3):
    return [base * (1 + per / 100) ** i for i in range(n)]


def test_a_falling_coin_has_no_survivable_floor():
    r = sf.measured_floor(bars(falling()))
    assert r["ok"] and r["floor_pct"] is None
    assert "no step" in r["detail"]


def test_an_oscillating_coin_gets_a_floor():
    r = sf.measured_floor(bars(oscillating()))
    assert r["ok"] and r["floor_pct"] is not None


def test_the_floor_is_the_TIGHTEST_surviving_step():
    r = sf.measured_floor(bars(oscillating()))
    qualifying = [t["step_pct"] for t in r["tried"] if t.get("qualifies")]
    assert r["floor_pct"] == min(qualifying)


def test_a_step_qualifying_by_a_hair_is_refused():
    """The margin exists so a floor is not one bad week from being wrong."""
    r = sf.measured_floor(bars(oscillating()), margin=99.0)
    assert r["floor_pct"] is None


def test_too_few_completions_cannot_set_a_floor():
    r = sf.measured_floor(bars(oscillating()), min_completions=10_000)
    assert r["floor_pct"] is None
    assert any("completed round trips" in (t.get("why") or "") for t in r["tried"])


def test_short_history_refuses_rather_than_guessing():
    r = sf.measured_floor(bars(oscillating(n=50)))
    assert r["ok"] is False and r["reason"] == "NOT_ENOUGH_HISTORY"
    assert r["floor_pct"] is None


def test_every_step_tried_is_reported_with_its_reason():
    r = sf.measured_floor(bars(oscillating()))
    for t in r["tried"]:
        assert "step_pct" in t
        if not t.get("qualifies"):
            assert t.get("why") or t.get("reason")


# ------------------------------------------------------- fleet floor

def _book():
    return {"GOOD": bars(oscillating()), "CALM": bars(oscillating(amp=2.0)),
            "BAD": bars(falling())}


def test_the_fleet_floor_takes_the_MAX_not_the_average():
    """A fleet-wide spacing must survive on every coin it is applied to."""
    r = sf.fleet_floor(_book(), fee_safe_pct=0.90)
    per = [v["floor_pct"] for v in r["per_coin"].values() if v["floor_pct"] is not None]
    assert r["measured_floor_pct"] == max(per)


def test_the_fee_floor_is_never_lowered_by_this():
    r = sf.fleet_floor(_book(), fee_safe_pct=9.0)
    assert r["recommended_floor_pct"] >= 9.0


def test_the_measured_floor_raises_a_too_low_fee_floor():
    r = sf.fleet_floor(_book(), fee_safe_pct=0.90)
    if r["measured_floor_pct"] and r["measured_floor_pct"] > 0.90:
        assert r["recommended_floor_pct"] == r["measured_floor_pct"]
        assert "pass both" in r["basis"]


def test_unmeasurable_coins_are_named_not_ignored():
    r = sf.fleet_floor(_book(), fee_safe_pct=0.90)
    assert "BAD" in r["coins_unmeasurable"]


def test_no_measurable_coin_says_so_loudly():
    r = sf.fleet_floor({"BAD": bars(falling())}, fee_safe_pct=0.90)
    assert r["measured_floor_pct"] is None
    assert "lose money" in r["basis"]


def test_the_caveat_says_a_floor_moves():
    r = sf.fleet_floor(_book(), fee_safe_pct=0.90)
    assert "re-measure" in r["caveat"]


# ------------------------------------------- structural guards

def test_it_reaches_no_venue():
    tree = ast.parse(open("spacing_floor.py").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + [getattr(n, "module", None)]
            for x in names:
                assert not any(b in (x or "") for b in
                               ("aiohttp", "requests", "coinbase", "urllib")), x


def test_it_only_recommends():
    tree = ast.parse(open("spacing_floor.py").read())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for banned in ("commit", "execute", "post"):
        assert banned not in called, f"spacing_floor calls {banned}"
