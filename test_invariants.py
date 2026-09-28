"""Each invariant must FAIL on the real defect it was written for.

A check that cannot fail is worse than no check - it converts an unknown
into a false assurance. So every test below feeds the check the ACTUAL
numbers from the day the defect was live, and asserts it reports FAIL.
"""
import pytest

import invariants as inv


# ── 1. fee rate agreement ───────────────────────────────────────────────
def test_it_catches_the_two_workers_disagreeing():
    """The real 2026-09-28 numbers: one worker's floor priced 0.0035/leg
    while another reported 0.0075/leg, same deploy, same second."""
    r = inv.fee_rate_agreement(floor_leg_rate=0.0035, reported_maker_leg=0.0075,
                               measured_leg_rate=0.006129)
    assert r["status"] == inv.FAIL
    assert "0.003500" in r["detail"] and "0.007500" in r["detail"]


def test_it_catches_the_system_disagreeing_with_coinbase():
    r = inv.fee_rate_agreement(0.0075, 0.0075, measured_leg_rate=0.0035)
    assert r["status"] == inv.FAIL
    assert "Coinbase actually billed" in r["detail"]


def test_it_passes_when_all_three_agree():
    r = inv.fee_rate_agreement(0.0035, 0.0035, 0.00352)
    assert r["status"] == inv.OK


def test_an_unreadable_rate_is_unknown_not_ok():
    assert inv.fee_rate_agreement(None, 0.0035, 0.0035)["status"] == inv.UNKNOWN
    assert inv.fee_rate_agreement(0.0035, 0.0035, None)["status"] == inv.UNKNOWN


def test_quoting_noise_does_not_trip_it():
    # 0.0001 apart - under half a basis point
    assert inv.fee_rate_agreement(0.0035, 0.0036, 0.0035)["status"] == inv.OK


# ── 2. stale spacing evidence ───────────────────────────────────────────
def test_it_catches_the_spacing_table_priced_at_the_old_fee():
    """The real find: FLEET_MIN_STEP_PCT rests on a table priced at a 1.37%
    round trip while the measured cost is 0.70%."""
    r = inv.spacing_evidence_current(0.0137, 0.0070)
    assert r["status"] == inv.FAIL
    assert "1.37%" in r["detail"] and "0.70%" in r["detail"]
    assert "Re-run" in r["detail"]


def test_evidence_priced_at_the_current_fee_passes():
    assert inv.spacing_evidence_current(0.0070, 0.0075)["status"] == inv.OK


def test_a_gap_under_the_net_margin_cannot_flip_a_spacing_decision():
    # 0.0019 apart, just under the 0.002 margin the floor itself adds
    assert inv.spacing_evidence_current(0.0070, 0.0089)["status"] == inv.OK


# ── 3. allocation backing ───────────────────────────────────────────────
def test_the_live_banner_figure_is_within_tolerance():
    """$7,429.11 claimed vs $7,422.04 real = 0.10%, real but small."""
    r = inv.allocation_backed(7429.11, 7422.04)
    assert r["status"] == inv.OK
    assert "0.10%" in r["detail"]


def test_a_real_backing_breach_fails():
    r = inv.allocation_backed(7429.11, 7000.00)
    assert r["status"] == inv.FAIL
    assert "MORE than is really there" in r["detail"]


def test_an_unexplained_surplus_is_unknown_and_never_a_pass():
    """Retargeted, not deleted. This asserted FAIL on a surplus, which was
    the behaviour before allocation_backed learned to tell the two
    directions apart: claiming MORE than is present is a backing breach,
    claiming LESS is not - the branches are fully backed and something
    else is holding the difference.

    A surplus nobody can account for is still not fine, so it reads
    UNKNOWN. It went FAIL for hours on a real $466.61 surplus that was
    entirely explained, and a check that cries breach on a surplus is a
    check people learn to ignore."""
    r = inv.allocation_backed(7000.00, 7429.11)
    assert r["status"] == inv.UNKNOWN
    assert r["status"] != inv.OK
    assert "MORE is present than is claimed" in r["detail"], r["detail"]
    assert "not a hole" in r["detail"]


def test_a_surplus_that_is_fully_explained_reads_ok():
    """$429.11 of surplus, all of it named: unspent cash plus coin
    deployed past what the branches claim."""
    r = inv.allocation_backed(7000.00, 7429.11,
                              unallocated_cash_usd=200.00,
                              over_deployed_usd=229.11)
    assert r["status"] == inv.OK


# ── 4. cash reconciliation ──────────────────────────────────────────────
def test_the_night_the_owner_thought_money_was_vanishing():
    """The real figures: $1,194.54 -> $994.39 with $274.18 bought and
    $35.79 of proceeds. Residual +$38.24, in the account's favour."""
    r = inv.cash_reconciles(1194.54, 994.39, 274.18, 35.79)
    assert r["status"] == inv.OK
    assert r["residual_usd"] == pytest.approx(38.24, abs=0.01)
    assert "rules out a leak" in r["detail"]


