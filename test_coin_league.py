"""Tests for the league table every coin competes in.

The failures that matter: crowning a coin because its market rose, and
crowning one on a sample too small to mean anything. Both produce a
blueprint that spreads luck to every other coin in the account.
"""
import ast

import pytest

import coin_league as cl


def tr(pnl, qty=1.0, entry=100.0):
    return {"pnl": pnl, "qty": qty, "entry_price": entry}


def many(n, pnl_pct, qty=1.0, entry=100.0):
    """n trades each returning pnl_pct of the capital risked."""
    return [tr(pnl_pct / 100 * qty * entry, qty, entry) for _ in range(n)]


# ------------------------------------------- the fair number

def test_a_small_coin_can_beat_a_big_one():
    """The whole point. $87 of PEPE and $2,250 of ZEC compete on return
    per dollar risked, so size cannot buy the crown."""
    out = cl.table(
        {"PEPE-USD": many(25, 1.2, qty=20000000.0, entry=0.00000433),
         "ZEC-USD": many(25, 0.4, qty=1.37, entry=1647.08)},
        window_returns={"PEPE-USD": 0.5, "ZEC-USD": 0.5})
    assert out["crown"] == "PEPE"
    assert out["ranked"][0]["coin"] == "PEPE"


def test_more_dollars_earned_does_not_win():
    """ZEC earns far more in dollars and still loses on the fair number."""
    out = cl.table(
        {"PEPE-USD": many(25, 1.2, qty=20000000.0, entry=0.00000433),
         "ZEC-USD": many(25, 0.4, qty=1.37, entry=1647.08)},
        window_returns={"PEPE-USD": 0.5, "ZEC-USD": 0.5})
    zec = next(c for c in out["ranked"] if c["coin"] == "ZEC")
    pepe = next(c for c in out["ranked"] if c["coin"] == "PEPE")
    assert zec["net_usd"] > pepe["net_usd"]          # more dollars
    assert zec["rank"] > pepe["rank"]                 # still ranked lower


def test_units_are_never_the_ranking_and_the_table_says_so():
    out = cl.table({"A-USD": many(25, 1.0)}, window_returns={"A-USD": 0.0})
    assert "20,232,619" in out["ranked_on"]
    assert "supply" in out["ranked_on"]


def test_a_trade_with_no_size_is_dropped_not_averaged_in_at_zero():
    """Averaging an unsized trade at 0% would drag a good coin down for a
    missing field."""
    c = cl.scorecard(many(10, 2.0) + [{"pnl": 5.0}], product_id="A-USD")
    assert c["trades"] == 11 and c["sized_trades"] == 10
    assert c["edge_pct_per_trade"] == pytest.approx(2.0)


# ------------------------------------------ the crown needs evidence

def test_a_coin_that_rose_only_gets_a_provisional_crown():
    """A long grid on a rising coin looks brilliant for reasons that are
    not the grid."""
    out = cl.table({"A-USD": many(25, 1.5)}, window_returns={"A-USD": 19.27})
    assert out["crown"] == "A" and out["crown_status"] == "PROVISIONAL"
    assert "not the grid" in out["crown_detail"]


def test_a_coin_that_earned_through_a_fall_is_confirmed():
    out = cl.table({"A-USD": many(25, 1.5)}, window_returns={"A-USD": -8.0})
    assert out["crown_status"] == "CONFIRMED"
    assert "that is the coin to copy" in out["crown_detail"]


def test_a_flat_market_also_confirms():
    out = cl.table({"A-USD": many(25, 1.5)}, window_returns={"A-USD": 0.4})
    assert out["crown_status"] == "CONFIRMED"


def test_an_unknown_window_never_confirms_a_crown():
    """Not knowing the regime is not the same as having survived one."""
    out = cl.table({"A-USD": many(25, 1.5)})
    assert out["crown_status"] == "PROVISIONAL"


def test_a_coin_with_too_few_trades_cannot_hold_the_crown():
    out = cl.table({"A-USD": many(6, 9.0), "B-USD": many(30, 1.0)},
                   window_returns={"A-USD": 0.0, "B-USD": 0.0})
    assert out["ranked"][0]["coin"] == "A"       # leads on the number
    assert out["crown"] == "B"                    # but cannot wear the crown


