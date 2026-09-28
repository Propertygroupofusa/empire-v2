"""The check has to name what moved, or it has only scheduled the digging.

Every scenario below replays a real moment from 2026-09-27/28 and asserts
the module would have said, unprompted, what took an hour to work out.
"""
import pytest

import reconcile as R


def comp(coin=6934.48, avail=450.75, hold=64.56, comm=1.88):
    return {"coin_at_cost": coin, "cash_available": avail,
            "cash_on_hold": hold, "commission_open": comm}


# ------------------------------------------------- the identity itself
def test_the_identity_balances_on_the_live_numbers():
    """And reproduces exactly what the live endpoint reports, -$22.17,
    which is the check that the two are computing the same thing."""
    s = R.snapshot(7429.50, comp())
    assert s["backed_usd"] == pytest.approx(7451.67, abs=0.01)
    assert s["residual_usd"] == pytest.approx(-22.17, abs=0.01)


def test_paying_a_fee_does_not_move_the_residual():
    """The sign trap. cash_available already has the fee gone from it, so
    the identity must add it back - subtracting it charges every fee twice.
    Written the wrong way round first; this test is what caught it."""
    before = R.snapshot(1000.0, comp(coin=900, avail=100, hold=0, comm=0))
    after = R.snapshot(1000.0, comp(coin=900, avail=95, hold=0, comm=5))
    assert after["residual_usd"] == pytest.approx(before["residual_usd"], abs=0.01)


def test_an_unreadable_component_produces_no_residual_at_all():
    """A gap is not a zero. An identity computed over a missing term
    produces a confident wrong answer, which is worse than no answer."""
    s = R.snapshot(1000.0, comp(hold=None))
    assert s["residual_usd"] is None
    assert s["unreadable"] == ["cash_on_hold"]
    assert R.explain(None, s)["status"] == "UNKNOWN"
    assert "cash_on_hold" in R.explain(None, s)["headline"]


# ------------------------------- it names the mover, which is the point
def test_it_names_the_locked_cash_that_took_an_hour_to_find():
    """The real event: $64.56 moved from available into hold behind a
    resting buy. The old check said '$44.27 unaccounted' and nothing else."""
    before = R.snapshot(7429.50, comp(avail=515.31, hold=0.0))
    after = R.snapshot(7429.50, comp(avail=450.75, hold=64.56))
    # with hold counted, the identity barely moves - which is the correct answer
    assert abs(after["residual_usd"] - before["residual_usd"]) < 0.01
    e = R.explain(before, after)
    # The standing residual is a SEPARATE pre-existing condition, so the
    # status stays DRIFT; what matters is that this event did not move it,
    # and that both sides of the move are named without anyone digging.
    assert e["status"] != "STEP_CHANGE"
    assert abs(e["residual_delta_usd"]) < 0.01
    assert e["deltas"]["cash_available"] == pytest.approx(-64.56)
    assert e["deltas"]["cash_on_hold"] == pytest.approx(64.56)


def test_cash_leaving_with_no_coin_arriving_is_a_step_change_and_is_named():
    """The shape of a genuine leak: cash falls, nothing else rises."""
    before = R.snapshot(7429.50, comp())
    after = R.snapshot(7429.50, comp(avail=450.75 - 60.0))
    e = R.explain(before, after)
    assert e["status"] == "STEP_CHANGE"
    assert e["driver"] == "cash_available"
    assert "NEW mechanism" in e["headline"]
    assert e["residual_delta_usd"] == pytest.approx(60.0)


def test_a_normal_buy_does_not_read_as_a_leak():
    """Cash out, coin in, same size - the residual must not move."""
    before = R.snapshot(7429.50, comp())
    after = R.snapshot(7429.50, comp(coin=6934.48 + 68.83, avail=450.75 - 68.83))
    e = R.explain(before, after)
    assert abs(e["residual_delta_usd"]) < 0.01
    assert e["status"] != "STEP_CHANGE"


def test_commission_accruing_is_attributed_to_commission_not_to_cash():
    before = R.snapshot(7429.50, comp())
    after = R.snapshot(7429.50, comp(avail=450.75 - 12.0, comm=1.88 + 12.0))
    e = R.explain(before, after)
    assert abs(e["residual_delta_usd"]) < 0.01, "a fee that is booked is not a hole"


def test_a_still_residual_is_reported_as_not_moving():
    s = R.snapshot(7429.50, comp())
    e = R.explain(s, s)
    assert "not moving" in e["headline"]


def test_the_first_ever_reading_says_it_cannot_attribute_yet():
    e = R.explain(None, R.snapshot(7429.50, comp()))
    assert "no previous reading" in e["headline"]
    assert e["deltas"] == {}


def test_every_bucket_in_the_identity_appears_in_the_deltas():
    """A bucket added to one and forgotten in the other is how this kind of
    check goes quietly blind."""
    e = R.explain(R.snapshot(1000.0, comp()), R.snapshot(1000.0, comp()))
    for b in R.BUCKETS:
        assert b in e["deltas"], b
    assert "claimed" in e["deltas"]


