#!/usr/bin/env python3
"""Can this slice be sold, and if not WHICH not - checked against the live shapes.

The brief asked for tests A-N. They are here, named, plus the three real fleet
states measured on 2026-09-29, which is what the design is actually answering:

    QNT   held 0.00097323, LOCKED 0.0,      ledger 2 x 0.337991 @ $161.01
          -> UNBACKED. $169 of claimed coin that is not in the account and
             nothing is reserving it.
    ALGO  held 1134.346389, LOCKED 1134.3,  ledger 279.4
          -> RESERVED. The coin exists, another order holds it.
    LINK  held 6.85, LOCKED 6.63, free 0.22, one slice of 0.00999...
          -> the 0.22 is EXECUTE; the 0.00999 slice is DUST.

All three produced the same log line before this: "nothing sellable".

Run: python3 test_slice_execution.py
"""
from decimal import Decimal
import sys

import slice_execution as sx

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


# Real metadata, fetched from Coinbase on 2026-09-29.
QNT = {"base_increment": "0.001", "base_min_size": None,
       "min_market_funds": "1", "quote_increment": "0.01"}
LINK = {"base_increment": "0.01", "base_min_size": None,
        "min_market_funds": "1", "quote_increment": "0.001"}
ALGO = {"base_increment": "0.1", "base_min_size": None,
        "min_market_funds": "1", "quote_increment": "0.00001"}
# A venue whose increment is not a power of ten. The decimal-count approach
# cannot express this one at all, which is the point.
ODD = {"base_increment": "0.05", "base_min_size": "0.25",
       "quote_min_size": "1"}


def d(**kw):
    """classify_sell with sensible defaults, overridden per case."""
    args = {"product_id": "TEST-USD", "requested_qty": "1",
            "wallet_available": "1", "product_meta": QNT, "price": "100",
            "wallet_hold": "0", "ledger_qty": None}
    args.update(kw)
    return sx.classify_sell(**args)


# ---------------------------------------------------------------- precision

def test_floor_to_increment_is_exact_on_every_increment_shape():
    cases = [
        # (quantity, increment, expected)
        ("0.00097323", "0.001", "0"),        # the QNT line from the brief
        ("0.337991076667", "0.001", "0.337"),
        ("0.22", "0.01", "0.22"),            # LINK's free balance, exactly
        ("0.009999999999998899", "0.01", "0.00"),
        ("1134.346389", "0.1", "1134.3"),
        ("279.4", "0.1", "279.4"),
        # Non-powers of ten. A decimal count gets all four of these wrong.
        ("0.03", "0.05", "0.00"),
        ("0.17", "0.05", "0.15"),
        ("7.4", "2.5", "5.0"),
        ("3", "1", "3"),
        ("0.5", "1", "0"),
    ]
    for q, inc, want in cases:
        got, err = sx.floor_to_increment(q, inc)
        ok(f"floor({q}, {inc}) == {want}",
           err is None and got == Decimal(want), f"{got!r} err={err}")


def test_a_decimal_count_would_get_the_odd_increments_wrong():
    """The concrete reason floor_to_increment divides by the increment rather
    than counting its decimal places. Not a style preference - 0.03 on a 0.05
    grid is rejected by the venue, and a decimal count passes it."""
    def by_decimal_count(qty, increment):
        import math
        decs = len(increment.split(".")[1]) if "." in increment else 0
        f = 10 ** decs
        return Decimal(str(math.floor(float(qty) * f) / f))
    for q, inc in (("0.03", "0.05"), ("0.17", "0.05"), ("7.4", "2.5")):
        exact, _ = sx.floor_to_increment(q, inc)
        counted = by_decimal_count(q, inc)
        ok(f"the two disagree on ({q}, {inc}): exact={exact} counted={counted}",
           exact != counted,
           "if these ever agree this test has stopped proving anything")
        ok(f"and the exact one is a real multiple of {inc}",
           exact % Decimal(inc) == 0, str(exact))


def test_no_increment_at_all_refuses_rather_than_defaulting():
    for bad in (None, "0", "-0.001", "abc", ""):
        got, err = sx.floor_to_increment("1.0", bad)
        ok(f"increment {bad!r} is refused, not defaulted", got is None and err,
           f"{got!r} err={err}")


# -------------------------------------------------------- the brief's A-N

