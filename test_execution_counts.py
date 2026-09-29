#!/usr/bin/env python3
"""A maker order nobody crossed is not a fill.

THE BUG
-------
The Live Ops panel computed

    submitted = GATE_PASS count
    filled    = max(0, submitted - rejected)
    fill_rate = filled / submitted

where `rejected` counted ORDER_REJECTED and nothing else. That is correct
only if a rejection is the one way a buy can fail. Three others exist, and
under maker-ONLY mode - where an unfilled maker order has no market
fallback - the first of them is the COMMON outcome, not a rare one:

    MAKER_EXPIRED     rested for its window, nobody crossed it
    ORDER_NOT_PLACED  no order was ever created
    ORDER_NO_FILL     no fill, and whether an order rested is UNKNOWN

Every one of those was counted as a FILL. So was a GATE_PASS that a later
check blocked before anything reached the venue. And GATE_OBSERVE - which
also lets the buy through, under the DEFAULT veto mode - was left out of the
attempt count entirely, so a real attempt read as no attempt at all and the
`max(0, ...)` clamp quietly absorbed the contradiction.

Every figure in the panel could therefore only ever be too flattering, on a
page whose first design rule is that nothing is fabricated.

WHAT THIS FILE TESTS
--------------------
The arithmetic itself, by calling it. execution_counts is pure - two
count-by-event-type dicts in, one report out - so these are real inputs and
real outputs, not assertions about how the source is written.

Two of the checks are drift guards, and they are the reason the groups and
the arithmetic were moved into one module:

  * every event type the query asks for must actually change the output,
    so a name cannot be added to the query and left out of the sum;
  * every event type order_outcome can emit must be covered by one of the
    groups, so a new cause cannot appear and silently count as a fill.
"""
import sys

import crypto_fleet_metrics as metrics
import order_outcome as oo

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


def counts(allowed=0, observe=0, **outcomes):
    """execution_counts with the gate side spelled out readably."""
    gate = {"GATE_PASS": allowed, "GATE_OBSERVE": observe,
            # Present and deliberately ignored: a blocked or errored verdict
            # never produced an order, so it is not an attempt.
            "GATE_BLOCK": 7, "GATE_ERROR": 3}
    return metrics.execution_counts(gate, outcomes)


def test_the_live_case_a_fleet_of_maker_expiries_is_not_a_full_fill_rate():
    """Ten buys allowed, eight maker orders nobody crossed, no rejections.
    The old arithmetic reported ten fills at 100%."""
    r = counts(allowed=10, MAKER_EXPIRED=8)
    ok("attempted counts all ten", r["attempted"] == 10, str(r["attempted"]))
    ok("filled is 2, not 10", r["filled"] == 2, str(r["filled"]))
    ok("the fill rate is 20%, not 100%", r["fill_rate_pct"] == 20.0,
       str(r["fill_rate_pct"]))
    ok("the expiries are reported in their own right",
       r["maker_expired"] == 8 and r["no_fill"] == 8, str(r))
    ok("and none of them is called a rejection", r["rejected"] == 0)


def test_every_no_fill_outcome_takes_one_off_the_fill_count():
    """The point of the fix, one event type at a time."""
    for event in ("ORDER_REJECTED", "MAKER_EXPIRED", "ORDER_NOT_PLACED",
                  "ORDER_NO_FILL"):
        base = counts(allowed=5)
        one = counts(allowed=5, **{event: 1})
        ok(f"{event} reduces filled by exactly 1",
           base["filled"] - one["filled"] == 1,
           f"{base['filled']} -> {one['filled']}")
        ok(f"{event} is counted in no_fill",
           one["no_fill"] - base["no_fill"] == 1)


