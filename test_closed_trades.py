"""Round-trip pairing - and the two ways the old one got it wrong."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from closed_trades import pair_round_trips  # noqa: E402

KNOWN = ("entry_pass", "exit_pass", "branch_entry", "branch_exit",
         "idle_cash_sweep", "opening_bar_entry", "opening_bar_exit",
         "unlabelled")


def O(symbol, side, qty, price, at, oid=None, coid=None):
    return {"symbol": symbol, "side": side, "filled_qty": str(qty),
            "filled_avg_price": str(price), "filled_at": at,
            "id": oid or f"{symbol}-{side}-{at}", "client_order_id": coid}


# ── BUG 1: the orders arrive newest-first ──────────────────────────────

def test_an_exit_never_predates_its_own_entry():
    """The whole visible symptom: 39 of 39 rows showed an exit BEFORE
    its entry, because desc-ordered orders were walked by an algorithm
    that assumes ascending."""
    orders = [
        O("USO", "buy", 1, 149.41, "2026-09-28T16:40:14Z"),
        O("USO", "sell", 1, 149.23, "2026-09-21T13:30:32Z"),
        O("USO", "buy", 1, 148.00, "2026-09-21T13:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    for t in out["trades"]:
        assert t["entry_at"] < t["exit_at"], t


def test_input_order_does_not_change_the_result():
    """Fed desc or asc, the pairing must be identical - the old code's
    answer depended entirely on which way the API happened to sort."""
    orders = [
        O("SH", "buy", 1, 32.00, "2026-09-20T14:00:00Z"),
        O("SH", "sell", 1, 32.50, "2026-09-20T18:00:00Z"),
        O("SH", "buy", 1, 31.00, "2026-09-22T14:00:00Z"),
        O("SH", "sell", 1, 33.00, "2026-09-22T18:00:00Z"),
    ]
    asc = pair_round_trips(orders, KNOWN)
    desc = pair_round_trips(list(reversed(orders)), KNOWN)
    assert asc["trades"] == desc["trades"]
    assert asc["totals"]["realised_pnl"] == desc["totals"]["realised_pnl"] == 2.5


def test_the_sell_pairs_with_the_buy_that_preceded_it():
    orders = [
        O("SH", "buy", 1, 30.00, "2026-09-20T14:00:00Z"),
        O("SH", "sell", 1, 32.00, "2026-09-20T18:00:00Z"),
        O("SH", "buy", 1, 99.00, "2026-09-25T14:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert len(out["trades"]) == 1
    assert out["trades"][0]["entry_price"] == 30.00
    assert out["trades"][0]["pnl"] == 2.0


# ── BUG 2: one lot per symbol, silently overwritten ────────────────────

def test_six_buys_of_one_symbol_are_six_lots_not_one():
    """The six META buys of 28 Sep collapsed to one in a dict keyed by
    symbol. Five real entries vanished and $612 of cost basis with
    them."""
    orders = [O("META", "buy", 0.163475, 748.91,
                f"2026-09-28T13:30:03.{800+i:03d}Z", oid=f"b{i}")
              for i in range(6)]
    orders.append(O("META", "sell", 0.98085, 722.28, "2026-09-28T16:39:50Z"))
    out = pair_round_trips(orders, KNOWN)
    assert out["totals"]["round_trips"] == 6
    assert sum(t["qty"] for t in out["trades"]) == pytest.approx(0.98085)


def test_the_pnl_covers_every_lot_the_sell_consumed():
    orders = [O("META", "buy", 0.163475, 748.91,
                f"2026-09-28T13:30:03.{800+i:03d}Z", oid=f"b{i}")
              for i in range(6)]
    orders.append(O("META", "sell", 0.98085, 722.28, "2026-09-28T16:39:50Z"))
    out = pair_round_trips(orders, KNOWN)
    assert out["totals"]["realised_pnl"] == pytest.approx(-26.12, abs=0.02)


def test_a_sell_can_span_several_lots():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z", oid="b1"),
        O("X", "buy", 1, 12.0, "2026-09-01T11:00:00Z", oid="b2"),
        O("X", "sell", 2, 15.0, "2026-09-01T12:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["totals"]["round_trips"] == 2
    assert out["totals"]["realised_pnl"] == pytest.approx(8.0)


def test_lots_are_consumed_oldest_first():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z", oid="b1"),
        O("X", "buy", 1, 20.0, "2026-09-01T11:00:00Z", oid="b2"),
        O("X", "sell", 1, 15.0, "2026-09-01T12:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["trades"][0]["entry_price"] == 10.0
    assert out["open_lots"][0]["entry_price"] == 20.0


def test_a_partial_sell_leaves_the_rest_of_the_lot_open():
    orders = [
        O("X", "buy", 2, 10.0, "2026-09-01T10:00:00Z"),
        O("X", "sell", 0.5, 12.0, "2026-09-01T12:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["trades"][0]["qty"] == pytest.approx(0.5)
    assert out["open_lots"][0]["qty"] == pytest.approx(1.5)


# ── what it refuses to invent ──────────────────────────────────────────

def test_a_sell_with_no_open_lot_is_unmatched_not_a_trade():
    """Its buy is older than the window. A guessed entry price would put
    a fabricated P&L on a page the owner reads for decisions."""
    out = pair_round_trips([O("Z", "sell", 1, 50.0, "2026-09-01T12:00:00Z")], KNOWN)
    assert out["totals"]["round_trips"] == 0
    assert out["totals"]["unmatched_sells"] == 1
    assert out["totals"]["realised_pnl"] == 0.0


def test_an_unmatched_sell_says_why():
    out = pair_round_trips([O("Z", "sell", 1, 50.0, "2026-09-01T12:00:00Z")], KNOWN)
    assert "older than the orders fetched" in out["unmatched_sells"][0]["why"]


def test_a_sell_larger_than_the_open_lots_reports_only_the_remainder():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z"),
        O("X", "sell", 3, 12.0, "2026-09-01T12:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["trades"][0]["qty"] == pytest.approx(1.0)
    assert out["unmatched_sells"][0]["qty"] == pytest.approx(2.0)
    assert out["unmatched_sells"][0]["partially_matched"] is True


def test_a_still_open_buy_is_not_a_closed_trade():
    out = pair_round_trips([O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z")], KNOWN)
    assert out["totals"]["round_trips"] == 0
    assert out["totals"]["open_lots"] == 1


def test_symbols_do_not_borrow_each_others_lots():
    orders = [
        O("A", "buy", 1, 10.0, "2026-09-01T10:00:00Z"),
        O("B", "sell", 1, 99.0, "2026-09-01T11:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["totals"]["round_trips"] == 0
    assert out["totals"]["unmatched_sells"] == 1
    assert out["totals"]["open_lots"] == 1


# ── bad rows are dropped, never coerced ────────────────────────────────

def test_an_unfilled_order_is_not_a_fill():
    out = pair_round_trips([
        {"symbol": "X", "side": "buy", "filled_qty": "1",
         "filled_avg_price": "10", "filled_at": None},
    ], KNOWN)
    assert out["totals"]["open_lots"] == 0


@pytest.mark.parametrize("price", ["0", "0.0", None, "", "abc"])
def test_a_zero_or_unreadable_price_is_not_a_free_trade(price):
    out = pair_round_trips([
        {"symbol": "X", "side": "buy", "filled_qty": "1",
         "filled_avg_price": price, "filled_at": "2026-09-01T10:00:00Z"},
    ], KNOWN)
    assert out["totals"]["open_lots"] == 0


def test_a_zero_quantity_is_not_a_fill():
    out = pair_round_trips([O("X", "buy", 0, 10.0, "2026-09-01T10:00:00Z")], KNOWN)
    assert out["totals"]["open_lots"] == 0


def test_no_orders_is_an_empty_answer_not_a_crash():
    out = pair_round_trips([], KNOWN)
    assert out["totals"]["round_trips"] == 0
    assert out["totals"]["realised_pnl"] == 0.0


def test_none_is_an_empty_answer_not_a_crash():
    assert pair_round_trips(None, KNOWN)["totals"]["round_trips"] == 0


# ── precision and attribution ──────────────────────────────────────────

def test_fractional_quantities_survive_at_full_precision():
    """Rounding 0.163475 to 2dp displays 0.16 and the page stops adding
    up - the old endpoint did exactly that."""
    orders = [
        O("META", "buy", 0.163475, 100.0, "2026-09-01T10:00:00Z"),
        O("META", "sell", 0.163475, 110.0, "2026-09-01T11:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["trades"][0]["qty"] == pytest.approx(0.163475, abs=1e-9)


def test_both_legs_carry_their_source_when_tagged():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z", coid="entry_pass-X-abc"),
        O("X", "sell", 1, 12.0, "2026-09-01T11:00:00Z", coid="exit_pass-X-def"),
    ]
    t = pair_round_trips(orders, KNOWN)["trades"][0]
    assert t["entry_source"] == "entry_pass"
    assert t["exit_source"] == "exit_pass"


def test_an_untagged_leg_is_unattributable_not_defaulted():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z", coid="a1b2-c3d4-e5f6"),
        O("X", "sell", 1, 12.0, "2026-09-01T11:00:00Z"),
    ]
    t = pair_round_trips(orders, KNOWN)["trades"][0]
    assert t["entry_source"] is None
    assert t["exit_source"] is None


def test_trades_are_returned_newest_exit_first():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z"),
        O("X", "sell", 1, 11.0, "2026-09-01T11:00:00Z"),
        O("X", "buy", 1, 10.0, "2026-09-02T10:00:00Z"),
        O("X", "sell", 1, 11.0, "2026-09-02T11:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["trades"][0]["exit_at"] > out["trades"][1]["exit_at"]


def test_winners_and_losers_are_counted_off_the_real_pnl():
    orders = [
        O("X", "buy", 1, 10.0, "2026-09-01T10:00:00Z"),
        O("X", "sell", 1, 11.0, "2026-09-01T11:00:00Z"),
        O("Y", "buy", 1, 10.0, "2026-09-01T10:00:00Z"),
        O("Y", "sell", 1, 9.0, "2026-09-01T11:00:00Z"),
    ]
    out = pair_round_trips(orders, KNOWN)
    assert out["totals"]["winners"] == 1
    assert out["totals"]["losers"] == 1
    assert out["totals"]["realised_pnl"] == 0.0


# ── the endpoint must not reintroduce either defect ────────────────────

import ast as _ast  # noqa: E402

_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "routers", "trading_dashboard.py")
with open(_ROUTER, encoding="utf-8") as _fh:
    _SRC = _fh.read()
_TREE = _ast.parse(_SRC)


def _node(name="get_closed_trades"):
    for node in _ast.walk(_TREE):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _endpoint_node():
    return _node("get_closed_trades")


def _endpoint_src():
    return _ast.get_source_segment(_SRC, _endpoint_node())


def _directions(name):
    """Every literal `direction` in that function's params dicts.

    Read off via AST, not by searching the source text: the first
    version of this test matched the word "desc" in the docstring that
    EXPLAINS the bug and failed on prose. Never match on source
    containing comments."""
    out = []
    for node in _ast.walk(_node(name)):
        if isinstance(node, _ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, _ast.Constant) and k.value == "direction" \
                        and isinstance(v, _ast.Constant):
                    out.append(v.value)
    return out


@pytest.mark.parametrize("name", ["get_closed_trades",
                                  "_alpaca_realized_record"])
def test_the_window_is_the_most_recent_orders_not_the_oldest(name):
    """THIS TEST USED TO PIN THE BUG IN PLACE.

    It asserted direction == "asc" on get_closed_trades, written on
    2026-09-28 against a real defect: orders were fetched newest-first
    and walked by a loop that assumed a buy was seen before its own
    sell, so every row showed an exit before its entry.

    That loop no longer exists. The pairing moved into
    closed_trades.pair_round_trips(), which sorts ascending itself, and
    two tests above cover that directly -
    test_an_exit_never_predates_its_own_entry and
    test_input_order_does_not_change_the_result. The guarantee lives
    where the arithmetic lives, so the fetch no longer has to carry it.

    Meanwhile "asc" with limit=500 means the OLDEST 500 closed orders -
    a window that stops moving the moment the account passes 500 and
    never moves again. Both of these functions published figures from
    the account's first 500 orders while the book turned over daily, and
    nothing flagged it because the number looked stable. Pinning "asc"
    here is what let that survive a year of passing tests.

    _alpaca_realized_record is included because it is the one the owner
    actually reads - it had no test of its own, which is why its copy of
    the same parameter was never questioned."""
    directions = _directions(name)
    assert directions, f"{name} sends no direction parameter at all"
    assert all(d == "desc" for d in directions), directions


def test_the_endpoint_does_not_pair_trades_itself():
    """The pairing belongs in a tested pure function. A second
    implementation beside it is a second thing to drift."""
    src = _endpoint_src()
    assert "closed_trades.pair_round_trips(" in src
    assert "buy_orders" not in src, "the endpoint is pairing orders again"


def test_the_endpoint_does_not_key_open_lots_by_symbol():
    src = _endpoint_src()
    assert "[symbol] = {" not in src


def test_unmatched_sells_and_open_lots_reach_the_reader():
    """They are the honest part of the answer. Dropping them is how a
    page looks complete while leaving money unexplained."""
    src = _endpoint_src()
    assert '"unmatched_sells"' in src
    assert '"open_lots"' in src


def test_the_totals_come_from_the_pairer_not_recomputed():
    """Two numbers that must agree, computed in two places, is this
    codebase's recurring bug."""
    src = _endpoint_src()
    assert 'totals["realised_pnl"]' in src
    assert "sum(t[" not in src