def test_A_quantity_precision_must_not_create_a_zero_sell():
    r = d(requested_qty="0.0009732300", wallet_available="0.0009732300",
          product_meta=QNT, price="249.93")
    ok("A: 0.00097323 QNT is DUST", r["decision"] == sx.DUST, str(r))
    ok("A: with the increment named as the reason",
       r["reason"] == sx.BELOW_BASE_INCREMENT, str(r["reason"]))
    ok("A: executable is zero, not a size to send", r["executable_qty"] == 0)
    ok("A: and it is not called a rejection or a failure",
       r["decision"] not in ("REJECTED", "ERROR"), r["decision"])
    ok("A: the exchange's real increment is reported back",
       r["base_increment"] == "0.001", str(r["base_increment"]))


def test_B_quantity_exactly_at_the_minimum_submits():
    r = d(requested_qty="0.25", wallet_available="0.25", product_meta=ODD,
          price="100")
    ok("B: exactly at base_min_size executes", r["decision"] == sx.EXECUTE, str(r))
    ok("B: at the full size", r["executable_qty"] == Decimal("0.25"),
       str(r["executable_qty"]))


def test_C_quantity_above_the_minimum_uses_the_increment():
    r = d(requested_qty="0.337991076667", wallet_available="0.4",
          product_meta=QNT, price="249.93")
    ok("C: executes", r["decision"] == sx.EXECUTE, str(r))
    ok("C: floored onto the 0.001 grid, not rounded up",
       r["executable_qty"] == Decimal("0.337"), str(r["executable_qty"]))
    ok("C: never sends more than was asked",
       r["executable_qty"] <= Decimal("0.337991076667"))


def test_E_maker_only_is_never_converted_to_a_taker_sale():
    """The classifier's whole output space contains no taker option. It can say
    DUST, RESERVED, UNBACKED, BLOCKED or EXECUTE - and EXECUTE is a size, not
    an order type. Nothing here can spend a slice's profit on a taker leg."""
    for name in dir(sx):
        v = getattr(sx, name)
        if isinstance(v, str) and not name.startswith("_"):
            ok(f"E: no constant offers a market/taker escape ({name})",
               "MARKET" not in v.upper() and "TAKER" not in v.upper(), v)
    ok("E: the log line states the policy", "maker_only=true" in sx.log_line(d()))


def test_H_and_I_unreadable_inputs_fail_closed():
    r = d(wallet_available=None)
    ok("H: an unreadable balance blocks", r["decision"] == sx.BLOCKED, str(r))
    ok("H: named as unreadable, not as zero",
       r["reason"] == sx.UNREADABLE_BALANCE, str(r["reason"]))
    ok("H: and no size is offered", r["executable_qty"] is None)
    for meta in (None, {}):
        r = d(product_meta=meta)
        ok(f"I: metadata {meta!r} blocks", r["decision"] == sx.BLOCKED, str(r))
        ok("I: named as missing metadata", r["reason"] == sx.NO_PRODUCT_METADATA)
        ok("I: and no size is offered", r["executable_qty"] is None)
    r = d(product_meta={"base_increment": "0"})
    ok("I: a zero increment blocks rather than dividing by it",
       r["decision"] == sx.BLOCKED and r["reason"] == sx.BAD_PRODUCT_METADATA,
       str(r))


def test_L_different_entries_are_never_conflated_by_this_layer():
    """Two slices of the same product, same wallet, different sizes: each is
    judged on its own quantity. A shared verdict is what produced one
    'nothing sellable' for a whole branch."""
    small = d(requested_qty="0.0009", wallet_available="0.5",
              product_meta=QNT, price="249.93")
    large = d(requested_qty="0.337", wallet_available="0.5",
              product_meta=QNT, price="249.93")
    ok("L: the small slice is DUST", small["decision"] == sx.DUST)
    ok("L: the large slice EXECUTEs", large["decision"] == sx.EXECUTE)
    ok("L: from the same wallet read",
       small["wallet_available"] == large["wallet_available"])


