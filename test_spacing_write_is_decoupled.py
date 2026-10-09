#!/usr/bin/env python3
"""The step a branch TRADES on, and the step that gets SAVED, are not the
same decision any more.

WHAT WAS WRONG. `branch.grid_pct = new_grid_pct` sat INSIDE the database
write guard:

    if new_grid_pct is not None and abs(new_grid_pct - branch.grid_pct) > 1e-9:
        ...db write...
        branch.grid_pct = new_grid_pct      # <- inside
    if new_grid_pct is not None:
        grid_pct = branch.grid_pct          # <- what the branch trades on

So the guard decided BOTH what got persisted and what the branch traded on
that cycle. That is why "skip the immaterial write" could not be done
safely: every proposal to raise the threshold was, without anyone meaning
it, a proposal to trade on a stale step. Two outside plans proposed exactly
that - one as a flat 0.01% floor, one as a tick-size check - and both were
described as disk optimisations.

MEASURED, two readings 55 minutes apart on 2026-10-09: 10 of 21 branches
drift continuously; 11 sit pinned at exactly 3.0000% (the fleet floor) and
never write. Drift ran +0.0008 to +0.0139 percentage points, i.e. roughly
0.0001-0.0003 points per cycle - far above the old 1e-9 guard, so each of
those 10 wrote every cycle. About 17,280 database writes a day to persist
noise.

WHAT IS ASSERTED HERE:

  1. The assignment to branch.grid_pct is NOT inside the write guard, so
     the branch always takes the freshly computed step. Checked on the
     PARSED TREE, so it holds whatever the wording becomes.
  2. The commit IS inside a guard on GRID_PCT_WRITE_MIN_DELTA.
  3. The log line reads a CAPTURED previous value, not the already-updated
     attribute - otherwise it prints "2.4138% -> 2.4138%" and reports a
     change as a non-change, which is the defect class this file's
     neighbours keep being written for.
  4. The threshold is env-tunable and defaults to 1e-5.
  5. The arithmetic: against the real measured drift, the threshold cuts
     writes without ever suppressing a real spacing move.

Run: python3 test_spacing_write_is_decoupled.py
"""
import ast
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import crypto_grid_bot

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


with open("crypto_grid_bot.py", encoding="utf-8") as fh:
    TREE = ast.parse(fh.read())


def _cycle_fn():
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_grid_branch_cycle":
            return node
    return None


def _assignments_to_branch_grid_pct(node):
    """Every `branch.grid_pct = ...` statement inside node, with its depth
    relative to the guard that mentions GRID_PCT_WRITE_MIN_DELTA."""
    out = []
    for st in ast.walk(node):
        if not isinstance(st, ast.Assign):
            continue
        for t in st.targets:
            if (isinstance(t, ast.Attribute) and t.attr == "grid_pct"
                    and isinstance(t.value, ast.Name) and t.value.id == "branch"):
                out.append(st)
    return out


def _guards_mentioning(node, name):
    found = []
    for st in ast.walk(node):
        if isinstance(st, ast.If):
            for n in ast.walk(st.test):
                if isinstance(n, ast.Name) and n.id == name:
                    found.append(st)
    return found


def test_the_function_is_still_where_we_think_it_is():
    fn = _cycle_fn()
    ok("run_grid_branch_cycle exists", fn is not None)
    ok("it is the async cycle function",
       fn is not None and isinstance(fn, ast.AsyncFunctionDef))


def test_the_trading_value_is_set_outside_the_write_guard():
    fn = _cycle_fn()
    guards = _guards_mentioning(fn, "GRID_PCT_WRITE_MIN_DELTA")
    ok("exactly one guard reads the write threshold", len(guards) == 1,
       f"{len(guards)} guard(s)")
    if len(guards) != 1:
        return
    guard = guards[0]
    inside = {id(a) for a in _assignments_to_branch_grid_pct(guard)}
    all_asgn = _assignments_to_branch_grid_pct(fn)
    ok("the cycle assigns branch.grid_pct at least once", len(all_asgn) >= 1,
       str(len(all_asgn)))
    ok("NO assignment to branch.grid_pct sits inside the write guard",
       not inside,
       f"{len(inside)} assignment(s) are inside it - the trading value "
       f"would follow the disk decision again")


