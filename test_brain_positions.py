"""A bot that opens positions and then forgets them is worse than one
that never opened them.

market_brain manages every exit by iterating state.positions, persisted
to brain_state.json on Railway's ephemeral disk - wiped on every push.
Empty book, empty exit loop: no stop, no take-profit, no trailing exit,
no force-close, on a position that is still open. ~20 deploys happened
on 28 Sep alone.
"""
import brain_positions as bp

EQ = 980.16
BROKER = [{"symbol": "META", "market_value": 705.90, "qty": 0.98085,
           "current_price": 719.7, "avg_entry_price": 748.91}]


def test_a_wiped_book_adopts_what_the_broker_holds():
    """The whole reason this module exists."""
    new, changes = bp.reconcile({}, BROKER, EQ)
    assert ("adopted", "META") in changes
    assert "META" in new, "the position would have been left unmanaged"
    assert new["META"]["entry_price"] == 748.91
    assert new["META"]["adopted_from_broker"] is True


def test_an_adopted_position_carries_what_the_exit_loop_needs():
    # mtf_exit(entry_px, current, peak_px) needs an entry price; the
    # other two degrade safely to the current price inside market_brain.
    new, _ = bp.reconcile({}, BROKER, EQ)
    pos = new["META"]
    assert pos.get("entry_price") is not None
    assert pos.get("cycles") == 0          # the force-close timer restarts


def test_a_known_position_keeps_its_peak_price():
    """Re-deriving peak_price would reset a trailing stop that is
    currently protecting a gain - the one field that must survive."""
    book = {"META": {"entry_price": 748.91, "peak_price": 800.0, "cycles": 4}}
    new, changes = bp.reconcile(book, BROKER, EQ)
    assert ("kept", "META") in changes
    assert new["META"]["peak_price"] == 800.0
    assert new["META"]["cycles"] == 4


def test_a_position_closed_elsewhere_is_dropped():
    # A human, another bot, or a broker stop closed it. Managing it
    # further would be managing something that does not exist.
    book = {"AAPL": {"entry_price": 100.0}}
    new, changes = bp.reconcile(book, BROKER, EQ)
    assert ("dropped", "AAPL") in changes
    assert "AAPL" not in new


def test_alloc_is_recomputed_from_the_real_market_value():
    new, _ = bp.reconcile({}, BROKER, EQ)
    assert round(new["META"]["alloc"], 4) == round(705.90 / EQ, 4)


def test_a_short_allocates_by_absolute_value():
    short = [{"symbol": "SH", "market_value": -200.0, "qty": -2.0,
              "current_price": 100.0, "avg_entry_price": 100.0}]
    new, _ = bp.reconcile({}, short, 1000.0)
    assert new["SH"]["alloc"] == 0.2


def test_an_unpriceable_row_refuses_the_whole_reconcile():
    """A PARTIAL reconcile is worse than none: dropping a symbol the
    broker actually holds orphans the exact position this protects."""
    bad = [{"symbol": "META", "market_value": None, "qty": None,
            "current_price": None}]
    new, reason = bp.reconcile({"META": {"entry_price": 1.0}}, bad, EQ)
    assert new is None
    assert "partial reconcile" in reason


def test_a_row_with_no_symbol_refuses():
    new, reason = bp.reconcile({}, [{"market_value": 100.0}], EQ)
    assert new is None and "no symbol" in reason


def test_unreadable_equity_refuses():
    for bad in (0, -1, None, "lots"):
        new, reason = bp.reconcile({}, BROKER, bad)
        assert new is None, bad


def test_an_empty_broker_and_an_empty_book_is_simply_empty():
    new, changes = bp.reconcile({}, [], EQ)
    assert new == {} and changes == []


def test_it_does_not_mutate_the_book_it_was_given():
    book = {"META": {"entry_price": 748.91, "peak_price": 800.0}}
    snapshot = {k: dict(v) for k, v in book.items()}
    bp.reconcile(book, BROKER, EQ)
    assert book == snapshot, "the caller's book was mutated in place"


def test_it_opens_and_closes_nothing():
    import ast, inspect
    tree = ast.parse(inspect.getsource(bp))
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("post", "put", "delete", "submit", "place", "send", "close"):
        assert forbidden not in verbs, f"calls .{forbidden}() - it must only compute"


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
