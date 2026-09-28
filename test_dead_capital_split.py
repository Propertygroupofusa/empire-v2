"""$2,240.54 sitting two basis points from a sale is not dead capital.

no_dead_capital reported one lump:

    $6,557.08 across 10 branch(es) can neither buy (full) nor sell
    (underwater): XLM, BCH, SOL, LINK, ACH, PEPE, ZEC, XRP, ETH, SHIB

Measured on the live fleet, more than half of that was within one ordinary
hour's move of selling - XRP-USD, $2,240.54 of it, was at -0.02%. Lumping
that with BCH at -4.98% made the figure useless for deciding anything.

Nothing here changes trading behaviour. No threshold moves, nothing sells
sooner. It only stops "about to trade" being reported as "stranded".
"""
import invariants as inv

LIVE = [
    ("XRP-USD", 2240.54, 10, 3, -0.02),
    ("ETH-USD", 400.00, 4, 3, -0.56),
    ("XLM-USD", 691.51, 4, 3, -0.67),
    ("LINK-USD", 127.11, 4, 3, -0.85),
    ("SOL-USD", 128.48, 4, 3, -0.93),
    ("SHIB-USD", 267.37, 7, 3, -1.22),
    ("ACH-USD", 91.16, 3, 3, -1.34),
    ("PEPE-USD", 166.54, 7, 3, -1.41),
    ("ZEC-USD", 2272.62, 7, 3, -2.72),
    ("BCH-USD", 171.75, 4, 3, -4.98),
]


def rows(items=LIVE):
    return [{"product_id": p, "allocated_usd": u, "open_slices": n,
             "num_levels": lv, "best_slice_net_pct": b} for p, u, n, lv, b in items]


def test_the_live_fleet_splits_into_stuck_and_between_fills():
    r = inv.no_dead_capital(rows())
    assert r["status"] == inv.FAIL
    assert r["stuck_usd"] == 2969.44
    assert r["near_exit_usd"] == 3587.64
    assert round(r["stuck_usd"] + r["near_exit_usd"], 2) == 6557.08


def test_xrp_two_basis_points_out_is_not_counted_as_stranded():
    r = inv.no_dead_capital(rows())
    assert "XRP-USD" in r["near_exit"]
    assert "XRP-USD" not in r["branches"]


def test_the_genuinely_underwater_ones_still_fail_loudly():
    r = inv.no_dead_capital(rows())
    for coin in ("ZEC-USD", "BCH-USD", "PEPE-USD", "SHIB-USD", "ACH-USD"):
        assert coin in r["branches"], coin
    assert "held, not gridded" in r["detail"]


def test_the_distance_is_printed_so_the_number_can_be_judged():
    """The whole point: -0.02% and -4.98% must not read the same."""
    d = inv.no_dead_capital(rows())["detail"]
    assert "XRP-USD (-0.02%)" in d
    assert "BCH-USD (-4.98%)" in d


def test_only_near_misses_is_a_pass_not_a_failure():
    r = inv.no_dead_capital(rows(LIVE[:5]))
    assert r["status"] == inv.OK
    assert "no branch is stranded" in r["detail"]
    assert r["near_exit_usd"] == 3587.64


def test_a_branch_in_profit_is_not_dead_at_all():
    r = inv.no_dead_capital(rows([("QNT-USD", 149.91, 3, 3, 61.0)]))
    assert r["status"] == inv.OK
    assert not r.get("near_exit")


def test_a_branch_with_a_free_level_is_never_dead_however_underwater():
    """It can still buy - that is the other half of the test."""
    r = inv.no_dead_capital(rows([("BTC-USD", 55.56, 2, 3, -9.0)]))
    assert r["status"] == inv.OK


def test_an_unreadable_branch_is_unknown_never_fabricated_as_stuck():
    """`(pct or 0)` in the caller turned a failed price read into 0.00%,
    and 0 is not less than 0 - so an unreadable slice made its branch look
    exactly like one at break-even and invented dead capital out of a gap.
    """
    r = inv.no_dead_capital(rows([("XRP-USD", 2240.54, 10, 3, None)]))
    assert r["status"] == inv.UNKNOWN
    assert r["unreadable"] == ["XRP-USD"]
    assert "Not read as a pass" in r["detail"]


def test_an_unreadable_branch_does_not_hide_a_real_one():
    r = inv.no_dead_capital(rows([("XRP-USD", 2240.54, 10, 3, None),
                                  ("BCH-USD", 171.75, 4, 3, -4.98)]))
    assert r["status"] == inv.FAIL
    assert r["branches"] == ["BCH-USD"]
    assert r["unreadable"] == ["XRP-USD"]


def test_the_boundary_is_inclusive_and_does_not_move_on_its_own():
    assert inv.NEAR_EXIT_PCT == 1.0
    at = inv.no_dead_capital(rows([("A-USD", 100.0, 3, 3, -1.0)]))
    assert at["status"] == inv.OK, "exactly at the boundary counts as near"
    past = inv.no_dead_capital(rows([("A-USD", 100.0, 3, 3, -1.01)]))
    assert past["status"] == inv.FAIL


def test_the_caller_no_longer_turns_an_unreadable_slice_into_zero():
    """Asserted on the parsed tree - the router comment quotes the broken
    expression verbatim while explaining it."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        last = node.values[-1]
        if isinstance(last, ast.Constant) and last.value == 0:
            for v in node.values[:-1]:
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "get" and v.args
                        and isinstance(v.args[0], ast.Constant)
                        and v.args[0].value == "unrealized_net_pct"):
                    bad.append(node.lineno)
    assert not bad, f"unrealized_net_pct is still defaulted to 0 at lines {bad}"
