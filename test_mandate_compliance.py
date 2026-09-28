"""Mandate compliance, against the schema this repo actually has.

.claude/MANDATE_INTEGRATION_PLAN.md sketches the check as

    for bot_name, mandate in ALL_MANDATES.items():
        db.query(ClosedTrade).filter(ClosedTrade.bot == bot_name,
                                     ~ClosedTrade.symbol.in_(mandate["universe"]))

which would report ZERO VIOLATIONS FOREVER for three separate reasons -
and a compliance report that always says "compliant" is worse than none,
because it is a false assurance somebody acts on.
"""
import re

import bot_mandates as bm
import mandate_compliance as mc


def trade(symbol, qty=1.0, entry=100.0, tid=1):
    return {"id": tid, "symbol": symbol, "qty": qty, "entry_price": entry,
            "closed_at": "2026-09-28T00:00:00"}


APEX = bm.ALL_MANDATES["apex"]


# ── defect 1: the bot names never matched ────────────────────────────────

def test_the_recorded_bot_name_is_among_the_identities():
    """ClosedTrade.bot holds prop_apex. Neither the ALL_MANDATES key
    ("apex") nor mandate["name"] ("prop_bot") equals it."""
    ids = mc.bot_identities(APEX)
    assert "prop_apex" in ids
    assert "apex" in ids and "prop_bot" in ids, "the old names must still match"


def test_records_as_matches_the_modules_own_BOT_NAME():
    """Asserted against the source, so a rename on either side fails here
    instead of silently emptying the report."""
    for key, module in (("apex", "prop_bot.py"), ("alpaca", "alpaca_swing_bot.py"),
                        ("crypto", "crypto_coinbase_bot.py")):
        src = open(module, encoding="utf-8").read()
        m = re.search(r'^BOT_NAME\s*=\s*["\'](.+?)["\']', src, re.M)
        assert m, f"{module} has no BOT_NAME"
        assert m.group(1) in bm.RECORDS_AS[key], (
            f"{module} writes {m.group(1)!r}, RECORDS_AS says {bm.RECORDS_AS[key]}")


# ── defect 2: the universe is a dict, with prose in it ───────────────────

def test_the_universe_flattens_across_categories():
    allowed = mc.allowed_symbols(APEX)
    assert "MES" in allowed and "AAPL" in allowed and "BTC-USD" in allowed


def test_the_restriction_sentence_is_not_treated_as_a_symbol():
    allowed = mc.allowed_symbols(APEX)
    assert not any("ALLOWED" in s or " " in s for s in allowed), sorted(allowed)[:5]


def test_prose_under_a_note_key_is_excluded_even_when_it_is_a_list():
    """The type guard alone is not enough. `restriction` happens to hold a
    STRING today, so skipping non-lists catches it by accident - and a
    mandate that ever writes its notes as a list would turn each sentence
    into an allowed symbol. The KEY is what makes it prose, not the type.
    """
    m = {"universe": {"equities": ["AAPL"],
                      "notes": ["No other symbols allowed", "Ask first"]}}
    allowed = mc.allowed_symbols(m)
    assert allowed == {"AAPL"}, allowed


def test_a_mandate_without_a_universe_blocks_the_check_rather_than_failing_everything():
    """None must not read as "nothing is allowed"."""
    assert mc.allowed_symbols({"capital": {}}) is None
    out = mc.compliance([trade("MES")], {"capital": {}})
    assert out["universe_violations"] is None
    assert out["status"] == "UNKNOWN"


# ── defect 3: the symbol formats differ ──────────────────────────────────

def test_the_mandates_format_and_the_fleets_format_compare_equal():
    assert mc.normalise_symbol("BTC/USD") == mc.normalise_symbol("BTC-USD") == "BTC-USD"
    assert mc.normalise_symbol("btcusd") == "BTC-USD"


def test_a_crypto_trade_in_the_universe_is_not_a_violation():
    """Compared raw, every crypto trade would be off-universe."""
    bad, _ = mc.universe_violations([trade("BTC-USD")], APEX)
    assert bad == []


def test_an_unnormalisable_symbol_is_neither_a_pass_nor_a_violation():
    bad, unknown = mc.universe_violations([trade("")], APEX)
    assert bad == []
    assert len(unknown) == 1


# ── the checks themselves ────────────────────────────────────────────────

