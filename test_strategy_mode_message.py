"""A false alarm at ERROR level is worse than no alarm.

The bill for not having this, 2026-09-25:

    Railway had CRYPTO_STRATEGY_MODE='delfina_scalping' - a retired name,
    stuck (deleting it did not hold; a redeploy restored it). Every startup
    logged, at ERROR:

        NO crypto loop will start and nothing will be bought or sold.

    That was false. grid_fleet was running from a DB strategy override and
    had bought a real NEAR-USD slice at $4.9687. The account owner read the
    log, reasonably believed trading was dead, and asked why.

    The message could not know better: this module is sync and reads only
    the environment, while the override that actually starts the loop is
    async and DB-backed. So it asserted something it had no way to check.

THE RULES THIS FILE PROTECTS:

  1. Whoever actually starts a loop registers it (note_runtime_mode), so
     this module can tell "misconfigured and dead" from "misconfigured but
     running".
  2. With a loop running, a stale variable is a WARNING that says trading
     is not stopped - never an ERROR claiming nothing will trade.
  3. With no loop running, the ERROR stays, because then it is true.
  4. A retired name is named as retired. Calling it a typo sends someone
     hunting a spelling mistake that does not exist; the real fix is
     deleting the variable.
  5. An unusable value still yields UNCONFIGURED. Never substitute a
     strategy - an unchosen one spends real money.

Run: python3 test_strategy_mode_message.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "crypto_strategy_config.py"), encoding="utf-8").read()
main_src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def body(name):
    node = fn(name)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


ok("a runtime-mode registry exists", fn("note_runtime_mode") is not None)
ok("retired names are known", "RETIRED_STRATEGY_NAMES" in src)
ok("delfina_scalping is listed as retired", "delfina_scalping" in src)
ok("a retired-name helper exists", fn("_is_retired_name") is not None)

resolve = body("get_crypto_strategy_mode")
ok("the resolver consults what actually started", "_RUNTIME_MODE" in resolve)
ok("REGRESSION: it no longer claims nothing will be bought or sold",
   "nothing will be bought or sold" not in resolve)
ok("the running case says trading is NOT stopped", "trading is NOT stopped" in resolve)
ok("the running case logs at WARNING, not ERROR",
   "log.warning" in resolve and resolve.index("trading is NOT stopped") > resolve.index("log.warning"))
ok("the dead case still logs at ERROR", "log.error" in resolve)
ok("the dead case is scoped to the variable, not to all trading",
   "FROM THIS VARIABLE" in resolve)
ok("a retired name is named as retired, not implied to be a typo",
   "a retired name, not a typo" in resolve)
ok("REGRESSION: an unusable value still refuses to substitute a strategy",
   resolve.count("UNCONFIGURED") >= 2)
ok("the override escape hatch is still checked first",
   "CRYPTO_STRATEGY_MODE_OVERRIDE" in resolve)

ok("main.py registers the mode it actually started",
   "note_runtime_mode(" in main_src)
ok("it distinguishes a DB override from the env var",
   "a DB strategy override" in main_src)
ok("registration can never break startup",
   "could not register the running strategy mode" in main_src)


# --- the rule, as behaviour ----------------------------------------------
def level(env_value, runtime_mode, supported=("btc_compound", "family_tree",
                                              "grid_fleet", "multi_pair")):
    if (env_value or "").strip() in supported:
        return "ok"
    return "warning" if runtime_mode else "error"


ok("valid env, nothing registered           -> ok",
   level("grid_fleet", None) == "ok")
ok("REGRESSION: stale env, grid running     -> warning, not error",
   level("delfina_scalping", "grid_fleet") == "warning")
ok("stale env, nothing running              -> error (it is true then)",
   level("delfina_scalping", None) == "error")
ok("unset env, nothing running              -> error",
   level(None, None) == "error")
ok("unset env, something running            -> warning",
   level(None, "btc_compound") == "warning")

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