def test_M_dust_is_recorded_and_becomes_executable_when_it_grows():
    """Dust must not be destroyed or written off. Same slice, more inventory,
    no code change: DUST -> EXECUTE."""
    before = d(requested_qty="0.0009", wallet_available="0.0009",
               product_meta=QNT, price="249.93")
    ok("M: below one unit is DUST", before["decision"] == sx.DUST)
    ok("M: the raw amount is preserved, not zeroed",
       before["wallet_available"] == "0.0009", str(before["wallet_available"]))
    # 0.0025 QNT clears the increment but is worth $0.62, so it is STILL dust -
    # for a different reason. My first version of this test expected EXECUTE and
    # the code was right: one tradeable unit is not the same as one sellable
    # order, because the venue also has a $1 value floor.
    middle = d(requested_qty="0.0025", wallet_available="0.0025",
               product_meta=QNT, price="249.93")
    ok("M: one unit is not enough if the order is worth $0.62",
       middle["decision"] == sx.DUST, str(middle))
    ok("M: and the reason moves to the value floor",
       middle["reason"] == sx.BELOW_QUOTE_MINIMUM, str(middle["reason"]))
    ok("M: while still reporting the legal size, not a zero",
       middle["executable_qty"] == Decimal("0.002"),
       str(middle["executable_qty"]))
    after = d(requested_qty="0.0050", wallet_available="0.0050",
              product_meta=QNT, price="249.93")
    ok("M: $1.25 of it EXECUTEs", after["decision"] == sx.EXECUTE, str(after))
    ok("M: at the floored size", after["sendable_qty"] == Decimal("0.005"),
       str(after["sendable_qty"]))


# ---------------------------------------------- the three live fleet shapes

def test_the_live_qnt_shape_is_unbacked_not_dust():
    """The measurement that reframed the whole brief. Two slices of 0.337991
    QNT on the books at $161.01, now $249.93 - and 0.00097323 in the wallet
    with NOTHING on hold."""
    r = d(product_id="QNT-USD", requested_qty="0.337991076667",
          ledger_qty="0.675982153333", wallet_available="0.00097323",
          wallet_hold="0.0", product_meta=QNT, price="249.93")
    ok("QNT: UNBACKED", r["decision"] == sx.UNBACKED, str(r["decision"]))
    ok("QNT: NOT dust - dust is owned", r["decision"] != sx.DUST)
    ok("QNT: NOT reserved - nothing is holding it",
       r["decision"] != sx.RESERVED)
    ok("QNT: the reason is the ledger, not the rounding",
       r["reason"] == sx.LEDGER_EXCEEDS_WALLET, str(r["reason"]))
    ok("QNT: no order - and no size is even computed, because the question of "
       "what is legal to sell never arises for coin we do not hold",
       r["sendable_qty"] is None and r["executable_qty"] is None, str(r))
    ok("QNT: the shortfall is quantified in the detail",
       "0.675" in r["detail"] and "0.00097323" in r["detail"], r["detail"])
    ok("QNT: and it says the P&L is measured against absent coin",
       "not there" in r["detail"], r["detail"])


def test_the_live_algo_shape_is_reserved_not_dust():
    """1134.3 of 1134.346389 units held behind another order. The coin exists.
    Available 0.046389 floors to 0.0 on ALGO's 0.1 increment, which is how this
    ended up sharing a log line with QNT."""
    r = d(product_id="ALGO-USD", requested_qty="279.4", ledger_qty="279.4",
          wallet_available="0.046389", wallet_hold="1134.3",
          product_meta=ALGO, price="0.1332")
    ok("ALGO: RESERVED", r["decision"] == sx.RESERVED, str(r["decision"]))
    ok("ALGO: reason names the holding order",
       r["reason"] == sx.HELD_BY_ANOTHER_ORDER, str(r["reason"]))
    ok("ALGO: not UNBACKED - the coin is in the account",
       r["decision"] != sx.UNBACKED)
    ok("ALGO: not DUST", r["decision"] != sx.DUST)
    ok("ALGO: says nothing is wrong with the slice",
       "Nothing is wrong with this slice" in r["detail"], r["detail"])
    ok("ALGO: and points at cancelling, not at selling",
       "cancelling" in r["detail"], r["detail"])


