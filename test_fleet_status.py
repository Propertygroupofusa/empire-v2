"""The fleet-status panel reported "Primary profit $0.00" permanently.

Not "sometimes wrong" - structurally incapable of ever being right. Three
independent faults stacked:

  1. get_primary_bot_profit() read `{BASE_DIR}/bot_session.json`. NOTHING in
     this repository writes that file. Three modules read it; zero write it.
     The grid bot records closed round trips in CryptoGridTradeHistory.
  2. BASE_DIR was the hardcoded developer path "/home/user/empire-v2", which
     does not exist in the deployed container.
  3. The missing file fell through to `return 0`, and the endpoint logged
     that 0 as though it were a measurement.

So the dashboard showed $0.00 while the account had really made +$19.55 over
82 closed round trips.

The second half of this file covers the Live Ops runner panel, which told the
reader to "set CRYPTO_STRATEGY_MODE per service" and never mentioned
SERVICE_ROLE - the variable service_entrypoint.py actually routes on. A
crypto-trading service without it runs main.py, which delegates straight back
to the crypto-trading service, and nothing trades with no error anywhere.

Run: python3 test_fleet_status.py
"""
import ast
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


sc_src = open(os.path.join(HERE, "scaling_coordinator.py")).read()
td_src = open(os.path.join(HERE, "routers/trading_dashboard.py")).read()
se_src = open(os.path.join(HERE, "service_entrypoint.py")).read()

# --- the file nothing writes is no longer the source of truth -------------
tree = ast.parse(sc_src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "get_primary_bot_profit")
body = ast.get_source_segment(sc_src, fn)

# Compare CODE, not prose. These docstrings deliberately describe the old
# bug, so a plain substring search over the source finds "bot_session.json"
# and "BASE_DIR" in the very comment explaining why they were removed. An
# earlier assertion in this session failed exactly this way; strip the
# docstring and test what actually executes.
def code_of(node, src):
    stripped = ast.parse(ast.get_source_segment(src, node)).body[0]
    if (stripped.body and isinstance(stripped.body[0], ast.Expr)
            and isinstance(stripped.body[0].value, ast.Constant)
            and isinstance(stripped.body[0].value.value, str)):
        stripped.body = stripped.body[1:]
    return ast.unparse(stripped)


body_code = code_of(fn, sc_src)
ok("primary profit no longer reads bot_session.json", "bot_session" not in body_code)
ok("it reads the same trade history the rest of the dashboard uses",
   "get_grid_trade_history" in body)
ok("and pulls the real realized total from it", "total_realized_pnl" in body)
ok("it is async, since the database read is", isinstance(fn, ast.AsyncFunctionDef))

# The heart of it: a failure must not look like zero profit.
ok("a failed read returns None, never 0",
   "return None" in body and not any(
       isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value == 0
       for n in ast.walk(fn)))

# --- nothing in the repo writes that file, which is why this was dead -----
import re

# A writer would have to open THAT path for writing. Checking "the filename
# appears anywhere AND 'w' appears anywhere in the module" is far too loose -
# scaling_coordinator.py mentions the path (for clone instances) and calls
# json.dump (for the registry), which are different files entirely.
WRITE_OPEN = re.compile(r"open\([^)]*bot_session[^)]*['\"][wa]", re.S)
writers = []
for name in os.listdir(HERE):
    if not name.endswith(".py") or name == os.path.basename(__file__):
        continue
    if WRITE_OPEN.search(open(os.path.join(HERE, name)).read()):
        writers.append(name)
ok("still true that nothing writes bot_session.json (the original fault)", not writers)

# --- hardcoded developer path -------------------------------------------
ok("the profit read no longer depends on the hardcoded developer path",
   "get_grid_trade_history" in body_code and "BASE_DIR" not in body_code)

# --- None must survive all the way out, not be coerced --------------------
status_fn = next(n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "get_fleet_status")
status_src = ast.get_source_segment(sc_src, status_fn)
ok("the endpoint payload passes None through rather than rounding it",
   "None if primary_profit is None" in status_src)
ok("the fleet total does too", "None if fleet_profit is None" in status_src)
ok("and the payload names where the number came from", "profit_source" in status_src)
ok("next_threshold is not computed against an unreadable profit",
   "None if primary_profit is None else" in status_src
   and status_src.index("next_threshold") > status_src.index("profit_source"))

# An unreadable primary must not be added to and reported as a fleet total.
fleet_fn = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "get_fleet_profit")
fleet_src = ast.get_source_segment(sc_src, fleet_fn)
ok("an unreadable primary makes the fleet total unreadable too",
   "if primary_profit is None" in fleet_src and "return None" in fleet_src)
ok("the fleet total reuses the primary figure rather than re-fetching it",
   "primary_profit" in [a.arg for a in fleet_fn.args.args])

# --- the log line that printed the fake zero ------------------------------
ok("the dashboard log renders an unreadable figure as 'unavailable'",
   "unavailable" in td_src and '_money(status[\'primary_bot_profit\'])' in td_src)
ok("and no longer formats it unconditionally as a dollar amount",
   "${status['primary_bot_profit']:,.2f}" not in td_src)

# --- the monitor loop must not clone on an unreadable profit --------------
mon_fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "monitor_fleet")
mon_src = ast.get_source_segment(sc_src, mon_fn)
ok("the monitor awaits the now-async profit read", "asyncio.run(get_primary_bot_profit())" in mon_src)
ok("and skips the cycle rather than treating None as below every threshold",
   "if current_profit is None" in mon_src and "continue" in mon_src)

# --- SERVICE_ROLE: the variable that actually routes the process ----------
ok("service_entrypoint still routes on SERVICE_ROLE (the premise of the gate)",
   'SERVICE_ROLE' in se_src and 'crypto-trading' in se_src and 'bot_runner.py' in se_src)
ok("the runner panel now has a SERVICE_ROLE gate", '"Grid runner service is wired up"' in td_src)
ok("its fix names BOTH variables, not just the mode",
   "SERVICE_ROLE=crypto-trading" in td_src and "AND CRYPTO_STRATEGY_MODE=grid_fleet" in td_src)
ok("and explains what happens without it",
   "launches main.py instead of bot_runner.py" in td_src)
ok("the gate admits it cannot read the other service's variables",
   "this page cannot read" in td_src)
ok("the gate reads the env var rather than assuming", 'os.getenv("SERVICE_ROLE")' in td_src)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
