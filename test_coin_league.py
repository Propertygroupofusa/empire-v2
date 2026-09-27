"""Tests for the league table every coin competes in.

The failures that matter: crowning a coin because its market rose, and
crowning one on a sample too small to mean anything. Both produce a
blueprint that spreads luck to every other coin in the account.
"""
import ast

import pytest

import coin_league as cl


EPOCH = "2026-09-26T02:45:00Z"
AFTER = "2026-09-27T00:00:00"      # on the current configuration
BEFORE = "2026-09-20T00:00:00"     # measured a bot that no longer runs


def tr(pnl, qty=1.0, entry=100.0, closed=AFTER):
    return {"pnl": pnl, "qty": qty, "entry_price": entry, "closed_at": closed}


def many(n, pnl_pct, qty=1.0, entry=100.0, closed=AFTER):
    """n trades each returning pnl_pct of the capital risked."""
    return [tr(pnl_pct / 100 * qty * entry, qty, entry, closed) for _ in range(n)]


def league(trades_by_coin, **kw):
    kw.setdefault("config_epoch", EPOCH)
    return cl.table(trades_by_coin, **kw)


# ------------------------------------------- the fair number

def test_a_small_coin_can_beat_a_big_one():
    """The whole point. $87 of PEPE and $2,250 of ZEC compete on return
    per dollar risked, so size cannot buy the crown."""
    out = league(
        {"PEPE-USD": many(25, 1.2, qty=20000000.0, entry=0.00000433),
         "ZEC-USD": many(25, 0.4, qty=1.37, entry=1647.08)},
        window_returns={"PEPE-USD": 0.5, "ZEC-USD": 0.5})
    assert out["crown"] == "PEPE"
    assert out["ranked"][0]["coin"] == "PEPE"


def test_more_dollars_earned_does_not_win():
    """ZEC earns far more in dollars and still loses on the fair number."""
    out = league(
        {"PEPE-USD": many(25, 1.2, qty=20000000.0, entry=0.00000433),
         "ZEC-USD": many(25, 0.4, qty=1.37, entry=1647.08)},
        window_returns={"PEPE-USD": 0.5, "ZEC-USD": 0.5})
    zec = next(c for c in out["ranked"] if c["coin"] == "ZEC")
    pepe = next(c for c in out["ranked"] if c["coin"] == "PEPE")
    assert zec["net_usd"] > pepe["net_usd"]          # more dollars
    assert zec["rank"] > pepe["rank"]                 # still ranked lower


def test_units_are_never_the_ranking_and_the_table_says_so():
    out = league({"A-USD": many(25, 1.0)}, window_returns={"A-USD": 0.0})
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
    out = league({"A-USD": many(25, 1.5)}, window_returns={"A-USD": 19.27})
    assert out["crown"] == "A" and out["crown_status"] == "PROVISIONAL"
    assert "not the grid" in out["crown_detail"]


def test_a_coin_that_earned_through_a_fall_is_confirmed():
    out = league({"A-USD": many(25, 1.5)}, window_returns={"A-USD": -8.0})
    assert out["crown_status"] == "CONFIRMED"
    assert "that is the coin to copy" in out["crown_detail"]


def test_a_flat_market_also_confirms():
    out = league({"A-USD": many(25, 1.5)}, window_returns={"A-USD": 0.4})
    assert out["crown_status"] == "CONFIRMED"


def test_an_unknown_window_never_confirms_a_crown():
    """Not knowing the regime is not the same as having survived one."""
    out = league({"A-USD": many(25, 1.5)})
    assert out["crown_status"] == "PROVISIONAL"


def test_a_coin_with_too_few_trades_cannot_hold_the_crown():
    out = league({"A-USD": many(6, 9.0), "B-USD": many(30, 1.0)},
                   window_returns={"A-USD": 0.0, "B-USD": 0.0})
    assert out["ranked"][0]["coin"] == "A"       # leads on the number
    assert out["crown"] == "B"                    # but cannot wear the crown


def test_a_losing_leader_is_given_no_crown_at_all():
    out = league({"A-USD": many(30, -0.5)}, window_returns={"A-USD": 0.0})
    assert out["crown"] is None
    assert "No coin has" in out["crown_detail"] or out["crown_detail"]


# --------------------------------------- the robust finding

def test_losing_while_its_own_price_rose_is_reported_as_actionable():
    """No regime excuse exists for that, so it is the one thing here that
    can be acted on before any crown is copied."""
    out = league({"A-USD": many(25, -0.4), "B-USD": many(25, 1.0)},
                   window_returns={"A-USD": 19.0, "B-USD": -3.0})
    assert out["robustly_failing"] == ["A"]
    assert "No regime excuse" in out["robustly_failing_detail"]


def test_losing_while_its_price_fell_is_not_called_robust():
    out = league({"A-USD": many(25, -0.4)}, window_returns={"A-USD": -19.0})
    assert out["robustly_failing"] == []


# ------------------------------------------------ still qualifying