def test_the_commit_is_inside_the_write_guard():
    fn = _cycle_fn()
    guards = _guards_mentioning(fn, "GRID_PCT_WRITE_MIN_DELTA")
    if len(guards) != 1:
        ok("a single write guard to inspect", False)
        return
    commits = [n for n in ast.walk(guards[0])
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "commit"]
    ok("the database commit IS gated by the threshold", len(commits) >= 1,
       f"{len(commits)} commit call(s) inside the guard")


def test_the_log_line_cannot_print_a_change_as_a_non_change():
    fn = _cycle_fn()
    guards = _guards_mentioning(fn, "GRID_PCT_WRITE_MIN_DELTA")
    if len(guards) != 1:
        ok("a single write guard to inspect", False)
        return
    # The spacing log line inside the guard must reference a captured
    # previous value, never branch.grid_pct (already updated by then).
    src = ast.unparse(guards[0])
    line = [l for l in src.split("\n") if "dynamic spacing" in l]
    ok("the spacing line is inside the guard", bool(line), src[:160])
    if not line:
        return
    joined = " ".join(line)
    ok("it prints the CAPTURED previous value",
       "_prev_grid_pct" in joined, joined[:200])
    ok("it does NOT print branch.grid_pct, which is already the new value",
       "branch.grid_pct" not in joined, joined[:200])


def test_the_threshold_is_tunable_and_defaults_as_documented():
    v = crypto_grid_bot.GRID_PCT_WRITE_MIN_DELTA
    ok("the threshold exists", v is not None)
    ok("defaults to 1e-5 (0.001 percentage points)", abs(v - 1e-5) < 1e-12,
       repr(v))
    ok("it is strictly above the old 1e-9 guard, or it saves nothing",
       v > 1e-9, repr(v))
    src = ast.unparse(TREE)
    ok("it is read from the environment, not hardcoded",
       'env_float(\'GRID_PCT_WRITE_MIN_DELTA\'' in src
       or 'env_float("GRID_PCT_WRITE_MIN_DELTA"' in src)


def test_the_arithmetic_against_the_real_measured_drift():
    T = crypto_grid_bot.GRID_PCT_WRITE_MIN_DELTA
    # The 10 branches that actually drifted, in POINTS over 55 minutes,
    # measured live 2026-10-09.
    drift_points_55min = {"BCH": 0.0081, "BTC": 0.0024, "ETH": 0.0079,
                          "LINK": 0.0037, "LTC": 0.0088, "PRIME": 0.0008,
                          "SHIB": 0.0139, "SOL": 0.0081, "XLM": 0.0060,
                          "XRP": 0.0041}
    cycles = 55 * 60 / 50.0          # ~50s per branch cycle
    suppressed = 0
    for name, pts in drift_points_55min.items():
        per_cycle = (pts / 100.0) / cycles     # points -> fraction
        if per_cycle < T:
            suppressed += 1
    ok("every one of the 10 drifting branches is below the threshold "
       "per cycle, so none of them writes every cycle any more",
       suppressed == 10, f"{suppressed} of 10")

    # And a REAL spacing move must still persist immediately. The live
    # fleet's own steps sit between 1.54% and 3.00%; the smallest change
    # anyone would call a change is a hundredth of a point.
    for pts in (0.01, 0.05, 0.10, 0.50):
        ok(f"a real move of {pts:.2f} points still writes at once",
           (pts / 100.0) >= T, f"{pts/100.0} vs {T}")

    # A branch pinned at the fleet floor writes nothing, before or after.
    ok("a pinned branch (zero drift) never writes", 0.0 < T)


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
