"""One rung of lookahead: the branch that is ABOUT to park its capital.

Written against the live 2026-09-28 book, where nine full branches held
$3,611.79 and XRP sat at 9/10 with $2,240.84 behind it. The old check
could describe the 44.6% perfectly and say nothing about the 27.7% that
was one fill from joining it.
"""
import invariants as inv

LIVE = [
    {"product_id": "ZEC-USD",  "allocated_usd": 2272.62, "open_slices": 7, "num_levels": 3,  "best_slice_net_pct": -0.78},
    {"product_id": "ETH-USD",  "allocated_usd": 400.00,  "open_slices": 4, "num_levels": 3,  "best_slice_net_pct": 0.45},
    {"product_id": "XRP-USD",  "allocated_usd": 2240.84, "open_slices": 9, "num_levels": 10, "best_slice_net_pct": -0.68},
    {"product_id": "XLM-USD",  "allocated_usd": 695.31,  "open_slices": 1, "num_levels": 3,  "best_slice_net_pct": 1.72},
]


def test_the_branch_one_rung_from_full_is_named_and_priced():
    r = inv.no_dead_capital(LIVE)
    assert r["nearly_full_branches"] == ["XRP-USD"], r["nearly_full_branches"]
    assert r["nearly_full_usd"] == 2240.84
    assert "ONE fill from" in r["detail"]
    assert "XRP-USD" in r["detail"]


def test_it_reports_where_the_parked_figure_would_land_not_just_the_delta():
    # The useful number is the destination: $3,611.79 -> $5,852.63, i.e.
    # 44.6% -> 72.3% of an $8,098.42 book. A reader should not have to add.
    r = inv.no_dead_capital(LIVE)
    assert r["full_usd"] == 2672.62          # ZEC + ETH in this fixture
    assert "$4,913.46" in r["detail"], r["detail"]


def test_a_full_branch_is_never_also_counted_as_nearly_full():
    # ZEC is 7/3 - over-full. n == lv - 1 must not catch it, and the two
    # buckets must never overlap, or the destination figure double-counts.
    r = inv.no_dead_capital(LIVE)
    assert not (set(r["full_branches"]) & set(r["nearly_full_branches"]))
    assert "ZEC-USD" not in r["nearly_full_branches"]


def test_a_single_level_branch_holding_nothing_is_one_rung_from_full():
    r = inv.no_dead_capital([
        {"product_id": "A-USD", "allocated_usd": 100.0, "open_slices": 0, "num_levels": 1,
         "best_slice_net_pct": None},
    ])
    assert r["nearly_full_branches"] == ["A-USD"]


def test_room_to_spare_raises_nothing(  ):
    r = inv.no_dead_capital([
        {"product_id": "B-USD", "allocated_usd": 100.0, "open_slices": 1, "num_levels": 5,
         "best_slice_net_pct": 2.0},
    ])
    assert r["nearly_full_usd"] == 0.0
    assert r["nearly_full_branches"] == []
    assert "ONE fill from" not in r["detail"]


def test_the_lookahead_is_present_on_every_verdict_including_a_pass():
    # Same rule the structural half already follows: a clean verdict must
    # not be the one place the warning is missing.
    for rows in (LIVE, [r for r in LIVE if r["product_id"] in ("XRP-USD", "XLM-USD")]):
        r = inv.no_dead_capital(rows)
        assert "nearly_full_usd" in r and "nearly_full_branches" in r, r["status"]
    passing = inv.no_dead_capital([r for r in LIVE if r["product_id"] in ("XRP-USD", "XLM-USD")])
    assert passing["status"] == inv.OK, passing["detail"]
    assert "ONE fill from" in passing["detail"]


def test_an_unreadable_branch_still_gets_the_lookahead():
    r = inv.no_dead_capital([
        {"product_id": "C-USD", "allocated_usd": 50.0, "open_slices": 3, "num_levels": 3,
         "best_slice_net_pct": None},
        {"product_id": "XRP-USD", "allocated_usd": 2240.84, "open_slices": 9, "num_levels": 10,
         "best_slice_net_pct": -0.68},
    ])
    assert r["status"] == inv.UNKNOWN
    assert "ONE fill from" in r["detail"]


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
