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


# ── the hook was too deep, and the endpoint proved it ────────────────────

def test_a_pre_mandate_refusal_has_the_same_row_shape():
    """Checked live an hour after shipping: /mandates/decisions returned
    zero rows, because the only hook was at the LAST gate. A row decided
    earlier must be indistinguishable in shape from one decided late."""
    r = dl.refusal("TSLA", "universe", "TSLA not in the approved universe",
                   mandate="apex", direction="long")
    row = dl.row_from_verdict(r, bot="prop_apex")
    assert row is not None
    assert row["admitted"] is False
    assert row["failed_rules"] == "universe"
    assert row["mandate"] == "apex"
    assert "universe" in row["checks_json"]


def test_a_refusal_verdict_reads_like_any_other():
    line = dl.explain(dl.refusal("TSLA", "universe", "TSLA not in the approved universe"))
    assert "TSLA REFUSED" in line
    assert "approved universe" in line


def test_refusals_decided_early_and_late_summarise_together():
    rows = [dl.row_from_verdict(dl.refusal("TSLA", "universe", "no"), bot="b"),
            dl.row_from_verdict(verdict(symbol="TSLA"), bot="b")]
    out = dl.summarise(rows)
    assert out["refused"] == 2
    assert out["top_blockers"][0]["rule"] == "universe"
    assert out["top_blockers"][0]["count"] == 2


def test_every_refusing_return_in_try_open_logs_a_decision():
    """The regression that motivated this: a `return False` above the
    mandate check that writes nothing. Asserted on the parsed tree over
    the whole function, not on the one path I happened to remember."""
    src = (HERE / "prop_bot.py").read_text()
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "try_open":
            fn = n
            break
    assert fn is not None, "try_open not found"
    body = ast.get_source_segment(src, fn)
    # Every early refusal that names MANDATE must record a decision.
    #
    # The entries-halted guard is deliberately NOT one of these: it is
    # tagged [KILL CONDITION] because it is the consequence of a halt
    # already decided and already recorded once at the top of the cycle.
    # Logging a row per contract per cycle there would duplicate that one
    # halt across every symbol scanned.
    for chunk in body.split("[MANDATE]")[1:]:
        upto = chunk.split("return False")[0]
        assert "_record_trade_decision" in upto, (
            "a MANDATE refusal returns without logging a decision:\n"
            + upto[:200])


# ── a halt is a decision, and it stops the cycle before the entry path ────

def test_a_fleet_wide_refusal_reads_without_a_symbol():
    """A kill condition has no symbol. "?" read like a missing field
    rather than a deliberate absence."""
    line = dl.explain(dl.refusal(None, "kill_condition",
                                 "Buying power critical: $90.94 < $150"))
    assert line.startswith("the account REFUSED")
    assert "?" not in line


def test_a_kill_condition_row_is_well_formed_without_a_symbol():
    row = dl.row_from_verdict(
        dl.refusal(None, "kill_condition", "Buying power critical: $90.94 < $150",
                   mandate="apex", value=90.94),
        bot="prop_apex", buying_power=90.94, equity=1008.46)
    assert row is not None
    assert row["symbol"] is None
    assert row["failed_rules"] == "kill_condition"
    assert row["buying_power"] == 90.94


def test_the_halt_is_logged_before_the_cycle_returns():
    """The halt stops the cycle BEFORE the scanner, so try_open is never
    reached and no entry-path logging can fire. Without this the log
    stays permanently empty while the bot halts every cycle - which is
    exactly what happened, on buying power $90.94 against a $150 floor.
    """
    src = (HERE / "prop_bot.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_prop_cycle")
    body = ast.get_source_segment(src, fn)
    head = body[:body.index("[KILL CONDITION]")]
    tail = body[body.index("[KILL CONDITION]"):]
    logged = tail.index("_record_trade_decision")
    returned = tail.index("return")
    assert logged < returned, "the cycle returns before the halt is recorded"


def test_a_repeating_halt_does_not_write_a_row_every_cycle():
    """1,400 identical rows a day is noise, not a record."""
    import importlib
    import prop_bot
    prop_bot._last_kill_logged.update(reason=None, at=0.0)
    r = "Buying power critical: $90.94 < $150"
    assert prop_bot._should_log_kill(r) is True, "the first one must be written"
    assert prop_bot._should_log_kill(r) is False
    assert prop_bot._should_log_kill(r) is False


def test_a_changed_halt_reason_is_never_deduped():
    import prop_bot
    prop_bot._last_kill_logged.update(reason=None, at=0.0)
    assert prop_bot._should_log_kill("Buying power critical: $90.94 < $150") is True
    assert prop_bot._should_log_kill("Daily loss limit hit: -$60.00") is True, (
        "a different reason is a different decision")


def test_a_standing_halt_still_leaves_a_trail():
    """Deduping must not make a permanent halt go silent after one row."""
    import time as _t
    import prop_bot
    r = "Buying power critical: $90.94 < $150"
    prop_bot._last_kill_logged.update(reason=r,
                                      at=_t.time() - prop_bot.KILL_LOG_HEARTBEAT_SECONDS - 1)
    assert prop_bot._should_log_kill(r) is True


