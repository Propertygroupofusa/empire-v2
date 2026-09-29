"""The exit-reason watcher must not repeat the three mistakes it exists for.

Each of these was made once on live data before the script existed:

  1. THE CAP DECIDED WHAT THE DATA SAID. trade-history served 50 of 132
     trades with nothing admitting it, so any distribution taken off it was
     a distribution of whatever happened to be recent.
  2. LEGACY ROWS DROWNED THE NEW ONES. At the first reading 131 of 132 rows
     predated the four-way split and carried the old binary label, where
     "profit_target" merely meant "not a stop". The naive count said
     "100% profit_target" and meant nothing.
  3. AN UNREADABLE FETCH IS NOT AN EMPTY BOOK.

Behavioural throughout: split() and analyse() are pure over plain dicts, so
the checks call them with real inputs.
"""
import importlib.util
import os
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


_spec = importlib.util.spec_from_file_location(
    "exit_reason_watch",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "scripts", "exit_reason_watch.py"))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

CUT = "2026-09-29T00:16:00"


def row(stamp, reason="profit_target"):
    return {"closed_at": stamp, "exit_reason": reason, "pnl": 1.0}


print("== the cutover split ==")

post, legacy = w.split([row("2026-09-29T00:40:00")], CUT)
ok("a row after the cutover is post-cutover", len(post) == 1 and not legacy)

post, legacy = w.split([row("2026-09-28T23:00:00")], CUT)
ok("a row before the cutover is legacy", len(legacy) == 1 and not post)

# STRICTLY AFTER. A row stamped exactly at the cutover cannot be shown to
# have been written by the new logic, and crediting it would inflate the
# only number that decides whether the answer means anything.
post, legacy = w.split([row(CUT)], CUT)
ok("a row exactly AT the cutover counts as legacy, not new",
   len(legacy) == 1 and not post,
   "it cannot be shown to postdate the change; crediting it inflates the "
   "readability count")

# A row with no timestamp cannot be placed. It must not default to "new".
post, legacy = w.split([{"exit_reason": "parked_sell"}], CUT)
ok("a row with NO closed_at counts as legacy, never as new",
   len(legacy) == 1 and not post,
   "defaulting an unplaceable row to the new side would let unlabelled "
   "history masquerade as evidence the new logic is working")


print("== the readability floor ==")

def payload(n_post, n_legacy, total=None, truncated=False, reason="profit_target",
            legacy_reason=None):
    rows = ([row(f"2026-09-29T01:{i:02d}:00", reason) for i in range(n_post)] +
            [row("2026-09-01T00:00:00", legacy_reason) for _ in range(n_legacy)])
    return {"recent_trades": rows,
            "total_trade_count": total if total is not None else len(rows),
            "recent_trades_returned": len(rows),
            "recent_trades_truncated": truncated,
            "recent_trades_omitted": 0}


r = w.analyse(payload(1, 131))
ok("one row under the new logic is NOT readable", r["readable"] is False,
   "this is the live case: 1 of 132, and reporting it as a distribution is "
   "the mistake the script exists to prevent")
ok("and the legacy rows are counted separately", r["legacy"] == 131)

r = w.analyse(payload(w.MIN_ROWS_TO_READ - 1, 10))
ok("one below the floor is still not readable", r["readable"] is False)

r = w.analyse(payload(w.MIN_ROWS_TO_READ, 10))
ok("at the floor it becomes readable", r["readable"] is True)

# A huge legacy pile must never make a small new sample look sufficient.
r = w.analyse(payload(2, 5000))
ok("legacy volume NEVER makes a small new sample readable",
   r["readable"] is False,
   "readability is a property of the post-cutover rows alone")


print("== unlabelled legacy is counted, not hidden ==")

r = w.analyse(payload(1, 10, legacy_reason=None))
ok("legacy rows with no exit_reason are counted",
   r["legacy_unlabelled"] == 10, f"got {r['legacy_unlabelled']}")

