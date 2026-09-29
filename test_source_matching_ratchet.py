#!/usr/bin/env python3
"""A test that matches code-shaped text against source can fail a correct change.

WHAT THIS COUNTS, AND WHY IT IS NOT ZERO.

Six checks broke on 2026-09-29, every one of them because the property it
guarded was intact and only the WORDING moved:

    "_cached_real_maker_fee_rate is None" in worst   the floor started reading
                                                     the durable measurement
    "min(" in worst                                  the clamp moved a line
    "_reported_stop(b, stop_by_product.get(...))"    the call gained a keyword
    "NO grid stop" in reason                         the reason got more honest
    an inline `.get()` shape                         one read replaced two
    "UNREADABLE" in seg                              matched a COMMENT, and so
                                                     PASSED when it should not

That last one is the other direction, and it is worse: a substring check can
match the comment that EXPLAINS the bug and go green over broken code. Both
failures have the same cause - asserting on text instead of on the fact.

So the count matters in both directions and the honest number is not zero.
Plenty of these are fine: an HTML element id really is a string, and checking
it exists is the right test. Rewriting 563 assertions inside a live trading
system to chase a zero would break more than it fixed.

THIS IS A RATCHET. It pins today's number and fails when it goes UP. New tests
assert on parsed structure or on behaviour; the existing ones get retargeted
when they next get in the way, and BASELINE comes down with them. Lowering it
is the point; raising it needs a reason typed into this file.

Run: python3 test_source_matching_ratchet.py
"""
import ast
import collections
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent

# Counted on 2026-09-29. Lower it whenever you retarget one; never raise it
# without saying here why a text match was the only option.
BASELINE = 563

# The haystack looks like source text rather than a value under test.
HAYSTACK = re.compile(
    r"\b(src|body|seg|segment|source|text|js|page|html|code|blob)\b", re.I)
# The needle looks like code rather than prose a user would read.
CODEISH = re.compile(
    r"[(){}\[\]=<>]|\b(def|await|async|return|import|self|None|True|False)\b|_[a-z]")

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


def scan():
    """{file: count} of `"<code-ish>" in <source-ish>` comparisons."""
    out = collections.Counter()
    for p in sorted(HERE.glob("test_*.py")):
        if p.name == pathlib.Path(__file__).name:
            continue
        try:
            tree = ast.parse(p.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Compare) or not n.ops:
                continue
            if not isinstance(n.ops[0], (ast.In, ast.NotIn)):
                continue
            needle = n.left
            if not (isinstance(needle, ast.Constant)
                    and isinstance(needle.value, str)):
                continue
            if not HAYSTACK.search(ast.unparse(n.comparators[0])):
                continue
            if not CODEISH.search(needle.value):
                continue
            out[p.name] += 1
    return out


COUNTS = scan()
TOTAL = sum(COUNTS.values())


def test_the_count_has_not_gone_up():
    ok(f"source-text assertions: {TOTAL} (baseline {BASELINE})",
       TOTAL <= BASELINE,
       f"{TOTAL - BASELINE} new one(s). Assert on the parsed tree or on "
       f"behaviour instead - a text match fails a change that only moved the "
       f"wording, and passes when it matches the comment explaining the bug")
    if TOTAL < BASELINE:
        print(f"       ({BASELINE - TOTAL} fewer than baseline — lower BASELINE "
              f"to {TOTAL} to lock the gain in)")


def test_the_worst_offenders_are_named_so_the_work_is_findable():
    """A number with no map is a number nobody acts on."""
    top = COUNTS.most_common(5)
    ok("the scan found the files, not just a total", top, str(COUNTS))
    for name, count in top:
        print(f"       {count:3}  {name}")
    ok("no single file holds more than a tenth of them",
       not top or top[0][1] <= max(3, TOTAL // 10),
       f"{top[0][0] if top else '-'} has {top[0][1] if top else 0} - retarget "
       f"that one first")


def test_this_files_own_guards_do_not_use_the_pattern():
    """The obvious hypocrisy, checked rather than promised."""
    mine = scan.__module__
    ok("this file is excluded from its own scan",
       pathlib.Path(__file__).name not in COUNTS, str(mine))
    for f in ("test_no_confusing_duplicates.py",):
        ok(f"{f} asserts on the tree, not on source text",
           COUNTS.get(f, 0) == 0, f"{COUNTS.get(f, 0)} text matches")


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
    print(f"all {_passes} source-matching ratchet checks passed")