def test_a_losing_leader_is_given_no_crown_at_all():
    out = cl.table({"A-USD": many(30, -0.5)}, window_returns={"A-USD": 0.0})
    assert out["crown"] is None
    assert "No coin has" in out["crown_detail"] or out["crown_detail"]


# --------------------------------------- the robust finding

def test_losing_while_its_own_price_rose_is_reported_as_actionable():
    """No regime excuse exists for that, so it is the one thing here that
    can be acted on before any crown is copied."""
    out = cl.table({"A-USD": many(25, -0.4), "B-USD": many(25, 1.0)},
                   window_returns={"A-USD": 19.0, "B-USD": -3.0})
    assert out["robustly_failing"] == ["A"]
    assert "No regime excuse" in out["robustly_failing_detail"]


def test_losing_while_its_price_fell_is_not_called_robust():
    out = cl.table({"A-USD": many(25, -0.4)}, window_returns={"A-USD": -19.0})
    assert out["robustly_failing"] == []


# ------------------------------------------------ still qualifying

def test_a_coin_below_the_minimum_is_shown_but_never_ranked():
    out = cl.table({"A-USD": many(2, 50.0), "B-USD": many(30, 1.0)},
                   window_returns={"A-USD": 0.0, "B-USD": 0.0})
    qual = [c["coin"] for c in out["still_qualifying"]]
    assert "A" in qual
    assert "A" not in [c["coin"] for c in out["ranked"]]
    a = next(c for c in out["still_qualifying"] if c["coin"] == "A")
    assert a["rank"] is None and "eighty" in a["why_not_ranked"]


def test_a_coin_with_no_sized_trades_cannot_be_compared_fairly():
    c = cl.scorecard([{"pnl": 1.0} for _ in range(30)], product_id="A-USD")
    assert c["ranked"] is False
    assert "cannot be computed" in c["why_not_ranked"]


# --------------------------------------------------- the blueprint

LEAGUE_CONFIRMED = cl.table(
    {"A-USD": many(25, 1.5), "B-USD": many(25, 0.3)},
    window_returns={"A-USD": -8.0, "B-USD": -8.0},
    configs={"A-USD": {"grid_pct": 0.025, "num_levels": 3},
             "B-USD": {"grid_pct": 0.009, "num_levels": 10}})


def test_the_blueprint_hands_over_the_settings_not_the_coin():
    b = cl.blueprint(LEAGUE_CONFIRMED, "B")
    assert b["ok"] is True
    assert b["copy"] == {"grid_pct": 0.025, "num_levels": 3}
    assert "settings, not the coin" in b["detail"]


def test_the_blueprint_names_the_gap_to_close():
    b = cl.blueprint(LEAGUE_CONFIRMED, "B")
    assert b["gap_pct_per_trade"] == pytest.approx(1.2, abs=0.01)


def test_a_provisional_crown_is_never_copied():
    """Copying it would spread a rising market's returns as a method."""
    league = cl.table({"A-USD": many(25, 1.5), "B-USD": many(25, 0.3)},
                      window_returns={"A-USD": 19.27, "B-USD": 19.0})
    b = cl.blueprint(league, "B")
    assert b["ok"] is False and b["reason"] == "CROWN_IS_PROVISIONAL"
    assert "as if they were a method" in b["detail"]


def test_no_crown_means_no_blueprint():
    league = cl.table({"A-USD": many(6, 1.5)}, window_returns={"A-USD": -8.0})
    assert cl.blueprint(league, "A")["reason"] == "NO_CROWN_YET"


def test_a_challenger_nobody_measured_is_refused():
    assert cl.blueprint(LEAGUE_CONFIRMED, "ZZZ")["reason"] == "UNKNOWN_CHALLENGER"


def test_the_blueprint_changes_nothing():
    assert cl.blueprint(LEAGUE_CONFIRMED, "B")["is_a_plan_not_a_change"] is True


# ------------------------------------------------- shape and guards

@pytest.mark.parametrize("junk", [None, {}, []])
def test_an_empty_league_does_not_crash(junk):
    out = cl.table(junk)
    assert out["coins"] == 0 and out["crown"] is None


def test_the_league_touches_no_venue_and_no_database():
    tree = ast.parse(open("coin_league.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database", "crypto_grid_bot")), n


def test_the_league_survives_json():
    import json
    out = cl.table({"A-USD": many(25, 1.5)}, window_returns={"A-USD": -8.0})
    assert json.loads(json.dumps(out)) == out
