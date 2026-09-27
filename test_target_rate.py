"""Tests for the arithmetic behind "it should make $20 an hour".

The expensive failure here is quoting the flattering rate. A fortnight
that went well, multiplied by a target, produces a capital plan built on
a number that was never there.
"""
import ast

import pytest

import target_rate as t


LIVE = dict(target_usd_per_hour=20.0, coins=9, account_total_usd=11554.0,
            deployed_usd=1933.69, recent_earned_usd=5.64, recent_hours=6.89,
            alltime_earned_usd=25.25, alltime_hours=27.59 * 24,
            alltime_avg_deployed_usd=150.0, trades=86)


def test_the_target_is_out_of_reach_on_this_account_and_says_so():
    a = t.assess(**LIVE)
    assert a["verdict"] == "NEEDS_MORE_CAPITAL_THAN_EXISTS"
    assert a["reachable_with_this_account"] is False
    assert a["multiple_of_account_alltime"] > 50


def test_a_per_coin_target_is_multiplied_by_the_coins():
    a = t.assess(**LIVE)
    assert a["fleet_target_usd_per_hour"] == 180.0
    assert a["fleet_target_usd_per_day"] == 4320.0


def test_both_rates_are_always_reported_never_just_the_flattering_one():
    """Quoting the recent one alone promises a return the fleet has never
    sustained; quoting only the long one ignores that the current config
    really is trading better. Both, always, with the gap stated.

    The gap is not fixed: it was 18.9x when $169 was deployed and fell to
    1.7x once adoption put $1,933 to work, because the recent rate is
    measured against the larger base. A test that pinned the multiple
    would break every time the account improved."""
    a = t.assess(**LIVE)
    assert a["recent_rate_pct_per_hour"] is not None
    assert a["alltime_rate_pct_per_hour"] is not None
    assert a["rates_disagree_by"] >= 1.0


def test_capital_needed_is_computed_from_BOTH_rates():
    a = t.assess(**LIVE)
    assert a["capital_needed_at_recent_rate_usd"] is not None
    assert a["capital_needed_at_alltime_rate_usd"] is not None
    #  the pessimistic rate must demand MORE capital, never less
    assert (a["capital_needed_at_alltime_rate_usd"]
            >= a["capital_needed_at_recent_rate_usd"])


def test_the_ceiling_without_adding_money_is_computed():
    """The useful figure: what the WHOLE account would produce."""
    a = t.assess(**LIVE)
    assert a["whole_account_at_alltime_rate_usd_per_hour"] == pytest.approx(2.94, abs=0.05)


def test_a_thin_sample_refuses_to_produce_a_capital_plan():
    a = t.assess(**{**LIVE, "trades": 4})
    assert a["available"] is False
    assert "lucky fortnight" in a["reason"]


def test_a_reachable_target_is_reported_as_reachable():
    a = t.assess(**{**LIVE, "target_usd_per_hour": 0.05, "coins": 1})
    assert a["verdict"] == "REACHABLE"


@pytest.mark.parametrize("bad", [0, None, -5, "x"])
def test_junk_never_produces_a_required_capital(bad):
    assert t.required_capital(bad, 0.001) is None
    assert t.required_capital(20.0, bad if isinstance(bad, float) else None) is None


def test_a_zero_rate_never_divides():
    assert t.required_capital(20.0, 0.0) is None
    assert t.rate_per_dollar_hour(5.0, 0, 100.0) is None
    assert t.rate_per_dollar_hour(5.0, 10.0, 0) is None


def test_it_never_annualises():
    tree = ast.parse(open("target_rate.py").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            assert n.value not in (365, 365.0, 252, 8760), n.value


def test_it_points_at_capital_not_at_a_faster_grid():
    """The one lever this account has tested is a WIDER step, not a
    faster one - 0.9% took it from +65.4% to -71.1%."""
    a = t.assess(**LIVE)
    assert "WIDER grid step, not a faster one" in a["what_moves_it"]


def test_it_touches_no_venue_and_no_database():
    tree = ast.parse(open("target_rate.py").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [x.name for x in n.names] + [getattr(n, "module", None)]
            for nm in names:
                assert not any(b in (nm or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), nm


def test_everything_survives_json():
    import json
    a = t.assess(**LIVE)
    assert json.loads(json.dumps(a)) == a