def test_an_observe_mode_verdict_is_an_attempt_because_the_buy_proceeds():
    """_net_edge_gate_ok returns True on the observe path, so an order
    follows. Counting GATE_PASS alone made three real attempts invisible -
    and observe is the default mode."""
    r = counts(allowed=0, observe=3, MAKER_EXPIRED=1)
    ok("three observe verdicts are three attempts", r["attempted"] == 3,
       str(r["attempted"]))
    ok("two of them filled", r["filled"] == 2, str(r["filled"]))
    ok("the fill rate is real, not None", r["fill_rate_pct"] == 66.7,
       str(r["fill_rate_pct"]))
    # The old code read GATE_PASS only: submitted 0, so filled 0 and the
    # rate None - three attempts and one expiry reported as no activity.
    ok("a pass and an observe verdict count the same",
       counts(allowed=3, MAKER_EXPIRED=1)["attempted"]
       == counts(observe=3, MAKER_EXPIRED=1)["attempted"])


def test_a_block_after_the_gate_is_not_an_attempt_and_not_a_fill():
    """LESSON_BLOCK is recorded after an allowing verdict, on a path that
    returns before grid_buy. Nothing reached the venue."""
    r = counts(allowed=5, LESSON_BLOCK=1)
    ok("the blocked one comes off the attempts", r["attempted"] == 4,
       str(r["attempted"]))
    ok("the other four are fills", r["filled"] == 4, str(r["filled"]))
    ok("the fill rate is over the attempts, not the verdicts",
       r["fill_rate_pct"] == 100.0, str(r["fill_rate_pct"]))
    ok("and it is reported rather than absorbed",
       r["blocked_after_gate"] == 1)
    ok("it is not counted as a no-fill either", r["no_fill"] == 0,
       "a buy that was never attempted cannot have failed to fill")


def test_more_outcomes_than_attempts_reports_nothing_rather_than_zero():
    """The case the old max(0, ...) hid. More no-fills than attempts means
    an outcome was recorded whose attempt was not, which is a finding about
    the telemetry - it must not render as a tidy 0 fills."""
    r = counts(allowed=2, MAKER_EXPIRED=5)
    ok("filled is None, not 0", r["filled"] is None, repr(r["filled"]))
    ok("the fill rate is None, not 0.0", r["fill_rate_pct"] is None,
       repr(r["fill_rate_pct"]))
    ok("and it says so out loud",
       r["integrity"] and "UNCOUNTABLE" in r["integrity"], repr(r["integrity"]))
    ok("the raw counts are still reported",
       r["attempted"] == 2 and r["no_fill"] == 5, str(r))


def test_no_attempts_at_all_reports_nothing_rather_than_zero_percent():
    """A quiet window is not a 0% fill rate, and not a 100% one either."""
    r = counts()
    ok("filled is None", r["filled"] is None, repr(r["filled"]))
    ok("the fill rate is None", r["fill_rate_pct"] is None,
       repr(r["fill_rate_pct"]))
    ok("with a reason attached", bool(r["integrity"]), repr(r["integrity"]))
    ok("blocks and errors alone are never attempts", r["attempted"] == 0,
       "the fixture passes GATE_BLOCK 7 and GATE_ERROR 3")


def test_the_fill_rate_can_never_leave_its_own_range():
    """Swept rather than argued. A rate above 100 or below 0 means the
    numerator and denominator are counting different things."""
    bad = []
    for allowed in range(0, 6):
        for observe in range(0, 3):
            for blocked in range(0, 3):
                for expired in range(0, 4):
                    for rejected in range(0, 3):
                        r = counts(allowed=allowed, observe=observe,
                                   LESSON_BLOCK=blocked,
                                   MAKER_EXPIRED=expired,
                                   ORDER_REJECTED=rejected)
                        rate, filled = r["fill_rate_pct"], r["filled"]
                        if rate is not None and not (0.0 <= rate <= 100.0):
                            bad.append((allowed, observe, blocked, expired,
                                        rejected, rate))
                        if filled is not None and filled < 0:
                            bad.append((allowed, observe, blocked, expired,
                                        rejected, filled))
    ok("no combination produces a rate outside 0-100 or a negative fill",
       not bad, f"{len(bad)} bad combinations, first: {bad[:3]}")


