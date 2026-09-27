"""Flat is not idle, and a rotator that cannot tell them apart churns.

Measured on the live fleet, three branches were flat at once: ONDO (traded
4h ago), TIA (5h ago), BONK (18 DAYS ago). The first two are grids between
fills; the third is capital in the wrong coin. crypto_grid_bot's rotation
keys on branch.created_at and flatness, so it would treat all three alike.
"""
from datetime import datetime, timedelta, timezone

import pytest

import idle_capital as ic

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def branch(pid, usd=69.0, open_slices=0, bot=None, created=None):
    return {"product_id": pid, "bot_name": bot or f"grid_{pid}", "allocated_usd": usd,
            "open_slices": open_slices, "created_at": created}


def trade(bot, when):
    return {"bot_name": bot, "closed_at": when}


# ---------------------------------------------------------------- the split

def test_a_branch_holding_slices_is_working_however_long_ago_it_traded():
    r = ic.classify(branch("ETH", open_slices=3), None, now=NOW)
    assert r["state"] == ic.WORKING
    assert "in the market" in r["why"]


def test_flat_but_traded_hours_ago_is_waiting_not_idle():
    r = ic.classify(branch("ONDO"), NOW - timedelta(hours=4.6), now=NOW)
    assert r["state"] == ic.WAITING
    assert "between" in r["why"]


def test_flat_and_silent_for_weeks_is_stale():
    r = ic.classify(branch("BONK"), NOW - timedelta(days=18.4), now=NOW)
    assert r["state"] == ic.STALE
    assert r["idle_hours"] == pytest.approx(18.4 * 24, abs=1)


def test_the_boundary_is_the_horizon_window():
    """72h is horizon_study's own 92.5% figure, not a round number."""
    assert ic.classify(branch("X"), NOW - timedelta(hours=71.9), now=NOW)["state"] == ic.WAITING
    assert ic.classify(branch("X"), NOW - timedelta(hours=72.1), now=NOW)["state"] == ic.STALE


def test_the_live_three_are_classified_the_way_the_fleet_showed_them():
    bs = [branch("ONDO", 69.58, bot="g4"), branch("TIA", 69.67, bot="g7"),
          branch("BONK", 69.23, bot="g3")]
    ts = [trade("g4", ago(hours=4.6)), trade("g7", ago(hours=5.8)),
          trade("g3", ago(days=18.4))]
    r = ic.report(bs, ts, now=NOW)
    got = {x["product_id"]: x["state"] for x in r["branches"]}
    assert got == {"ONDO": ic.WAITING, "TIA": ic.WAITING, "BONK": ic.STALE}
    assert r["stale_usd"] == 69.23


def test_a_naive_flat_check_would_have_moved_all_three():
    """The thing this module exists to prevent, stated as a test."""
    bs = [branch("ONDO", bot="g4"), branch("TIA", bot="g7"), branch("BONK", bot="g3")]
    ts = [trade("g4", ago(hours=4)), trade("g7", ago(hours=5)), trade("g3", ago(days=18))]
    flat_only = [b for b in bs if b["open_slices"] == 0]
    r = ic.report(bs, ts, now=NOW)
    assert len(flat_only) == 3
    assert len(r["stale"]) == 1


# ------------------------------------------------- a gap is not a zero

def test_a_branch_missing_from_a_TRUNCATED_window_is_not_called_dead():
    """The trade feed is capped. Absence from it is not proof of silence -
    the same mistake as an unread census drawing a crash out of a gap."""
    bs = [branch("OLD", bot="gX")]
    ts = [trade("other", ago(days=3))]
    r = ic.report(bs, ts, now=NOW, total_trade_count=500)
    assert r["window_truncated"] is True
    row = r["branches"][0]
    assert row["state"] == ic.UNKNOWN_AGE
    assert row["at_least"] is True
    assert "floor, not a measurement" in row["why"]