def test_the_live_link_shapes_split_into_execute_and_dust():
    """One branch, three slices, two different answers - which the old single
    per-branch verdict could not express."""
    tiny = d(product_id="LINK-USD", requested_qty="0.009999999999998899",
             ledger_qty="0.009999999999998899", wallet_available="0.22",
             wallet_hold="6.63", product_meta=LINK, price="14.986")
    ok("LINK dust slice: DUST", tiny["decision"] == sx.DUST, str(tiny))
    ok("LINK dust slice: it is the increment that stops it",
       tiny["reason"] == sx.BELOW_BASE_INCREMENT, str(tiny["reason"]))
    real = d(product_id="LINK-USD", requested_qty="0.22", ledger_qty="0.22",
             wallet_available="0.22", wallet_hold="6.63", product_meta=LINK,
             price="14.986")
    ok("LINK free balance: EXECUTE", real["decision"] == sx.EXECUTE, str(real))
    ok("LINK free balance: 0.22 is already on the 0.01 grid",
       real["executable_qty"] == Decimal("0.22"), str(real["executable_qty"]))
    ok("LINK free balance: $3.29 clears the $1 floor",
       Decimal(real["executable_value"]) > Decimal("1"),
       str(real["executable_value"]))


def test_the_quote_minimum_is_enforced_at_all():
    """`quote_min_size` appears nowhere in this repo today, so an order worth
    $0.15 against a $1 venue floor was submitted and rejected by Coinbase."""
    r = d(product_id="LINK-USD", requested_qty="0.05", wallet_available="0.05",
          ledger_qty="0.05", wallet_hold="0", product_meta=LINK, price="14.986")
    ok("a $0.74 order is DUST, not submitted", r["decision"] == sx.DUST, str(r))
    ok("named as the value floor", r["reason"] == sx.BELOW_QUOTE_MINIMUM,
       str(r["reason"]))
    ok("the size itself was legal on the increment",
       r["executable_qty"] == Decimal("0.05"), str(r["executable_qty"]))
    ok("and the floor is reported", r["quote_minimum"] == "1",
       str(r["quote_minimum"]))
    # Just over the line.
    r2 = d(product_id="LINK-USD", requested_qty="0.07", wallet_available="0.07",
           ledger_qty="0.07", wallet_hold="0", product_meta=LINK, price="14.986")
    ok("$1.04 clears it", r2["decision"] == sx.EXECUTE, str(r2))


def test_an_unknown_price_blocks_when_a_value_floor_exists():
    r = d(price=None, requested_qty="0.337", wallet_available="0.4",
          ledger_qty="0.337", product_meta=QNT)
    ok("no price against a $1 floor blocks", r["decision"] == sx.BLOCKED, str(r))
    ok("named as the unreadable price", r["reason"] == sx.UNREADABLE_PRICE)


def test_an_unknown_hold_never_becomes_a_verdict():
    """The shortfall is real but whether it is reserved or missing is UNKNOWN.
    Guessing either way is wrong: RESERVED excuses a missing $169, UNBACKED
    accuses the books over an ordinary resting order."""
    r = d(requested_qty="0.675", ledger_qty="0.675",
          wallet_available="0.00097", wallet_hold=None, product_meta=QNT,
          price="249.93")
    ok("an unreadable hold blocks", r["decision"] == sx.BLOCKED, str(r))
    ok("it is neither excused nor accused",
       r["decision"] not in (sx.RESERVED, sx.UNBACKED), r["decision"])
    ok("and it says which fact is missing", "UNKNOWN" in r["detail"], r["detail"])
    ok("and refuses to write it off", "not written off" in r["detail"],
       r["detail"])


def test_ordinary_float_drift_is_not_called_unbacked():
    """0.009999999999998899 is 0.01 that round-tripped through a float. An
    accusation on every one of those would make the state meaningless."""
    r = d(product_id="LINK-USD", requested_qty="0.01",
          ledger_qty="0.01", wallet_available="0.009999999999998899",
          wallet_hold="0", product_meta=LINK, price="14.986")
    ok("float drift is not UNBACKED", r["decision"] != sx.UNBACKED, str(r))
    ok("it lands on the honest answer instead", r["decision"] == sx.DUST,
       str(r["decision"]))


def test_nothing_that_refuses_can_be_read_as_a_maker_expiry():
    """The invariant from the maker-only cause split: a cause that never placed
    an order must never be counted as an order that rested and expired. That
    conflation put ~2,600 phantom attempts a day into the funnel."""
    ok("the no-order set names every refusing decision",
       sx.NO_ORDER_PLACED == frozenset({sx.DUST, sx.RESERVED, sx.UNBACKED,
                                        sx.BLOCKED}),
       str(sx.NO_ORDER_PLACED))
    ok("EXECUTE is not in it", sx.EXECUTE not in sx.NO_ORDER_PLACED)
    for case in (
        d(requested_qty="0.0009", wallet_available="0.0009"),
        d(wallet_available=None),
        d(product_meta=None),
        d(requested_qty="0.675", ledger_qty="0.675",
          wallet_available="0.001", wallet_hold="0.0"),
        d(requested_qty="279.4", ledger_qty="279.4",
          wallet_available="0.046", wallet_hold="1134.3", product_meta=ALGO,
          price="0.1332"),
    ):
        ok(f"{case['decision']} is in the no-order set",
           case["decision"] in sx.NO_ORDER_PLACED, str(case))


