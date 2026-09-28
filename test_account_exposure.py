"""60% of the ACCOUNT, not 60% of one bot's memory of itself.

market_brain's can_open_position sums State.positions - the trades
market_brain itself opened, persisted to brain_state.json on Railway's
ephemeral disk. Two live-money consequences:

  it cannot see prop_bot, which trades the same account
  it forgets its own positions on every redeploy

Measured on the real account, 2026-09-28: META $705.90 of $980.16
equity is 72.0% exposure, already past the 60% ceiling. market_brain
starting from an empty brain_state.json measures 0% and would allow a
further 60% - about 132% combined.
"""
import account_exposure as ae

EQ = 980.16
REAL = [{"symbol": "META", "market_value": 705.90}]
CAP = 0.60


def test_the_real_book_and_the_bots_own_book_disagree_dangerously():
    over, _why, _ = ae.can_open(REAL, EQ, 122.43, CAP)
    blind, _w2, _ = ae.can_open([], EQ, 122.43, CAP)
    assert over is False, "the real account is already past 60% and must refuse"
    assert blind is True, "an empty book allows it - this is the hazard, stated"


def test_exposure_is_measured_off_the_whole_account():
    assert round(ae.exposure_fraction(REAL, EQ), 4) == round(705.90 / EQ, 4)


def test_positions_sum_across_symbols():
    book = [{"symbol": "META", "market_value": 300.0},
            {"symbol": "AAPL", "market_value": 200.0}]
    assert round(ae.exposure_fraction(book, 1000.0), 4) == 0.5


def test_a_short_counts_as_exposure_not_as_a_credit():
    # A negative market_value is still risk. Netting it against a long
    # would report an under-exposed account.
    book = [{"symbol": "META", "market_value": 300.0},
            {"symbol": "SH", "market_value": -200.0}]
    assert round(ae.exposure_fraction(book, 1000.0), 4) == 0.5


def test_a_row_that_cannot_be_priced_refuses_rather_than_counting_zero():
    # A partial book UNDERSTATES exposure, which is the dangerous
    # direction - it is the whole reason this file exists.
    book = [{"symbol": "META", "market_value": None, "qty": None, "current_price": None}]
    assert ae.exposure_fraction(book, EQ) is None
    ok, why, _ = ae.can_open(book, EQ, 10.0, CAP)
    assert ok is False and "unreadable" in why


def test_market_value_can_be_reconstructed_from_qty_and_price():
    book = [{"symbol": "META", "qty": 0.98085, "current_price": 719.7}]
    assert round(ae.exposure_fraction(book, 1000.0), 3) == round(0.98085 * 719.7 / 1000.0, 3)


def test_a_row_with_no_symbol_refuses():
    assert ae.exposure_fraction([{"market_value": 100.0}], 1000.0) is None


def test_unreadable_inputs_all_refuse():
    for kwargs in ({"equity": 0}, {"equity": None}, {"equity": "lots"},
                   {"new_notional": 0}, {"new_notional": float("nan")},
                   {"max_exposure": None}, {"max_exposure": 0}):
        args = {"positions": [], "equity": EQ, "new_notional": 10.0, "max_exposure": CAP}
        args.update(kwargs)
        ok, why, _ = ae.can_open(**args)
        assert ok is False, (kwargs, why)


def test_an_order_inside_the_ceiling_is_allowed():
    # A gate that refuses everything is not a gate.
    book = [{"symbol": "META", "market_value": 100.0}]
    ok, why, proj = ae.can_open(book, 1000.0, 100.0, CAP)
    assert ok is True, why
    assert round(proj, 3) == 0.2


def test_the_ceiling_is_inclusive_not_exclusive():
    # Exactly 60% is allowed; a cent more is not.
    book = [{"symbol": "META", "market_value": 500.0}]
    assert ae.can_open(book, 1000.0, 100.0, CAP)[0] is True
    assert ae.can_open(book, 1000.0, 100.01, CAP)[0] is False


def test_the_reason_names_what_a_human_needs():
    ok, why, _ = ae.can_open(REAL, EQ, 122.43, CAP)
    assert ok is False
    assert "72.0%" in why and "60%" in why and "122.43" in why


def test_it_only_answers_and_never_acts():
    import ast, inspect
    tree = ast.parse(inspect.getsource(ae))
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("post", "put", "delete", "submit", "place", "send", "write"):
        assert forbidden not in verbs, f"calls .{forbidden}() - it must only decide"


def test_it_agrees_with_market_brains_own_ceiling():
    # The number is the owner's, read from their module, not restated here.
    import market_brain as mb
    assert CAP == mb.CONFIG["max_exposure"]


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
