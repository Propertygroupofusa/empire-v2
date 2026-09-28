"""Parked capital split by cause - and the ways that split can lie."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parked_capital import split_by_cause  # noqa: E402


def B(pid, alloc, slices, levels, pct=None):
    return {"product_id": pid, "allocated_usd": alloc, "open_slices": slices,
            "num_levels": levels, "best_slice_net_pct": pct}


def L(pid, usd, pct=100.0):
    return {"product_id": pid, "locked_usd": usd, "locked_pct": pct}


# ── the split itself ───────────────────────────────────────────────────

def test_no_branches_is_unreadable_not_zero():
    out = split_by_cause([])
    assert out["readable"] is False


def test_a_full_underwater_branch_is_parked_by_price():
    out = split_by_cause([B("ZEC-USD", 2272.62, 3, 3, -4.63)], [])
    assert out["buckets"]["underwater"]["allocated_usd"] == 2272.62
    assert out["buckets"]["underwater"]["actionable_today"] is False


def test_a_full_profitable_branch_is_the_grid_working():
    out = split_by_cause([B("BTC-USD", 500.0, 3, 3, 0.8)], [])
    assert out["buckets"]["full_but_profitable"]["allocated_usd"] == 500.0
    assert out["buckets"]["underwater"]["allocated_usd"] == 0


def test_a_branch_with_room_left_is_not_parked_at_all():
    out = split_by_cause([B("SOL-USD", 300.0, 1, 3, -2.0)], [])
    assert out["parked_now_usd"] == 0


def test_one_fill_from_full_is_named_but_not_counted_as_parked():
    out = split_by_cause([B("XRP-USD", 2240.84, 2, 3, -1.5)], [])
    assert out["parked_now_usd"] == 0
    assert out["buckets"]["one_fill_from_full"]["allocated_usd"] == 2240.84
    assert out["parked_if_every_near_branch_fills_usd"] == 2240.84


def test_a_locked_branch_is_the_one_bucket_anyone_can_act_on():
    out = split_by_cause([B("XLM-USD", 600.0, 3, 3, -0.4)], [L("XLM-USD", 442.88)])
    assert out["buckets"]["locked_at_venue"]["allocated_usd"] == 600.0
    assert out["actionable_today_usd"] == 600.0
    assert out["buckets"]["locked_at_venue"]["actionable_today"] is True


# ── the double-counting trap ───────────────────────────────────────────

def test_a_branch_that_is_both_locked_and_underwater_is_counted_once():
    """Never add two overlapping sets; take the union. A branch parked
    twice over is still one branch's worth of capital."""
    out = split_by_cause([B("ACH-USD", 400.0, 3, 3, -4.30)], [L("ACH-USD", 70.41, 62.0)])
    assert out["buckets"]["locked_at_venue"]["allocated_usd"] == 400.0
    assert out["buckets"]["underwater"]["allocated_usd"] == 0
    assert out["parked_now_usd"] == 400.0


def test_the_precedence_is_stated_not_left_to_be_inferred():
    out = split_by_cause([B("ACH-USD", 400.0, 3, 3, -4.30)], [L("ACH-USD", 70.41)])
    assert "locked_at_venue > underwater" in out["precedence"]


def test_the_buckets_sum_to_the_parked_total():
    branches = [B("A-USD", 100.0, 3, 3, -5.0), B("B-USD", 200.0, 3, 3, 1.0),
                B("C-USD", 300.0, 3, 3, -0.2), B("D-USD", 400.0, 2, 3)]
    out = split_by_cause(branches, [L("C-USD", 50.0)])
    bk = out["buckets"]
    total = (bk["locked_at_venue"]["allocated_usd"]
             + bk["underwater"]["allocated_usd"]
             + bk["full_but_profitable"]["allocated_usd"])
    assert total == out["parked_now_usd"] == 600.0


# ── the two denominators ───────────────────────────────────────────────

def test_committed_capital_and_reserved_coin_are_kept_apart():
    """allocated_usd and locked_usd measure different things. Adding them
    would invent capital that does not exist."""
    out = split_by_cause([B("XLM-USD", 600.0, 3, 3, -0.4)], [L("XLM-USD", 442.88)])
    b = out["buckets"]["locked_at_venue"]
    assert b["allocated_usd"] == 600.0
    assert b["coin_reserved_usd"] == 442.88
    assert out["parked_now_usd"] == 600.0      # not 1042.88


def test_the_payload_says_why_the_two_are_not_added():
    out = split_by_cause([B("XLM-USD", 600.0, 3, 3)], [L("XLM-USD", 442.88)])
    assert "never added together" in out["two_denominators"]


# ── gaps stay gaps ─────────────────────────────────────────────────────

