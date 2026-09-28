"""Cancelling the stops that hold the grid's own coin - and nothing else.

At 09:44Z six live grid branches had $923.23 of inventory reserved by
resting stops this system placed. ALGO had 0.046 of 1134.35 units free:
that branch could not sell anything at all.

Every assertion here is about a way this could take something it should
not. There is no place verb in the module - the worst outcome available
is an unprotected position, never a sale.
"""
import free_locked_inventory as f

STOPS = {
    "XLM": {"order_id": "o-xlm", "stop_price": "0.203835", "base_size": "2258.4"},
    "ALGO": {"order_id": "o-algo", "stop_price": "0.110283", "base_size": "1134.3"},
    "JASMY": {"order_id": "o-jasmy", "stop_price": "0.00443", "base_size": "5862.0"},
}
TRACKED = {"XLM-USD": 2200.0, "ALGO-USD": 1000.0}
HOLDINGS = [
    {"asset": "XLM", "units": 3011.20574764, "available_units": 1030.43934739,
     "price": 0.211675},
    {"asset": "ALGO", "units": 1134.346389, "available_units": 0.046389,
     "price": 0.125905},
    {"asset": "JASMY", "units": 5862.75696592, "available_units": 0.75696592,
     "price": 0.004989},
]


def acts(res):
    return {a["asset"]: a for a in res["actions"]}


# ── what it cancels ──────────────────────────────────────────────────────

def test_it_cancels_the_stops_holding_grid_inventory():
    a = acts(f.plan(STOPS, TRACKED, HOLDINGS))
    assert a["XLM"]["action"] == f.CANCEL
    assert a["ALGO"]["action"] == f.CANCEL


def test_a_coin_no_branch_trades_is_left_alone():
    """JASMY is reserved too and is not a grid branch. Cancelling it
    would be tidying somebody else's account."""
    a = acts(f.plan(STOPS, TRACKED, HOLDINGS))
    assert a["JASMY"]["action"] == f.SKIP
    assert a["JASMY"]["reason"] == "NOT_GRID_INVENTORY"


def test_it_reports_what_each_cancel_frees():
    """Computed from THIS fixture's own units and price, not copied from
    the live snapshot that prompted the module - those were taken a
    minute apart at different prices, and a test that asserts a
    remembered figure passes or fails on the market, not on the code."""
    a = acts(f.plan(STOPS, TRACKED, HOLDINGS))
    assert a["XLM"]["frees_units"] == 1980.76640025
    assert a["XLM"]["frees_usd"] == round(1980.76640025 * 0.211675, 2) == 419.28
    assert a["ALGO"]["frees_usd"] == round(1134.3 * 0.125905, 2) == 142.81


def test_an_order_with_no_id_is_skipped_not_guessed_at():
    stops = dict(STOPS, XLM={"order_id": None, "stop_price": "0.2", "base_size": "1"})
    a = acts(f.plan(stops, TRACKED, HOLDINGS))
    assert a["XLM"]["action"] == f.SKIP and a["XLM"]["reason"] == "NO_ORDER_ID"


# ── failing closed ───────────────────────────────────────────────────────

def test_an_unreadable_order_book_cancels_nothing():
    r = f.plan(None, TRACKED, HOLDINGS)
    assert r["ok"] is False and r["actions"] == []
    assert "could not be read" in r["reason"]


def test_an_unreadable_grid_cancels_nothing():
    """Every protection in this codebase fails OPEN. This one moves live
    orders, so not knowing means not acting."""
    r = f.plan(STOPS, None, HOLDINGS)
    assert r["ok"] is False and r["actions"] == []


def test_an_empty_grid_reading_is_not_the_same_as_an_unreadable_one():
    """{} is a real answer - no branch holds anything - and cancels
    nothing because nothing is grid inventory. None is a gap."""
    r = f.plan(STOPS, {}, HOLDINGS)
    assert r["ok"] is True
    assert all(x["action"] == f.SKIP for x in r["actions"])