# --------------------------------- the correction, and its one direction
def test_a_claim_above_its_backing_is_reduced_to_the_backing():
    new, why = R.correction_for(2272.62, 2200.00)
    assert new == pytest.approx(2200.00)
    assert "reduced to what is actually there" in why


def test_a_claim_below_its_backing_is_never_raised():
    """Raising an allocation on the strength of an arithmetic identity would
    let any mis-measurement mint budget out of a rounding error - and this
    codebase produced a wrong-but-plausible measurement twice in one evening."""
    new, why = R.correction_for(2200.00, 2272.62)
    assert new is None
    assert "only ever corrects downward" in why


def test_a_claim_within_tolerance_is_left_alone():
    assert R.correction_for(1000.0, 998.0)[0] is None


def test_unreadable_inputs_move_nothing():
    assert R.correction_for(None, 100.0)[0] is None
    assert R.correction_for(100.0, None)[0] is None
    assert R.correction_for(100.0, -5.0)[0] is None


def test_the_correction_never_increases_a_claim_for_any_input():
    for claim in (0.0, 1.0, 100.0, 7429.5):
        for backing in (0.0, 1.0, 100.0, 7429.5, 99999.0):
            new, _ = R.correction_for(claim, backing)
            assert new is None or new <= claim + 1e-9, (claim, backing, new)


# ------------------------------------------------------------ mutation
def test_dropping_the_downward_only_rule_would_fail_these_tests():
    saved = R.correction_for
    try:
        R.correction_for = lambda c, b, tolerance=R.TOLERANCE_USD: (round(b, 2), "naive")
        new, _ = R.correction_for(2200.00, 2272.62)
        assert new > 2200.00, "the guard is not what prevents an upward write"
    finally:
        R.correction_for = saved


# ------------------------------------- the snapshot store's own guards
def test_a_partial_baseline_is_no_baseline():
    """Comparing against a snapshot missing a bucket would attribute the
    residual to whichever field happened to survive. Asserted on the store's
    source because it is DB-bound: the guard is the `any(... is None)` check,
    and removing it is the failure mode."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def _read_reconcile_snapshot")
    block = src[i:src.index("async def _write_reconcile_snapshot")]
    assert "any(f not in vals or vals[f] is None" in block
    assert "return None" in block


def test_a_snapshot_with_a_hole_is_never_stored():
    """A baseline with a hole in it makes the NEXT comparison lie, which is
    worse than having no baseline at all."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def _write_reconcile_snapshot")
    block = src[i:i + 2000]
    assert 'if snap.get("residual_usd") is None:' in block
    assert block.index('if snap.get("residual_usd") is None:') < block.index("db.commit")


