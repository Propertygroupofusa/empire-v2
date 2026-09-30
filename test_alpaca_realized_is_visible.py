"""The stock side lost money for 141 days and the page said 0.00.

Measured 2026-09-30 20:0xZ against the live account:

    /alpaca-overview   bots[0..7].profit = 0.00   (all eight)
    /trades/closed     232 round trips, net -$3.20, 32.8% win rate

Both numbers were correct and neither was the answer. _bot_profit is
max(0, _bot_pl) - floored for WITHDRAWAL eligibility, since you cannot
withdraw a loss - and _bot_pl is a capital-bucket delta, which is a
different question from what the trading earned. So the overview the
owner reads showed eight buckets at 0.00 over an account whose equity
ranged $973.03 to $1,016.50 across 141 days and never grew, while a
32.8% win rate ran underneath it.

The realised record now rides on the same endpoint, from the same
tested pairing function /trades/closed uses - not a second copy of the
arithmetic, because two implementations of "what did we earn" is how
two numbers start disagreeing.

The rule this shares with the rest of the codebase: a reading that
failed is never a zero. An unreachable order history returns
readable=False with its reason, never a clean 0.00 that would read as
"flat" on the one page used to decide whether a strategy works.
"""
import ast

SRC = open("routers/trading_dashboard.py").read()
TREE = ast.parse(SRC)


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


# --- the helper exists and cannot silently report zeros ------------------

def test_helper_exists_and_reuses_the_tested_pairing_function():
    src = ast.get_source_segment(SRC, _fn("_alpaca_realized_record"))
    assert "closed_trades.pair_round_trips" in src, \
        "a second copy of the pairing arithmetic is how two 'earned' numbers diverge"
    assert "_KNOWN_ORDER_SOURCES" in src


def test_a_failed_read_is_not_a_zero():
    src = ast.get_source_segment(SRC, _fn("_alpaca_realized_record"))
    # every early return on failure must carry readable=False, never a 0 total
    assert src.count('"readable": False') >= 2, \
        "an unreachable order history must say so, not report a flat record"
    assert "HTTP {r.status} fetching order history" in src
    for bad in ('"net_pnl": 0.0,\n                "readable": False',):
        assert bad not in src


def test_zero_trades_is_distinguishable_from_unreadable():
    src = ast.get_source_segment(SRC, _fn("_alpaca_realized_record"))
    assert '"readable": True, "round_trips": 0' in src, \
        "a genuine empty record must still be readable=True"
    assert '"win_rate_pct": None' in src, \
        "a win rate over zero trades is undefined, not 0%"


# --- the overview serves it, and labels the floored field ----------------

def test_overview_serves_the_realized_record():
    src = ast.get_source_segment(SRC, _fn("get_alpaca_overview"))
    assert "realized = await _alpaca_realized_record(session)" in src
    assert '"realized": realized,' in src


def test_the_floored_profit_field_is_labelled_where_it_is_served():
    src = ast.get_source_segment(SRC, _fn("get_alpaca_overview"))
    assert "bot_profit_is_floored_at_zero" in src, \
        "profit=max(0,pl) reads as 'made nothing' for a bucket that is DOWN"
    assert "realized.net_pnl" in src, "the label must point at the real number"


def test_the_floor_is_still_there_on_purpose():
    # The floor is correct for its own question - withdrawal eligibility.
    # This pins that the fix ADDED a number rather than quietly changing
    # what 'profit' means for the withdrawal path that depends on it.
    src = ast.get_source_segment(SRC, _fn("_bot_profit"))
    assert "max(0.0, _bot_pl(bot))" in src, \
        "_bot_profit's floor was removed; withdrawal eligibility depends on it"


def test_status_endpoint_was_not_given_the_new_block():
    # /status serves the same bot buckets. The realised record was added to
    # the owner-facing overview only; if it is ever wanted on /status it
    # must come from the same helper, not a copy.
    src = ast.get_source_segment(SRC, _fn("get_dashboard_status"))
    assert '"realized"' not in src


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)