def test_filled_and_no_fill_always_account_for_every_attempt():
    """When a figure IS reported it has to add up - otherwise something is
    being counted twice or not at all."""
    bad = []
    for allowed in range(0, 7):
        for expired in range(0, 4):
            for not_placed in range(0, 3):
                for unknown in range(0, 3):
                    r = counts(allowed=allowed, MAKER_EXPIRED=expired,
                               ORDER_NOT_PLACED=not_placed,
                               ORDER_NO_FILL=unknown)
                    if r["filled"] is None:
                        continue
                    if r["filled"] + r["no_fill"] != r["attempted"]:
                        bad.append((allowed, expired, not_placed, unknown, r))
    ok("filled + no_fill == attempted whenever filled is reported", not bad,
       f"{len(bad)} mismatches, first: {bad[:1]}")


def test_every_event_type_the_query_asks_for_changes_the_answer():
    """DRIFT GUARD ONE. The dashboard queries exactly
    EXECUTION_OUTCOME_EVENTS. A name in that set that the arithmetic does
    not read is a row fetched from the database and thrown away - which is
    how `filled = submitted - rejected` survived three new outcomes."""
    base = counts(allowed=20)
    for event in metrics.EXECUTION_OUTCOME_EVENTS:
        one = counts(allowed=20, **{event: 1})
        ok(f"{event} is actually consumed", one != base,
           "it is in EXECUTION_OUTCOME_EVENTS but changes nothing in the output")


def test_every_outcome_the_bot_can_emit_is_covered_by_a_group():
    """DRIFT GUARD TWO, and the one that ties this to the cause split.

    order_outcome.event_for is the only thing that names these events, so
    it is the source of truth. Every event type it can produce for a buy
    with no fill must land in a group execution_counts subtracts. A fifth
    cause added later without touching this module would otherwise count
    as a FILL - exactly the failure being fixed, one layer up.
    """
    covered = set(metrics.ORDER_REJECTED_EVENTS) | set(metrics.NO_FILL_EVENTS)
    # Discovered from the module rather than listed here, so a new constant
    # is picked up without this test being edited.
    causes = [getattr(oo, name) for name in oo.__all__
              if isinstance(getattr(oo, name), str)]
    ok("the causes were discovered, not assumed", len(causes) >= 4,
       f"found {causes}")
    for cause in causes + [None, "a_cause_invented_later"]:
        event, _ = oo.event_for(cause, 25.0, detail="d", wait_seconds=240)
        ok(f"{cause!r} -> {event} is subtracted from fills",
           event in covered,
           f"{event} is in neither ORDER_REJECTED_EVENTS nor NO_FILL_EVENTS, "
           f"so a buy that ended this way would be counted as a fill")


def test_a_disabled_gate_still_counts_the_buys_it_waved_through():
    """GATE_DISABLED returns True - the buy goes in with no economic check.
    Leaving it out of the attempt count meant that switching the gate off
    made every attempt vanish from the fill rate while real money traded."""
    r = metrics.execution_counts({"GATE_DISABLED": 6}, {"MAKER_EXPIRED": 2})
    ok("six unchecked buys are six attempts", r["attempted"] == 6,
       str(r["attempted"]))
    ok("four of them filled", r["filled"] == 4, str(r["filled"]))
    ok("and the rate is reported rather than UNCOUNTABLE",
       r["fill_rate_pct"] is not None and r["integrity"] is None, str(r))


# ─────────────────────────────────────────────────────────────────────────
# The invariant that keeps the attempt count honest, driven through the real
# gate: if _net_edge_gate_ok returns True a buy follows, so whatever verdict
# it wrote on the way out MUST be one the arithmetic counts as an attempt.
# Each True path that writes nothing, or writes a verdict outside
# GATE_ALLOWED_EVENTS, is an attempt this panel cannot see.
# ─────────────────────────────────────────────────────────────────────────

