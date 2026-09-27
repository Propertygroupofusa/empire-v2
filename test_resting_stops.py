"""Tests for orders that will rest at a venue holding real coins.

The refusals matter more than the successes here. A resting sell stop
placed wrong does not sit there doing nothing - it fires, and it sells.
"""
import ast

import pytest

import resting_stops as rs


# ZEC is on the default exclusion list (the trimmer owns it), so the tests
# about ORDER MECHANICS opt out of that explicitly. The exclusion itself is
# tested separately below - a fixture that silently dodged it would make
# every mechanics test pass for the wrong reason.
ZEC = dict(units_available=1.42, price=1639.01, stop_price=1414.55,
           base_increment="0.00000001", quote_increment="0.01",
           excluded=set())


# ------------------------------------------------- the catastrophic one

def test_a_stop_at_or_above_the_market_is_refused():
    """This would fire on acceptance and liquidate the position."""
    for sp in (1639.01, 1639.02, 2000.0):
        r = rs.plan_stop("ZEC", **{**ZEC, "stop_price": sp})
        assert r["ok"] is False and r["reason"] == "STOP_ABOVE_MARKET", sp


def test_a_stop_one_tick_below_the_market_is_still_refused():
    r = rs.plan_stop("ZEC", **{**ZEC, "stop_price": 1639.00})
    assert r["ok"] is False and r["reason"] == "TOO_CLOSE"


def test_the_minimum_distance_is_enforced_exactly():
    p = 100.0
    just_in = rs.plan_stop("X", units_available=100, price=p, stop_price=98.0,
                           quote_increment="0.01", excluded=set())
    just_out = rs.plan_stop("X", units_available=100, price=p, stop_price=98.5,
                            quote_increment="0.01", excluded=set())
    assert just_in["ok"] is True          # exactly 2.0% away
    assert just_out["ok"] is False and just_out["reason"] == "TOO_CLOSE"


# ------------------------------------------------------- the limit band

def test_the_limit_sits_below_the_stop():
    r = rs.plan_stop("ZEC", **ZEC)
    assert float(r["limit_price"]) < float(r["stop_price"])


def test_the_band_is_the_configured_width():
    r = rs.plan_stop("ZEC", **ZEC, limit_band_pct=2.0)
    stop, lim = float(r["stop_price"]), float(r["limit_price"])
    assert (stop - lim) / stop * 100 == pytest.approx(2.0, abs=0.02)


def test_worst_case_is_reported_and_is_worse_than_the_stop():
    r = rs.plan_stop("ZEC", **ZEC)
    assert r["worst_case_usd"] < r["protects_usd"]


# ---------------------------------------------------------- the hold

def test_coverage_leaves_units_available():
    r = rs.plan_stop("ZEC", **ZEC)
    assert r["units_left_available"] > 0
    assert r["units"] < ZEC["units_available"]


def test_full_coverage_leaves_nothing_and_that_is_visible():
    r = rs.plan_stop("ZEC", **ZEC, coverage_pct=100)
    assert r["units_left_available"] == pytest.approx(0, abs=1e-8)


def test_coverage_default_is_not_everything():
    """100% coverage disables the trimmer for that coin."""
    assert 0 < rs.COVERAGE_PCT < 100


# ------------------------------------------------------ bad inputs

@pytest.mark.parametrize("bad", [None, 0, -1, "x", float("nan")])
def test_unusable_units_refuse(bad):
    r = rs.plan_stop("ZEC", **{**ZEC, "units_available": bad})
    assert r["ok"] is False and r["reason"] == "NOTHING_AVAILABLE"


@pytest.mark.parametrize("bad", [None, 0, -5, "x", float("inf")])
def test_unusable_price_refuses(bad):
    r = rs.plan_stop("ZEC", **{**ZEC, "price": bad})
    assert r["ok"] is False and r["reason"] == "UNPRICED"


@pytest.mark.parametrize("bad", [None, 0, -5, "x", float("nan")])
def test_missing_level_refuses(bad):
    r = rs.plan_stop("ZEC", **{**ZEC, "stop_price": bad})
    assert r["ok"] is False and r["reason"] == "NO_LEVEL"


def test_a_dust_position_is_refused():
    r = rs.plan_stop("FLOCK", units_available=1000, price=0.0009,
                     stop_price=0.0007, quote_increment="0.000001", excluded=set())
    assert r["ok"] is False and r["reason"] == "TOO_SMALL"


def test_below_product_minimum_is_refused():
    r = rs.plan_stop("ZEC", **ZEC, base_min_size="10")
    assert r["ok"] is False and r["reason"] == "BELOW_MIN_SIZE"


# ------------------------------------------------------ rounding

def test_size_never_exceeds_what_is_available():
    r = rs.plan_stop("ZEC", **ZEC, coverage_pct=100)
    assert r["units"] <= ZEC["units_available"]


def test_prices_round_down_not_up():
    """Rounding a sell trigger UP moves protection toward the market."""
    r = rs.plan_stop("X", units_available=100, price=100.0, stop_price=93.999,
                     quote_increment="0.01", excluded=set())
    assert float(r["stop_price"]) <= 93.999


def test_size_lands_on_the_product_grid():
    r = rs.plan_stop("XRP", units_available=1558.651817, price=1.5263,
                     stop_price=1.4, base_increment="0.000001",
                     quote_increment="0.0001", excluded=set())
    frac = r["base_size"].split(".")[1] if "." in r["base_size"] else ""
    assert len(frac.rstrip("0")) <= 6


# ------------------------------------------------------ the ratchet

