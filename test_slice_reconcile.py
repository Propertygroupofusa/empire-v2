"""Writing off tracked coin that another subsystem already sold.

Measured 2026-09-28. The trimmer sold $885.43 of ZEC and $244.78 of XRP,
and a resting stop took 0.347873 ETH. None of those touched a slice row,
so the branches went on claiming units the wallet no longer had:

    ZEC   tracked 1.418971  held 1.331766  short 0.087205
    ETH   tracked 0.162240  held 0.130640  short 0.031600
    XRP   tracked 1486.9255 held 1417.0996 short 69.8259
    BCH   tracked 0.687577  held 0.308254  short 0.379323
    PEPE  tracked 47389839.61 held 39093467.07 short 8296372.54
"""
import slice_reconcile as sr

# The live ZEC branch: 7 slices, one small and six in two groups.
ZEC = [
    {"id": 1, "qty": 0.043386, "entry_price": 1586.44, "opened_at": "2026-09-20T10:00:00"},
    {"id": 2, "qty": 0.378167, "entry_price": 1650.61, "opened_at": "2026-09-21T10:00:00"},
    {"id": 3, "qty": 0.378167, "entry_price": 1650.61, "opened_at": "2026-09-22T10:00:00"},
    {"id": 4, "qty": 0.378167, "entry_price": 1650.61, "opened_at": "2026-09-23T10:00:00"},
    {"id": 5, "qty": 0.080361, "entry_price": 1659.17, "opened_at": "2026-09-24T10:00:00"},
    {"id": 6, "qty": 0.080361, "entry_price": 1659.17, "opened_at": "2026-09-25T10:00:00"},
    {"id": 7, "qty": 0.080361, "entry_price": 1659.17, "opened_at": "2026-09-26T10:00:00"},
]
HELD = 1.331766


def test_the_live_zec_case_reduces_the_oldest_slice():
    actions, rep = sr.plan(ZEC, HELD, price=1545.27)
    assert rep["status"] == "READY"
    # The seven fixture quantities sum to 1.418970 against the live
    # 1.418971 - a rounding artefact of the figures as reported, not a
    # discrepancy in the plan.
    assert abs(rep["surplus_units"] - 0.087205) < 1e-5
    assert actions[0]["slice_id"] == 1, "oldest first"
    assert rep["units_removed"] == rep["surplus_units"]


def test_the_first_slice_is_removed_whole_when_it_is_smaller_than_the_surplus():
    actions, _ = sr.plan(ZEC, HELD, price=1545.27)
    assert actions[0]["action"] == "REMOVE"          # 0.043386 < 0.087205
    assert actions[1]["action"] == "REDUCE"          # the remainder comes off slice 2
    assert actions[1]["qty_after"] < actions[1]["qty_before"]


def test_removed_units_exactly_close_the_gap():
    actions, rep = sr.plan(ZEC, HELD)
    remaining = sum(a["qty_after"] for a in actions) + sum(
        s["qty"] for s in ZEC if s["id"] not in {a["slice_id"] for a in actions})
    assert abs(remaining - HELD) < 1e-9


def test_the_cost_basis_written_off_is_reported_in_dollars():
    _, rep = sr.plan(ZEC, HELD, price=1545.27)
    assert rep["cost_basis_removed_usd"] > 0
    assert rep["market_value_removed_usd"] > 0
    assert rep["cost_basis_removed_usd"] != rep["market_value_removed_usd"]


def test_it_does_not_invent_a_realised_pnl():
    """The trim log records USD and a timestamp, not units or a fill
    price. A number here would be a guess that is indistinguishable from
    a measurement afterwards."""
    _, rep = sr.plan(ZEC, HELD, price=1545.27)
    assert "No P&L is booked" in rep["detail"]
    assert not any(k.startswith("realized") or k.startswith("pnl") for k in rep)


def test_it_says_plainly_that_this_is_not_a_loss():
    _, rep = sr.plan(ZEC, HELD, price=1545.27)
    assert "NOT a loss" in rep["detail"]
    assert "already in the wallet as cash" in rep["detail"]


