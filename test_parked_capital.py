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
    assert "locked_at_venue first" in out["precedence"]
    assert "whether or not it is full" in out["precedence"]


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


# ── found by reading the endpoint's FIRST LIVE OUTPUT ──────────────────
#
# It reported parked $219.64 with $3,234.93 "unreadable", while
# grid_inventory_is_free reported $945.90 locked across 7 branches and
# no_dead_capital reported $3,912.68 full. Three numbers about the same
# fleet, all different. Two separate defects:
#
#   1. The endpoint passed RAW /grid-status branches, which carry
#      `slices` and no `open_slices` or `best_slice_net_pct` - those are
#      derived. Every full branch therefore looked unclassifiable.
#   2. The lock check sat BELOW the full/not-full split, so a locked
#      branch with a rung still free was never counted as locked.

def test_a_locked_branch_counts_even_with_a_rung_free():
    """XLM was 100% reserved. It cannot sell a unit whether or not its
    last rung has filled, and a cancel frees it either way."""
    out = split_by_cause([B("XLM-USD", 442.88, 2, 3, -0.4)], [L("XLM-USD", 442.88)])
    assert out["buckets"]["locked_at_venue"]["allocated_usd"] == 442.88
    assert out["buckets"]["one_fill_from_full"]["allocated_usd"] == 0
    assert out["actionable_today_usd"] == 442.88


def test_a_locked_branch_records_whether_it_was_also_full():
    out = split_by_cause([B("XLM-USD", 442.88, 2, 3, -0.4)], [L("XLM-USD", 442.88)])
    assert out["buckets"]["locked_at_venue"]["branches"][0]["was_full"] is False


def test_the_locked_total_matches_the_invariant_that_found_them():
    """Two numbers about the same condition, computed in two places,
    that disagreed live: $219.64 here against $945.90 there."""
    locked = [L("XLM-USD", 442.88), L("ALGO-USD", 152.67), L("LINK-USD", 99.32),
              L("SOL-USD", 92.42), L("NEAR-USD", 78.77), L("ACH-USD", 70.41),
              L("PRIME-USD", 9.43)]
    branches = [B("XLM-USD", 500.0, 2, 3, -0.4), B("ALGO-USD", 200.0, 3, 3, -0.5),
                B("LINK-USD", 300.0, 2, 3, -1.0), B("SOL-USD", 250.0, 3, 3, -1.5),
                B("NEAR-USD", 220.0, 2, 3, -0.9), B("ACH-USD", 180.0, 3, 3, -4.3),
                B("PRIME-USD", 150.0, 1, 3, -1.4)]
    out = split_by_cause(branches, locked)
    assert len(out["buckets"]["locked_at_venue"]["branches"]) == 7
    assert out["buckets"]["locked_at_venue"]["coin_reserved_usd"] == pytest.approx(945.90)


def test_a_priced_full_branch_is_never_unreadable():
    """The live run put every full branch in `unreadable` because the
    key it prices on was never on a raw branch."""
    out = split_by_cause([B("ZEC-USD", 2272.62, 3, 3, -4.63)], [])
    assert out["buckets"]["unreadable"]["allocated_usd"] == 0
    assert out["buckets"]["underwater"]["allocated_usd"] == 2272.62


# ── the shared derivation both pages now read ──────────────────────────

def test_branch_rows_derives_the_fields_parked_capital_needs():
    import invariants
    status = {"branches": [{
        "product_id": "ZEC-USD", "allocated_usd": 2272.62, "num_levels": 3,
        "slices": [{"unrealized_net_pct": -0.0463},
                   {"unrealized_net_pct": -0.0501},
                   {"unrealized_net_pct": -0.0402}],
    }]}
    row = invariants.branch_rows(status)[0]
    assert row["open_slices"] == 3
    assert row["best_slice_net_pct"] == pytest.approx(-4.02)


def test_branch_rows_feeds_split_by_cause_without_losing_anything():
    import invariants
    status = {"branches": [{
        "product_id": "ZEC-USD", "allocated_usd": 2272.62, "num_levels": 3,
        "slices": [{"unrealized_net_pct": -0.0463}] * 3,
    }]}
    out = split_by_cause(invariants.branch_rows(status), [])
    assert out["buckets"]["underwater"]["allocated_usd"] == 2272.62
    assert out["buckets"]["unreadable"]["allocated_usd"] == 0


def test_branch_rows_keeps_an_unpriced_slice_as_none_not_zero():
    """0 is not less than 0, so a failed price read would look exactly
    like a branch sitting at break-even."""
    import invariants
    status = {"branches": [{
        "product_id": "X-USD", "allocated_usd": 10.0, "num_levels": 2,
        "slices": [{"unrealized_net_pct": None}, {"unrealized_net_pct": None}],
    }]}
    assert invariants.branch_rows(status)[0]["best_slice_net_pct"] is None


def test_the_endpoint_uses_the_shared_derivation():
    src = _ast.get_source_segment(_ROUTER_SRC, _endpoint())
    assert "inv.branch_rows(status)" in src
    assert 'status.get("branches")' not in src, \
        "the endpoint is reading raw branches again"
