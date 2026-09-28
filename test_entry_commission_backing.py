"""Commission already paid is spent, not missing.

THE TIMING HOLE, measured live 2026-09-28 01:4xZ.

A buy debits the wallet by the spend PLUS Coinbase's commission. The slice
records basis = entry_price * qty - the spend alone - and no branch's
allocated_usd moves. So between buying and selling, the commission has left
the account and nothing in the book has recognised it.

    branches claim            $7,429.50
    really there              $7,385.23   ($6,934.48 coin + $450.75 cash)
    unaccounted                  $44.27
    entry commission on the
      $6,934.48 of open
      slices @ 0.006088/leg      $42.22   <- 4.6% of the gap, the rest is
                                             price drift between two reads

That $44.27 is also why real_free_cash_usd read -$246.87 and blocked
funding a new coin: the fleet was budgeting cash it had already spent.

It is NOT a leak. _grid_slice_net_pnl charges both legs when the slice
sells and the result lands in allocated_usd, so a completed round trip
reconciles exactly. The point of naming it is that a known, correct timing
effect must not sit in the same number as a real hole - otherwise the first
genuine leak hides inside it.
"""
import pytest

import allocation_backing as AB


def sl(qty, price, rate=None, adopted=False):
    d = {"qty": qty, "entry_price": price, "adopted": adopted}
    if rate is not None:
        d["entry_fee_rate"] = rate
    return d


# --------------------------------------------- the commission itself
def test_a_real_buy_carries_its_recorded_commission():
    assert AB.slice_entry_commission(sl(10, 10.0, 0.0035)) == pytest.approx(0.35)


def test_an_adopted_slice_paid_nothing():
    """No order was placed. Charging one would invent a hole exactly the
    size of the fleet's adopted inventory - $6,600 x 0.006 = ~$40."""
    assert AB.slice_entry_commission(sl(10, 10.0, 0.0035, adopted=True)) == 0.0
    assert AB.slice_entry_commission(sl(10, 10.0, None, adopted=True)) == 0.0


def test_an_unrecorded_rate_is_unknown_not_free():
    assert AB.slice_entry_commission(sl(10, 10.0, None)) is None


def test_a_recorded_zero_is_genuinely_zero():
    assert AB.slice_entry_commission(sl(10, 10.0, 0.0)) == 0.0


def test_an_unpriceable_slice_is_unknown():
    assert AB.slice_entry_commission({"qty": None, "entry_price": 10.0,
                                      "entry_fee_rate": 0.0035}) is None


# ------------------------------------------------ the backing verdict
def test_the_gap_is_reported_net_of_commission_already_paid():
    branches = [{"allocated_usd": 1000.0,
                 "slices": [sl(100, 9.0, 0.0035)]}]     # basis 900, fee 3.15
    r = AB.backing(branches, wallet_cash=96.85)         # 900 + 96.85 = 996.85
    assert r["gross_unbacked_usd"] == pytest.approx(3.15, abs=0.01)
    assert r["open_entry_commission_usd"] == pytest.approx(3.15, abs=0.01)
    assert r["unbacked_usd"] == pytest.approx(0.0, abs=0.01)
    assert r["verdict"] == "backed"


def test_a_real_hole_still_shows_through_the_commission():
    """The thing that must not break: naming the timing effect cannot hide
    a genuine shortfall sitting on top of it."""
    branches = [{"allocated_usd": 1000.0, "slices": [sl(100, 9.0, 0.0035)]}]
    r = AB.backing(branches, wallet_cash=46.85)         # $50 genuinely gone
    assert r["open_entry_commission_usd"] == pytest.approx(3.15, abs=0.01)
    assert r["unbacked_usd"] == pytest.approx(50.0, abs=0.01)
    assert r["verdict"] != "backed"


def test_adopted_inventory_does_not_manufacture_a_gap():
    """The whole adopted book at once: no commission, so no deduction."""
    branches = [{"allocated_usd": 2272.62,
                 "slices": [sl(1.0, 2272.62, None, adopted=True)]}]
    r = AB.backing(branches, wallet_cash=0.0)
    assert r["open_entry_commission_usd"] == 0.0
    assert r["unbacked_usd"] == pytest.approx(0.0, abs=0.01)


def test_slices_with_no_recorded_rate_are_counted_not_guessed():
    branches = [{"allocated_usd": 1000.0,
                 "slices": [sl(100, 9.0, None), sl(10, 1.0, 0.0035)]}]
    r = AB.backing(branches, wallet_cash=90.0)
    assert r["slices_without_fee_rate"] == 1
    assert "NOT deducted" in r["detail"]


def test_the_detail_names_the_commission_so_nobody_has_to_dig():
    branches = [{"allocated_usd": 1000.0, "slices": [sl(100, 9.0, 0.0035)]}]
    r = AB.backing(branches, wallet_cash=96.85)
    assert "already paid" in r["detail"] and "3.15" in r["detail"]


