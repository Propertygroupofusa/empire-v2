"""106 refusals of one rule are one problem, not 106.

On 2026-09-28 /mandates/decisions read, for three consecutive review
passes: "106 decisions, 0 admitted, the rule refusing most often was
kill_condition (106 time(s))". Each pass logged it as unexplained and
moved on, because that sentence is indistinguishable from 106 separate
failures of a rule nobody has read.

Every one of them was a single condition on the prop_apex mandate:
buying power below the $150 floor, because $732.60 of $810.64 cash was
unsettled sale proceeds in a CASH account, where trading unsettled
money causes good-faith violations. The guard was doing its job.

The part the summary could not say at all is the part that mattered:
across 5h13m buying power went $92.98 -> $78.04 while unsettled rose
by exactly the same $14.94. The block was moving AWAY from clearing.
An alarm that reads the same whether a condition is clearing or
worsening is one people stop reading - and this one already had been.
"""
import decision_log as dl


def row(admitted=False, rules="kill_condition", bp=None, at=None, reason="blocked"):
    return {"admitted": admitted, "failed_rules": rules,
            "buying_power": bp, "decided_at": at, "reason": reason}


LIVE = [
    row(bp=92.98, at="2026-09-28T08:08:57", reason="Buying power critical: $92.98 < $150"),
    row(bp=98.14, at="2026-09-28T09:30:00", reason="Buying power critical: $98.14 < $150"),
    row(bp=89.14, at="2026-09-28T11:00:00", reason="Buying power critical: $89.14 < $150"),
    row(bp=78.04, at="2026-09-28T13:21:41", reason="Buying power critical: $78.04 < $150"),
]


def test_one_repeated_rule_is_reported_as_one_condition():
    out = dl.summarise(LIVE)
    b = out["blocking_condition"]
    assert b is not None
    assert b["rule"] == "kill_condition"
    assert b["count"] == 4
    assert "SAME condition" in out["detail"]
    assert "not 4 separate problems" in out["detail"]


def test_it_says_which_way_the_blocking_value_is_moving():
    b = dl.summarise(LIVE)["blocking_condition"]
    assert b["trend"].startswith("WORSENING"), b["trend"]
    assert b["first_value"] == 92.98
    assert b["last_value"] == 78.04
    assert b["change"] == -14.94          # the fixture's own arithmetic
    assert b["max_value"] == 98.14        # best it ever reached, still under $150


def test_a_clearing_condition_reads_differently_from_a_worsening_one():
    # The whole point of the fix: these two must not produce the same
    # sentence. Same rule, same count, opposite direction.
    improving = [row(bp=78.04, at="2026-09-28T08:00:00"),
                 row(bp=140.00, at="2026-09-28T13:00:00")]
    a = dl.summarise(LIVE)["blocking_condition"]["detail"]
    b = dl.summarise(improving)["blocking_condition"]["detail"]
    assert a != b
    assert "WORSENING" in a and "IMPROVING" in b


def test_a_flat_condition_is_neither(  ):
    flat = [row(bp=90.0, at="2026-09-28T08:00:00"),
            row(bp=90.0, at="2026-09-28T13:00:00")]
    b = dl.summarise(flat)["blocking_condition"]
    assert b["trend"] == "FLAT at 90.00"
    assert b["change"] == 0.0


def test_rows_out_of_order_still_give_the_right_direction():
    # The endpoint returns newest-first. Reading first/last off the list
    # order rather than the timestamp would invert every trend.
    b = dl.summarise(list(reversed(LIVE)))["blocking_condition"]
    assert b["first_value"] == 92.98 and b["last_value"] == 78.04
    assert b["trend"].startswith("WORSENING")


def test_a_mixed_window_gets_no_single_story():
    mixed = [row(rules="kill_condition", bp=90.0, at="2026-09-28T08:00:00"),
             row(rules="universe", bp=90.0, at="2026-09-28T09:00:00")]
    out = dl.summarise(mixed)
    assert out["blocking_condition"] is None
    assert "SAME condition" not in out["detail"]


def test_a_window_with_an_admitted_trade_gets_no_single_story():
    """Something got through, so "one condition blocks everything" is
    false and must not be printed.

    This test's first draft used an admitted row with NO failed rule,
    which the every-row-carries-it guard rejects on its own - so it
    passed against a build with the `admitted` guard deleted and proved
    nothing. A surviving mutant is the test's fault. It now isolates
    that guard: every row carries the same single rule, so the count
    check cannot fire, and only "did anything get through" can stop it.
    """
    some = [row(admitted=True, rules="kill_condition", bp=200.0, at="2026-09-28T08:00:00"),
            row(admitted=False, rules="kill_condition", bp=90.0, at="2026-09-28T09:00:00")]
    # the other two guards are satisfied - one rule, on every row
    assert dl.summarise(some)["top_blockers"] == [{"rule": "kill_condition", "count": 2}]
    assert dl.summarise(some)["admitted"] == 1
    assert dl.summarise(some)["blocking_condition"] is None


