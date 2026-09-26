"""Tests for the tiering, and for the actions each tier is allowed.

The failure this file exists to catch: a position falling into a tier
whose rule would sell it when it should not be sold. Cash sorted into the
tail, an unpriced holding read as tiny, a stranded position counted as
covered - each of those is a wrong SALE, not a wrong label.
"""
import ast
from datetime import datetime, timedelta

import pytest

import auto_trim as at
import position_rules as pr


NOW = datetime(2026, 9, 26, 20, 0, 0)
T = 11243.82


def H(asset, usd):
    return {"asset": asset, "usd": usd}


# ------------------------------------------------------------- tiering

@pytest.mark.parametrize("usd,expect", [
    (2864.01, pr.ANCHOR),      # 25.47%
    (2367.44, pr.ANCHOR),      # 21.06%
    (2248.77, pr.ANCHOR),      # just over 20%
    (2248.00, pr.CORE),        # just under
    (1466.29, pr.CORE),        # 13.04%
    (562.20, pr.CORE),         # exactly 5.0%
    (561.00, pr.SATELLITE),
    (192.89, pr.SATELLITE),    # 1.72%
    (112.44, pr.SATELLITE),    # exactly 1.0%
    (111.00, pr.TAIL),
    (29.42, pr.TAIL),
    (5.00, pr.TAIL),           # exactly the venue floor
    (4.99, pr.STRANDED),
    (0.91, pr.STRANDED),
])
def test_tier_boundaries(usd, expect):
    assert pr.classify(usd, T) == expect


@pytest.mark.parametrize("sym", sorted(pr.STABLE))
def test_cash_is_never_a_position(sym):
    """A dollar sorted into the tail would be sold for dollars."""
    assert pr.classify(66.46, T, asset=sym) == pr.CASH
    assert pr.RULES[pr.CASH]["action"] == pr.DECIDE_NOTHING


def test_cash_tier_survives_any_size():
    for amt in (0.01, 66.46, 9000.0):
        assert pr.classify(amt, T, asset="USDC") == pr.CASH


def test_stable_set_matches_the_census():
    import account_census
    assert pr.STABLE == account_census.STABLE, \
        "position_rules and account_census disagree about what cash is"


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), float("inf"), -1])
def test_unpriced_is_blind_not_tiny(bad):
    assert pr.classify(bad, T) == pr.BLIND
    assert pr.RULES[pr.BLIND]["action"] == pr.DECIDE_NOTHING


@pytest.mark.parametrize("bad", [None, 0, -1, "x", float("nan")])
def test_unreadable_total_is_blind(bad):
    assert pr.classify(1000, bad) == pr.BLIND


def test_stranded_has_no_action():
    assert pr.RULES[pr.STRANDED]["action"] == pr.NO_ACTION_POSSIBLE


def test_only_two_tiers_can_sell():
    selling = {t for t, r in pr.RULES.items()
               if r["action"] in (pr.TRIM, pr.CONSOLIDATE)}
    assert selling == {pr.ANCHOR, pr.TAIL}


def test_exitable_floor_agrees_with_the_watch():
    import holdings_watch
    assert pr.MIN_EXITABLE_USD == holdings_watch.MIN_EXITABLE_USD


# ---------------------------------------------------------------- book

def _live_book():
    holdings = [H("ZEC", 2864.01), H("XRP", 2367.44), H("BTC", 1466.29),
                H("ETH", 1243.93), H("SHIB", 1065.88), H("XLM", 572.01),
                H("ALGO", 192.89), H("SOL", 125.12), H("ETC", 29.42),
                H("USD", 66.46), H("FLOCK", 0.91)]
    return pr.book(holdings, T, unpriced=[{"asset": "GAL", "units": 0.78}])


def test_every_holding_lands_in_exactly_one_tier():
    b = _live_book()
    assert b["positions"] == sum(v["count"] for v in b["tiers"].values())


def test_unpriced_assets_are_carried_into_the_book():
    b = _live_book()
    assert "GAL" in b["tiers"][pr.BLIND]["assets"]


def test_a_missing_holding_would_be_a_hole():
    """Nothing may be silently dropped: totals must reconcile."""
    b = _live_book()
    named = {a for v in b["tiers"].values() for a in v["assets"]}
    assert {r["asset"] for r in b["rows"]} == named


def test_headline_names_the_tail_and_the_stranded():
    b = _live_book()
    assert "tail" in b["headline"] and "stranded" in b["headline"]


def test_actionable_excludes_cash_and_blind():
    b = _live_book()
    acting = [r for r in b["rows"] if r["action"] in (pr.TRIM, pr.CONSOLIDATE)]
    assert all(r["asset"] not in pr.STABLE for r in acting)
    assert all(r["tier"] != pr.BLIND for r in acting)


# ------------------------------------------------------- plan_actions

def _holdings():
    return [H("ZEC", 2864.01), H("XRP", 2367.44), H("BTC", 1466.29),
            H("ETH", 1243.93), H("SHIB", 1065.88), H("XLM", 572.01),
            H("ALGO", 192.89), H("SOL", 125.12),
            H("ETC", 29.42), H("RNDR", 29.33), H("FLOKI", 23.13),
            H("HOPR", 22.15), H("USD", 66.46), H("FLOCK", 0.91)]


