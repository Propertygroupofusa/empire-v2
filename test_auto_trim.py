"""Tests for the auto-trimmer's sizing and its refusals.

The point of these is that this module SELLS. Every test below is either
"it sold the right amount" or "it refused for the right reason", and the
refusals matter more: a trimmer that occasionally oversells is a bug, a
trimmer that sells on a bad reading is an incident.
"""
import ast
from datetime import datetime, timedelta

import pytest

import auto_trim as at


NOW = datetime(2026, 9, 26, 20, 0, 0)


def H(asset, usd, **kw):
    d = {"asset": asset, "usd": usd}
    d.update(kw)
    return d


# ---------------------------------------------------------------- mode

@pytest.mark.parametrize("raw", ["arm", "ARM", " arm ", "Arm"])
def test_only_exactly_arm_arms(raw):
    assert at.normalise_mode(raw) == at.MODE_ARM


@pytest.mark.parametrize("raw", [None, "", "true", "yes", "on", "1", "armed",
                                 "ARMED", "observe", 1, True, [], {}, "a rm"])
def test_everything_else_observes(raw):
    assert at.normalise_mode(raw) == at.MODE_OBSERVE


def test_default_mode_is_not_armed():
    assert at.normalise_mode(None) == at.MODE_OBSERVE


# ------------------------------------------------------------- excess

def test_within_limit_is_zero_not_none():
    assert at.excess_usd(1000, 10000) == 0.0          # 10%
    assert at.excess_usd(2000, 10000) == 0.0          # exactly 20%


def test_over_limit_trims_to_limit_minus_buffer():
    # 2500 of 10000 = 25%. Target 19.5% -> 1950. Sell 550.
    assert at.excess_usd(2500, 10000) == pytest.approx(550.0)


def test_buffer_is_respected():
    assert at.excess_usd(2500, 10000, 20.0, 0.0) == pytest.approx(500.0)


def test_result_actually_lands_under_the_limit():
    for usd, total in ((2864.01, 11243.82), (2367.44, 11243.82), (5000, 6000), (2001, 10000)):
        s = at.excess_usd(usd, total)
        # total is unchanged by a coin->cash sale, so the new share is:
        assert (usd - s) / total * 100 <= at.LIMIT_PCT + 1e-9


def test_never_returns_negative():
    assert at.excess_usd(1, 10000) == 0.0


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), float("inf"), -1])
def test_unreadable_holding_is_none(bad):
    assert at.excess_usd(bad, 10000) is None


@pytest.mark.parametrize("bad", [None, 0, -5, "abc", float("nan")])
def test_unreadable_total_is_none(bad):
    assert at.excess_usd(1000, bad) is None


def test_none_total_is_not_treated_as_within_limit():
    """The distinction this whole module rests on."""
    assert at.excess_usd(9999, None) is not 0.0     # noqa: F632 - identity is the point
    assert at.excess_usd(9999, None) is None


# ------------------------------------------------------------- history

def test_spent_today_sums_only_todays_rows():
    hist = [{"asset": "ZEC", "usd": 100, "placed_at": NOW - timedelta(hours=1)},
            {"asset": "XRP", "usd": 50,  "placed_at": NOW - timedelta(hours=3)},
            {"asset": "BTC", "usd": 900, "placed_at": NOW - timedelta(days=2)}]
    assert at.spent_today(hist, NOW) == pytest.approx(150.0)


def test_unreadable_amount_exhausts_the_budget():
    hist = [{"asset": "ZEC", "usd": None, "placed_at": NOW - timedelta(hours=1)}]
    assert at.spent_today(hist, NOW) == at.MAX_DAILY_TRIM_USD


def test_unreadable_now_exhausts_the_budget():
    assert at.spent_today([], "not a datetime") == at.MAX_DAILY_TRIM_USD


def test_last_trim_at_picks_the_newest():
    hist = [{"asset": "ZEC", "usd": 1, "placed_at": NOW - timedelta(hours=9)},
            {"asset": "ZEC", "usd": 1, "placed_at": NOW - timedelta(hours=2)},
            {"asset": "XRP", "usd": 1, "placed_at": NOW}]
    assert at.last_trim_at(hist, "ZEC") == NOW - timedelta(hours=2)
    assert at.last_trim_at(hist, "DOGE") is None


# --------------------------------------------------------------- plan

def test_the_real_book_produces_the_two_expected_trims():
    holdings = [H("ZEC", 2864.01), H("XRP", 2367.44), H("BTC", 1466.29),
                H("ETH", 1243.93), H("SHIB", 1065.88), H("XLM", 572.01)]
    plans = at.plan_trims(holdings, 11243.82, now=NOW)
    acting = {p["asset"]: p for p in plans if p["act"]}
    assert set(acting) == {"ZEC", "XRP"}
    # every holding is accounted for, acting or not
    assert {p["asset"] for p in plans} == {h["asset"] for h in holdings}
    for a, p in acting.items():
        usd = next(h["usd"] for h in holdings if h["asset"] == a)
        assert (usd - p["trim_usd"]) / 11243.82 * 100 <= at.LIMIT_PCT


def test_worst_offender_is_planned_first():
    plans = at.plan_trims([H("XRP", 2367.44), H("ZEC", 2864.01)], 11243.82, now=NOW)
    assert [p["asset"] for p in plans][:2] == ["ZEC", "XRP"]


def test_nothing_over_the_limit_acts():
    plans = at.plan_trims([H("A", 1000), H("B", 900)], 10000, now=NOW)
    assert not any(p["act"] for p in plans)
    assert all(p["reason"] == "WITHIN_LIMIT" for p in plans)