def test_a_real_leak_fails():
    r = inv.cash_reconciles(1194.54, 700.00, 274.18, 35.79)
    assert r["status"] == inv.FAIL
    assert "LOWER than the trades explain" in r["detail"]


def test_the_arithmetic_is_shown_not_just_the_verdict():
    r = inv.cash_reconciles(1000.0, 900.0, 100.0, 0.0)
    for piece in ("1,000.00", "100.00", "900.00"):
        assert piece in r["detail"]


def test_an_unreadable_leg_is_unknown():
    assert inv.cash_reconciles(None, 994.39, 274.18, 35.79)["status"] == inv.UNKNOWN


# ── 5. dead capital ─────────────────────────────────────────────────────
def test_it_catches_a_branch_that_can_neither_buy_nor_sell():
    """ZEC on the night it was found: 6 slices of 3 levels, underwater."""
    r = inv.no_dead_capital([
        {"product_id": "ZEC-USD", "allocated_usd": 2272.62,
         "open_slices": 6, "num_levels": 3, "best_slice_net_pct": -4.15},
        {"product_id": "NEAR-USD", "allocated_usd": 214.50,
         "open_slices": 1, "num_levels": 3, "best_slice_net_pct": -0.2},
    ])
    assert r["status"] == inv.FAIL
    assert r["branches"] == ["ZEC-USD"]
    assert r["stuck_usd"] == pytest.approx(2272.62)


def test_a_full_branch_that_can_still_sell_is_not_dead():
    r = inv.no_dead_capital([{"product_id": "HBAR-USD", "allocated_usd": 163.38,
                              "open_slices": 3, "num_levels": 3,
                              "best_slice_net_pct": 0.66}])
    assert r["status"] == inv.OK


def test_an_underwater_branch_with_room_to_buy_is_not_dead():
    r = inv.no_dead_capital([{"product_id": "XLM-USD", "allocated_usd": 469.0,
                              "open_slices": 3, "num_levels": 4,
                              "best_slice_net_pct": -1.88}])
    assert r["status"] == inv.OK


# ── 6. the read itself ──────────────────────────────────────────────────
def test_it_catches_the_truncated_two_hundred():
    """18,615 bytes of a ~108,000 byte payload, HTTP 200."""
    r = inv.read_complete(18615, parsed_ok=False, expected_min_bytes=90000)
    assert r["status"] == inv.FAIL
    assert "NOT a zero" in r["detail"]


def test_it_catches_a_short_payload_that_still_parsed():
    r = inv.read_complete(40000, parsed_ok=True, expected_min_bytes=90000)
    assert r["status"] == inv.FAIL


def test_a_whole_read_passes():
    assert inv.read_complete(108281, True, 90000)["status"] == inv.OK


# ── the summary ─────────────────────────────────────────────────────────
def test_a_single_failure_makes_the_whole_summary_fail():
    s = inv.summarize([inv.fee_rate_agreement(0.0035, 0.0035, 0.0035),
                       inv.spacing_evidence_current(0.0137, 0.0070)])
    assert s["status"] == inv.FAIL
    assert "spacing_evidence_current" in s["headline"]


def test_unknown_never_reads_as_healthy():
    """Silence must not look like health - the whole point of a third verdict."""
    s = inv.summarize([inv.fee_rate_agreement(0.0035, 0.0035, 0.0035),
                       inv.cash_reconciles(None, 1.0, 1.0, 1.0)])
    assert s["status"] == inv.UNKNOWN
    assert "could not be checked" in s["headline"]


def test_failures_sort_first():
    s = inv.summarize([inv.read_complete(108281, True, 9000),
                       inv.cash_reconciles(None, 1.0, 1.0, 1.0),
                       inv.allocation_backed(7429.11, 7000.0)])
    assert [c["status"] for c in s["checks"]] == [inv.FAIL, inv.UNKNOWN, inv.OK]


# ── every check must be able to fail ────────────────────────────────────
def test_no_check_is_incapable_of_failing():
    """A check that can only ever pass is a false assurance. Each entry
    below is a call that MUST come back FAIL."""
    must_fail = [
        inv.fee_rate_agreement(0.0035, 0.0075, 0.006129),
        inv.spacing_evidence_current(0.0137, 0.0070),
        inv.allocation_backed(7429.11, 7000.00),
        inv.cash_reconciles(1194.54, 700.00, 274.18, 35.79),
        # -1.0 exactly was this fixture for a while, and it stopped being
        # a failing case the moment NEAR_EXIT_PCT landed on 1.0: a branch
        # AT the boundary counts as between fills, not stuck. Moved clear
        # of the threshold rather than the threshold moved to suit it.
        inv.no_dead_capital([{"product_id": "X", "allocated_usd": 1.0,
                              "open_slices": 3, "num_levels": 3,
                              "best_slice_net_pct": -4.5}]),
        inv.read_complete(18615, False, 90000),
        inv.maker_only_holds(37, 109, "2026-09-28T07:49:41+00:00",
                             "2026-09-28T04:35:16+00:00"),
        inv.coin_tracked_is_held({"ETH-USD": 0.162240}, {"ETH": 0.130640},
                                 {"ETH-USD": 2651.0}),
        inv.grid_inventory_is_free(
            {"ALGO-USD": 1000.0},
            [{"asset": "ALGO", "units": 1134.34638900,
              "available_units": 0.04638900, "price": 0.125905}]),
    ]
    assert [r["status"] for r in must_fail] == [inv.FAIL] * 9
    # and every public check is represented above
    checked = {r["name"] for r in must_fail}
    public = {n for n in dir(inv)
              if not n.startswith("_") and callable(getattr(inv, n))
              and n not in ("summarize",)}
    assert checked == public, f"a check has no failing test: {public - checked}"


