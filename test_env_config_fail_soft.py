"""One mistyped variable must never be able to take the fleet offline.

WHAT HAPPENED, 2026-10-04
-------------------------
A recommendation was written as "GRID_MAKER_ONLY_WAIT_SECONDS: 240 -> 3600"
and pasted into Railway verbatim, arrow and all. That variable is read at
MODULE level::

    MAKER_ONLY_ORDER_WAIT_SECONDS = int(os.getenv("GRID_MAKER_ONLY_WAIT_SECONDS", "240"))

int() raised while crypto_grid_bot was being imported, so the module never
loaded. The blast radius of one typo:

    /grid-status, /grid-status/trade-history, /trading-profile -> 500
    experiment guard, coin deployment, startup fix, rotation task and
    claim reconciliation -> never started
    [rotation] [truth] [adopt] [idle-rot] passes -> failed every cycle
    [stops] could not read grid positions (ValueError) -
        placing without that protection this pass

That last line is the one that matters: the stop layer ran BLIND. A bad
tuning knob is a config error; it must not be allowed to become a safety
outage. And the whole cause existed only as a single WARNING in the
container log, so the dashboard showed a bare "module not available" and
the real reason took hours to find.

WHAT THIS GUARDS
----------------
1. A malformed numeric variable falls back to the declared default instead
   of raising, so the import always completes.
2. The fallback is LOUD, never silent. A quiet wrong number is worse than a
   loud outage: the owner sets 3600, gets 240, and nothing says so. Every
   fallback is recorded and surfaced by /module-health.
3. The defaults themselves did not move in the conversion. This is the
   important one - a refactor that quietly changed a risk threshold while
   claiming to be about parsing would be far worse than the bug it fixed.
"""
import ast
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

checks = []


def ok(label, cond, detail=""):
    checks.append((label if not detail else f"{label}  -- {detail}", bool(cond)))


# ---------------------------------------------------------------- the helper
import env_config as E

print("\n[1] the exact value that took the fleet down")
POISON = "240 → 3600"
os.environ["T_POISON"] = POISON
E.ENV_PARSE_FALLBACKS.clear()
got = E.env_int("T_POISON", 240)
ok("a value with an arrow in it does not raise", True)
ok("...it falls back to the declared default", got == 240, f"got {got}")
ok("...and the fallback is RECORDED, not swallowed", "T_POISON" in E.ENV_PARSE_FALLBACKS)
rec = E.ENV_PARSE_FALLBACKS.get("T_POISON", {})
ok("...the record names the bad value so it can be fixed", rec.get("raw") == POISON)
ok("...and says which default is actually running", rec.get("default") == 240)

print("\n[2] good values still work - this is not a bypass")
os.environ["T_GOOD"] = "3600"
ok("a clean integer is honoured", E.env_int("T_GOOD", 240) == 3600)
os.environ["T_PAD"] = "  3600  "
ok("surrounding whitespace is tolerated", E.env_int("T_PAD", 240) == 3600)
os.environ["T_FLOATY"] = "3600.0"
ok("3600.0 means 3600, not a crash", E.env_int("T_FLOATY", 240) == 3600)
os.environ["T_F"] = "0.25"
ok("floats parse as floats", E.env_float("T_F", 0.5) == 0.25)
ok("an unset variable is simply the default", E.env_int("T_NEVER_SET_XYZ", 99) == 99)
os.environ["T_EMPTY"] = "   "
ok("an empty value is 'unset', not a parse failure", E.env_float("T_EMPTY", 1.5) == 1.5)

print("\n[3] a silent fallback is its own hazard")
E.ENV_PARSE_FALLBACKS.clear()
os.environ["T_BAD2"] = "none"
E.env_float("T_BAD2", 0.01)
rep = E.env_fallback_report()
ok("the report counts it", rep["count"] == 1, f"count={rep['count']}")
ok("...names the variable", rep["fallbacks"][0]["variable"] == "T_BAD2")
ok("...and says plainly that the set value is NOT in effect",
   "NOT in effect" in rep["note"])
E.ENV_PARSE_FALLBACKS.clear()
ok("a clean environment reports clean", E.env_fallback_report()["count"] == 0)
for k in list(os.environ):
    if k.startswith("T_"):
        os.environ.pop(k, None)

print("\n[4] the import survives the poisoned value FOR REAL")
# Not a mock: import the actual module with the actual bad value set.
os.environ["GRID_MAKER_ONLY_WAIT_SECONDS"] = POISON
try:
    import crypto_grid_bot as _grid
    imported = True
    wait = _grid.MAKER_ONLY_ORDER_WAIT_SECONDS
except Exception as e:  # pragma: no cover - this is the regression
    imported, wait = False, f"import raised {type(e).__name__}: {e}"
os.environ.pop("GRID_MAKER_ONLY_WAIT_SECONDS", None)
ok("crypto_grid_bot imports with the poisoned variable set", imported, str(wait))
ok("...running on the default, not on garbage", wait == 240, f"got {wait}")

print("\n[5] NO DEFAULT MOVED in the conversion")
# Every converted declaration, checked against the value it had before.
# A parsing refactor that shifted a risk threshold would be worse than the
# bug it fixed, so these are pinned individually.
SRC = (REPO / "crypto_grid_bot.py").read_text(encoding="utf-8")
EXPECTED = {
    "GRID_DRAWDOWN_BREAKER_PCT": "0.25",
    "GRID_CASH_RESERVE_USD": "88.0",
    "GRID_PARKED_MIN_NET_PCT": "0.010",
    "GRID_STOP_LOSS_PCT": "0.08",
    "GRID_TARGET_NET_MARGIN_PCT": "0.002",
    "GRID_MAKER_ONLY_WAIT_SECONDS": "240",
    "GRID_MAKER_ORDER_WAIT_SECONDS": "45",
    "GRID_DUST_SLICE_USD": "1.00",
    "GRID_AUTO_DEPLOY_AMOUNT_USD": "70.0",
    "GRID_MIN_REQUIRED_ROI_PCT": "20.0",
    "GRID_FLEET_MIN_STEP_PCT": "0.030",
    "GRID_GATE_CLEARING_MAX_PCT": "0.06",
    "GRID_AUTO_ROTATE_MIN_USD": "10.0",
}
for var, want in EXPECTED.items():
    m = re.search(r'env_(?:int|float)\("' + re.escape(var) + r'", *([0-9][0-9_.*\s]*?)\)', SRC)
    found = m.group(1).strip() if m else None
    same = found is not None and abs(float(found) - float(want)) < 1e-12
    ok(f"{var} still defaults to {want}", same, f"found {found}")

print("\n[6] nothing module-level can raise on a bad value any more")
bare = [
    ln.strip() for ln in SRC.splitlines()
    if re.match(r'^[A-Z_0-9]+ *= *(int|float)\(os\.(getenv|environ)', ln)
]
ok("no module-level int()/float() of a raw env var survives in crypto_grid_bot",
   not bare, f"{len(bare)} left: {bare[:3]}")

print("\n[7] env_config itself can never be the import that fails")
mod = ast.parse((REPO / "env_config.py").read_text(encoding="utf-8"))
imported_names = set()
for node in ast.walk(mod):
    if isinstance(node, ast.Import):
        imported_names.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imported_names.add(node.module.split(".")[0])
ok("it imports only the standard library", imported_names <= {"logging", "os", "typing", "__future__"},
   str(sorted(imported_names)))
top_calls = [n for n in mod.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
ok("and runs no code at import time that could throw", not top_calls)

width = max(len(l) for l, _ in checks)
print()
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