def _gate_paths():
    import asyncio
    import contextlib

    import crypto_grid_bot as grid
    import crypto_nine_coin_scanner as scanner
    import trading_profile

    @contextlib.contextmanager
    def patched(module, **names):
        missing = [n for n in names if not hasattr(module, n)]
        if missing:
            raise AssertionError(f"{module.__name__} has no {missing}")
        old = {n: getattr(module, n) for n in names}
        try:
            for n, v in names.items():
                setattr(module, n, v)
            yield
        finally:
            for n, v in old.items():
                setattr(module, n, v)

    def aval(value):
        async def _f(*a, **k):
            return value
        return _f

    guarded = trading_profile.GUARDED

    def run(*, profile=guarded, gate_active=True, economics=(True, "priced ok", {}),
            veto_mode="off", micro_ok=True, explode=False):
        seen = []

        async def _record(bot_name, product_id, verdict, reason):
            seen.append(verdict)

        def _evaluate(*a, **k):
            if explode:
                raise RuntimeError("book maths blew up")
            return economics

        with patched(grid,
                     get_trading_profile=aval(profile),
                     is_net_edge_gate_active=aval(gate_active),
                     is_maker_only_active=aval(False),
                     MICROSTRUCTURE_VETO_MODE=veto_mode,
                     _record_gate_decision=_record), \
             patched(grid.engine,
                     get_book_top_and_depth=aval((100.0, 100.1, 5000.0, 5000.0)),
                     get_average_hourly_swing_pct=aval(1.2),
                     get_real_fee_tier=aval((0.004, 0.006, "tier", None)),
                     get_recent_market_trades=aval([])), \
             patched(scanner,
                     evaluate_grid_step=_evaluate,
                     book_imbalance=lambda *a, **k: 0.5,
                     trade_aggression=lambda *a, **k: 0.5,
                     microstructure_veto=lambda *a, **k: (
                         micro_ok, "book is hostile")):
            allowed, _reason = asyncio.run(grid._net_edge_gate_ok(
                object(), "TEST-USD", 0.02, 50.0, bot_name="test-bot"))
        return allowed, seen

    return (
        ("the profile turns the economic gates off", dict(profile="aug2026")),
        ("the gate is switched off in the database", dict(gate_active=False)),
        ("the economics do not clear", dict(economics=(False, "target too thin", {}))),
        ("the microstructure veto enforces",
         dict(veto_mode="enforce", micro_ok=False)),
        ("the microstructure veto only observes",
         dict(veto_mode="observe", micro_ok=False)),
        ("everything passes cleanly", dict(veto_mode="observe", micro_ok=True)),
        ("the gate itself raises", dict(explode=True)),
    ), run


def test_every_allowed_buy_writes_a_verdict_the_attempt_count_can_see():
    paths, run = _gate_paths()
    for label, kwargs in paths:
        allowed, verdicts = run(**kwargs)
        print(f"\n  [{label}] -> allowed={allowed} verdicts={verdicts}")
        ok(f"{label}: exactly one verdict was written", len(verdicts) == 1,
           f"wrote {verdicts}")
        if len(verdicts) != 1:
            continue
        counted = verdicts[0] in metrics.GATE_ALLOWED_EVENTS
        ok(f"{label}: counted as an attempt exactly when the buy was allowed",
           counted == bool(allowed),
           f"allowed={allowed} but verdict {verdicts[0]!r} counted={counted}")


def test_a_maker_expiry_is_a_non_fill_even_though_it_is_benign():
    """Two different questions, and conflating them is what BENIGN_CAUSES
    is for. is_execution_fault says whether anything is WRONG; this panel
    counts whether anything FILLED. A maker expiry is not a fault and is
    not a fill."""
    ok("a maker expiry is not an execution fault",
       oo.is_execution_fault(oo.MAKER_EXPIRED) is False)
    ok("and is still subtracted from fills",
       "MAKER_EXPIRED" in metrics.NO_FILL_EVENTS)
    ok("it is reported apart from rejections",
       counts(allowed=4, MAKER_EXPIRED=2)["rejected"] == 0)


def test_zz_nothing_above_failed():
    """Runs last, by name: ok() records rather than raises so one failure
    cannot hide the rest of the report."""
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        t()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} execution-count checks passed")
