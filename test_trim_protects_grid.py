"""The trimmer sold coin the grid still had on its books.

AUTO_TRIM_MODE=arm has been live. Its ceiling rule sold:

    2026-09-27 02:42:15  ZEC  $746.25
    2026-09-27 02:42:16  XRP  $136.43
    2026-09-28 02:43:06  ZEC  $139.18
    2026-09-28 02:43:07  XRP  $108.35

$885.43 of ZEC and $244.78 of XRP. The grid's branches went on claiming
those units, and coin_tracked_is_held later found ZEC short 0.087205
(~$134.76) and XRP short 69.83 (~$103.35) - the 09-28 trims, near enough
to the cent once the price moved.

Two subsystems enforcing the same 20% ceiling against different books,
neither telling the other. A sale of those slices would have been an
order for units that do not exist.
"""
from datetime import datetime, timezone

import auto_trim

NOW = datetime(2026, 9, 28, 6, 40, tzinfo=timezone.utc)


def holding(asset, usd, units=1.0, price=None):
    return {"asset": asset, "usd": usd, "units": units,
            "price": price if price is not None else (usd / units if units else 0)}


# ZEC over the 20% ceiling on a $10,000 book.
BOOK = [holding("ZEC", 2600.0, 1.7), holding("XRP", 2000.0, 1400.0),
        holding("BTC", 1500.0, 0.018), holding("USD", 3900.0, 3900.0)]
TOTAL = 10000.0


def test_without_protection_the_ceiling_rule_trims_zec():
    """The behaviour that caused this - kept as the control."""
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW)
    zec = next(r for r in rows if r["asset"] == "ZEC")
    assert zec["act"] is True
    assert zec["reason"] == "OVER_LIMIT"


def test_a_coin_the_grid_is_trading_is_not_trimmed():
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW, actively_traded={"ZEC"})
    zec = next(r for r in rows if r["asset"] == "ZEC")
    assert zec["act"] is False
    assert zec["reason"] == "ACTIVELY_TRADED"
    assert zec["trim_usd"] == 0.0
    assert "still has on its books" in zec["detail"]


def test_the_refusal_still_reports_how_far_over_it_was():
    """A risk control that goes quiet is worse than one that acts. The
    excess is still measured and shown so it can be raised with the grid."""
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW, actively_traded={"ZEC"})
    zec = next(r for r in rows if r["asset"] == "ZEC")
    assert zec["excess_usd"] > 0
    assert "over the 20% rule" in zec["detail"]


def test_protecting_one_coin_does_not_protect_another():
    # 2800 of 12800 is 21.9% - genuinely over, not sitting exactly on the
    # line, which is where the first version of this fixture landed.
    book = BOOK + [holding("SHIB", 2800.0, 5e8)]
    rows = auto_trim.plan_trims(book, 12800.0, now=NOW, actively_traded={"ZEC"})
    by = {r["asset"]: r for r in rows}
    assert by["ZEC"]["reason"] == "ACTIVELY_TRADED"
    assert by["SHIB"]["act"] is True


def test_product_ids_are_matched_on_the_base_asset():
    """The grid keys on ZEC-USD, the census on ZEC."""
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW, actively_traded={"ZEC-USD".split("-")[0]})
    assert next(r for r in rows if r["asset"] == "ZEC")["act"] is False


def test_it_is_one_directional_and_never_causes_a_trim():
    """Naming a coin that is nowhere near the limit must not act on it."""
    rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW, actively_traded={"BTC"})
    btc = next(r for r in rows if r["asset"] == "BTC")
    assert btc["act"] is False
    assert btc["reason"] == "WITHIN_LIMIT"


def test_no_protection_supplied_behaves_exactly_as_before():
    """Fails OPEN: an unreadable grid must not silently disarm the rule."""
    for empty in ((), None, set()):
        rows = auto_trim.plan_trims(BOOK, TOTAL, now=NOW, actively_traded=empty)
        assert next(r for r in rows if r["asset"] == "ZEC")["act"] is True


def test_the_tail_sweep_also_refuses_an_actively_traded_coin():
    """plan_actions can sell a whole small position. ONDO-USD was $55.00
    and had completed a profitable round trip four hours earlier."""
    book = BOOK + [holding("ONDO", 55.0, 96.0)]
    rows = auto_trim.plan_actions(book, 10055.0, now=NOW, actively_traded={"ONDO"})
    ondo = [r for r in rows if r["asset"] == "ONDO"]
    assert ondo, "every holding must appear in the output"
    assert all(not r.get("act") for r in ondo)
    assert any(r.get("reason") == "ACTIVELY_TRADED" for r in ondo)


def test_the_worker_passes_the_grid_positions_through():
    """Asserted on the parsed tree: the worker is the path that actually
    sells, and the docstrings here quote plan_trims verbatim."""
    import ast
    import pathlib
    src = pathlib.Path(auto_trim.__file__).with_name("auto_trim_worker.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "plan_trims"]
    assert calls, "the worker must still call plan_trims"
    for c in calls:
        assert "actively_traded" in {k.arg for k in c.keywords}, (
            "the selling path must be given the grid's open positions")