# ── the shape the endpoint actually sends ────────────────────────────────

def test_summarise_accepts_the_to_dict_shape_the_endpoint_sends():
    """THE 500. row_from_verdict stores failed_rules as a comma-joined
    STRING; TradeDecision.to_dict splits it back into a LIST - and the
    endpoint passes to_dict output. .split on a list raised AttributeError
    and the endpoint returned 500.

    It only fired once a row WITH failed_rules existed, so the endpoint
    worked perfectly while the table was empty and broke the moment the
    kill-condition logging gave it something to read. The tests fed it
    row_from_verdict output and never the shape the caller sends.
    """
    out = dl.summarise([{"failed_rules": ["kill_condition"], "admitted": False}])
    assert out["top_blockers"] == [{"rule": "kill_condition", "count": 1}]
    assert out["refused"] == 1


def test_summarise_still_accepts_the_row_shape():
    out = dl.summarise([{"failed_rules": "universe,rsi_oversold", "admitted": False}])
    assert {b["rule"] for b in out["top_blockers"]} == {"universe", "rsi_oversold"}


def test_summarise_survives_a_row_with_no_failed_rules():
    for empty in (None, "", []):
        out = dl.summarise([{"failed_rules": empty, "admitted": True}])
        assert out["top_blockers"] == []
        assert out["admitted"] == 1


def test_the_real_model_to_dict_round_trips_through_summarise():
    """Built from the model itself rather than a hand-written dict, so the
    fixture cannot drift from what the endpoint really passes."""
    from models import TradeDecision
    row = TradeDecision(bot="prop_apex", symbol=None, direction=None,
                        mandate="apex", admitted=False,
                        reason="Buying power critical: $90.94 < $150",
                        failed_rules="kill_condition", checks_json=None)
    out = dl.summarise([row.to_dict()])
    assert out["refused"] == 1
    assert out["top_blockers"][0]["rule"] == "kill_condition"


# ── the dedupe keyed on a string containing a live number ────────────────

def test_a_drifting_figure_in_the_reason_does_not_defeat_the_dedupe():
    """THE LIVE FAILURE. 30 rows in nine minutes - every ~40 seconds -
    because the reason carries the buying power to the cent and it moves
    every cycle, so every cycle looked like a NEW reason:

        Buying power critical: $90.22 < $150 | $720.42 of the $810.64...
        Buying power critical: $90.58 < $150 | $720.06 of the $810.64...

    Keying on a string that contains a live figure is keying on the
    figure.
    """
    import prop_bot
    prop_bot._last_kill_logged.update(reason=None, at=0.0)
    seq = [f"Buying power critical: ${bp} < $150 | ${c} of the $810.64 cash"
           for bp, c in (("90.22", "720.42"), ("90.58", "720.06"),
                         ("89.86", "720.78"), ("89.50", "721.14"))]
    wrote = [prop_bot._should_log_kill(r) for r in seq]
    assert wrote == [True, False, False, False], wrote


def test_a_different_condition_is_still_never_deduped():
    import prop_bot
    prop_bot._last_kill_logged.update(reason=None, at=0.0)
    assert prop_bot._should_log_kill("Buying power critical: $90.22 < $150") is True
    assert prop_bot._should_log_kill("Daily loss limit hit: -$60.00") is True
    assert prop_bot._should_log_kill("Equity below survival level: $700.00 < $800") is True
    assert prop_bot._should_log_kill("Too many open positions: 7 > 6") is True


def test_the_key_is_the_condition_name_not_the_numbers():
    import prop_bot
    assert prop_bot._kill_key("Buying power critical: $90.22 < $150") == "Buying power critical"
    assert (prop_bot._kill_key("Buying power critical: $89.86 < $150")
            == prop_bot._kill_key("Buying power critical: $90.58 < $150"))
    assert prop_bot._kill_key("Daily loss limit hit: -$60") != \
        prop_bot._kill_key("Buying power critical: $90 < $150")


def test_a_reason_with_no_colon_still_produces_a_key():
    import prop_bot
    assert prop_bot._kill_key("something odd happened") == "something odd happened"
    assert prop_bot._kill_key("") == "unknown"
    assert prop_bot._kill_key(None) == "unknown"


def test_the_exact_figures_are_still_recorded_on_the_rows_written():
    """Only the dedupe key is coarsened. The record must not be."""
    row = dl.row_from_verdict(
        dl.refusal(None, "kill_condition",
                   "Buying power critical: $90.22 < $150", mandate="apex"),
        bot="prop_apex", buying_power=90.22, equity=1008.46)
    assert "90.22" in row["reason"]
    assert row["buying_power"] == 90.22


def test_the_halt_guard_does_not_write_a_row_per_contract():
    """It is the consequence of a decision already recorded, not a new
    one. A row per symbol per cycle would bury the single halt that
    matters under its own echoes."""
    src = (HERE / "prop_bot.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "try_open")
    body = ast.get_source_segment(src, fn)
    guard = body[body.index("if entries_halted:"):]
    guard = guard[:guard.index("return False")]
    assert "_record_trade_decision" not in guard
    assert "[KILL CONDITION]" in guard
