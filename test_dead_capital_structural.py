"""A figure that moves 80% in a quarter hour is a price reading.

no_dead_capital reported, on 2026-09-28:

    12:38Z   $3,156.23 across 8 branches
    12:55Z     $588.59 across 4
    13:14Z     $717.07

Nothing was bought or sold to explain that. The check requires BOTH
"full on rungs" AND "underwater past the near-exit band", and the second
condition moves with every tick, so the dollar figure inherits its
volatility entirely.

The first condition does not move. Measured at 13:14Z, NINE branches
were full on their rungs and so could not buy at any price:

    ZEC 7/3 $2,272.62   ETH 4/3 $400.00   SHIB 7/3 $267.37
    BCH 4/3   $171.75   PEPE 7/3 $166.54   SOL 4/3 $128.48
    ACH 3/3    $91.16   ONDO 3/3  $58.31   BTC 3/3  $55.56

$3,611.79 - 44.6% of allocated capital - structurally unable to deploy
another dollar until a slice sells. That is the number a reader needs,
and it was invisible: the check only ever reported the price-filtered
subset of it.

Same shape as the maker_only_holds fix earlier today. There, a frozen
number could not show recency. Here, a volatile number hides a stable
one. Both cases: report the thing that answers the question, beside the
thing that was already there.

THE VERDICT IS UNCHANGED. Full-on-rungs alone is not a failure - such a
branch can still sell at a profit, which is it working. FAIL still means
full AND underwater.
"""
import invariants as inv


def br(pid, usd, n, lv, best):
    return {"product_id": pid, "allocated_usd": usd,
            "open_slices": n, "num_levels": lv, "best_slice_net_pct": best}


# The live fleet at 13:14Z, the nine full branches plus two that are not.
LIVE = [
    br("ZEC-USD", 2272.62, 7, 3, -0.71), br("ETH-USD", 400.00, 4, 3, -0.60),
    br("SHIB-USD", 267.37, 7, 3, -2.20), br("BCH-USD", 171.75, 4, 3, -6.90),
    br("PEPE-USD", 166.54, 7, 3, 0.71), br("SOL-USD", 128.48, 4, 3, -0.97),
    br("ACH-USD", 91.16, 3, 3, -4.60), br("ONDO-USD", 58.31, 3, 3, -3.20),
    br("BTC-USD", 55.56, 3, 3, -0.70),
    br("XLM-USD", 691.51, 2, 3, -0.77), br("LINK-USD", 129.23, 2, 3, 1.34),
]


def test_the_structural_figure_is_reported_at_all():
    """It was not. Only the price-filtered subset of it ever appeared."""
    r = inv.no_dead_capital(LIVE)
    assert "full_usd" in r
    assert r["full_usd"] == round(sum(b["allocated_usd"] for b in LIVE
                                      if b["open_slices"] >= b["num_levels"]), 2)
    assert r["full_usd"] == 3611.79


def test_the_structural_figure_counts_a_full_branch_that_is_in_profit():
    """PEPE is full at +0.71%. It cannot buy either - being green does not
    give a branch a spare rung."""
    r = inv.no_dead_capital(LIVE)
    assert "PEPE-USD" in r["full_branches"]
    assert "PEPE-USD" not in r.get("branches", [])


def test_the_two_figures_are_not_the_same_number():
    r = inv.no_dead_capital(LIVE)
    assert r["full_usd"] > r["stuck_usd"] * 3, (
        "if these track each other the split has achieved nothing")


def test_the_detail_says_which_one_moves_on_price():
    r = inv.no_dead_capital(LIVE)
    d = r["detail"].lower()
    assert "price" in d
    assert "cannot buy" in d or "could not buy" in d


def test_the_structural_figure_is_reported_even_when_nothing_is_stuck():
    """A clean verdict must not hide that half the fleet cannot buy."""
    green = [br("A-USD", 100.0, 3, 3, 2.0), br("B-USD", 50.0, 1, 3, 1.0)]
    r = inv.no_dead_capital(green)
    assert r["status"] == inv.OK
    assert r["full_usd"] == 100.0
    assert r["full_branches"] == ["A-USD"]


def test_a_branch_with_a_spare_rung_is_in_neither_figure():
    r = inv.no_dead_capital(LIVE)
    for pid in ("XLM-USD", "LINK-USD"):
        assert pid not in r["full_branches"]
        assert pid not in r.get("branches", [])


# ── the verdict must not have moved ──────────────────────────────────────

def test_full_on_rungs_alone_is_still_not_a_failure():
    """Such a branch can still sell at a profit, which is it working."""
    r = inv.no_dead_capital([br("A-USD", 100.0, 3, 3, 4.0)])
    assert r["status"] == inv.OK


def test_full_and_underwater_is_still_a_failure():
    r = inv.no_dead_capital([br("A-USD", 100.0, 3, 3, -4.0)])
    assert r["status"] == inv.FAIL
    assert r["stuck_usd"] == 100.0


def test_the_near_exit_split_still_works():
    r = inv.no_dead_capital([br("A-USD", 100.0, 3, 3, -4.0),
                             br("B-USD", 50.0, 3, 3, -0.5)])
    assert r["stuck_usd"] == 100.0
    assert r["near_exit_usd"] == 50.0


def test_an_unreadable_slice_is_still_never_guessed_at():
    r = inv.no_dead_capital([br("A-USD", 100.0, 3, 3, None)])
    assert r["status"] == inv.UNKNOWN
    assert "A-USD" in r["unreadable"]


def test_no_branches_is_still_unknown():
    assert inv.no_dead_capital([])["status"] == inv.UNKNOWN
