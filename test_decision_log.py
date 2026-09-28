"""Recording WHY a trade did or did not happen.

ClosedTrade has carried entry_rsi, entry_trend and entry_atr_pct since it
was created and prop_bot writes NONE of them - every one is NULL. And
validate_entry could not have filled them anyway: it short-circuits on
the first failing rule and returns the bare string "OK" on success, so an
admitted trade recorded nothing about why it qualified.

Refusals are the more valuable half. "Why has nothing traded for six
hours" was unanswerable while the answer was computed every cycle and
thrown away.
"""
import ast
import itertools
import pathlib
import subprocess
import sys
import types

import bot_mandates as bm
import decision_log as dl

HERE = pathlib.Path(__file__).parent


def verdict(symbol="MES", rsi=25.0, bp=5000.0, pos=0, notional=0.0, equity=5000.0,
            bot="prop_bot", direction="long"):
    return bm.validate_entry_verbose(bot, symbol, rsi, 1.0, bp, pos, notional,
                                     equity, direction)


# ── the verbose evaluator must not change any live verdict ───────────────

def test_validate_entry_is_unchanged_across_a_grid_of_inputs():
    """validate_entry now delegates. It is live trading code, so identical
    behaviour is asserted against the previous implementation rather than
    assumed."""
    old_src = subprocess.run(
        ["git", "-C", str(HERE), "show", "HEAD~1:bot_mandates.py"],
        capture_output=True, text=True).stdout
    if not old_src:
        return  # no prior revision available in this checkout
    old = types.ModuleType("old_bm")
    sys.path.insert(0, str(HERE))
    exec(compile(old_src, "old_bm", "exec"), old.__dict__)
    cases = itertools.product(
        ["prop_bot", "crypto_coinbase_bot", "nope"], ["MES", "BTC/USD", "TSLA"],
        [10.0, 30.0, 71.0], [0.0, 150.0, 5000.0], [0, 4, 9], [0.0, 10_000.0],
        ["long", "short"])
    for bot, sym, rsi, bp, pos, notional, dirn in cases:
        a = old.validate_entry(bot, sym, rsi, 1.0, bp, pos, notional, 1000.0, dirn)
        b = bm.validate_entry(bot, sym, rsi, 1.0, bp, pos, notional, 1000.0, dirn)
        assert a == b, (bot, sym, rsi, bp, pos, notional, dirn, a, b)


def test_every_rule_is_evaluated_not_just_the_first_failure():
    """The refusal that motivated this: five rules broken, one reported."""
    v = verdict(symbol="TSLA", rsi=80.0, bp=10.0, pos=9, notional=99_999.0, equity=1000.0)
    assert len(v["failed"]) == 5
    assert bm.validate_entry("prop_bot", "TSLA", 80.0, 1.0, 10.0, 9, 99_999.0,
                             1000.0)[1] == v["checks"][0]["detail"]


def test_an_admitted_trade_records_what_it_cleared():
    v = verdict()
    assert v["admitted"] is True
    assert v["reason"] == "OK", "the live message must stay OK"
    line = dl.explain(v)
    assert "ADMITTED" in line and "cleared" in line
    assert "oversold" in line and "Buying power" in line


def test_a_rule_that_was_not_evaluated_is_not_a_rule_that_passed():
    m = {"universe": {}, "entry": {}, "capital": {}}
    import unittest.mock as mock
    with mock.patch.object(bm, "get_bot_mandate", lambda n: m):
        v = bm.validate_entry_verbose("x", "MES", 25.0, 1.0, 5000.0, 0, 0.0, 5000.0)
    uni = [c for c in v["checks"] if c["name"] == "universe"]
    assert uni and uni[0]["passed"] is None
    assert "not evaluated" in dl.explain(v) or v["blocked"]


# ── the lookup that could have refused every trade ───────────────────────