def test_plan_actions_covers_trims_and_the_tail():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    kinds = {p["kind"] for p in plans if p.get("act")}
    assert kinds == {"TRIM", "CONSOLIDATE"}


def test_consolidation_is_capped_per_pass():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    acting = [p for p in plans if p.get("act") and p["kind"] == "CONSOLIDATE"]
    assert len(acting) <= at.MAX_CONSOLIDATE_PER_PASS


def test_largest_tail_position_goes_first():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    acting = [p for p in plans if p.get("act") and p["kind"] == "CONSOLIDATE"]
    assert [p["asset"] for p in acting] == ["ETC", "RNDR", "FLOKI"]


def test_cash_is_never_actioned():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    assert all(p["asset"] not in pr.STABLE for p in plans if p.get("act"))


def test_stranded_is_never_actioned():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    assert all(p["asset"] != "FLOCK" for p in plans if p.get("act"))


def test_trims_and_consolidations_share_one_daily_budget():
    plans = at.plan_actions(_holdings(), T, now=NOW, max_daily_usd=700,
                            max_trim_usd=10_000)
    assert sum(p["trim_usd"] for p in plans if p.get("act")) <= 700 + 1e-9


def test_trims_are_funded_before_the_tail():
    """A $671 anchor trim reduces risk; a $29 tail sale tidies the book."""
    plans = at.plan_actions(_holdings(), T, now=NOW, max_daily_usd=700)
    acting = [p for p in plans if p.get("act")]
    assert any(p["kind"] == "TRIM" for p in acting)
    trimmed = sum(p["trim_usd"] for p in acting if p["kind"] == "TRIM")
    assert trimmed > 0


def test_cooldown_applies_to_consolidation_too():
    hist = [{"asset": "ETC", "usd": 29.42, "placed_at": NOW - timedelta(hours=3)}]
    plans = at.plan_actions(_holdings(), T, now=NOW, history=hist)
    etc = next(p for p in plans if p["asset"] == "ETC" and p.get("kind") == "CONSOLIDATE")
    assert etc["act"] is False and etc["reason"] == "COOLDOWN"


def test_unreadable_total_stops_every_action():
    plans = at.plan_actions(_holdings(), None, now=NOW)
    assert not any(p.get("act") for p in plans)


def test_tail_rows_carry_a_reason_even_when_blocked():
    plans = at.plan_actions(_holdings(), T, now=NOW)
    for p in plans:
        if p.get("kind") == "CONSOLIDATE" and not p.get("act"):
            assert p.get("reason") and p.get("detail")


def test_plan_trims_is_unchanged_by_the_new_path():
    a = at.plan_trims(_holdings(), T, now=NOW)
    b = [p for p in at.plan_actions(_holdings(), T, now=NOW)
         if p.get("kind") != "CONSOLIDATE"]
    assert [(p["asset"], p["trim_usd"]) for p in a if p["act"]] == \
           [(p["asset"], p["trim_usd"]) for p in b if p["act"] and p["kind"] == "TRIM"]


# --------------------------------------------------- structural guards

def test_rule_book_places_no_orders():
    tree = ast.parse(open("position_rules.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(bad in (n or "") for bad in
                               ("aiohttp", "requests", "httpx", "urllib", "coinbase")), \
                    f"rule book imports {n}"


def test_every_tier_has_a_rule_and_an_action():
    for tier in (pr.ANCHOR, pr.CORE, pr.SATELLITE, pr.TAIL,
                 pr.STRANDED, pr.CASH, pr.BLIND):
        r = pr.RULES[tier]
        assert r["action"] and r["rule"] and r["does"] and r["why"]


def test_tier_boundaries_are_ordered():
    assert 0 < pr.SATELLITE_MIN_PCT < pr.CORE_MIN_PCT < pr.ANCHOR_MIN_PCT < 100
    assert pr.MIN_EXITABLE_USD > 0


def test_consolidation_floor_is_the_venue_not_the_trim_floor():
    """A $23 tail position must be clearable.

    With MIN_TRIM_USD as the floor it never would be: it sits below the
    bar forever, which is the exact state consolidation exists to end.
    """
    assert at.MIN_CONSOLIDATE_USD < at.MIN_TRIM_USD
    assert at.MIN_CONSOLIDATE_USD == pr.MIN_EXITABLE_USD
    plans = at.plan_actions([H("ZEC", 2864.01), H("FLOKI", 23.13)], T, now=NOW)
    floki = next(p for p in plans if p["asset"] == "FLOKI" and p.get("kind") == "CONSOLIDATE")
    assert floki["act"] is True


def test_a_position_under_the_venue_floor_says_so_honestly():
    plans = at.plan_actions([H("ZEC", 2864.01), H("FLOCK", 0.91)], T, now=NOW)
    rows = [p for p in plans if p["asset"] == "FLOCK"]
    # FLOCK is STRANDED, so it never even reaches the consolidation list
    assert not any(p.get("act") for p in rows)


def test_each_asset_appears_once_in_plan_actions():
    """The ceiling pass and the tail pass must not both speak for one coin."""
    plans = at.plan_actions(_holdings(), T, now=NOW)
    names = [p["asset"] for p in plans]
    assert len(names) == len(set(names)), \
        f"duplicated: {sorted({n for n in names if names.count(n) > 1})}"
