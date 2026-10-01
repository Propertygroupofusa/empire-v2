#!/usr/bin/env python3
"""A failed ledger read must be a GAP, never "0 trades, $0.00".

WHAT THIS CAUGHT. The growth panel's own integrity check reported:

    "This series is not trustworthy. Realised profit and the trade count
     only accumulate, and 141 reading(s) show one falling (trades from
     156 to 0). That is a rewritten ledger or a failed read stored as a
     number - not a loss."

It was right, and the cause was one line in growth_ledger_worker:

    trades = []                       # <- initialised BEFORE the try
    try:
        trades = await _read_ledger(session_factory)
    except Exception as exc:
        notes.append(...)             # <- trades is still []

capital_kpis.compute([]) returns trades=0 and net_usd=0.0 - a completely
believable reading - and that got written into the permanent series. 141
times. Those are the vertical spikes to zero on every chart of the panel,
and they drag every "change over window" figure computed across them.

It is the same shape as the auto_trim_worker bug: a failure collapsing
into a falsy value indistinguishable from real data. A gap is not a zero.
"""
import ast, asyncio, re, sys
import growth_ledger_worker as W
import capital_kpis

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

SRC = open("growth_ledger_worker.py").read()
# Comments stripped: the comment describing the old bug must not satisfy
# a test looking for the old bug's absence.
CODE = re.sub(r"^\s*#.*$", "", SRC, flags=re.M)

print("\n[1] the premise: an empty list computes to a believable zero")
k = capital_kpis.compute([], allocated_usd=7767.0, free_cash_usd=1661.0,
                         account_total_usd=11397.0)
ok("compute([]) gives trades=0", k.get("trades") == 0, repr(k.get("trades")))
ok("...and net_usd=0.0", k.get("net_usd") == 0.0, repr(k.get("net_usd")))
ok("so an empty list is INDISTINGUISHABLE from a real flat ledger",
   k.get("trades") == 0 and k.get("net_usd") == 0.0)

print("\n[2] the old shape is gone from the code")
ok("no bare `trades = []` before the read",
   not re.search(r"^\s*trades\s*=\s*\[\]\s*$", CODE, re.M),
   "an empty-list initialiser still precedes the try")
ok("the failure path sets None", "trades = None" in CODE)
ok("and the caller can tell the two apart",
   "ledger_unreadable = trades is None" in CODE)

print("\n[3] every ledger-derived field is blanked, not zeroed")
ok("LEDGER_DERIVED exists", hasattr(W, "LEDGER_DERIVED"))
for f in ("trades", "net_usd", "net_edge_per_trade_usd",
          "profit_factor", "win_rate_pct", "capital_velocity"):
    ok(f"  {f} is listed", f in W.LEDGER_DERIVED, str(W.LEDGER_DERIVED))
# Each one must actually compute from an empty ledger, or listing it is
# cargo-cult rather than a fix.
for f in ("trades", "net_usd"):
    ok(f"  {f} really does compute to a number from []", k.get(f) is not None)

print("\n[4] the blanking happens on the ROW, after from_kpis")
# from_kpis copies FIELDS out of the kpi dict, so blanking only the kpi
# dict is not enough if anything repopulates. Both are done.
ok("the row is blanked too",
   re.search(r"if ledger_unreadable:\s*\n\s*for f in LEDGER_DERIVED:\s*\n\s*row\[f\] = None",
             CODE) is not None, "row blanking not found")
ok("the bottleneck is not named from blanked inputs",
   re.search(r"if ledger_unreadable:\s*\n\s*cause = None", CODE) is not None)

print("\n[5] it retries before giving up")
ok("three attempts", "range(3)" in CODE)
ok("with backoff", "2 ** _attempt" in CODE)
ok("and only notes the failure once, on the last attempt",
   "_attempt == 2" in CODE)

print("\n[6] _read_ledger's contract is stated")
doc = (W._read_ledger.__doc__ or "")
ok("the docstring distinguishes None from []",
   "None" in doc and "[]" in doc, doc[:120])

print("\n[7] a genuinely empty ledger still records a real zero")
# The fix must not blank a legitimate 0 - a new account with no trades
# has trades=0, and that is a fact worth plotting.
tree = ast.parse(SRC)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_once")
body = ast.get_source_segment(SRC, fn)
ok("blanking is gated on `ledger_unreadable`, not on falsiness",
   "if ledger_unreadable:" in body and "if not trades:" not in body,
   "an empty-but-real ledger would be blanked too")
ok("compute still receives a list, never None",
   "capital_kpis.compute(trades or []" in body)

print("\n[8] the integrity check that caught this still works")
import growth_ledger as G
# Real datetimes: rows() drops readings whose captured_at is not one,
# so an int here silently produced an EMPTY series and a passing
# "nothing was flagged" - a test that proved nothing while looking green.
from datetime import datetime, timedelta
_t0 = datetime(2026, 10, 1, 4, 0, 0)
snaps = [{"captured_at": _t0, "trades": 156, "net_usd": 90.0},
         {"captured_at": _t0 + timedelta(minutes=15), "trades": 0, "net_usd": 0.0},
         {"captured_at": _t0 + timedelta(minutes=30), "trades": 163, "net_usd": 92.89}]
ok("the sample series survived rows() - otherwise [8] proves nothing",
   len(G.rows(snaps)) == 3, f"rows() kept {len(G.rows(snaps))} of 3")
rep = G.integrity(snaps)
ok("a 156 -> 0 drop is still flagged", len(rep["monotonic_breaks"]) > 0,
   str(rep["monotonic_breaks"]))
gapped = [dict(s, trades=None, net_usd=None) if s["trades"] == 0 else s
          for s in snaps]
rep2 = G.integrity(gapped)
ok("...and the same series with a GAP instead is NOT flagged",
   len(rep2["monotonic_breaks"]) == 0, str(rep2["monotonic_breaks"]))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
