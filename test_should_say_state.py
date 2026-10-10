#!/usr/bin/env python3
"""46,656 lines a day of unchanged state, and the one line that mattered
was in there somewhere.

WHAT WENT WRONG. A branch cycles about every 50 seconds - 1,728 lines a
day for every per-branch line that prints unconditionally. Counted on the
live fleet 2026-10-09: ADOPTED STOP ARMED on 13 branches, the per-branch
stop line on 8, the drawdown breaker on 2, rise-trigger-found-nothing on
3, ZEC's exit notice on 1. 46,656 lines a day, none of them saying
anything the previous one had not.

WHAT IT COST. At 19:51:22Z that day, 1,134.30 ALGO left the account on a
taker sell with no ledger row, after three CRITICAL alerts about ALGO had
fired in the hours before it. Nobody saw any of it. The owner, looking at
that log: "Fix this."

THE TRAP THIS FILE EXISTS FOR. The first version of the suppressor
compared whole log lines, and three of the five repeaters embed a number
that moves every cycle:

    ADOPTED STOP ARMED ... 6.00x its own 5.81% daily volatility
    real equity $412.07 is down 22% from its own $531.14 peak
    real rise trigger fired ($0.4412 >= $0.4488) but no open slice ...

Volatility is remeasured each cycle, equity moves with the mark, the price
is the price. Compare whole lines and every one is "new" every 50 seconds:
the suppressor runs, costs a dict write, and suppresses NOTHING. So the
caller passes a fingerprint of the state and prints whatever it likes.

WHAT IS ASSERTED HERE, by calling the decision and reading what comes back
rather than grepping the source - which is how six assertions in this
repository broke on wording that moved:

  1. A state never seen prints. Nothing is swallowed after a restart.
  2. The same state does not print again.
  3. A changed state prints, including a change BACK to an earlier value.
  4. An unchanged state is re-asserted on the cadence - quiet, never
     silent. Boundary checked on both sides.
  5. Keys are independent: one branch's stop changing cannot suppress its
     breaker, and no branch can suppress another.
  6. THE REGRESSION: the three real live lines, fingerprinted exactly as
     their call sites fingerprint them, are suppressed while their text
     keeps moving - and still print when the state underneath them moves.

Run: python3 test_should_say_state.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import crypto_grid_bot

say = crypto_grid_bot.should_say_state

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


HOUR = 3600.0


def test_a_state_never_seen_is_always_said():
    seen = {}
    ok("first sight prints",
       say("crypto_grid_1:stop", "armed:12.4", 1000.0, HOUR, seen) is True)
    ok("a different key's first sight also prints",
       say("crypto_grid_2:stop", "armed:12.4", 1000.0, HOUR, seen) is True)
    ok("both were recorded", len(seen) == 2, repr(seen))


def test_the_same_state_does_not_repeat():
    seen = {}
    say("k", "s", 1000.0, HOUR, seen)
    for i in range(1, 60):          # ~50 minutes of 50-second cycles
        t = 1000.0 + i * 50.0
        if say("k", "s", t, HOUR, seen) is not False:
            ok(f"unchanged state silent across cycles (failed at cycle {i})", False)
            return
    ok("unchanged state silent across 59 cycles", True)


def test_a_changed_state_is_said_at_once():
    seen = {}
    say("k", "armed:12.4", 1000.0, HOUR, seen)
    ok("a change prints immediately, not on the next cadence",
       say("k", "armed:12.5", 1050.0, HOUR, seen) is True)
    ok("and the new state is then the one held",
       say("k", "armed:12.5", 1100.0, HOUR, seen) is False)


def test_a_change_back_to_an_earlier_state_is_still_a_change():
    # A breaker that clears and re-breaches inside the hour must say so.
    # Keeping a set of everything ever seen would swallow exactly that.
    seen = {}
    say("k", "breached:22", 1000.0, HOUR, seen)
    ok("state leaves", say("k", "clear", 1050.0, HOUR, seen) is True)
    ok("state returns and is said again",
       say("k", "breached:22", 1100.0, HOUR, seen) is True)


def test_an_unchanged_state_is_reasserted_on_the_cadence():
    seen = {}
    say("k", "s", 1000.0, HOUR, seen)
    ok("one second short of the cadence: still silent",
       say("k", "s", 1000.0 + HOUR - 1, HOUR, seen) is False)
    ok("exactly on the cadence: said again",
       say("k", "s", 1000.0 + HOUR, HOUR, seen) is True)
    ok("the clock restarts from the re-assertion, not from first sight",
       say("k", "s", 1000.0 + HOUR + 1, HOUR, seen) is False)
    ok("and again one cadence later",
       say("k", "s", 1000.0 + 2 * HOUR, HOUR, seen) is True)


def test_keys_are_independent():
    seen = {}
    say("crypto_grid_1:stop", "armed:12.4", 1000.0, HOUR, seen)
    say("crypto_grid_1:breaker", "breached:22", 1000.0, HOUR, seen)
    say("crypto_grid_2:stop", "armed:30.0", 1000.0, HOUR, seen)
    # One branch's stop changes.
    say("crypto_grid_1:stop", "armed:9.0", 1050.0, HOUR, seen)
    ok("a branch's own breaker is untouched by its stop changing",
       say("crypto_grid_1:breaker", "breached:22", 1100.0, HOUR, seen) is False)
    ok("another branch is untouched entirely",
       say("crypto_grid_2:stop", "armed:30.0", 1100.0, HOUR, seen) is False)
    ok("and that other branch still speaks when ITS state moves",
       say("crypto_grid_2:stop", "armed:31.0", 1150.0, HOUR, seen) is True)


def test_it_reads_no_clock_and_no_state_of_its_own():
    # Time only ever comes from the caller: a test can drive a year in a
    # loop, and nothing here can be wrong about "now".
    seen = {}
    say("k", "s", 0.0, HOUR, seen)
    ok("a caller-supplied clock far in the past is honoured",
       say("k", "s", 1.0, HOUR, seen) is False)
    ok("a caller-supplied clock far in the future re-asserts",
       say("k", "s", 10 ** 9, HOUR, seen) is True)
    # The module-level store is not touched when a store is passed in.
    before = dict(crypto_grid_bot._STATE_LINE_SEEN)
    say("isolation-probe", "s", 1000.0, HOUR, seen)
    ok("passing a store leaves the module's own store alone",
       crypto_grid_bot._STATE_LINE_SEEN == before)


def test_the_default_cadence_is_an_hour_and_is_settable():
    ok("default re-assert cadence is one hour",
       crypto_grid_bot.GRID_STATE_REASSERT_SECONDS == 3600,
       repr(crypto_grid_bot.GRID_STATE_REASSERT_SECONDS))
    seen = {}
    say("k", "s", 1000.0, None, seen)
    ok("omitting the cadence falls back to the module default, not to 0",
       say("k", "s", 1001.0, None, seen) is False)


# ── THE REGRESSION. The three live lines whose text moves every cycle. ──
#
# Each case is (label, key, a sequence of (fingerprint, seconds) as the
# call site computes it while the underlying state does NOT move, then the
# fingerprint once it DOES).

def _stop_fingerprint(level, source, stop_pct, has_slices):
    """Exactly what the adopted-stop call site passes."""
    return f"{level}:{source}:{round(stop_pct * 100, 1)}:{has_slices}"


def test_an_adopted_stop_is_quiet_while_only_its_volatility_drifts():
    seen = {}
    # adaptive_stop re-reads daily volatility each cycle. 5.81 -> 5.83 ->
    # 5.80 moves the printed line and the stop by hundredths; the state the
    # line describes - armed, adopted, ~34.9% - has not moved.
    said = 0
    for i, vol_driven_stop in enumerate([0.3486, 0.3490, 0.3493, 0.3488, 0.3491]):
        fp = _stop_fingerprint("warning", "adopted_adaptive", vol_driven_stop, True)
        if say("crypto_grid_20:stop", fp, 1000.0 + i * 50.0, HOUR, seen):
            said += 1
    ok("volatility drift under a tenth of a percent prints once, not five times",
       said == 1, f"said {said} times")
    # The stop crossing a tenth of a percent IS a change worth a line.
    ok("a stop that actually moves a tenth of a percent is reported",
       say("crypto_grid_20:stop",
           _stop_fingerprint("warning", "adopted_adaptive", 0.3520, True),
           1400.0, HOUR, seen) is True)
    # Arming or disarming is never swallowed, whatever the cadence.
    ok("the stop going away is reported at once",
       say("crypto_grid_20:stop",
           _stop_fingerprint("warning", "adopted-unarmed", 0.0, True),
           1450.0, HOUR, seen) is True)
    ok("a slice opening under a stopless branch is reported at once",
       say("crypto_grid_20:stop",
           _stop_fingerprint("info", "adopted-unarmed", 0.0, False),
           1500.0, HOUR, seen) is True)


def test_the_breaker_is_quiet_while_only_the_equity_moves():
    seen = {}
    said = 0
    # The message carries $412.07 ... $531.14, both of which move with the
    # mark. The fingerprint carries 22.
    for i, dd in enumerate([0.2241, 0.2238, 0.2245, 0.2239, 0.2243]):
        if say("crypto_grid_11:breaker", f"breached:{round(dd * 100)}",
               1000.0 + i * 50.0, HOUR, seen):
            said += 1
    ok("equity moving inside the same whole percent prints once",
       said == 1, f"said {said} times")
    ok("the drawdown deepening a whole percent is reported",
       say("crypto_grid_11:breaker", f"breached:{round(0.2351 * 100)}",
           1400.0, HOUR, seen) is True)


def test_the_rise_trigger_is_quiet_while_only_the_price_moves():
    seen = {}
    said = 0
    # Here the message embeds the price twice and nothing in it repeats.
    # What is actually standing still is the holding: 3 slices, none
    # sellable at a profit.
    for i in range(6):
        if say("crypto_grid_4:norise", "no-profitable-slice:3",
               1000.0 + i * 50.0, HOUR, seen):
            said += 1
    ok("a standing hold prints once, not once per price tick",
       said == 1, f"said {said} times")
    ok("a slice opening or closing changes the hold and is reported",
       say("crypto_grid_4:norise", "no-profitable-slice:4",
           1400.0, HOUR, seen) is True)


def test_the_measured_saving_is_what_was_claimed():
    # The five live repeaters, at 1,728 cycles a branch a day, against the
    # same five under an hourly re-assert. This is the number in the commit
    # message, computed rather than asserted from memory.
    cycles_per_day = 1728
    branches = {"adopted stop": 13, "stop line": 8, "breaker": 2,
                "rise trigger": 3, "exit notice": 1}
    before = sum(branches.values()) * cycles_per_day
    seen = {}
    after = 0
    for name, count in branches.items():
        for b in range(count):
            for i in range(cycles_per_day):
                if say(f"bot{b}:{name}", "unchanged", i * 50.0, HOUR, seen):
                    after += 1
    ok("the fleet printed 46,656 of these lines a day", before == 46656, str(before))
    ok("an unchanged day now costs 24 lines a branch-state, not 1,728",
       after == sum(branches.values()) * 24, str(after))
    ok("46,656 lines a day becomes 648, a 99% cut, state still stated hourly",
       after == 648 and round((1 - after / before) * 100) == 99,
       f"{after} lines, {round((1 - after / before) * 100)}%")


# ── THE ONE WAY THIS FIX COULD BREAK TRADING ──────────────────────────
#
# Every call site wraps a LOG line. If a `return`, an `await`, or an
# assignment ever ends up inside one of those blocks, a suppressed line
# stops being a quiet log and starts skipping a decision - a branch would
# trade differently on the second cycle than on the first. Asserted on the
# PARSED TREE, so it holds whatever the wording becomes.

def test_no_call_site_can_swallow_anything_but_a_log_line():
    import ast
    with open("crypto_grid_bot.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = [n for n in ast.walk(node.test)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "should_say_state"]
        if not calls:
            continue
        sites += 1
        where = f"line {node.lineno}"
        ok(f"{where}: guards a log line and nothing else",
           all(isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
               for st in node.body),
           f"body holds {[type(st).__name__ for st in node.body]}")
        banned = [type(inner).__name__
                   for st in node.body for inner in ast.walk(st)
                   if isinstance(inner, (ast.Return, ast.Await,
                                         ast.Assign, ast.AugAssign))]
        ok(f"{where}: no return, await or assignment is suppressed with it",
           not banned, f"found {sorted(set(banned))}")
        ok(f"{where}: has no else branch to diverge on",
           not node.orelse)
    # Not a pinned count. This read `sites == 7` and failed the moment an
    # eighth guarded log line was added - the same magic-number failure
    # test_newsroom had with `== 8`, and the same fix: compare against what
    # the file actually contains. The scan's real claim is that it REACHED
    # every call, i.e. that none sits outside an `if` test where the three
    # checks above could not see it. So count every should_say_state call
    # in the file and require the two numbers to agree.
    every_call = sum(1 for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)
                     and n.func.id == "should_say_state")
    ok("every live call site was reached by this scan",
       sites == every_call and sites > 0,
       f"{sites} reached of {every_call} in the file")


def test_zz_nothing_above_failed():
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL {_passes} ASSERTIONS PASS")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
