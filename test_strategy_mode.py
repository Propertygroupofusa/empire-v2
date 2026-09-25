"""An unusable CRYPTO_STRATEGY_MODE must start NOTHING, never a substitute.

The bill for the old behaviour, 2026-09-25:

    CRYPTO_STRATEGY_MODE='delfina_scalping' - one typo - silently resolved
    to 'btc_compound' on the web service, because the fallback existed "so
    the account is not left idle". btc_compound is a single-position
    strategy. It converted essentially the whole Coinbase balance into one
    BTC position (0.00684381 BTC, $576.08). The grid fleet - the strategy
    the operator had actually configured and funded - was left with $0.29
    and could not open a slice for days.

The old docstring argued a wrong-but-announced strategy is "recoverable in
the seconds it takes to read one line". It was not: the WARNING went unread
for days, and the substitute did not merely fail to trade, it SPENT capital
allocated to another strategy on a position nobody chose.

An idle account is visibly idle and loses nothing. A silently substituted
capital-deploying strategy is neither. These checks keep it that way.

Run: python3 test_strategy_mode.py
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import crypto_strategy_config as cfg

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def mode_for(value):
    """Resolve the mode with CRYPTO_STRATEGY_MODE set to `value` (None = unset)."""
    before = os.environ.get("CRYPTO_STRATEGY_MODE")
    try:
        if value is None:
            os.environ.pop("CRYPTO_STRATEGY_MODE", None)
        else:
            os.environ["CRYPTO_STRATEGY_MODE"] = value
        importlib.reload(cfg)
        return cfg.get_crypto_strategy_mode()
    finally:
        if before is None:
            os.environ.pop("CRYPTO_STRATEGY_MODE", None)
        else:
            os.environ["CRYPTO_STRATEGY_MODE"] = before
        importlib.reload(cfg)


# --- the exact value that caused the incident ----------------------------
ok("'delfina_scalping' does NOT resolve to a tradeable strategy",
   mode_for("delfina_scalping") == cfg.UNCONFIGURED)
ok("and specifically never to btc_compound",
   mode_for("delfina_scalping") != "btc_compound")
ok("the sentinel is not a supported strategy, so no dispatch branch matches",
   cfg.UNCONFIGURED not in cfg.SUPPORTED_CRYPTO_STRATEGIES)

# --- every other unusable shape --------------------------------------------
for label, value in [("unset", None), ("empty", ""), ("whitespace", "   "),
                     ("a typo", "grid_flee"), ("wrong case", "GRID_FLEET"),
                     ("a sentence", "run the grid please")]:
    ok(f"{label} resolves to UNCONFIGURED", mode_for(value) == cfg.UNCONFIGURED)

# --- every real strategy still resolves ------------------------------------
for name in sorted(cfg.SUPPORTED_CRYPTO_STRATEGIES):
    ok(f"{name!r} still resolves to itself", mode_for(name) == name)

# --- quoting and whitespace, a previously observed Railway failure ---------
ok('a value pasted with double quotes still works', mode_for('"grid_fleet"') == "grid_fleet")
ok("with single quotes too", mode_for("'family_tree'") == "family_tree")
ok("with surrounding whitespace", mode_for("  grid_fleet  ") == "grid_fleet")
ok("and quotes plus whitespace together", mode_for('  "btc_compound" ') == "btc_compound")

# --- the grid-fleet helper -------------------------------------------------
ok("is_grid_fleet_mode is true only for grid_fleet",
   mode_for("grid_fleet") == "grid_fleet")
os.environ["CRYPTO_STRATEGY_MODE"] = "delfina_scalping"
importlib.reload(cfg)
ok("and false for the bad value", cfg.is_grid_fleet_mode() is False)
os.environ.pop("CRYPTO_STRATEGY_MODE", None)
importlib.reload(cfg)

# --- the substitute must be gone from the source entirely ------------------
src = open(os.path.join(HERE, "crypto_strategy_config.py"), encoding="utf-8").read()
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
# Compare executable code, not the docstring that explains the removal.
body = code.split('"""')[0] + code.split('"""')[-1] if code.count('"""') >= 2 else code
ok("no DEFAULT_CRYPTO_STRATEGY constant remains", "DEFAULT_CRYPTO_STRATEGY" not in body)
ok("the failure is logged at ERROR, not WARNING", "log.error(" in body and "log.warning(" not in body)
ok("the message says nothing will be bought or sold",
   "nothing will be bought or sold" in src)
ok("and names both services' correct values",
   "SERVICE_ROLE=crypto-trading" in src and "family_tree on the web" in src)

# --- the dedicated runner must not blame the web service ------------------
runner = open(os.path.join(HERE, "bot_runner.py"), encoding="utf-8").read()
ok("bot_runner imports the sentinel", "UNCONFIGURED" in runner)
ok("and handles it before the 'owned by the web service' branch",
   runner.index("UNCONFIGURED:") < runner.index("owned by the web service"))
# Matched within a single source line: the message is split across adjacent
# string literals, so a phrase spanning the break never appears in the file.
ok("and says no loop is running on EITHER service",
   "loop is running anywhere" in runner and "not on the web service" in runner)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
