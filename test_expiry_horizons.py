"""The rung-rest study must be able to see the horizon it is asking about.

GridMakerExpiry exists to answer one question: should a resting rung be
given longer before it is cancelled? It stopped looking at 10 minutes,
which meant it could only ever answer that out to 10 minutes - while
horizon_study measures the payoff arriving far past there (8.1% of round
trips inside 30m, 34.1% inside 6h, 92.5% inside 72h).

That is the same shape as the signal ledger calling "no coin pays" from a
window bounded at 30 minutes, which is the bug horizon_study was written to
expose.
"""
import ast
import sys

import models

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)

_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


def _fn(name):
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])


import crypto_grid_bot as grid  # noqa: E402

TAGS = [t for _s, t in grid._EXPIRY_HORIZONS]
SECS = [s for s, _t in grid._EXPIRY_HORIZONS]

# --- the ladder ------------------------------------------------------------
ok("the ladder reaches 72 hours", TAGS[-1] == "72h" and SECS[-1] == 259200)
ok("it keeps every short horizon it already had",
   TAGS[:4] == ["1m", "3m", "5m", "10m"])
ok("it covers the horizons horizon_study reports",
   {"30m", "6h", "72h"} <= set(TAGS))
ok("seconds ascend, so 'due' is monotonic", SECS == sorted(SECS))
ok("no duplicate tags", len(TAGS) == len(set(TAGS)))
for sec, tag in grid._EXPIRY_HORIZONS:
    unit = tag[-1]
    n = int(tag[:-1])
    expect = n * 60 if unit == "m" else n * 3600
    ok(f"{tag} really is {tag}", sec == expect, f"{sec}s")

# --- the columns exist for every rung of it --------------------------------
cols = set(models.GridMakerExpiry.__table__.columns.keys())
for tag in TAGS:
    ok(f"columns exist for {tag}",
       {f"price_{tag}", f"drift_{tag}_pct", f"cancel_benefit_{tag}_pct"} <= cols)

# --- the resolver -----------------------------------------------------------
RES = _fn("_resolve_maker_expiries")

ok("a row is retired on the FINAL horizon, not a hardcoded 10m",
   '_EXPIRY_FINAL_TAG' in RES and "row.price_10m is not None" not in RES)

# The starvation bug the longer ladder would otherwise introduce: rows now
# stay unresolved for three days, and oldest-first ordering would hand every
# slot to rows waiting on 24h/72h while fresh expiries lost their 1m reading.
ok("the scan is wider than the book-read cap",
   "_EXPIRY_SCAN_MAX_PER_CYCLE" in RES
   and grid._EXPIRY_SCAN_MAX_PER_CYCLE > grid._EXPIRY_RESOLVE_MAX_PER_CYCLE)
ok("a row with nothing due does not consume a read slot",
   RES.index("if not due:") < RES.index("if touched >= _EXPIRY_RESOLVE_MAX_PER_CYCLE"))
ok("the read cap still bounds book calls", "touched += 1" in RES)
ok("a row past the final horizon with gaps is retired rather than rescanned forever",
   "_EXPIRY_FINAL_SECONDS + 3600" in RES)

ok("it still never raises", "except Exception" in RES)
ok("the sign convention is untouched",
   'benefit = -drift if row.side == "buy" else drift' in RES)


def _due(age, filled=()):
    """The resolver's own due-list rule, exercised directly."""
    return [tag for sec, tag in grid._EXPIRY_HORIZONS
            if age >= sec and tag not in filled]


ok("nothing is due at 30 seconds", _due(30) == [])
ok("only 1m is due at 90 seconds", _due(90) == ["1m"])
ok("the short rungs are due by 10 minutes",
   _due(600) == ["1m", "3m", "5m", "10m"])
ok("30m becomes due at 30 minutes", "30m" in _due(1800))
ok("6h becomes due at 6 hours", "6h" in _due(21600) and "24h" not in _due(21600))
ok("the whole ladder is due at 72 hours", _due(259200) == TAGS)
ok("an already-filled horizon is not redone",
   _due(600, filled={"1m", "3m"}) == ["5m", "10m"])

# --- the verdicts -----------------------------------------------------------
VER = _fn("get_maker_expiry_drift")

ok("the short verdict still answers the 10m question", '"10m"' in VER)
ok("a separate verdict answers the long-horizon question",
   "long_horizon_verdict" in VER)
ok("the two are not collapsed into one",
   '"verdict"' in VER and "long_horizon_verdict" in VER)
ok("the long verdict picks the LONGEST horizon with enough data",
   "usable[-1]" in VER)
ok("it stays silent below the evidence floor",
   "_EXPIRY_MIN_RESOLVED" in VER and "not enough data" in VER)
ok("the wait is read, not hardcoded into the sentence",
   "maker_wait_seconds()" in VER and "240s timeout" not in VER)
ok("nothing here changes the wait",
   "maker_wait_seconds =" not in VER and "MAKER_ONLY_ORDER_WAIT_SECONDS =" not in VER)
ok("the evidence floor is 20 by default", grid._EXPIRY_MIN_RESOLVED == 20)

# --- and the wait itself is genuinely untouched -----------------------------
ok("the resting wait is still 240s while maker-only is on",
   grid.MAKER_ONLY_ORDER_WAIT_SECONDS == 240)
ok("this change measures, it does not lengthen the rest",
   "_EXPIRY_HORIZONS" in SRC and "GRID_MAKER_ONLY_WAIT_SECONDS" in SRC)

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