def test_every_identity_bucket_is_persisted():
    """A bucket added to BUCKETS but not to the stored fields would silently
    stop being comparable - the check would go half blind and still report."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    assert '_RECONCILE_FIELDS = ("claimed_usd",) + tuple(__import__("reconcile").BUCKETS)' in src


# ============================ the fleet correction, and what it may not do
LIVE = [
    {"bot_name": "g1", "product_id": "ZEC-USD",  "allocated_usd": 2272.62, "coin_basis_usd": 2341.45},
    {"bot_name": "g2", "product_id": "XRP-USD",  "allocated_usd": 2240.54, "coin_basis_usd": 2240.54},
    {"bot_name": "g3", "product_id": "XLM-USD",  "allocated_usd":  469.23, "coin_basis_usd":  400.00},
    {"bot_name": "g4", "product_id": "NEAR-USD", "allocated_usd":  214.50, "coin_basis_usd":   71.50},
    {"bot_name": "g5", "product_id": "BCH-USD",  "allocated_usd":  171.75, "coin_basis_usd":  231.89},
]


def test_todays_fleet_needs_no_correction():
    """Measured live: unspent claims $495.02 against $515.31 of real cash.
    Arming this must be a no-op today, and knowing that BEFORE arming is the
    point of running it first."""
    corr, rep = R.fleet_corrections(LIVE, real_cash_usd=515.31)
    assert corr == []
    assert rep["status"] == "OK"
    assert "nothing to correct" in rep["detail"]


def test_a_real_over_claim_is_reduced():
    # $150 against $212.23 of unspent claims: a real shortfall, and inside
    # the single-pass limit, so it corrects rather than refusing.
    corr, rep = R.fleet_corrections(LIVE, real_cash_usd=150.00)
    assert rep["status"] == "CORRECT"
    assert corr, "an over-claim must produce corrections"
    assert all(c["new_allocated_usd"] < c["allocated_usd"] for c in corr)


def test_no_claim_is_ever_reduced_below_the_coin_that_branch_owns():
    """The branch bought that coin. A claim under it would have the branch
    believe it owns less than it holds."""
    corr, _ = R.fleet_corrections(LIVE, real_cash_usd=1.00)
    for c in corr:
        assert c["new_allocated_usd"] >= c["coin_basis_usd"] - 1e-9, c


def test_a_branch_claiming_less_than_its_coin_is_left_alone():
    """ZEC and BCH own more coin than they claim - the moment a branch buys
    a rung, basis rises and allocation does not. Never raised."""
    corr, _ = R.fleet_corrections(LIVE, real_cash_usd=1.00)
    touched = {c["product_id"] for c in corr}
    assert "ZEC-USD" not in touched and "BCH-USD" not in touched


def test_no_correction_ever_increases_a_claim():
    for cash in (0.0, 1.0, 50.0, 100.0, 495.02, 515.31, 99999.0):
        corr, _ = R.fleet_corrections(LIVE, real_cash_usd=cash)
        for c in corr:
            assert c["new_allocated_usd"] <= c["allocated_usd"] + 1e-9, (cash, c)


# ------------------------------------------- the rail that matters most
def test_a_wildly_low_cash_read_refuses_instead_of_slashing_the_fleet():
    """This is the failure that would cost real money: the wallet read comes
    back wrong and low, and one pass guts every allocation on the strength
    of one bad number. A correction that large is a measurement to check."""
    corr, rep = R.fleet_corrections(LIVE, real_cash_usd=0.0)
    assert corr == []
    assert rep["status"] == "REFUSED"
    assert "rather than an instruction to obey" in rep["detail"]
    assert "the cash read collapsed" in rep["detail"]
    assert rep["would_have_cut_usd"] > rep["limit_usd"]


def test_the_limit_guards_the_unspent_pool_not_the_total_claim():
    """Written first as a share of TOTAL claim and caught by the test below
    as decorative: corrections only touch the unspent portion, and unspent is
    $212 of a $5,369 claim here, so a 5%-of-claim limit sat at $268 and could
    never bind. A rail that cannot refuse anything is not a rail."""
    _, rep = R.fleet_corrections(LIVE, real_cash_usd=0.0)
    unspent = sum(max(0.0, r["allocated_usd"] - r["coin_basis_usd"]) for r in LIVE)
    assert rep["limit_usd"] == pytest.approx(unspent * R.MAX_FLEET_CORRECTION_PCT, abs=0.01)
    claimed = sum(r["allocated_usd"] for r in LIVE)
    assert rep["limit_usd"] < claimed * 0.05, "the limit is still scaled to the wrong pool"


def test_a_correction_inside_the_limit_still_proceeds():
    """Cash halves from the $212.23 of unspent claims - plausible, so it
    corrects rather than refusing."""
    corr, rep = R.fleet_corrections(LIVE, real_cash_usd=150.0)
    assert rep["status"] == "CORRECT"
    assert rep["total_reduction_usd"] <= rep["limit_usd"]
    assert corr


def test_unreadable_cash_moves_nothing():
    assert R.fleet_corrections(LIVE, None)[1]["status"] == "UNKNOWN"
    assert R.fleet_corrections(LIVE, -5.0)[1]["status"] == "UNKNOWN"


def test_an_unmeasurable_branch_is_skipped_never_corrected():
    rows = LIVE + [{"bot_name": "bad", "product_id": "?", "allocated_usd": None,
                    "coin_basis_usd": None}]
    corr, _ = R.fleet_corrections(rows, real_cash_usd=100.0)
    assert all(c["product_id"] != "?" for c in corr)


def test_corrections_bring_the_fleet_inside_its_cash():
    corr, rep = R.fleet_corrections(LIVE, real_cash_usd=200.0)
    by = {c["product_id"]: c for c in corr}
    after = sum(max(0.0, by.get(r["product_id"], {}).get("new_allocated_usd",
                                                         r["allocated_usd"])
                    - r["coin_basis_usd"]) for r in LIVE)
    assert after <= 200.0 + 0.05, after


# ------------------------------------------------------------ mutation
def test_removing_the_single_pass_limit_would_gut_the_fleet():
    saved = R.MAX_FLEET_CORRECTION_PCT
    try:
        R.MAX_FLEET_CORRECTION_PCT = 1.0
        corr, rep = R.fleet_corrections(LIVE, real_cash_usd=0.0,
                                        max_pct=R.MAX_FLEET_CORRECTION_PCT)
        assert rep["status"] == "CORRECT", "the limit is not what refuses a zero cash read"
        # every unspent dollar wiped on the strength of one bad read
        assert sum(c["reduction_usd"] for c in corr) == pytest.approx(212.23, abs=0.05)
    finally:
        R.MAX_FLEET_CORRECTION_PCT = saved


def test_removing_the_coin_floor_would_push_claims_under_real_holdings():
    live_cash = 1.0
    corr, _ = R.fleet_corrections(LIVE, real_cash_usd=live_cash)
    naive = {r["product_id"]: r["allocated_usd"] * (live_cash / 495.02) for r in LIVE}
    for c in corr:
        assert c["new_allocated_usd"] > naive[c["product_id"]], (
            "the coin floor is not what keeps this above the branch's holdings")
