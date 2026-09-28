"""Claiming less than you have is not the same as claiming more than you have.

allocation_backed ran FAIL for hours:

    branches claim $8,072.26 against $8,538.95 really there -
    $466.69 (5.78%) less than is really there, over the 0.50% tolerance.

Nobody was short a dollar. There was MORE real money than the branches
claimed, and the check called it the same failure as a branch claiming
money that does not exist. The residual never moved because there was
nothing to move - the arithmetic was exact and entirely benign:

    unallocated cash    $76.49   real USD no branch has claimed yet
    over-deployed      $390.20   coin costing more than its allocation
    open commission      $3.18   buy-leg fees already paid
                       -------
                       $469.87   the whole surplus, to the cent
"""
import allocation_backing as ab
import invariants as inv

# The live fleet at 04:49Z.
CLAIMED, BACKED = 8072.26, 8542.13
CASH, OVER, COMM = 76.49, 390.20, 3.18


def test_the_live_surplus_is_explained_and_passes():
    r = inv.allocation_backed(CLAIMED, BACKED, unallocated_cash_usd=CASH,
                              over_deployed_usd=OVER, open_commission_usd=COMM)
    assert r["status"] == inv.OK
    assert "MORE money than is claimed" in r["detail"]
    assert r["surplus_usd"] == 469.87
    assert r["explained_usd"] == 469.87


def test_over_claiming_is_still_a_hard_failure():
    """The direction this check exists for must not be softened."""
    r = inv.allocation_backed(BACKED, CLAIMED, unallocated_cash_usd=CASH,
                              over_deployed_usd=OVER, open_commission_usd=COMM)
    assert r["status"] == inv.FAIL
    assert "MORE than is really there" in r["detail"]
    assert r["gap_usd"] > 0


def test_a_surplus_with_no_components_is_unknown_not_a_pass():
    r = inv.allocation_backed(CLAIMED, BACKED)
    assert r["status"] == inv.UNKNOWN
    assert "not read as a pass" in r["detail"]


def test_a_partly_explained_surplus_is_unknown_and_names_the_remainder():
    r = inv.allocation_backed(CLAIMED, BACKED, unallocated_cash_usd=CASH,
                              over_deployed_usd=0.0, open_commission_usd=COMM)
    assert r["status"] == inv.UNKNOWN
    assert r["unexplained_usd"] == 390.20
    assert "unexplained" in r["detail"]


def test_a_small_difference_either_way_is_still_within_tolerance():
    assert inv.allocation_backed(10_000.0, 10_020.0)["status"] == inv.OK
    assert inv.allocation_backed(10_020.0, 10_000.0)["status"] == inv.OK


def test_unreadable_inputs_stay_unknown():
    assert inv.allocation_backed(None, BACKED)["status"] == inv.UNKNOWN
    assert inv.allocation_backed(CLAIMED, None)["status"] == inv.UNKNOWN


# ── the components, measured off real branch rows ───────────────────────

def _branch(alloc, slices):
    return {"allocated_usd": alloc,
            "slices": [{"qty": q, "entry_price": p, "adopted": True} for q, p in slices]}


def test_reserve_and_over_deployment_are_counted_separately():
    """Summing them first would cancel one against the other and destroy
    the distinction - they mean opposite things."""
    out = ab.backing([
        _branch(100.0, [(1.0, 40.0)]),   # $60 of allocation not yet spent
        _branch(50.0, [(1.0, 90.0)]),    # $40 of coin beyond its allocation
    ], wallet_cash=200.0)
    assert out["unspent_reserve_usd"] == 60.0
    assert out["over_deployed_usd"] == 40.0


def test_unallocated_cash_is_the_wallet_beyond_every_branch_reserve():
    out = ab.backing([_branch(100.0, [(1.0, 40.0)])], wallet_cash=200.0)
    assert out["unspent_reserve_usd"] == 60.0
    assert out["unallocated_cash_usd"] == 140.0


def test_the_components_close_the_identity_exactly():
    """claimed - backed must equal -(unallocated cash + over-deployed +
    commission). If this drifts, the explanation is wrong somewhere."""
    branches = [_branch(100.0, [(1.0, 40.0)]), _branch(50.0, [(1.0, 90.0)])]
    out = ab.backing(branches, wallet_cash=200.0)
    gap = out["claimed_usd"] - out["backed_usd"]
    explained = (out["unallocated_cash_usd"] + out["over_deployed_usd"]
                 + out["open_entry_commission_usd"])
    assert abs(-gap - explained) < 0.01, (gap, explained)


def test_the_identity_holds_on_the_real_fleet_numbers():
    assert round(CASH + OVER + COMM, 2) == round(BACKED - CLAIMED, 2) == 469.87