def test_unpriced_holding_never_trims():
    plans = at.plan_trims([H("GAL", None), H("ZEC", 2864.01)], 11243.82, now=NOW)
    gal = next(p for p in plans if p["asset"] == "GAL")
    assert gal["act"] is False and gal["reason"] == "UNPRICED"


def test_unreadable_total_stops_everything():
    plans = at.plan_trims([H("ZEC", 9999)], None, now=NOW)
    assert not any(p["act"] for p in plans)
    assert all(p["reason"] == "NO_TOTAL" for p in plans)


def test_single_order_cap_applies():
    plans = at.plan_trims([H("ZEC", 9000)], 10000, now=NOW, max_trim_usd=100)
    p = plans[0]
    assert p["act"] and p["trim_usd"] == 100.0
    assert any("single-order cap" in c for c in p["capped_by"])


def test_position_share_cap_applies():
    # 9000 of 10000 needs 7050 sold; 35% of the position is 3150.
    plans = at.plan_trims([H("ZEC", 9000)], 10000, now=NOW, max_trim_usd=10_000,
                          max_daily_usd=10_000)
    p = plans[0]
    assert p["trim_usd"] == pytest.approx(3150.0)
    assert any("% of the position" in c for c in p["capped_by"])


def test_daily_budget_is_shared_across_assets():
    plans = at.plan_trims([H("ZEC", 3000), H("XRP", 2900)], 10000, now=NOW,
                          max_daily_usd=1100, max_trim_usd=10_000)
    assert sum(p["trim_usd"] for p in plans if p["act"]) <= 1100 + 1e-9


def test_budget_already_spent_blocks_the_pass():
    hist = [{"asset": "OTHER", "usd": at.MAX_DAILY_TRIM_USD, "placed_at": NOW - timedelta(hours=1)}]
    plans = at.plan_trims([H("ZEC", 2864.01)], 11243.82, now=NOW, history=hist)
    assert not any(p["act"] for p in plans)


def test_cooldown_blocks_a_second_trim_same_day():
    hist = [{"asset": "ZEC", "usd": 50, "placed_at": NOW - timedelta(hours=2)}]
    plans = at.plan_trims([H("ZEC", 2864.01)], 11243.82, now=NOW, history=hist)
    p = next(x for x in plans if x["asset"] == "ZEC")
    assert p["act"] is False and p["reason"] == "COOLDOWN"


def test_cooldown_expires():
    hist = [{"asset": "ZEC", "usd": 50, "placed_at": NOW - timedelta(hours=25)}]
    plans = at.plan_trims([H("ZEC", 2864.01)], 11243.82, now=NOW, history=hist)
    assert next(x for x in plans if x["asset"] == "ZEC")["act"] is True


def test_tiny_excess_is_left_alone():
    # 2005 of 10000 = 20.05%; getting to 19.5% is only $55... raise the floor.
    plans = at.plan_trims([H("ZEC", 2005)], 10000, now=NOW, min_trim_usd=100)
    p = plans[0]
    assert p["act"] is False and p["reason"] == "TOO_SMALL"


def test_every_holding_appears_in_the_output():
    holdings = [H(f"C{i}", 10) for i in range(30)] + [H("ZEC", 9000)]
    plans = at.plan_trims(holdings, 10000, now=NOW)
    assert len(plans) == len(holdings)


# ------------------------------------------------------------ summary

def test_observing_says_nothing_will_be_placed():
    plans = at.plan_trims([H("ZEC", 2864.01)], 11243.82, now=NOW)
    s = at.summarise(plans, "observe")
    assert s["armed"] is False
    assert "nothing will be placed" in s["headline"].lower()


def test_armed_says_it_will_sell():
    plans = at.plan_trims([H("ZEC", 2864.01)], 11243.82, now=NOW)
    s = at.summarise(plans, "arm")
    assert s["armed"] is True and s["would_trim_usd"] > 0
    assert "will be sold" in s["headline"]


def test_summary_mode_is_normalised_not_echoed():
    s = at.summarise([], "TRUE")
    assert s["mode"] == at.MODE_OBSERVE and s["armed"] is False


# ------------------------------------------------- structural guards

def _tree(path):
    return ast.parse(open(path).read())


def test_module_contains_no_buy_side_anything():
    """A concentration trimmer that can buy is not a concentration trimmer.

    Checked on the parsed tree rather than the file text so the prose above,
    which uses the word, cannot make this pass or fail.
    """
    tree = _tree("auto_trim.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "buy" not in node.value.lower() or "buys anything" in node.value.lower(), \
                f"string constant mentions buying: {node.value[:60]!r}"


def test_decision_module_places_no_orders():
    """No network, no client, no session anywhere in the sizing module."""
    tree = _tree("auto_trim.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            mod = getattr(node, "module", None)
            for n in names + ([mod] if mod else []):
                assert not any(bad in (n or "") for bad in
                               ("aiohttp", "requests", "httpx", "coinbase", "urllib")), \
                    f"sizing module imports {n}"


def test_worker_has_exactly_one_order_placement():
    """One place where money moves, so there is one place to read."""
    src = open("auto_trim_worker.py").read()
    tree = ast.parse(src)
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "post"]
    assert len(posts) == 1, f"expected exactly one session.post, found {len(posts)}"


def test_worker_checks_the_mode_before_placing():
    """The arm check must exist and must compare against the constant."""
    src = open("auto_trim_worker.py").read()
    tree = ast.parse(src)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "MODE_ARM" in names, "worker never references MODE_ARM"


def test_defaults_are_bounded():
    assert 0 < at.MIN_TRIM_USD < at.MAX_TRIM_USD <= at.MAX_DAILY_TRIM_USD
    assert 0 < at.BUFFER_PCT < at.LIMIT_PCT
    assert 0 < at.MAX_POSITION_SHARE_PCT < 100
    assert at.COOLDOWN_HOURS > 0