def test_a_rule_that_only_some_refusals_carry_is_not_collapsed():
    # One rule name in the tally, but a refusal with no named rule at
    # all - so the count does not cover the window and there is no
    # single condition to report.
    partial = [row(rules="kill_condition", bp=90.0, at="2026-09-28T08:00:00"),
               row(rules="", bp=90.0, at="2026-09-28T09:00:00")]
    assert dl.summarise(partial)["blocking_condition"] is None


def test_a_missing_blocking_value_is_UNKNOWN_never_a_guess():
    novals = [row(bp=None, at="2026-09-28T08:00:00"),
              row(bp=None, at="2026-09-28T13:00:00")]
    b = dl.summarise(novals)["blocking_condition"]
    assert b["trend"] == "UNKNOWN"
    assert "cannot be established" in b["detail"]
    assert "first_value" not in b


def test_failed_rules_as_a_comma_string_works_too():
    # row_from_verdict stores a STRING, TradeDecision.to_dict returns a
    # LIST, and both shapes reach summarise. This bit the caller once
    # already with a 500.
    as_list = [dict(r, failed_rules=["kill_condition"]) for r in LIVE]
    assert dl.summarise(as_list)["blocking_condition"]["count"] == 4


def test_the_existing_summary_fields_are_untouched():
    out = dl.summarise(LIVE, returned=2)
    assert out["decisions"] == 4 and out["admitted"] == 0 and out["refused"] == 4
    assert out["top_blockers"] == [{"rule": "kill_condition", "count": 4}]
    assert out["returned"] == 2
    assert "2 of them are shown below" in out["detail"]


def test_an_empty_window_still_has_the_key():
    out = dl.summarise([])
    assert out["decisions"] == 0
    assert out.get("blocking_condition") is None


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)


# ── what the CALLER actually sends ─────────────────────────────────────
#
# blocking_condition's first live call returned trend UNKNOWN, first_at
# null, last_at null and reason "" - while every test above said
# WORSENING. The endpoint's window query selected two columns,
# `admitted` and `failed_rules`, deliberately, so counting a whole
# window stays cheap. The three fields the trend needs were not in it.
#
# summarise()'s own source already carries a comment about this exact
# mistake: "My tests fed it row_from_verdict output and never the
# to_dict output the caller actually sends." Made twice now, in the
# same function. These tests pin the caller's shape, not a convenient
# fixture.
import ast
import os

ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "routers", "trading_dashboard.py")

# Row keys _one_blocking_condition reads. Derived from the source below,
# not typed out, so the two cannot drift.
def _fields_the_summary_reads():
    import decision_log as _dl
    import inspect
    src = inspect.getsource(_dl._one_blocking_condition)
    tree = ast.parse(src.lstrip())
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "g" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            found.add(node.args[1].value)
    return found


def test_the_window_projection_carries_every_field_the_summary_reads():
    """The static guard. If someone narrows that select again, or the
    summary starts reading a new field, this fails instead of the
    feature silently degrading to UNKNOWN in production."""
    src = open(ROUTER, encoding="utf-8").read()
    tree = ast.parse(src)
    selected = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "select"):
            for a in node.args:
                if (isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
                        and a.value.id == "TradeDecision"):
                    selected.add(a.attr)
    needed = _fields_the_summary_reads()
    # `reason` is passed separately by the caller, on purpose - free text
    # is not pulled across a 5000-row window.
    needed.discard("reason")
    missing = needed - selected
    assert not missing, (
        f"the decisions window projection does not select {sorted(missing)}, "
        f"which _one_blocking_condition reads - it can only report UNKNOWN. "
        f"Selected: {sorted(selected)}")


def test_the_two_column_shape_the_endpoint_used_to_send_reports_unknown():
    """The shape that actually shipped. It must degrade honestly, not
    invent a direction from fields it does not have."""
    two_col = [{"admitted": False, "failed_rules": "kill_condition"} for _ in range(106)]
    b = dl.summarise(two_col)["blocking_condition"]
    assert b is not None and b["count"] == 106
    assert b["trend"] == "UNKNOWN"
    assert "cannot be established" in b["detail"]


def test_the_reason_can_be_passed_in_when_the_window_does_not_carry_it():
    two_col = [{"admitted": False, "failed_rules": "kill_condition",
                "decided_at": "2026-09-28T08:08:57", "buying_power": 92.98},
               {"admitted": False, "failed_rules": "kill_condition",
                "decided_at": "2026-09-28T13:21:41", "buying_power": 78.04}]
    b = dl.summarise(two_col, sample_reason="Buying power critical: $78.04 < $150")["blocking_condition"]
    assert b["reason"] == "Buying power critical: $78.04 < $150"
    assert b["trend"].startswith("WORSENING")
    assert "Buying power critical" in b["detail"]


def test_a_row_carrying_its_own_reason_still_works_without_the_argument():
    b = dl.summarise(LIVE)["blocking_condition"]
    assert "Buying power critical: $92.98" in b["reason"]