def test_an_unpriced_full_branch_is_unreadable_not_break_even():
    """This is the bug no_dead_capital already shipped once: an unpriced
    slice arrived as 0, and 0 is not less than 0, so it read exactly like
    a branch sitting at break-even."""
    out = split_by_cause([B("PEPE-USD", 250.0, 3, 3, None)], [])
    assert out["buckets"]["unreadable"]["allocated_usd"] == 250.0
    assert out["buckets"]["full_but_profitable"]["allocated_usd"] == 0
    assert out["buckets"]["underwater"]["allocated_usd"] == 0


def test_unreadable_branches_are_flagged_not_quietly_dropped():
    out = split_by_cause([B("PEPE-USD", 250.0, 3, 3, None)], [])
    assert "NO cause bucket" in out["caveat_unreadable"]


def test_a_branch_with_no_slice_counts_is_unreadable():
    out = split_by_cause([{"product_id": "X-USD", "allocated_usd": 10.0}], [])
    assert out["buckets"]["unreadable"]["allocated_usd"] == 10.0


def test_an_unread_lock_state_is_not_an_absent_lock():
    """Passing None must not quietly produce a clean locked bucket - that
    would tell the owner nothing is actionable when nothing was checked."""
    out = split_by_cause([B("XLM-USD", 600.0, 3, 3, -0.4)])
    assert out["lock_state_read"] is False
    assert "not an absent lock" in out["caveat"]


def test_an_empty_lock_list_does_mean_nothing_is_locked():
    out = split_by_cause([B("XLM-USD", 600.0, 3, 3, -0.4)], [])
    assert out["lock_state_read"] is True
    assert "caveat" not in out


def test_a_near_exit_branch_is_not_called_underwater():
    """Marginally under but within a round trip of a profitable exit is a
    grid between fills - the same line no_dead_capital draws."""
    out = split_by_cause([B("TIA-USD", 58.22, 3, 3, -0.30)], [])
    assert out["buckets"]["underwater"]["allocated_usd"] == 0
    assert out["buckets"]["full_but_profitable"]["allocated_usd"] == 58.22


# ── the numbers the owner actually asked about ─────────────────────────

def test_the_actionable_figure_excludes_everything_price_controls():
    branches = [B("XLM-USD", 442.88, 3, 3, -0.4), B("ALGO-USD", 152.67, 3, 3, -0.5),
                B("ZEC-USD", 2272.62, 3, 3, -4.63)]
    locked = [L("XLM-USD", 442.88), L("ALGO-USD", 152.67)]
    out = split_by_cause(branches, locked)
    assert out["actionable_today_usd"] == 595.55
    assert out["buckets"]["underwater"]["allocated_usd"] == 2272.62


def test_each_bucket_says_what_would_release_it():
    out = split_by_cause([B("A-USD", 1.0, 3, 3, -9.0)], [])
    for name, bucket in out["buckets"].items():
        assert bucket["moves_when"], f"{name} does not say what would move it"


# ── the endpoint must stay read-only, and must not re-derive the lock ───

import ast as _ast  # noqa: E402

_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "routers", "trading_dashboard.py")
with open(_ROUTER, encoding="utf-8") as _fh:
    _ROUTER_SRC = _fh.read()
_ROUTER_TREE = _ast.parse(_ROUTER_SRC)


def _endpoint():
    for node in _ast.walk(_ROUTER_TREE):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == "get_parked_capital":
            return node
    raise AssertionError("get_parked_capital endpoint not found")


def test_the_endpoint_exists_and_is_a_get():
    """Claimed three times in this session that something was flippable
    from the dashboard with no route behind it. Grep before claiming."""
    assert '@router.get("/parked-capital")' in _ROUTER_SRC


def test_the_endpoint_places_and_cancels_nothing():
    src = _ast.get_source_segment(_ROUTER_SRC, _endpoint())
    for forbidden in ("place_order", "cancel_order", "free_locked",
                      "close_branch", "reconcile_slices", "session.post",
                      "session.delete"):
        assert forbidden not in src, \
            f"a read-only endpoint must not reference {forbidden}"


def test_the_endpoint_reuses_the_invariant_rather_than_recomputing_the_lock():
    """Two numbers that must agree, computed in two places from two
    sources, with nothing forcing them to match - the recurring bug."""
    src = _ast.get_source_segment(_ROUTER_SRC, _endpoint())
    assert "inv.grid_inventory_is_free(" in src
    assert "available_units" not in src, \
        "the endpoint is recomputing the lock instead of reading the invariant"


def test_an_unreadable_lock_is_passed_as_none_not_as_empty():
    """None means 'not read'; [] means 'read, nothing locked'. Collapsing
    them would report nothing actionable when nothing was checked."""
    src = _ast.get_source_segment(_ROUTER_SRC, _endpoint())
    assert "locked_positions = None" in src
    assert "locked_positions = []" in src