def test_a_stop_never_moves_down():
    ok, why = rs.needs_replacement(1500.0, 1400.0)
    assert ok is False and "never down" in why


def test_an_equal_level_is_not_replaced():
    ok, _ = rs.needs_replacement(1500.0, 1500.0)
    assert ok is False


def test_a_trivial_move_up_is_not_worth_the_replace():
    ok, why = rs.needs_replacement(1500.0, 1503.0)     # 0.2%
    assert ok is False and "two requests" in why


def test_a_real_move_up_replaces():
    ok, why = rs.needs_replacement(1500.0, 1560.0)     # 4%
    assert ok is True and "higher" in why


@pytest.mark.parametrize("bad", [None, 0, -1, "x"])
def test_unreadable_levels_never_trigger_a_replace(bad):
    assert rs.needs_replacement(bad, 1500.0)[0] is False
    assert rs.needs_replacement(1500.0, bad)[0] is False


# ------------------------------------------------------ the mode

@pytest.mark.parametrize("raw", [None, "", "true", "yes", "1", "ARMED", "observe", 1])
def test_only_exactly_arm_arms(raw):
    assert rs.normalise_mode(raw) == rs.MODE_OBSERVE


def test_arm_arms():
    assert rs.normalise_mode("arm") == rs.MODE_ARM


# ------------------------------------------------ the order it builds

def test_the_order_is_a_stop_down_sell():
    r = rs.plan_stop("ZEC", **ZEC)
    cfg = r["order_configuration"]["stop_limit_stop_limit_gtc"]
    assert cfg["stop_direction"] == "STOP_DIRECTION_STOP_DOWN"
    assert float(cfg["limit_price"]) < float(cfg["stop_price"])
    assert cfg["base_size"] == r["base_size"]


def test_the_summary_states_what_it_cannot_do():
    s = rs.summarise([rs.plan_stop("ZEC", **ZEC)], "arm")
    assert "gaps through the limit" in s["what_it_does_not_do"]
    assert "hold the coins" in s["the_cost"]


# ------------------------------------------- structural guards

def test_module_places_nothing():
    tree = ast.parse(open("resting_stops.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "httpx", "urllib", "coinbase")), n


def test_no_buy_side_order_can_be_built():
    """Every configuration this module emits must be a sell."""
    src = open("resting_stops.py").read()
    assert "STOP_DIRECTION_STOP_UP" not in src
    assert '"side": "BUY"' not in src


def test_defaults_are_sane():
    assert 0 < rs.MIN_DISTANCE_PCT < 100
    assert 0 < rs.LIMIT_BAND_PCT < 10
    assert rs.MIN_STOP_USD > 0
    assert rs.REPLACE_MOVE_PCT > 0


# ------------------------------------------------- the trimmer's coins

def test_zec_is_excluded_by_default():
    """The account owner's decision: the trimmer has ZEC."""
    assert "ZEC" in rs.DEFAULT_EXCLUDED
    r = rs.plan_stop("ZEC", **{**ZEC, "excluded": {"ZEC"}})
    assert r["ok"] is False and r["reason"] == "TRIMMERS_COIN"
    assert "trimmer" in r["detail"]


def test_an_excluded_coin_is_refused_before_anything_else_is_checked():
    """Even a perfectly placeable stop is refused if the trimmer owns it."""
    r = rs.plan_stop("ZEC", units_available=1.42, price=1639.01, stop_price=1000.0,
                     excluded={"ZEC"})
    assert r["reason"] == "TRIMMERS_COIN"


def test_a_coin_near_the_limit_is_the_trimmers_without_being_listed():
    mine, why = rs.is_trimmers("FOO", share_pct=18.0, limit_pct=20.0, excluded=set())
    assert mine is True and "trimmer will want" in why


def test_a_small_coin_is_not_the_trimmers():
    mine, why = rs.is_trimmers("FOO", share_pct=4.0, limit_pct=20.0, excluded=set())
    assert mine is False and why is None


def test_the_exclusion_list_can_be_emptied():
    """Empty must mean 'exclude nothing', not 'fall back to the default'."""
    assert rs.excluded_assets("") == set()
    assert rs.excluded_assets(None) == set(rs.DEFAULT_EXCLUDED)
    assert rs.excluded_assets("ZEC, xrp") == {"ZEC", "XRP"}


def test_excluding_nothing_lets_zec_through():
    r = rs.plan_stop("ZEC", **{**ZEC, "excluded": set()})
    assert r["ok"] is True


def test_the_summary_names_who_is_excluded():
    s = rs.summarise([], "observe")
    assert "ZEC" in s["excluded"]


# ------------------------------------------- the worker's own guards

def test_worker_only_recognises_its_own_orders():
    src = open("resting_stops_worker.py").read()
    assert "COID_PREFIX" in src
    tree = ast.parse(src)
    # cancel must never be reachable without the prefix check that builds `mine`
    assert 'startswith(COID_PREFIX)' in src, \
        "the worker must identify its own orders before cancelling anything"


def test_worker_never_buys():
    src = open("resting_stops_worker.py").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value != "BUY", "the worker builds a BUY somewhere"


def test_worker_places_exactly_one_kind_of_order():
    src = open("resting_stops_worker.py").read()
    tree = ast.parse(src)
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "post"]
    # one to place, one to cancel
    assert len(posts) == 2, f"expected place+cancel, found {len(posts)} posts"


def test_worker_checks_the_mode():
    src = open("resting_stops_worker.py").read()
    assert "is_armed()" in src and "MODE_ARM" in src
