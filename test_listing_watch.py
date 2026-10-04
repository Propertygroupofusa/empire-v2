"""A delisting is a venue fact, and it has already cost this account twice.

JUP-USD returned "INVALID_ARGUMENT: Invalid product_id" on every buy, over
multiple sessions, while branches kept landing on it. MATIC-USD migrated to
POL-USD and left a branch stuck on a dead ticker. Both were knowable the
moment the exchange's own product list changed.
"""
import listing_watch as lw


def _snap(**kw):
    return {pid: (v if isinstance(v, dict) else {"status": "online"})
            for pid, v in kw.items()}


ONLINE = {"status": "online", "trading_disabled": False}
HALTED = {"status": "offline", "trading_disabled": True}


# --- the dangerous one: a failed read is not a delisting -------------------

def test_an_unreadable_snapshot_reports_nothing():
    """One timeout must not look identical to 'everything was delisted'."""
    prev = {"ZEC-USD": ONLINE, "XRP-USD": ONLINE}
    for empty in ({}, None):
        assert lw.plan_alerts(prev, empty, watched=["ZEC-USD", "XRP-USD"]) == []


def test_a_first_run_does_not_announce_the_whole_catalogue():
    cur = {f"C{i}-USD": ONLINE for i in range(300)}
    assert lw.plan_alerts({}, cur, watched=[]) == []
    assert lw.plan_alerts(None, cur, watched=[]) == []


# --- delisting -------------------------------------------------------------

def test_a_watched_coin_vanishing_is_critical():
    out = lw.plan_alerts({"JUP-USD": ONLINE, "BTC-USD": ONLINE},
                         {"BTC-USD": ONLINE}, watched=["JUP-USD", "BTC-USD"])
    assert len(out) == 1
    a = out[0]
    assert a["kind"] == lw.KIND_DELISTED
    assert a["asset"] == "JUP-USD"
    assert a["severity"] == "CRITICAL"
    assert a["dedupe_key"] == "listing:DELISTED:JUP-USD"


def test_an_unwatched_coin_vanishing_is_not_reported():
    """The venue drops products constantly. Only what this fleet trades."""
    out = lw.plan_alerts({"SOMETHING-USD": ONLINE, "BTC-USD": ONLINE},
                         {"BTC-USD": ONLINE}, watched=["BTC-USD"])
    assert out == []


# --- halt and resume -------------------------------------------------------

def test_a_halt_is_critical():
    out = lw.plan_alerts({"ZEC-USD": ONLINE}, {"ZEC-USD": HALTED}, watched=["ZEC-USD"])
    assert len(out) == 1 and out[0]["kind"] == lw.KIND_HALTED
    assert out[0]["severity"] == "CRITICAL"


def test_resuming_is_info_not_critical():
    out = lw.plan_alerts({"ZEC-USD": HALTED}, {"ZEC-USD": ONLINE}, watched=["ZEC-USD"])
    assert len(out) == 1 and out[0]["kind"] == lw.KIND_RESUMED
    assert out[0]["severity"] == "INFO"


def test_a_steady_state_says_nothing():
    snap = {"ZEC-USD": ONLINE, "BTC-USD": ONLINE}
    assert lw.plan_alerts(snap, snap, watched=["ZEC-USD", "BTC-USD"]) == []
    halted = {"ZEC-USD": HALTED}
    assert lw.plan_alerts(halted, halted, watched=["ZEC-USD"]) == []


# --- what counts as tradeable ---------------------------------------------

def test_a_missing_field_cannot_invent_a_halt():
    """An API tier that omits a flag must not read as 'not tradeable'."""
    assert lw.is_tradeable({}) is True
    assert lw.is_tradeable({"status": None}) is True
    assert lw.is_tradeable(None) is True


def test_every_explicit_disable_counts():
    for bad in ({"trading_disabled": True}, {"is_disabled": True},
                {"cancel_only": True}, {"post_only": True},
                {"status": "offline"}, {"status": "delisted"}):
        assert lw.is_tradeable(bad) is False, bad


# --- new listings ----------------------------------------------------------

def test_a_new_usd_product_is_info():
    out = lw.plan_alerts({"BTC-USD": ONLINE}, {"BTC-USD": ONLINE, "NEW-USD": ONLINE},
                         watched=["BTC-USD"])
    assert len(out) == 1 and out[0]["kind"] == lw.KIND_NEW
    assert out[0]["severity"] == "INFO"


def test_other_quotes_are_not_announced():
    out = lw.plan_alerts({"BTC-USD": ONLINE},
                         {"BTC-USD": ONLINE, "NEW-EUR": ONLINE, "NEW-BTC": ONLINE},
                         watched=["BTC-USD"])
    assert out == []


# --- shape and hygiene -----------------------------------------------------

def test_case_and_whitespace_do_not_create_phantom_events():
    out = lw.plan_alerts({" zec-usd ": ONLINE}, {"ZEC-USD": ONLINE}, watched=["zec-usd"])
    assert out == []


def test_alerts_match_the_shape_alert_worker_writes():
    out = lw.plan_alerts({"JUP-USD": ONLINE}, {"BTC-USD": ONLINE}, watched=["JUP-USD"])
    for a in out:
        assert set(a) == {"kind", "asset", "severity", "message", "detail", "dedupe_key"}
        assert a["severity"] in {"CRITICAL", "HIGH", "INFO"}
        assert isinstance(a["dedupe_key"], str) and a["dedupe_key"]


def test_dedupe_keys_are_unique_within_one_pass():
    prev = {"A-USD": ONLINE, "B-USD": ONLINE, "C-USD": HALTED}
    cur = {"B-USD": HALTED, "C-USD": ONLINE, "D-USD": ONLINE}
    out = lw.plan_alerts(prev, cur, watched=["A-USD", "B-USD", "C-USD"])
    keys = [a["dedupe_key"] for a in out]
    assert len(keys) == len(set(keys))


def test_snapshot_keeps_only_what_is_compared():
    snap = lw.snapshot_from_products([
        {"product_id": "zec-usd", "status": "online", "price": "1334.71",
         "base_increment": "0.00000001", "trading_disabled": False},
        {"id": "BTC-USD", "status": "online"},
        {"no_id": True}, "junk", None,
    ])
    assert set(snap) == {"ZEC-USD", "BTC-USD"}
    assert set(snap["ZEC-USD"]) == {"status", "trading_disabled", "is_disabled",
                                    "cancel_only", "post_only"}
    assert "price" not in snap["ZEC-USD"]


def test_the_module_cannot_trade():
    """Checked on the parsed CODE, not on substrings - the docstring says
    "places no order", which a text search reads as a violation of itself."""
    import ast
    tree = ast.parse(open("listing_watch.py").read())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == [], "a module that only compares two dicts imports nothing"
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("post", "get_usd_balance", "place_maker_buy",
                      "place_market_buy", "place_maker_sell", "place_market_sell"):
        assert forbidden not in called, forbidden