def test_an_untruncated_window_can_judge_absence_against_creation():
    bs = [branch("OLD", bot="gX", created=ago(days=20))]
    ts = [trade("other", ago(days=3))]
    r = ic.report(bs, ts, now=NOW, total_trade_count=1)
    assert r["window_truncated"] is False
    assert r["branches"][0]["state"] == ic.STALE


def test_a_brand_new_branch_is_never_stale():
    r = ic.classify(branch("NEW", created=ago(hours=2)), None, now=NOW)
    assert r["state"] == ic.TOO_NEW


def test_a_new_branch_inside_grace_is_not_judged_even_with_no_creation_window():
    r = ic.classify(branch("NEW", created=ago(hours=1)), None, now=NOW, window_truncated=True)
    assert r["state"] == ic.TOO_NEW


# ------------------------------------------------------------- the report

def test_money_is_totalled_per_state():
    bs = [branch("A", 100.0, open_slices=3, bot="a"), branch("B", 50.0, bot="b"),
          branch("C", 25.0, bot="c")]
    ts = [trade("b", ago(hours=1)), trade("c", ago(days=9))]
    r = ic.report(bs, ts, now=NOW)
    assert r["by_state"][ic.WORKING] == {"count": 1, "usd": 100.0}
    assert r["by_state"][ic.WAITING] == {"count": 1, "usd": 50.0}
    assert r["by_state"][ic.STALE] == {"count": 1, "usd": 25.0}


def test_stale_branches_sort_first():
    bs = [branch("A", open_slices=3, bot="a"), branch("B", bot="b")]
    ts = [trade("b", ago(days=10))]
    assert ic.report(bs, ts, now=NOW)["branches"][0]["product_id"] == "B"


def test_nothing_stale_says_so_plainly():
    bs = [branch("A", bot="a")]
    r = ic.report(bs, [trade("a", ago(hours=2))], now=NOW)
    assert r["stale"] == [] and "No branch is past" in r["detail"]


def test_it_rotates_nothing():
    r = ic.report([branch("A", bot="a")], [], now=NOW)
    assert r["rotates_nothing"] is True


def test_it_cannot_reach_the_network_or_the_database():
    import ast
    tree = ast.parse(open("idle_capital.py").read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & {"aiohttp", "requests", "sqlalchemy", "models", "httpx"})


def test_it_never_calls_a_rotation_helper():
    """Asserted on CALLS in the parsed tree, not on the source text.

    The docstring of this module explains what rotation is and why it is
    not done here, so any substring search for "rotate" matches the
    explanation and reads the warning as the offence - the same trap
    test_status_strip already documents for its own brace matcher.
    """
    import ast
    tree = ast.parse(open("idle_capital.py").read())
    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    forbidden = {"withdraw_from_grid_branch", "move_cash_between_grid_branches",
                 "place_order", "place_market_sell", "run_grid_auto_rotate_sweep",
                 "_maybe_rotate_one_grid_branch", "add_cash_to_grid_branch"}
    assert not (called & forbidden), sorted(called & forbidden)


def test_bad_timestamps_do_not_crash_it():
    bs = [branch("A", bot="a")]
    ts = [{"bot_name": "a", "closed_at": "not-a-date"}, {"bot_name": None}, "junk"]
    r = ic.report(bs, ts, now=NOW)
    assert r["branches"][0]["state"] in (ic.UNKNOWN_AGE, ic.STALE, ic.TOO_NEW)


def test_a_missing_open_slices_key_is_treated_as_flat_not_as_working():
    """Absent must not read as 'holding something' - that would hide a
    stale branch behind a missing field."""
    r = ic.classify({"bot_name": "a", "allocated_usd": 10.0},
                    NOW - timedelta(days=9), now=NOW)
    assert r["state"] == ic.STALE


def test_the_window_is_owner_settable():
    assert ic.classify(branch("X"), NOW - timedelta(hours=10), now=NOW,
                       stale_after_hours=6)["state"] == ic.STALE
