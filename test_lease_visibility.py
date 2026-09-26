"""A trading loop that has decided not to work must say so where someone reads.

2026-09-26: the grid heartbeat read "entered" for 40+ minutes with no gate
events and no trades. The loop was alive and being refused the lease every
cycle. The only code that knew logged at DEBUG, and the lease row was not
exposed anywhere, so the stall was undiagnosable from outside the server.
"""
import ast, sys
_p = _f = 0
def ok(l, c, d=""):
    global _p, _f
    if c: _p += 1; print(f"  PASS  {l}")
    else: _f += 1; print(f"  FAIL  {l}" + (f"  -- {d}" if d else ""))

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
def fn(n):
    for x in ast.walk(TREE):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == n: return x

print("\nthe refusal is audible")
cyc = ast.get_source_segment(SRC, fn("run_grid_branches_cycle")) or ""
ok("the lease refusal no longer logs at debug", "log.debug(f\"[GRID] {why}\")" not in cyc)
ok("it logs at warning", "log.warning" in cyc and "not cycling" in cyc)
ok("a refused cycle stamps its OWN stage, not plain 'entered'",
   '_record_grid_heartbeat("lease_refused")' in cyc,
   "otherwise 'alive but locked out' is indistinguishable from 'working'")
import re as _re
_m = _re.search(r"_HEARTBEAT_STAGES\s*=\s*\{.*?\}", SRC, _re.S)
ok("lease_refused is a known heartbeat stage",
   _m is not None and '"lease_refused"' in _m.group(0),
   "an unknown stage is not comparable against the others")
ok("the known stages still include cycled and entered",
   _m is not None and '"cycled"' in _m.group(0) and '"entered"' in _m.group(0))

print("\nthe lease is inspectable without server logs")
ls = fn("read_grid_lease_state")
ok("read_grid_lease_state exists", ls is not None)
src = ast.get_source_segment(SRC, ls) or ""
for field in ("held_by_this_process", "last_seen_age_seconds",
              "takeover_possible", "timestamp_looks_corrupt"):
    ok(f"it reports {field}", field in src)
ok("it is READ-ONLY - never claims or renews the lease",
   "db.commit" not in src and "db.add" not in src,
   "a diagnostic that mutates the thing it measures is a second bug")
ok("a future timestamp is flagged as corrupt, not read as fresh",
   "age < -5" in src,
   "a future last_seen makes `age > STALE` permanently false and deadlocks "
   "every process out of the loop")
ok("the lease is exposed on grid-status", '"loop_lease"' in SRC)
print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