r = w.analyse(payload(1, 10, legacy_reason="profit_target"))
ok("legacy rows that DO carry a label are not counted as unlabelled",
   r["legacy_unlabelled"] == 0, f"got {r['legacy_unlabelled']}")

# THE CASE THAT SEPARATES "legacy only" FROM "all rows". Both fixtures above
# give the same answer either way, because no post-cutover row was null - so
# a mutant counting over every row survived them. A post-cutover null is a
# real shape: close_all wrote None until 519ca01, and any path that fails to
# set a reason produces one.
mixed = {"recent_trades": [
    {"closed_at": "2026-09-29T01:00:00", "exit_reason": None},     # post, null
    {"closed_at": "2026-09-29T01:01:00", "exit_reason": "stop_loss"},
    {"closed_at": "2026-09-01T00:00:00", "exit_reason": None},     # legacy, null
    {"closed_at": "2026-09-01T00:01:00", "exit_reason": None},     # legacy, null
], "total_trade_count": 4, "recent_trades_returned": 4,
    "recent_trades_truncated": False, "recent_trades_omitted": 0}
r = w.analyse(mixed)
ok("legacy_unlabelled counts ONLY the legacy nulls",
   r["legacy_unlabelled"] == 2,
   f"got {r['legacy_unlabelled']} - 3 would mean a post-cutover null is "
   f"being blamed on the legacy era, which hides a live path that failed "
   f"to record a reason")
ok("and a post-cutover null still shows up in the new counts",
   r["post_counts"].get("None") == 1,
   f"got {r['post_counts']} - a new row with no reason is a finding, not "
   f"something to fold into the legacy pile")

# The post-cutover counts must not include legacy rows at all.
r = w.analyse(payload(3, 99, reason="parked_sell", legacy_reason="profit_target"))
ok("post-cutover counts contain ONLY post-cutover rows",
   r["post_counts"] == {"parked_sell": 3},
   f"got {r['post_counts']} - a legacy row leaking in here is exactly the "
   f"contamination this split exists to stop")


print("== truncation is surfaced ==")

r = w.analyse(payload(30, 5, total=500, truncated=True))
ok("a truncated window is flagged", r["truncated"] is True,
   "a capped window is a distribution of whatever is recent")
r = w.analyse(payload(30, 5))
ok("an untruncated window is not flagged", r["truncated"] is False)


print("== an unreadable fetch is a gap, not a zero ==")

_orig = w.urllib.request.urlopen


class _Boom:
    def __call__(self, *a, **k):
        raise OSError("connection reset")


w.urllib.request.urlopen = _Boom()
payload_, err = w.fetch(1000)
w.urllib.request.urlopen = _orig
ok("a failed fetch returns None, never an empty book",
   payload_ is None and err, f"got payload={payload_!r} err={err!r}")
ok("and the reason travels with it", isinstance(err, str) and err,
   "a bare None would leave the caller unable to say WHY")

# The three outcomes must be distinguishable by exit code, or a caller
# cannot tell "nothing new" from "could not look".
import inspect
src = inspect.getsource(w.main)
ok("the unreadable path returns its own exit code",
   "return 1" in src and "UNREADABLE" in src)
ok("the reportable path returns its own exit code",
   "return 2" in src and "REPORTABLE" in src)
ok("the quiet path returns zero", "return 0" in src)
ok("a missing baseline counts as reportable",
   "no previous run" in src,
   "a first run with nothing to compare against must not be reported as "
   "'nothing changed'")


print("== labels outside the four-way set are surfaced ==")

r = w.analyse(payload(25, 0, reason="something_else"))
unknown = sorted(set(r["labels_seen"]) - set(w.KNOWN))
ok("a label the new logic cannot emit is detectable",
   unknown == ["something_else"],
   "an unexpected label means either a path nobody accounted for or a "
   "legacy row on the wrong side of the cutover")
ok("and the four known labels are the documented set",
   set(w.KNOWN) == {"profit_target", "parked_sell", "stop_loss", "close_all"},
   f"got {w.KNOWN}")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all exit-reason watcher checks passed")