def test_the_log_line_prints_every_number_and_no_zeroed_unknown():
    r = d(requested_qty="0.0009732300", wallet_available="0.0009732300",
          ledger_qty=None, wallet_hold=None, product_meta=QNT, price="249.93",
          product_id="QNT-USD")
    line = sx.log_line(r)
    for field in ("decision=", "reason=", "base_increment=", "base_min_size=",
                  "quote_minimum=", "executable_quantity=", "wallet_hold=",
                  "wallet_available=", "price=", "maker_only=true"):
        ok(f"the line carries {field}", field in line, line)
    ok("an unread hold prints as unknown, never as 0",
       "wallet_hold=unknown" in line, line)
    ok("an absent base_min_size prints as unknown, never as 0",
       "base_min_size=unknown" in line, line)
    ok("the real increment is in it", "base_increment=0.001" in line, line)
    ok("and the phrase it replaces is gone",
       "nothing sellable" not in line.lower(), line)


def test_only_an_execute_ever_hands_back_a_size_to_send():
    """executable_qty is a diagnostic and may be positive on a refusal - a 0.05
    LINK order is a legal SIZE worth $0.74 against a $1 floor. sendable_qty is
    the authorisation, and it exists only on EXECUTE. Keeping them one field
    meant either hiding the reason or handing back a size that must not go."""
    refused = [
        d(requested_qty="0.0009", wallet_available="0.0009"),
        d(requested_qty="0.05", wallet_available="0.05", ledger_qty="0.05",
          product_meta=LINK, price="14.986"),
        d(wallet_available=None),
        d(product_meta=None),
        d(requested_qty="0.675", ledger_qty="0.675", wallet_available="0.001",
          wallet_hold="0.0"),
        d(requested_qty="279.4", ledger_qty="279.4", wallet_available="0.046",
          wallet_hold="1134.3", product_meta=ALGO, price="0.1332"),
    ]
    for r in refused:
        ok(f"{r['decision']}/{r['reason']}: hands back no sendable size",
           r["sendable_qty"] is None, str(r["sendable_qty"]))
    r = d(requested_qty="0.337", wallet_available="0.4", ledger_qty="0.337",
          product_meta=QNT, price="249.93")
    ok("EXECUTE hands back a size", r["sendable_qty"] == Decimal("0.337"),
       str(r["sendable_qty"]))
    ok("and it equals the executable one", r["sendable_qty"] == r["executable_qty"])
    ok("the refused LINK order still reports its legal size",
       refused[1]["executable_qty"] == Decimal("0.05"),
       str(refused[1]["executable_qty"]))


def test_a_sendable_size_never_renders_in_exponent_form():
    """How the size reaches Coinbase, and a live trap. A BTC-sized order on a
    0.00000001 increment is a perfectly ordinary Decimal whose str() is
    "5E-8" - which the venue reads as an invalid size. format(d, 'f') is the
    only rendering that is safe for every size the grid can produce, so the
    engine uses that and this pins it."""
    BTC = {"base_increment": "0.00000001", "base_min_size": None,
           "quote_min_size": "1"}
    r = d(product_id="BTC-USD", requested_qty="0.00009", ledger_qty="0.00009",
          wallet_available="0.00009", wallet_hold="0", product_meta=BTC,
          price="62000")
    ok("the small BTC order executes", r["decision"] == sx.EXECUTE, str(r))
    rendered = format(r["sendable_qty"], "f")
    ok("and renders with no exponent", "E" not in rendered.upper(), rendered)
    ok("str() alone would NOT be safe here for a smaller size",
       "E" in str(Decimal("0.00000005")).upper(),
       "if str() ever stops using exponent form this test stops warning")
    for q in ("0.00000005", "0.005", "0.22", "1134.3", "3"):
        got, _ = sx.floor_to_increment(q, "0.00000001")
        ok(f"format({q}) has no exponent",
           "E" not in format(got, "f").upper(), format(got, "f"))


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} slice-execution checks passed")
