"""The rise route must clear a floor, not merely sit above zero.

MEASURED CAUSE, 2026-10-10. APE-USD closed -$0.10 tagged profit_target, the
first negative that rule had booked in 226 closes. Not a pricing bug: the gate
decided at 0.15332 where the slice netted +$0.0495 on a $76.11 basis - 0.065%
of basis - and the fill landed at 0.15302, 0.1957% worse. The fee model was if
anything conservative (0.6946% implied against 0.700% modelled).

_pick_profitable_slice_to_sell certified on bare `net > 0`. The parked route
has had GRID_PARKED_MIN_NET_PCT since it was written; this one had nothing,
and _pick_parked_slice_to_sell's own docstring says so: "any profit clears a
gate with no floor under it."

These tests pin the floor AND pin that it never loosens anything.
"""
import crypto_grid_bot as grid


class S:
    """A slice as the picker reads it: qty, entry_price, and the attributes
    _slice_rate looks for."""
    def __init__(self, qty, entry_price, entry_fee_rate=None, adopted=False, sid=None):
        self.qty = qty
        self.entry_price = entry_price
        self.entry_fee_rate = entry_fee_rate
        self.adopted = adopted
        self.id = sid
        self.slice_state = "ACCOUNTED"


RATE = 0.007          # the account's measured maker round trip
APE_QTY, APE_ENTRY = 500.19, 0.15216
APE_DECISION_PRICE = 0.15332


def _net(s, price, rate=RATE):
    return grid._grid_slice_net_pnl(s.qty, s.entry_price, price, rate)


def test_the_ape_slice_is_what_the_old_gate_let_through():
    """Anchors the arithmetic the rest of this file depends on."""
    s = S(APE_QTY, APE_ENTRY)
    net = _net(s, APE_DECISION_PRICE)
    basis = APE_QTY * APE_ENTRY
    assert 0 < net < 0.06, f"the real slice netted about +$0.05, got {net}"
    assert 0.0005 < net / basis < 0.0008, \
        f"it cleared by about 0.065% of basis, got {net / basis:.6%}"


def test_floor_zero_is_exactly_the_old_behaviour():
    s = S(APE_QTY, APE_ENTRY)
    assert grid._pick_profitable_slice_to_sell(
        [s], APE_DECISION_PRICE, RATE, min_net_pct=0.0) is s


def test_the_default_floor_refuses_the_ape_slice():
    s = S(APE_QTY, APE_ENTRY)
    assert grid._pick_profitable_slice_to_sell(
        [s], APE_DECISION_PRICE, RATE, min_net_pct=grid.GRID_RISE_MIN_NET_PCT) is None


def test_the_shipped_default_is_the_measured_one():
    """0.15% is where the sweep stopped the loss without yet paying for it.
    A change here should be a deliberate one, with its own measurement."""
    assert grid.GRID_RISE_MIN_NET_PCT == 0.0015


def test_the_floor_only_ever_refuses_more_never_less():
    """A losing slice is refused at every floor, including zero. The floor
    narrows the gate; it must never widen it."""
    losing = S(100.0, 1.00)
    for floor in (0.0, 0.0015, 0.05):
        assert grid._pick_profitable_slice_to_sell(
            [losing], 0.90, RATE, min_net_pct=floor) is None


def test_fifo_is_preserved_among_slices_that_clear():
    """The oldest qualifying slice still wins - the floor changes who
    qualifies, not the order they are considered in."""
    older = S(100.0, 1.00, sid="older")
    newer = S(100.0, 0.50, sid="newer")     # far more profitable
    got = grid._pick_profitable_slice_to_sell(
        [older, newer], 1.30, RATE, min_net_pct=0.0015)
    assert got is older, "FIFO must still decide between slices that both clear"


def test_a_thin_slice_is_skipped_and_a_later_fat_one_is_taken():
    """The floor must not abort the walk at the first thin slice - that would
    strand a branch holding one marginal rung in front of a good one."""
    thin = S(APE_QTY, APE_ENTRY, sid="thin")          # +0.065% of basis
    fat = S(100.0, 1.00, sid="fat")
    got = grid._pick_profitable_slice_to_sell(
        [thin, fat], APE_DECISION_PRICE, RATE, min_net_pct=0.0015)
    assert got is None or got is fat
    # and with a price where the fat slice really is well clear:
    fat2 = S(100.0, 0.10, sid="fat2")
    got2 = grid._pick_profitable_slice_to_sell(
        [thin, fat2], APE_DECISION_PRICE, RATE, min_net_pct=0.0015)
    assert got2 is fat2, "a thin slice must not block a later qualifying one"


def test_a_zero_basis_row_is_skipped_not_divided_by():
    """A dust or zero-quantity row has no percentage to judge."""
    zero = S(0.0, 1.00)
    noprice = S(100.0, 0.0)
    assert grid._pick_profitable_slice_to_sell(
        [zero, noprice], 2.00, RATE, min_net_pct=0.0015) is None


def test_a_clearly_profitable_slice_still_sells():
    """The floor is 0.15%; a slice well above it must be unaffected."""
    s = S(100.0, 1.00)
    got = grid._pick_profitable_slice_to_sell([s], 1.10, RATE, min_net_pct=0.0015)
    assert got is s
    assert _net(s, 1.10) / (100.0 * 1.00) > 0.0015


def test_the_floor_is_a_share_of_basis_not_a_dollar_amount():
    """A small slice and a large one at the same percentage must be treated
    the same - the fleet holds everything from 0.0002 BTC to 23,530 JASMY."""
    small = S(10.0, 1.00)
    large = S(10000.0, 1.00)
    for s in (small, large):
        assert grid._pick_profitable_slice_to_sell(
            [s], 1.002, RATE, min_net_pct=0.0015) is None
        assert grid._pick_profitable_slice_to_sell(
            [s], 1.05, RATE, min_net_pct=0.0015) is s


def test_the_floor_can_let_a_newer_slice_sell_before_an_older_one():
    """A REAL behavioural consequence, pinned here rather than discovered later.

    FIFO is preserved among slices that qualify, but the floor changes WHO
    qualifies - so an older slice sitting just above zero is now skipped and a
    newer, better one can go out ahead of it. That is a departure from strict
    oldest-first, and it is deliberate: it is the same trade-off
    _pick_parked_slice_to_sell already makes for the parked route, where
    picking the first marginally-positive slice let a +0.1% rung mask a +3.0%
    one. Named explicitly so nobody reads it as an accident."""
    older_marginal = S(APE_QTY, APE_ENTRY, sid="older_marginal")   # +0.065%
    newer_good = S(100.0, 0.10, sid="newer_good")                  # far clear

    at_zero = grid._pick_profitable_slice_to_sell(
        [older_marginal, newer_good], APE_DECISION_PRICE, RATE, min_net_pct=0.0)
    assert at_zero is older_marginal, "at floor 0 the old oldest-first rule holds"

    at_floor = grid._pick_profitable_slice_to_sell(
        [older_marginal, newer_good], APE_DECISION_PRICE, RATE,
        min_net_pct=grid.GRID_RISE_MIN_NET_PCT)
    assert at_floor is newer_good, \
        "the floor skips the marginal older slice - a newer one sells first"
