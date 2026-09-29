#!/usr/bin/env python3
"""The stop line an adopted branch logs must not contradict itself.

WHAT WENT WRONG. The live log read, every cycle:

    [GRID] crypto_grid_20: ADOPTED STOP ARMED - no stop on this adopted branch:
    GRID_ADOPTED_STOP_MODE is not 'arm', so nothing sells JASMY-USD
    automatically at any price

"ARMED" sitting directly on top of a reason that says it is not armed. The
cause was an ordering: the code asked about the SLICES before it asked about
the STOP.

    if stop_pct == 0 and slices:   -> NO GRID STOP
    elif resolved is not None:     -> ADOPTED STOP ARMED

JASMY holds no open slices, so the first arm's `and slices` was false and
control fell into the second, whose condition is true whenever an adopted-stop
dict came back at all - armed or not. No money moved: the stop was still 0 and
there was nothing to stop out. But this is the defect the whole adopted-stop
change was made to prevent, in the log rather than the trade.

WHAT IS ASSERTED HERE. The four states of (stop, slices), by calling the
decision and reading what comes out - not by grepping the source, which is how
six assertions in this repo broke on wording that moved. The property that
matters is stated once and checked against every state:

    ARMED appears if and only if the adopted-stop engine answered AND the stop
    it gave back is greater than zero.

Both halves are load-bearing. Drop the second and JASMY is back. Drop the
first and a branch that named its own 8% stop gets described as an adopted
catastrophe stop it was never given.

Run: python3 test_stop_report_line.py
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import crypto_grid_bot

line = crypto_grid_bot.stop_report_line

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


# The two adopted-stop dicts adaptive_stop.adopted_stop actually returns.
UNARMED = {
    "stop_pct": 0.0,
    "source": "adopted-unarmed",
    "reason": ("no stop on this adopted branch: GRID_ADOPTED_STOP_MODE is not "
               "'arm', so nothing sells JASMY-USD automatically at any price"),
}
ARMED = {
    "stop_pct": 0.35,
    "source": "adopted-catastrophe",
    "reason": ("catastrophe stop 35.0% for ZEC-USD (6.0x a 5.8% move, capped "
               "at 35%)"),
}

# (label, stop_pct, resolved, has_slices)
STATES = [
    ("jasmy: unarmed, no slices", 0.0, UNARMED, False),
    ("zec: unarmed, holding slices", 0.0, UNARMED, True),
    ("armed, holding slices", 0.35, ARMED, True),
    ("armed, no slices yet", 0.35, ARMED, False),
    ("plain 0 override, no slices", 0.0, None, False),
    ("plain 0 override, holding slices", 0.0, None, True),
    ("real override of 8%", 0.08, None, True),
    ("adopted stop threw, left at 0", 0.0, None, True),
]


def test_the_shape_is_a_level_and_a_message():
    for label, pct, resolved, slices in STATES:
        got = line("crypto_grid_20", pct, resolved, slices)
        ok(f"{label}: returns (level, message)",
           isinstance(got, tuple) and len(got) == 2, repr(got))
        if not (isinstance(got, tuple) and len(got) == 2):
            continue
        lvl, msg = got
        ok(f"{label}: level is one this caller can dispatch on",
           lvl in ("warning", "info"), repr(lvl))
        ok(f"{label}: names the branch", "crypto_grid_20" in msg, msg[:80])
        ok(f"{label}: leaks no placeholder",
           not any(bad in msg for bad in ("None", "{", "}")), msg[:120])


def test_armed_is_said_if_and_only_if_an_adopted_stop_exists():
    """THE one property. Said once, checked against every state, because the
    live bug was a state nobody had enumerated."""
    for label, pct, resolved, slices in STATES:
        _, msg = line("crypto_grid_20", pct, resolved, slices)
        says_armed = "ARMED" in msg
        truly_armed = resolved is not None and pct > 0
        ok(f"{label}: ARMED ({says_armed}) matches being armed ({truly_armed})",
           says_armed == truly_armed, msg[:160])
        # The half that was actually broken, checked on its own so a future
        # rewrite cannot satisfy the line above by loosening both sides.
        ok(f"{label}: never claims ARMED at a stop of zero",
           not (says_armed and pct == 0), msg[:160])


def test_the_exact_live_line_no_longer_claims_to_be_armed():
    """JASMY-USD: stop 0, override 0, zero open slices, mode off."""
    lvl, msg = line("crypto_grid_20", 0.0, UNARMED, False)
    ok("jasmy does not say ARMED", "ARMED" not in msg, msg)
    ok("and does not say ARMED in any casing", "armed" not in msg.lower(), msg)
    ok("it says there is no stop", "no stop" in msg.lower(), msg)
    ok("it says why there is nothing to shout about",
       "no open slice" in msg, msg)
    ok("it keeps the reason the engine gave", UNARMED["reason"] in msg, msg)
    ok("and it is quiet, because nothing is exposed", lvl == "info", lvl)


def test_an_unstopped_branch_holding_coin_is_still_loud():
    """The regression that must NOT be traded away for a quieter log: a branch
    with open slices and no trigger at any price."""
    lvl, msg = line("crypto_grid_9", 0.0, UNARMED, True)
    ok("it warns", lvl == "warning", lvl)
    ok("it is the siren line", "🚨" in msg, msg[:80])
    ok("and it names the absence", "NO GRID STOP" in msg, msg[:80])
    ok("it keeps the engine's reason", UNARMED["reason"] in msg, msg)


def test_a_real_armed_stop_reports_the_number_it_will_sell_at():
    lvl, msg = line("crypto_grid_9", 0.35, ARMED, True)
    ok("armed says ARMED", "ADOPTED STOP ARMED" in msg, msg[:80])
    ok("armed is a warning, not an info line", lvl == "warning", lvl)
    ok("it carries the resolved percentage", "35.0%" in msg, msg)
    ok("it names the product", "ZEC-USD" in msg, msg)


def test_a_plain_override_is_neither_adopted_nor_a_siren():
    """A branch that named its own 8% stop is not an adopted branch and must
    not be described as one."""
    lvl, msg = line("crypto_grid_3", 0.08, None, True)
    ok("it does not claim to be adopted", "ADOPTED" not in msg, msg)
    ok("it does not shout", lvl == "info" and "🚨" not in msg, f"{lvl} {msg}")
    ok("it reports the override it has", "8.0%" in msg, msg)


def test_a_stopless_branch_with_no_resolution_still_points_somewhere():
    """stop 0, no adopted dict - either an explicit 0 override or the adopted
    call threw. Either way the owner needs to be told where to look."""
    for slices in (True, False):
        _, msg = line("crypto_grid_3", 0.0, None, slices)
        ok(f"slices={slices}: points at /resting-stops",
           "/resting-stops" in msg, msg[:140])
        ok(f"slices={slices}: names the field to look under",
           "uncovered" in msg, msg[:140])
        ok(f"slices={slices}: claims no cover it cannot prove",
           "No portfolio cover is claimed" in msg, msg[:140])


def test_it_reads_no_state_of_its_own():
    """Pure, so it stays checkable. If it grew a database or env read, the four
    states above would stop being the whole story."""
    import inspect
    src = inspect.getsource(line)
    body = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    # Docstring excluded: it quotes the old code it replaced.
    doc = line.__doc__ or ""
    body = body.replace(doc, "")
    for forbidden in ("await", "os.environ", "getenv", "get_state", "session"):
        ok(f"it does not reach for {forbidden}", forbidden not in body,
           f"{forbidden} appears in the body")
    ok("it takes exactly the four facts it decides on",
       list(inspect.signature(line).parameters) ==
       ["bot_name", "stop_pct", "resolved", "has_slices"],
       str(list(inspect.signature(line).parameters)))


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} stop-report-line checks passed")