def test_an_off_universe_trade_is_caught():
    bad, _ = mc.universe_violations([trade("TSLA")], APEX)
    assert [b["normalised"] for b in bad] == ["TSLA"]


def test_a_position_over_the_per_position_limit_is_caught():
    limit = APEX["capital"]["max_per_position"]
    over = mc.capital_violations([trade("MES", qty=1.0, entry=limit * 3)], APEX)
    assert len(over) == 1
    assert over[0]["over_by_usd"] > 0


def test_a_position_inside_the_limit_is_not():
    limit = APEX["capital"]["max_per_position"]
    assert mc.capital_violations([trade("MES", qty=1.0, entry=limit / 2)], APEX) == []


def test_the_score_counts_a_trade_once_even_if_it_breaks_two_rules():
    limit = APEX["capital"]["max_per_position"]
    out = mc.compliance([trade("TSLA", qty=1.0, entry=limit * 3, tid=7)], APEX, "apex")
    assert out["compliance_pct"] == 0.0
    assert out["trades_examined"] == 1


def test_a_clean_run_scores_a_hundred():
    out = mc.compliance([trade("MES", tid=1), trade("AAPL", tid=2)], APEX, "apex")
    assert out["compliance_pct"] == 100.0
    assert out["status"] == "COMPLIANT"


def test_the_threshold_is_the_plans_own_ninety_five_percent():
    rows = [trade("MES", tid=i) for i in range(19)] + [trade("TSLA", tid=99)]
    out = mc.compliance(rows, APEX, "apex")
    assert out["compliance_pct"] == 95.0
    assert out["status"] == "COMPLIANT"
    rows.append(trade("NFLX", tid=98))
    assert mc.compliance(rows, APEX, "apex")["status"] == "VIOLATING"


def test_no_trades_scores_nothing_rather_than_a_hundred():
    """A bot that has not traded is not 100% compliant - it is unmeasured."""
    out = mc.compliance([], APEX, "apex")
    assert out["compliance_pct"] is None
    assert out["status"] == "UNKNOWN"


def test_a_check_that_could_not_run_is_not_a_pass():
    out = mc.compliance([trade("MES")], {"universe": {}, "capital": {}}, "x")
    assert out["compliance_pct"] is None
    assert out["status"] == "UNKNOWN"
    assert "not a pass" in out["detail"]
    assert len(out["checks_blocked"]) == 2


def test_every_live_mandate_states_a_universe_this_can_read():
    """If one stops being readable the report must not quietly shrink."""
    for key, m in bm.ALL_MANDATES.items():
        if not m.get("active"):
            continue
        if key == "monitoring":
            continue          # an analytics bot trades nothing by design
        assert mc.allowed_symbols(m), f"{key} states no readable universe"


# ── the endpoints ────────────────────────────────────────────────────────

def _endpoint(name):
    import ast
    import pathlib
    src = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError(f"{name} not found")


def test_the_violations_endpoint_matches_on_identities_not_the_key():
    """The exact defect the plan's version carries."""
    src = _endpoint("mandate_violations_endpoint")
    assert "mc.bot_identities(mandate)" in src
    assert "ClosedTrade.bot == key" not in src
    assert "ClosedTrade.bot == bot_name" not in src


def test_trades_belonging_to_no_mandate_are_reported_not_dropped():
    """Silently dropping them is how a report stays clean while a bot
    trades outside every rule there is."""
    src = _endpoint("mandate_violations_endpoint")
    assert "unmandated_bots" in src
    assert "no mandate" in src


def test_an_unscoreable_fleet_does_not_report_compliance():
    src = _endpoint("mandate_violations_endpoint")
    assert "That is not compliance" in src


def test_the_per_bot_endpoint_refuses_an_unknown_bot_by_name():
    src = _endpoint("mandate_compliance_endpoint")
    assert "status_code=404" in src
    assert "Known:" in src


def test_both_endpoints_are_read_only():
    """No mandate check should be able to change anything."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()
    tree = ast.parse(src)
    for name in ("mandate_violations_endpoint", "mandate_compliance_endpoint"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        body = ast.get_source_segment(src, fn)
        for writes in ("db.add(", "db.delete(", "await db.commit()", "place_market"):
            assert writes not in body, f"{name} must not {writes}"