def test_a_mandate_resolves_under_every_name_the_bot_is_known_by():
    """BOT_NAME is prop_apex - the obvious thing to pass, and what every
    ClosedTrade row holds. It used to return {}, which validate_entry
    turns into "Unknown bot" and REFUSES the trade."""
    for name in ("prop_bot", "apex", "prop_apex"):
        assert bm.get_bot_mandate(name), name


def test_an_unknown_name_still_returns_nothing():
    assert bm.get_bot_mandate("not_a_bot") == {}
    assert bm.validate_entry("not_a_bot", "MES", 25.0, 1.0, 5000.0, 0, 0.0, 5000.0) \
        == (False, "Unknown bot: not_a_bot")


# ── the row ──────────────────────────────────────────────────────────────

def test_a_refusal_row_names_every_failing_rule():
    row = dl.row_from_verdict(verdict(symbol="TSLA", rsi=80.0, bp=10.0, pos=9,
                                      notional=99_999.0, equity=1000.0),
                              bot="prop_apex", rsi=80.0)
    assert row["admitted"] is False
    assert row["failed_rules"].count(",") == 4
    assert "universe" in row["failed_rules"]


def test_an_admitted_row_carries_the_checks_not_just_ok():
    row = dl.row_from_verdict(verdict(), bot="prop_apex")
    assert row["admitted"] is True
    assert row["reason"] == "OK"
    assert row["checks_json"] and "threshold" in row["checks_json"]


def test_something_that_is_not_a_verdict_writes_no_row():
    """A caller that cannot describe its decision must write nothing
    rather than a row of NULLs that looks like a recorded decision."""
    for junk in (None, {}, "OK", {"checks": []}):
        assert dl.row_from_verdict(junk, bot="x") is None


def test_an_enormous_payload_is_truncated_not_stored_whole():
    v = verdict()
    v["checks"] = [{"name": f"r{i}", "detail": "x" * 400} for i in range(100)]
    row = dl.row_from_verdict(v, bot="x")
    assert len(row["checks_json"]) < dl.MAX_CHECKS_JSON
    assert "truncated" in row["checks_json"]


# ── reading it back ──────────────────────────────────────────────────────

def test_the_summary_names_the_rule_that_refuses_most_often():
    rows = ([dl.row_from_verdict(verdict(symbol="TSLA"), bot="b")] * 3
            + [dl.row_from_verdict(verdict(), bot="b")])
    out = dl.summarise(rows)
    assert out["decisions"] == 4 and out["admitted"] == 1 and out["refused"] == 3
    assert out["top_blockers"][0]["rule"] == "universe"


def test_no_decisions_is_itself_reported_as_a_finding():
    out = dl.summarise([])
    assert out["decisions"] == 0
    assert "itself a finding" in out["detail"]


# ── the wiring ───────────────────────────────────────────────────────────

def test_the_entry_path_logs_before_it_returns_on_a_refusal():
    """A refused trade must still be recorded - that is the half that
    answers "why has nothing traded"."""
    src = (HERE / "prop_bot.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and "_record_trade_decision" not in n.name
              and "_record_trade_decision" in ast.get_source_segment(src, n))
    body = ast.get_source_segment(src, fn)
    record_at = body.index("_record_trade_decision(")
    refuse_at = body.index("MANDATE BLOCKED")
    assert record_at < refuse_at, "the refusal returns before the decision is logged"


def test_logging_cannot_decide_whether_a_trade_happens():
    src = (HERE / "prop_bot.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_record_trade_decision")
    body = ast.get_source_segment(src, fn)
    assert "except Exception" in body
    assert "non-fatal" in body


def test_the_module_opens_no_database_of_its_own():
    """Pure, so it can be tested without a session and cannot itself break
    a trading cycle."""
    src = (HERE / "decision_log.py").read_text()
    for forbidden in ("import models", "get_session_factory", "sqlalchemy"):
        assert forbidden not in src, forbidden