# ------------------------------------------ the live numbers reproduce
def test_the_live_shortfall_is_explained_by_commission():
    """$6,934.48 of open basis at the measured 0.006088/leg."""
    basis, rate = 6934.48, 0.006088
    branches = [{"allocated_usd": 7429.50,
                 "slices": [sl(1.0, basis, rate)]}]
    r = AB.backing(branches, wallet_cash=450.75)
    assert r["gross_unbacked_usd"] == pytest.approx(44.27, abs=0.02)
    assert r["open_entry_commission_usd"] == pytest.approx(42.22, abs=0.02)
    assert abs(r["unbacked_usd"]) <= AB.BACKING_TOLERANCE_USD
    assert r["verdict"] == "backed"


# ------------------------------------------------------------ mutation
def test_charging_adopted_slices_would_invent_a_hole():
    saved = AB.slice_entry_commission
    try:
        AB.slice_entry_commission = lambda s: (AB.slice_cost(s) or 0) * 0.006088
        branches = [{"allocated_usd": 2272.62,
                     "slices": [sl(1.0, 2272.62, None, adopted=True)]}]
        r = AB.backing(branches, wallet_cash=0.0)
        assert r["unbacked_usd"] < -13.0, "the adopted guard is not what prevents this"
    finally:
        AB.slice_entry_commission = saved


# ------------------------------- the fix must not be INERT in production
def test_get_grid_status_emits_the_fields_the_deduction_needs():
    """Caught before shipping: slices_out carried neither entry_fee_rate nor
    adopted, so slice_entry_commission() would have returned None for every
    slice in production. The deduction would have been inert - fix merged,
    tests green, live gap unmoved. Both fields are load-bearing.
    """
    import ast
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crypto_grid_bot.py"), encoding="utf-8").read()
    i = src.index("slices_out.append({")
    block = src[i:src.index("})", i)]
    assert '"entry_fee_rate"' in block
    assert '"adopted"' in block


def test_a_slice_dict_shaped_like_production_is_actually_deducted():
    """The end-to-end shape, not the test's own convenient one."""
    production_shaped = {
        "entry_price": 9.0, "qty": 100, "opened_at": "2026-09-28T00:00:00Z",
        "unrealized_net_usd": -1.0, "unrealized_net_pct": -0.001,
        "entry_fee_rate": 0.0035, "adopted": False,
    }
    assert AB.slice_entry_commission(production_shaped) == pytest.approx(3.15)
    r = AB.backing([{"allocated_usd": 1000.0, "slices": [production_shaped]}], 96.85)
    assert r["open_entry_commission_usd"] == pytest.approx(3.15, abs=0.01)
    assert r["slices_without_fee_rate"] == 0


# ------------------------------------------- USD locked is still backing
def test_locked_usd_counts_as_backing():
    """Live 2026-09-28: the wallet held $515.31 of USD - $450.75 available,
    $64.56 locked behind a resting maker buy. get_usd_balance returns
    available only (right for sizing a buy, wrong for backing), so $64.56 of
    real money vanished from the check while the branch that committed it
    still counted it as unspent budget."""
    branches = [{"allocated_usd": 1000.0, "slices": []}]
    without = AB.backing(branches, wallet_cash=935.44)
    with_hold = AB.backing(branches, wallet_cash=935.44, usd_on_hold=64.56)
    assert without["unbacked_usd"] == pytest.approx(64.56, abs=0.01)
    assert with_hold["unbacked_usd"] == pytest.approx(0.0, abs=0.01)
    assert with_hold["usd_on_hold"] == pytest.approx(64.56)
    assert with_hold["verdict"] == "backed"


def test_an_unreadable_hold_counts_as_zero_not_as_backing():
    """Conservative on purpose: a failed read reproduces the old figure
    rather than inventing backing nobody confirmed."""
    b = [{"allocated_usd": 1000.0, "slices": []}]
    assert AB.backing(b, 935.44, None)["usd_on_hold"] == 0.0
    assert AB.backing(b, 935.44, "nonsense")["usd_on_hold"] == 0.0
    assert AB.backing(b, 935.44, -5)["usd_on_hold"] == 0.0


def test_the_caller_subtracts_available_before_passing_hold():
    """fetch_balances' 'held' map is TOTAL (available + hold) - its own line
    is `total = avail + hold`. Passing it straight through would add the
    available balance to itself and INFLATE backing, hiding a real hole
    instead of reporting a false one. Caught before shipping."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crypto_grid_bot.py"), encoding="utf-8").read()
    i = src.index("usd_on_hold = None")
    block = src[i:i + 1200]
    assert "available_units" in block
    assert "float(_total) - float(_avail)" in block
    assert "max(0.0," in block