def test_missing_holdings_still_cancels_but_claims_no_figure():
    """The lock size is a nice-to-have; not knowing it must not silently
    become $0.00 freed."""
    a = acts(f.plan(STOPS, TRACKED, None))
    assert a["ALGO"]["action"] == f.CANCEL
    assert a["ALGO"]["frees_usd"] is None


# ── the summary line ─────────────────────────────────────────────────────

def test_the_headline_names_the_amount_and_that_nothing_is_placed():
    s = f.summarise(f.plan(STOPS, TRACKED, HOLDINGS), dry_run=True)
    assert s["cancel_count"] == 2
    assert s["frees_usd"] == 562.09
    assert "$562.09" in s["headline"] and "would be freed" in s["headline"]
    assert "No order is placed" in s["headline"]
    assert s["places_no_orders"] is True


def test_a_partial_total_is_never_printed_as_the_whole_figure():
    holdings = [h for h in HOLDINGS if h["asset"] != "ALGO"]
    s = f.summarise(f.plan(STOPS, TRACKED, holdings), dry_run=True)
    assert s["positions_without_price"] == 1
    assert "no readable price" in s["headline"]


def test_nothing_to_do_says_so_rather_than_reporting_zero_dollars():
    s = f.summarise(f.plan({}, TRACKED, HOLDINGS), dry_run=True)
    assert s["cancel_count"] == 0
    assert "No resting stop is holding grid inventory." in s["headline"]


def test_a_refusal_headline_carries_the_reason_not_a_number():
    s = f.summarise(f.plan(None, TRACKED, HOLDINGS), dry_run=True)
    assert s["ok"] is False
    assert "Nothing can be cancelled" in s["headline"]
    assert s["frees_usd"] is None


def test_applying_reads_differently_from_previewing():
    p = f.summarise(f.plan(STOPS, TRACKED, HOLDINGS), dry_run=True)
    a = f.summarise(f.plan(STOPS, TRACKED, HOLDINGS), dry_run=False)
    assert "would be freed" in p["headline"]
    assert "would be freed" not in a["headline"] and "freed" in a["headline"]


# ── the thing it must never grow ─────────────────────────────────────────

def test_this_module_has_no_way_to_place_an_order():
    """The safety property the whole design rests on, asserted on the
    source rather than trusted: there is no place, buy or sell verb
    anywhere in it."""
    import ast
    import inspect
    src = inspect.getsource(f)
    tree = ast.parse(src)
    names = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for forbidden in ("place", "place_market_sell", "place_market_buy",
                      "grid_sell", "grid_buy", "post"):
        assert forbidden not in names, f"{forbidden} appeared in a cancel-only module"
    assert "CANCEL" in src and "SELL" not in src.replace("resting sell", "")


# ── the endpoint's own guarantees ────────────────────────────────────────

def _endpoint_src():
    import ast
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "routers", "trading_dashboard.py")
    src = open(p, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "free_locked_inventory_endpoint")
    return ast.get_source_segment(src, fn), fn


def test_the_endpoint_previews_by_default():
    """Asserted on the parsed signature, not on a docstring saying so."""
    _, fn = _endpoint_src()
    dry = next(a for a in fn.args.args if a.arg == "dry_run")
    i = fn.args.args.index(dry) - (len(fn.args.args) - len(fn.args.defaults))
    assert fn.args.defaults[i].value is True


def test_the_endpoint_never_places_an_order():
    """The property the whole design rests on. If a place call is ever
    added here, this fails before it reaches the venue."""
    import ast
    src, fn = _endpoint_src()
    calls = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("place", "place_market_sell", "place_market_buy",
                      "grid_sell", "grid_buy"):
        assert forbidden not in calls, f"{forbidden} is called from a cancel-only endpoint"
    assert "cancel" in calls


def test_applying_requires_the_plan_to_have_said_ok():
    """An unreadable book or grid must not reach the cancel loop."""
    src, _ = _endpoint_src()
    assert 'if result.get("ok") and not dry_run:' in src


def test_the_outcome_is_recorded_from_what_happened_not_from_the_plan():
    src, _ = _endpoint_src()
    assert 'a["cancelled"] = bool(ok)' in src
    assert '"cancel_failed"' in src
