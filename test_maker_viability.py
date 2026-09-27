"""Tests for the per-coin fee correction.

The live dashboard reports five branches below the $750,000 depth floor -
ACH, FLOKI, TIA, SHIB and BONK - whose orders cross the spread and pay
taker. Every one of them had its sells priced at the MAKER rate, which
understates the exit leg by 0.40 points and green-lights sales that net
between -0.40% and zero.

So the property that matters most here is that wiring this in can only
ever RAISE a fee. A fee correction that could lower one would be the
same bug with a different sign.
"""
import ast

import pytest

import maker_viability as mv

MAKER, TAKER = 0.0035, 0.0075

LIVE_THIN = {"ACH-USD": 52_080, "FLOKI-USD": 114_810, "TIA-USD": 395_209,
             "SHIB-USD": 471_080, "BONK-USD": 744_989}
LIVE_DEEP = {"BTC-USD": 88_050_448, "XRP-USD": 49_643_565, "ETH-USD": 50_533_854,
             "NEAR-USD": 38_630_336, "QNT-USD": 40_170_532, "PEPE-USD": 798_129}


@pytest.fixture(autouse=True)
def clean():
    mv._KNOWN.clear()
    yield
    mv._KNOWN.clear()


# ---------------------------------------- it can only ever raise a fee

@pytest.mark.parametrize("pid,notional", list(LIVE_THIN.items()) + list(LIVE_DEEP.items()))
def test_the_rate_is_never_lower_than_the_makers(pid, notional):
    mv.remember(pid, notional)
    assert mv.leg_fee_rate(pid, MAKER, TAKER) >= MAKER


def test_a_coin_nothing_is_known_about_keeps_todays_behaviour():
    assert mv.leg_fee_rate("NEVER-SEEN-USD", MAKER, TAKER) == MAKER
    assert mv.maker_ok("NEVER-SEEN-USD") is None


def test_maker_only_off_means_the_callers_own_rate_stands():
    mv.remember("ACH-USD", 52_080)
    assert mv.leg_fee_rate("ACH-USD", MAKER, TAKER, maker_only_active=False) == MAKER


# ------------------------------------------------- the live five

def test_every_branch_below_the_floor_is_repriced_to_taker():
    for pid, n in LIVE_THIN.items():
        mv.remember(pid, n)
        assert mv.maker_ok(pid) is False, pid
        assert mv.leg_fee_rate(pid, MAKER, TAKER) == TAKER, pid


def test_bonk_fails_by_eleven_thousand_dollars_and_is_not_rounded_through():
    """$744,989 against a $750,000 floor. A 'close enough' here is a coin
    paying double the fee it is priced at."""
    mv.remember("BONK-USD", 744_989)
    assert mv.maker_ok("BONK-USD") is False
    assert mv.leg_fee_rate("BONK-USD", MAKER, TAKER) == TAKER


def test_a_deep_book_keeps_the_maker_rate():
    for pid, n in LIVE_DEEP.items():
        mv.remember(pid, n)
        assert mv.maker_ok(pid) is True, pid
        assert mv.leg_fee_rate(pid, MAKER, TAKER) == MAKER, pid


def test_exactly_on_the_floor_counts_as_deep_enough():
    mv.remember("EDGE-USD", mv.MIN_24H_NOTIONAL_USD)
    assert mv.maker_ok("EDGE-USD") is True


# -------------------------------------- unknown is its own answer

def test_unmeasured_is_none_never_false_and_never_true():
    """"We have not measured this book" and "this book is fine" are
    different facts. Collapsing them is how the maker rate got applied to
    a $52,080 order book."""
    for junk in (None, "x", float("nan"), -1):
        assert mv.can_fill_as_maker(junk) is None


def test_zero_depth_is_a_measurement_not_a_gap():
    assert mv.can_fill_as_maker(0) is False


def test_the_summary_never_overstates_its_own_coverage():
    s = mv.summarise()
    assert s["measured"] == 0
    assert "Nothing measured yet" in s["detail"]


def test_the_summary_names_the_thin_coins():
    for pid, n in LIVE_THIN.items():
        mv.remember(pid, n)
    s = mv.summarise()
    assert set(s["too_thin_pays_taker"]) == {"ACH", "FLOKI", "TIA", "SHIB", "BONK"}
    assert "-0.40%" in s["detail"]


# ----------------------------------------------- bulk recording

def test_a_whole_scan_can_be_recorded_at_once():
    rows = [{"product_id": p, "notional_24h_usd": n} for p, n in LIVE_THIN.items()]
    assert mv.remember_many(rows) == 5
    assert len(mv.thin_coins()) == 5


def test_rows_without_a_readable_depth_are_skipped_not_assumed():
    rows = [{"product_id": "A-USD", "notional_24h_usd": None},
            {"product_id": "B-USD"}, {"no_id": 1}, "junk",
            {"product_id": "C-USD", "notional_24h_usd": 900_000}]
    assert mv.remember_many(rows) == 1
    assert mv.maker_ok("A-USD") is None and mv.maker_ok("C-USD") is True


def test_the_floor_matches_the_one_the_scanner_already_uses():
    import universe_scan
    assert mv.MIN_24H_NOTIONAL_USD == universe_scan.MIN_24H_NOTIONAL_USD


# ------------------------------------------- structural guards

def test_it_never_reaches_a_venue_because_the_trading_loop_calls_it():
    tree = ast.parse(open("maker_viability.py").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [x.name for x in n.names] + [getattr(n, "module", None)]
            for nm in names:
                assert not any(b in (nm or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), nm


def test_everything_survives_json():
    import json
    for pid, n in LIVE_THIN.items():
        mv.remember(pid, n)
    s = mv.summarise()
    assert json.loads(json.dumps(s)) == s


# ------------------------------- the wiring into the live fee path

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)


def _fn(name):
    n = next(x for x in ast.walk(TREE)
             if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


def test_the_fee_function_takes_a_coin_now():
    assert "async def expected_leg_fee_rate(product_id: str = None)" in SRC


def test_calling_it_without_a_coin_is_unchanged():
    """Every existing caller that does not name a coin must behave exactly
    as before, or this 'correction' is a silent behaviour change."""
    body = _fn("expected_leg_fee_rate")
    assert "if product_id is None:" in body
    assert body.index("if product_id is None:") < body.index("maker_viability")


def test_a_failure_to_consult_the_verdict_never_lowers_the_fee():
    body = _fn("expected_leg_fee_rate")
    i = body.index("except Exception:")
    assert "return maker" in body[i:], "the fallback must return the maker rate, not None"


def test_the_cycle_passes_the_coin_it_is_trading():
    assert "expected_leg_fee_rate(branch.product_id)" in SRC


def test_the_slice_level_rate_passes_its_own_coin():
    """This is the rate _pick_profitable_slice_to_sell decides on. If it
    stays coin-blind the fix does not reach the decision that matters."""
    body = _fn("slice_round_trip_fee_rate")
    assert 'getattr(slice_row, "product_id", None)' in body