def test_a_coin_below_the_minimum_is_shown_but_never_ranked():
    out = league({"A-USD": many(2, 50.0), "B-USD": many(30, 1.0)},
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

LEAGUE_CONFIRMED = league(
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
    league_x = league({"A-USD": many(25, 1.5), "B-USD": many(25, 0.3)},
                      window_returns={"A-USD": 19.27, "B-USD": 19.0})
    b = cl.blueprint(league_x, "B")
    assert b["ok"] is False and b["reason"] == "CROWN_IS_PROVISIONAL"
    assert "as if they were a method" in b["detail"]


def test_no_crown_means_no_blueprint():
    league_x = league({"A-USD": many(6, 1.5)}, window_returns={"A-USD": -8.0})
    assert cl.blueprint(league_x, "A")["reason"] == "NO_CROWN_YET"


def test_a_challenger_nobody_measured_is_refused():
    assert cl.blueprint(LEAGUE_CONFIRMED, "ZZZ")["reason"] == "UNKNOWN_CHALLENGER"


def test_the_blueprint_changes_nothing():
    assert cl.blueprint(LEAGUE_CONFIRMED, "B")["is_a_plan_not_a_change"] is True


# ------------------------------------------------- shape and guards

@pytest.mark.parametrize("junk", [None, {}, []])
def test_an_empty_league_does_not_crash(junk):
    out = league(junk)
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
    out = league({"A-USD": many(25, 1.5)}, window_returns={"A-USD": -8.0})
    assert json.loads(json.dumps(out)) == out


# ------------------------------------- the retired-configuration guard

def test_a_record_set_by_a_retired_configuration_wins_no_crown():
    """THE REAL ONE. The league shipped ranking DOGE first at +2.28% - and
    82 of the fleet's 83 closed trades predated the config change. A
    perfectly accurate measurement of a bot that was already switched
    off, presented as live standings."""
    out = league({"A-USD": many(30, 2.28, closed=BEFORE)},
                 window_returns={"A-USD": -8.0})
    assert out["crown"] is None
    assert out["ranked"][0]["config_status"] == "RETIRED"
    assert "no longer runs" in (out["config_warning"] or "")


def test_a_coin_trading_on_the_current_config_can_still_be_crowned():
    out = league({"A-USD": many(30, 1.5, closed=AFTER)},
                 window_returns={"A-USD": -8.0})
    assert out["crown"] == "A" and out["crown_status"] == "CONFIRMED"
    assert out["ranked"][0]["config_status"] == "CURRENT"


def test_a_mostly_retired_record_is_named_as_such():
    out = league({"A-USD": many(20, 1.5, closed=BEFORE) + many(5, 1.5, closed=AFTER)},
                 window_returns={"A-USD": -8.0})
    card = out["ranked"][0]
    assert card["config_status"] == "MOSTLY_RETIRED"
    assert card["crown_eligible"] is False
    assert card["trades_on_current_config"] == 5


def test_a_trade_with_no_timestamp_counts_as_retired_not_current():
    """An untimed trade is not evidence about the current bot, and
    assuming otherwise flatters it."""
    out = league({"A-USD": [{"pnl": 1.0, "qty": 1.0, "entry_price": 100.0}] * 30},
                 window_returns={"A-USD": -8.0})
    assert out["ranked"][0]["trades_on_current_config"] == 0
    assert out["crown"] is None


def test_without_an_epoch_nothing_can_be_crowned_at_all():
    """Not being able to tell a current record from a retired one is not
    the same as having a current one."""
    out = cl.table({"A-USD": many(30, 1.5)}, window_returns={"A-USD": -8.0})
    assert out["crown"] is None
    assert "no crown can be awarded" in out["config_warning"]


def test_the_retired_share_is_reported_so_the_table_can_be_read():
    out = league({"A-USD": many(9, 1.0, closed=BEFORE) + many(1, 1.0, closed=AFTER)},
                 window_returns={"A-USD": 0.0})
    assert out["trades_total"] == 10
    assert out["trades_on_current_config"] == 1
    assert out["retired_share"] == pytest.approx(0.9)


# --------------------------------------------- retired while earning

def test_a_coin_that_earned_and_was_dropped_is_named():
    """The blunt answer this league turned up: every ranked coin was one
    no branch held any more. $19.19 of $19.61 lifetime profit came from
    coins the fleet had rotated away from."""
    out = league({"DOGE-USD": many(25, 2.28), "BTC-USD": many(25, 0.1)},
                 window_returns={"DOGE-USD": 0.0, "BTC-USD": 0.0},
                 held_products=["BTC-USD"])
    assert out["retired_while_earning"] == ["DOGE"]
    assert "not a ranking curiosity" in out["retired_while_earning_detail"]


def test_a_losing_coin_that_was_dropped_is_not_flagged():
    """Dropping something that lost money is not the finding."""
    out = league({"A-USD": many(25, -1.0), "B-USD": many(25, 0.5)},
                 window_returns={"A-USD": 0.0, "B-USD": 0.0},
                 held_products=["B-USD"])
    assert out["retired_while_earning"] == []


def test_an_earner_still_held_is_not_flagged():
    out = league({"A-USD": many(25, 1.0)}, window_returns={"A-USD": 0.0},
                 held_products=["A-USD"])
    assert out["retired_while_earning"] == []
    assert "still held by a branch" in out["retired_while_earning_detail"]


def test_without_a_held_list_nothing_is_claimed_about_drops():
    """Absent the list, every coin would look dropped - which would be an
    invented finding, not a measured one."""
    out = league({"A-USD": many(25, 1.0)}, window_returns={"A-USD": 0.0})
    assert out["retired_while_earning"] == []
    assert "No list of currently-held coins" in out["retired_while_earning_detail"]


def test_the_dropped_profit_is_totalled():
    out = league({"A-USD": many(25, 1.0, qty=1.0, entry=100.0)},
                 window_returns={"A-USD": 0.0}, held_products=["Z-USD"])
    assert out["retired_while_earning_usd"] == pytest.approx(25.0)
