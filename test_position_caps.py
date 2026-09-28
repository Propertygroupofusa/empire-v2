"""Six orders of $122.42 are a $734.57 position. Nothing summed them.

The real event, 2026-09-28 13:30:03Z: prop_bot placed SIX separate
market orders for META in one second, 0.163475 shares each at $748.91,
on a $1,007 account. $734.57 - 73% of equity - against a mandate that
caps a position at $120 and total notional at $300, and against the
code's own 50% MAX_RISK_PERCENT.

Every one of the six passed every check that existed, because every
check looked at ONE order, or at positions already FILLED - and none of
the six had filled when the next was sized.

These tests replay those exact six orders.
"""
import position_caps as pc

MANDATE = {"max_per_position": 120.0, "max_total_notional": 300.0,
           "max_open_positions": 4}
QTY, PRICE = 0.163475, 748.91          # the real order, six times over


def replay(capital, n=6, symbol="META", open_notional=0.0, open_count=0):
    led = pc.CapLedger()
    allowed = 0
    for _ in range(n):
        v, _why, notional = pc.check_order(symbol, QTY, PRICE, capital, ledger=led,
                                           open_notional=open_notional,
                                           open_position_count=open_count)
        if v == pc.ALLOW:
            led.record(symbol, notional)
            allowed += 1
    return allowed, round(led.committed_total(), 2)


def test_the_six_meta_orders_do_not_all_get_through():
    allowed, total = replay(MANDATE)
    assert allowed < 6, "all six passed - the cap is not summing the pass"
    assert total < 734.57


def test_a_generous_cap_still_stops_the_runaway():
    # The point is not the exact number; it is that the pass is summed.
    # With room for two orders, the third must be refused.
    allowed, total = replay({"max_per_position": 250.0,
                             "max_total_notional": 500.0,
                             "max_open_positions": 4})
    assert allowed == 2, allowed
    assert total == 244.86            # the fixture's own arithmetic


def test_the_total_cap_sums_ACROSS_SYMBOLS_within_one_pass():
    """The surviving mutant this test was written for.

    Within a single symbol the per-position cap always binds first, so
    every earlier test in this file refused on per-position and the
    TOTAL check was never reached - a build with led.committed_total()
    deleted from the total check passed all fourteen of them. The defect
    that module exists to fix was untested by the file testing it.

    Across DIFFERENT symbols nothing else binds, so the total cap is the
    only thing standing between one pass and an unbounded book.
    """
    cap = {"max_per_position": 1e9, "max_total_notional": 300.0,
           "max_open_positions": 99}
    led = pc.CapLedger()
    got = []
    for sym in ("AAPL", "MSFT", "NVDA", "AMZN"):
        v, why, n = pc.check_order(sym, QTY, PRICE, cap, ledger=led)
        got.append((sym, v))
        if v == pc.ALLOW:
            led.record(sym, n)
    # $122.43 each: two fit under $300, the third does not.
    assert [v for _s, v in got] == [pc.ALLOW, pc.ALLOW, pc.REFUSE, pc.REFUSE], got
    assert round(led.committed_total(), 2) == 244.86


def test_without_a_ledger_every_order_looks_like_the_first():
    # This is precisely the defect, reproduced. A caller that forgets the
    # ledger gets the old behaviour, so the test states the consequence
    # rather than letting it be rediscovered in production.
    cap = {"max_per_position": 250.0, "max_total_notional": 500.0,
           "max_open_positions": 4}
    verdicts = [pc.check_order("META", QTY, PRICE, cap, ledger=None)[0]
                for _ in range(6)]
    assert verdicts == [pc.ALLOW] * 6, verdicts


def test_the_total_cap_counts_positions_already_filled_too():
    cap = {"max_per_position": 250.0, "max_total_notional": 300.0,
           "max_open_positions": 4}
    v, why, _ = pc.check_order("AAPL", QTY, PRICE, cap, ledger=pc.CapLedger(),
                               open_notional=250.0)
    assert v == pc.REFUSE
    assert "max_total_notional" in why


def test_the_position_count_cap_counts_new_symbols_in_this_pass():
    cap = {"max_per_position": 1e9, "max_total_notional": 1e9,
           "max_open_positions": 2}
    led = pc.CapLedger()
    led.record("AAPL", 10.0)
    led.record("MSFT", 10.0)
    v, why, _ = pc.check_order("NVDA", 1, 10.0, cap, ledger=led, open_position_count=0)
    assert v == pc.REFUSE and "max_open_positions" in why


def test_adding_to_a_symbol_already_in_the_pass_is_not_a_new_position():
    cap = {"max_per_position": 1e9, "max_total_notional": 1e9,
           "max_open_positions": 1}
    led = pc.CapLedger(); led.record("AAPL", 10.0)
    v, _why, _ = pc.check_order("AAPL", 1, 10.0, cap, ledger=led, open_position_count=0)
    assert v == pc.ALLOW


# ── it fails CLOSED, every way it can fail ────────────────────────────
# This gates live money. A protection that cannot evaluate itself must
# not wave an order through.

def test_a_missing_mandate_refuses():
    assert pc.check_order("META", QTY, PRICE, None)[0] == pc.REFUSE


def test_an_unreadable_cap_refuses():
    for bad in (None, "", "lots", float("nan"), float("inf")):
        cap = dict(MANDATE, max_total_notional=bad)
        v, why, _ = pc.check_order("META", QTY, PRICE, cap, ledger=pc.CapLedger())
        assert v == pc.REFUSE, (bad, why)


def test_an_unreadable_price_or_qty_refuses():
    for q, p in ((None, PRICE), (QTY, None), (float("nan"), PRICE),
                 (QTY, float("inf")), ("a lot", PRICE)):
        assert pc.check_order("META", q, p, MANDATE)[0] == pc.REFUSE


def test_a_non_positive_order_refuses():
    assert pc.check_order("META", 0, PRICE, MANDATE)[0] == pc.REFUSE
    assert pc.check_order("META", -1, PRICE, MANDATE)[0] == pc.REFUSE


def test_an_unreadable_open_notional_refuses():
    v, why, _ = pc.check_order("META", QTY, PRICE, MANDATE, ledger=pc.CapLedger(),
                               open_notional="unknown")
    assert v == pc.REFUSE and "open notional" in why


def test_it_can_only_refuse_and_never_place():
    # The shape every guard in this repo is held to: one-directional.
    import ast, inspect
    src = inspect.getsource(pc)
    tree = ast.parse(src)
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("post", "put", "delete", "submit", "place", "send"):
        assert forbidden not in verbs, f"this module calls .{forbidden}() - it must only decide"


def test_an_order_inside_every_cap_is_allowed():
    # A gate that refuses everything is not a gate.
    cap = {"max_per_position": 500.0, "max_total_notional": 1000.0,
           "max_open_positions": 4}
    v, why, n = pc.check_order("META", QTY, PRICE, cap, ledger=pc.CapLedger())
    assert v == pc.ALLOW, why
    assert round(n, 2) == 122.43


def test_the_reason_names_the_numbers_a_human_needs():
    v, why, _ = pc.check_order("META", QTY, PRICE, MANDATE, ledger=pc.CapLedger())
    assert v == pc.REFUSE
    assert "META" in why and "120" in why and "122.43" in why


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