# ------------------------------- an UNKNOWN must say why it could not look
def test_the_endpoint_records_why_a_check_went_blind():
    """Found live 2026-09-28 01:41Z: fee_rate_agreement and
    spacing_evidence_current BOTH read UNKNOWN while
    /grid-status/fee-reality was answering the same question perfectly well
    (61 classified fills, 0.006088/leg). The endpoint had swallowed the
    exception and set the rate to None, so two of four checks were blind and
    there was no way to tell a rate-limited call from a renamed function.

    A blind check is worse than a failing one - it looks like silence. So
    the cause is captured and attached to the verdict.
    """
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def grid_invariants_endpoint")
    block = src[i:i + 4000]
    assert "blind_because" in block
    # the bare swallow must be gone
    assert "except Exception:\n            measured_leg = None" not in block
    assert "except Exception as e:" in block
    assert 'f"{type(e).__name__}: {e}"' in block
    # and a starved sample is distinguished from a thrown call
    assert "classified_fills" in block


def test_a_starved_sample_and_a_thrown_call_are_different_causes():
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def grid_invariants_endpoint")
    block = src[i:i + 4000]
    assigns = block.count("blind_because = ")
    assert assigns >= 3, "expected: init, starved-sample, exception"


def test_the_cause_is_only_attached_to_unknowns_not_to_fails():
    """A FAIL already carries its arithmetic; appending a cause to it would
    bury the number under plumbing."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def grid_invariants_endpoint")
    block = src[i:i + 4000]
    assert 'r["status"] == inv.UNKNOWN and blind_because' in block


# ================= like-for-like: maker-only floor vs maker-billed rate
def test_the_maker_only_floor_is_compared_against_the_maker_billed_rate():
    """A category error reported as a defect, found on the 02:45Z pass.

    fee_rate_agreement FAILED with "the system prices 0.003500/leg but
    Coinbase actually billed 0.006137/leg". Both numbers were right and they
    answer different questions. Under maker-ONLY there is no market fallback,
    so the floor prices the MAKER leg - and every coin that filled 100% maker
    bills exactly 0.003500. The 0.006137 blend is pure-taker fills (XRP and
    XYO at 0.0075, ARB at 0.0062) mixed in, most of them predating the mode.

    Comparing those two makes the check cry wolf on correct behaviour, which
    is how a check stops being read.
    """
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def grid_invariants_endpoint")
    block = src[i:i + 6000]
    assert "is_maker_only_active" in block
    assert 'fills.get("maker_leg_fee_rate")' in block
    # the blend is still used when the fallback exists
    assert 'fills.get("real_leg_fee_rate")' in block


def test_a_taker_fill_under_maker_only_is_its_own_finding():
    """It must not hide inside a blended rate. get_fill_mix's own note says
    a taker leg counted while maker-only is on is a bug."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "routers", "trading_dashboard.py"), encoding="utf-8").read()
    i = src.index("async def grid_invariants_endpoint")
    block = src[i:i + 7000]
    assert '"name": "maker_only_holds"' in block
    assert "taker_fills" in block


def test_the_separated_rates_are_none_not_zero_when_nothing_filled():
    """'No maker fills in this window' is not 'maker legs are free', and a
    caller that floors a spacing on it must be able to tell them apart."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crypto_btc_compound_bot.py"), encoding="utf-8").read()
    assert "maker_leg_rate = (maker_commission / maker_notional) if maker_notional > 0 else None" in src
    assert "taker_leg_rate = (taker_commission / taker_notional) if taker_notional > 0 else None" in src


def test_maker_and_taker_commission_are_accumulated_separately():
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crypto_btc_compound_bot.py"), encoding="utf-8").read()
    assert 'if liq == "MAKER":\n            maker_commission += comm' in src
    assert 'elif liq == "TAKER":\n            taker_commission += comm' in src
    # and the blend still exists - this adds a view, it does not replace one
    assert "commission_total += comm" in src


def test_the_live_numbers_make_the_check_pass_like_for_like():
    """Measured 02:45Z: maker-billed 0.0035 against a 0.0035 floor."""
    import invariants as inv
    assert inv.fee_rate_agreement(0.0035, 0.0035, 0.0035)["status"] == inv.OK
    # and the old comparison is what was failing
    assert inv.fee_rate_agreement(0.0035, 0.0035, 0.006137)["status"] == inv.FAIL