def test_a_branch_that_is_fully_backed_changes_nothing():
    actions, rep = sr.plan(ZEC, sum(s["qty"] for s in ZEC))
    assert actions == []
    assert rep["status"] == "OK"


def test_more_coin_held_than_tracked_also_changes_nothing():
    actions, rep = sr.plan(ZEC, 99.0)
    assert actions == []
    assert rep["status"] == "OK"


def test_an_unreadable_holding_writes_nothing_off():
    """A gap is not a zero - and a zero here would delete every slice."""
    actions, rep = sr.plan(ZEC, None)
    assert actions == []
    assert rep["status"] == "UNKNOWN"


def test_a_dust_remainder_removes_the_whole_slice():
    """Leaving 0.3% of a rung behind creates a slice too small to sell."""
    one = [{"id": 1, "qty": 1.0, "entry_price": 100.0, "opened_at": "2026-09-20"}]
    actions, _ = sr.plan(one, 0.001)
    assert actions[0]["action"] == "REMOVE"
    assert actions[0]["qty_after"] == 0.0


def test_a_real_partial_is_reduced_not_removed():
    one = [{"id": 1, "qty": 1.0, "entry_price": 100.0, "opened_at": "2026-09-20"}]
    actions, _ = sr.plan(one, 0.6)
    assert actions[0]["action"] == "REDUCE"
    assert abs(actions[0]["qty_after"] - 0.6) < 1e-9


def test_a_slice_with_no_age_is_not_the_first_thing_deleted():
    mixed = [{"id": 9, "qty": 1.0, "entry_price": 100.0, "opened_at": None},
             {"id": 1, "qty": 1.0, "entry_price": 100.0, "opened_at": "2026-09-20"}]
    actions, _ = sr.plan(mixed, 1.0)
    assert actions[0]["slice_id"] == 1, "unknown age sorts last, not first"


def test_an_unpriced_slice_is_named_and_the_basis_called_a_floor():
    one = [{"id": 1, "qty": 1.0, "entry_price": None, "opened_at": "2026-09-20"}]
    _, rep = sr.plan(one, 0.0)
    assert rep["unpriced_slices"] == [1]
    assert "the real figure is larger" in rep["detail"]


def test_the_whole_fleet_shortfall_is_reproducible():
    """Every coin coin_tracked_is_held flagged plans cleanly."""
    for held, tracked in ((1.331766, 1.418971), (0.130640, 0.162240),
                          (1417.099564, 1486.925496), (0.308254, 0.687577),
                          (39093467.068798, 47389839.608853)):
        one = [{"id": 1, "qty": tracked, "entry_price": 1.0, "opened_at": "2026-09-20"}]
        actions, rep = sr.plan(one, held)
        assert rep["status"] == "READY"
        assert abs(rep["units_removed"] - (tracked - held)) < 1e-6


# ── the endpoint's own guards ────────────────────────────────────────────

def _endpoint_src():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "reconcile_slices_endpoint":
            return ast.get_source_segment(src, n)
    raise AssertionError("reconcile_slices_endpoint not found")


def test_the_endpoint_is_dry_run_by_default():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "reconcile_slices_endpoint")
    defaults = dict(zip([a.arg for a in fn.args.args[-len(fn.args.defaults):]],
                        fn.args.defaults))
    assert defaults["dry_run"].value is True
    assert defaults["accept_writeoff"].value is False


def test_deleting_basis_needs_two_deliberate_flags():
    src = _endpoint_src()
    assert "if not accept_writeoff:" in src
    assert "not reversible" in src


def test_an_unreadable_wallet_refuses_rather_than_deleting_everything():
    """A zero here would delete every slice on the fleet."""
    src = _endpoint_src()
    assert "A gap is not a zero" in src
    assert "delete" in src and "every slice on the fleet" in src


def test_a_coin_absent_from_the_reading_is_skipped_not_zeroed():
    src = _endpoint_src()
    assert "NOT_IN_WALLET_READING" in src
    assert "never zero" in src


def test_it_reuses_the_planner_rather_than_deciding_inline():
    src = _endpoint_src()
    assert "slice_reconcile.plan(" in src
    for invented in ("sort(", "oldest", "dust_fraction"):
        assert invented not in src, f"the endpoint must not re-decide {invented}"
