"""Tests for the thing that decides where real capital goes next.

The replay is the part that can lie. A grid backtest that only counts the
round trips it closed looks profitable in every market, including the ones
that would have buried it, so most of these are about the bag.
"""
import ast

import pytest

import grid_universe as gu


def bars(closes, spread=0.0):
    """Hourly bars from a close series; low/high straddle by `spread` pct."""
    return [{"close": c, "low": c*(1-spread/100), "high": c*(1+spread/100)} for c in closes]


def oscillating(n=240, amp=6.0, base=100.0):
    import math
    return [base*(1+amp/100*math.sin(i/3.0)) for i in range(n)]


def falling(n=240, base=100.0, per_bar=-0.3):
    return [base*(1+per_bar/100)**i for i in range(n)]


# ------------------------------------------------------------ replay

def test_a_rung_cannot_open_and_close_in_one_bar():
    """Intrabar ordering is unknowable; allowing it manufactures profit.

    One bar spans the whole grid - rungs fill on the way down and the same
    bar's high clears their targets. If same-bar closing were allowed this
    would report three free round trips. Every later bar stays BELOW the
    targets so nothing can legitimately close afterwards either.
    """
    b = [{"close": 100, "low": 100, "high": 100}] + \
        [{"close": 85, "low": 80, "high": 120}] + \
        [{"close": 85, "low": 84, "high": 86}]*30
    r = gu.replay(b, step_pct=3.75)
    assert r["ok"] and r["completions"] == 0


def test_an_oscillating_coin_completes_round_trips():
    r = gu.replay(bars(oscillating()), step_pct=3.75)
    assert r["ok"] and r["completions"] > 0 and r["net_per_day_pct"] > 0


def test_a_falling_coin_strands_its_rungs_and_loses():
    r = gu.replay(bars(falling()), step_pct=3.75)
    assert r["ok"]
    assert r["stranded"] > 0
    assert r["unrealised_pct"] < 0
    assert r["net_pct"] < r["realised_pct"], "the bag must reduce the result"


def test_the_bag_is_always_counted():
    """net = realised + unrealised, never just realised."""
    r = gu.replay(bars(falling()), step_pct=3.75)
    assert r["net_pct"] == pytest.approx(r["realised_pct"] + r["unrealised_pct"], abs=0.01)


def test_rungs_are_capped_at_the_level_count():
    r = gu.replay(bars(falling()), step_pct=1.0, levels=3)
    assert r["stranded"] <= 3


def test_short_history_refuses():
    r = gu.replay(bars([100]*10))
    assert r["ok"] is False and r["reason"] == "NOT_ENOUGH_HISTORY"


def test_a_step_under_the_fee_is_refused():
    r = gu.replay(bars(oscillating()), step_pct=0.5, fee_pct=0.70)
    assert r["ok"] is False and r["reason"] == "STEP_UNDER_FEE"


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_bad_step_refuses(bad):
    r = gu.replay(bars(oscillating()), step_pct=bad)
    assert r["ok"] is False


def test_unreadable_bars_are_skipped_not_guessed():
    b = bars(oscillating())
    b[5] = {"close": None, "low": None, "high": None}
    assert gu.replay(b)["ok"] is True


# ----------------------------------------------------------- verdict

def test_a_still_coin_is_not_worth_a_branch():
    r = gu.replay(bars([100.0]*240))
    v, why = gu.verdict(r)
    assert v == "TOO_STILL" and "parked" in why


def test_a_trending_coin_is_named_as_trending():
    r = gu.replay(bars(falling()), step_pct=3.75)
    v, why = gu.verdict(r)
    assert v == "TRENDING", f"a crashing coin must not be called {v}"
    assert "did not come back" in why


def test_an_oscillating_coin_is_worth_a_branch():
    r = gu.replay(bars(oscillating()), step_pct=3.75)
    assert gu.verdict(r)[0] == "WORTH_A_BRANCH"


def test_no_data_is_its_own_verdict():
    assert gu.verdict({"ok": False, "detail": "x"})[0] == "NO_DATA"


# -------------------------------------------------------------- rank

def _book():
    return {"GOOD": bars(oscillating()), "FLAT": bars([100.0]*240),
            "BAD": bars(falling()), "ALSO": bars(oscillating(amp=8.0))}


def test_ranking_is_by_rate_not_by_completions():
    ranked = gu.rank(_book())
    rates = [r["net_per_day_pct"] for r in ranked]
    assert rates == sorted(rates, reverse=True)


def test_current_branches_are_marked():
    ranked = gu.rank(_book(), current=["GOOD"])
    assert next(r for r in ranked if r["coin"] == "GOOD")["in_fleet"] is True
    assert next(r for r in ranked if r["coin"] == "BAD")["in_fleet"] is False


def test_every_candidate_appears():
    assert len(gu.rank(_book())) == 4


# --------------------------------------------------------- recommend

def test_it_recommends_adding_the_good_ones():
    rec = gu.recommend(gu.rank(_book(), current=["FLAT"]))
    assert "GOOD" in [a["coin"] for a in rec["add"]]
    assert "FLAT" not in [a["coin"] for a in rec["add"]]


def test_a_current_branch_that_no_longer_qualifies_is_flagged():
    rec = gu.recommend(gu.rank(_book(), current=["FLAT"]))
    assert "FLAT" in [f["coin"] for f in rec["flagged_current"]]


def test_it_never_changes_anything():
    rec = gu.recommend(gu.rank(_book()))
    assert rec["is_a_recommendation_not_a_change"] is True


def test_the_caveat_names_the_capital_split():
    rec = gu.recommend(gu.rank(_book()))
    assert "splits the same capital thinner" in rec["caveat"]


def test_max_branches_is_respected():
    rec = gu.recommend(gu.rank(_book()), max_branches=1)
    assert rec["branches_recommended"] == 1


# ------------------------------------------- structural guards

def test_module_touches_no_venue():
    tree = ast.parse(open("grid_universe.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "httpx", "coinbase", "urllib")), n


def test_it_cannot_place_or_allocate():
    src = open("grid_universe.py").read()
    for banned in ("order_configuration", "base_size", "client_order_id"):
        assert banned not in src, f"selector mentions {banned}"



def test_a_coin_earning_exactly_zero_outranks_one_that_loses():
    """0.0 is a real value, not a missing one."""
    ranked = gu.rank({"FLAT": bars([100.0]*240), "BAD": bars(falling())})
    assert [r["coin"] for r in ranked] == ["FLAT", "BAD"]


def test_a_crash_is_never_labelled_still():
    r = gu.replay(bars(falling()), step_pct=3.75)
    v, why = gu.verdict(r)
    assert "parked" not in why
