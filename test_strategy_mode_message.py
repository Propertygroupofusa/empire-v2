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
#
# This block used to re-implement the rule:
#
#     return "warning" if runtime_mode else "error"
#
# and so it shared the bug instead of catching it. The real
# note_runtime_mode() accepted ANY value, and main.py registers whatever
# lifespan() resolved - UNCONFIGURED, when the variable is stale and there is
# no DB override. 'unconfigured' is truthy, so the model above scored that as
# "warning" and called it correct, while the live web service logged
#
#     but 'unconfigured' is running from CRYPTO_STRATEGY_MODE, so trading is
#     NOT stopped
#
# with nothing running at all. A model of the code cannot fail where the code
# fails. So these now drive the actual module and read the actual log record.
import importlib
import logging

sys.path.insert(0, HERE)
import crypto_strategy_config as cfg


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def observe(env_value, register=None):
    """Resolve for real and return (level, message, resolved_mode)."""
    before = os.environ.get("CRYPTO_STRATEGY_MODE")
    cap = _Capture()
    try:
        if env_value is None:
            os.environ.pop("CRYPTO_STRATEGY_MODE", None)
        else:
            os.environ["CRYPTO_STRATEGY_MODE"] = env_value
        importlib.reload(cfg)          # clears _RUNTIME_MODE too
        if register is not None:
            cfg.note_runtime_mode(register, "a DB strategy override")
        cfg.log.addHandler(cap)
        cfg.log.setLevel(logging.DEBUG)
        resolved = cfg.get_crypto_strategy_mode()
    finally:
        cfg.log.removeHandler(cap)
        if before is None:
            os.environ.pop("CRYPTO_STRATEGY_MODE", None)
        else:
            os.environ["CRYPTO_STRATEGY_MODE"] = before
    loud = [r for r in cap.records if r.levelno >= logging.WARNING]
    if not loud:
        return "ok", "", resolved
    r = loud[-1]
    return r.levelname.lower(), r.getMessage(), resolved


lvl, msg, mode = observe("grid_fleet")
ok("valid env, nothing registered           -> no complaint", lvl == "ok")
ok("  and it resolves to the strategy asked for", mode == "grid_fleet")

lvl, msg, mode = observe("delfina_scalping", register="grid_fleet")
ok("stale env, grid REALLY running          -> warning", lvl == "warning")
ok("  and it says trading is not stopped", "NOT stopped" in msg)
ok("  and it names what is actually running", "grid_fleet" in msg)

# THE REGRESSION. main.py passes the mode it resolved, which here is the
# UNCONFIGURED sentinel - a truthy string that is not a strategy.
lvl, msg, mode = observe("delfina_scalping", register=cfg.UNCONFIGURED)
ok("REGRESSION: registering UNCONFIGURED is refused, not believed",
   "unconfigured" not in msg)
ok("  so it never reports trading as running when nothing is",
   "NOT stopped" not in msg)
ok("  and note_runtime_mode says so in its return value",
   cfg.note_runtime_mode(cfg.UNCONFIGURED, "x") is False
   and cfg.note_runtime_mode("grid_fleet", "x") is True)

lvl, msg, mode = observe("delfina_scalping")
ok("retired name, nothing running           -> warning, named as retired",
   lvl == "warning" and "RETIRED" in msg)
ok("  it says there is no code behind the name, so no one hunts for a typo",
   "no code behind that name" in msg and "not a typo" in msg)
ok("  and the fix is deleting the variable, not choosing a replacement",
   "DELETE the stale variable" in msg)
ok("  it still refuses to substitute anything", mode == cfg.UNCONFIGURED)

lvl, msg, mode = observe("grid_flee")
ok("a real TYPO, nothing running            -> error (someone's bot is dead)",
   lvl == "error")
ok("  and the accepted values are listed, because a typo has a fix",
   "grid_fleet" in msg and "Accepted values" in msg)

# ABSENT is not WRONG. The fix printed inside the old ERROR was "on the web
# service leave CRYPTO_STRATEGY_MODE UNSET", and unsetting it produced
# "CRYPTO_STRATEGY_MODE=None is not a known strategy" - obeying the advice
# re-raised the alarm. Deleting the stale variable is the whole remedy, so it
# has to actually end the noise.
lvl, msg, mode = observe(None)
ok("REGRESSION: unset env is not an ERROR - it is the fix being applied",
   lvl == "ok")
ok("  and it still starts nothing", mode == cfg.UNCONFIGURED)
lvl, msg, mode = observe(None, register="btc_compound")
ok("unset env, something running            -> still quiet", lvl == "ok")

# ...but the one process where absence IS fatal must still shout, or that
# step-down would hide a dead fleet.
runner = open(os.path.join(HERE, "bot_runner.py"), encoding="utf-8").read()
_after = runner.split("if strategy_mode == UNCONFIGURED:", 1)[1][:900]
ok("bot_runner still logs its OWN error when the mode is unusable",
   "log.error(" in _after)
ok("  and it says no loop runs on EITHER service, so nothing is hidden",
   "loop is running anywhere" in _after)
ok("  and it refuses to start the fleet", "return" in _after)

# The advice inside those messages must not start a second strategy on the
# balance grid_fleet is trading.
for label, (l, m, _) in (("retired", observe("delfina_scalping")),
                         ("typo", observe("grid_flee"))):
    ok(f"the {label} message never tells a second service to set family_tree",
       "family_tree on the web" not in m)
    # It used to say "leave CRYPTO_STRATEGY_MODE UNSET on the web service".
    # Measured on the live deployment, the web service is what holds the grid
    # loop lease - so unsetting it leaves the fleet standing on the DB
    # override alone. The advice now names the strategy instead of a service.
    ok(f"the {label} message names grid_fleet as the value to set",
       "CRYPTO_STRATEGY_MODE=grid_fleet" in m)
    ok(f"the {label} message says why two grid_fleet processes are safe",
       "lease" in m and "standby" in m)
    ok(f"the {label} message still warns off a DIFFERENT mode beside the fleet",
       "same Coinbase balance" in m)

importlib.reload(cfg)   # leave the module as we found it

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
