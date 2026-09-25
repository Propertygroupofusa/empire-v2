"""`from database import AsyncSessionLocal` captures None. Forever. Anywhere.

THE MECHANISM

database.py sets, at module scope:

    engine = None
    AsyncSessionLocal = None

Neither is ever assigned again except inside get_engine() and
get_session_factory(), which create them on first call. So the module-level
names are None until somebody calls an accessor - and `from database import
AsyncSessionLocal` binds the value AT IMPORT TIME. Importing it before the
first accessor call binds None permanently, and no later initialisation can
reach that binding, because it is a different name in a different module.

The failure is then `TypeError: 'NoneType' object is not callable` at every
use site.

WHAT IT ACTUALLY COST

This was found three times in this codebase before this file existed:

  * crypto_grid_bot.py - two surviving call sites after a partial fix. Every
    completed grid round trip since 2026-09-22 went unrecorded, because
    _log_grid_trade caught the exception and carried on.
  * crypto_selection_backtest.py - the same.
  * alpaca_swing_bot.py - and this one is the reason the rule is now
    mechanical rather than a habit. _record_closed_trade() wraps its write in
    try/except, logs "trade happened, sample lost" at WARNING, and continues.
    So every closed swing trade silently failed to reach the ledger - AND
    intraday_slots_allowed(), the governor that decides how many positions
    the bot may open, reads its verdict from that same ledger. A governor
    making live sizing decisions from a table that could never be written.

That is the shape of this bug every time: it does not crash, it deletes
evidence. A swallowed exception plus a permanently-None binding produces a
system that looks healthy and knows nothing.

THE RULE

Import the ACCESSORS - get_session_factory() and get_engine() - and call
them at use time:

    async with get_session_factory()() as db:     # correct
    async with AsyncSessionLocal() as db:          # None, forever

A function-level `from database import AsyncSessionLocal` is not a
loophole. It happens to work only when something else already called an
accessor first, which makes correctness depend on import order that nobody
is tracking. It is banned here too.

Run: python3 test_no_none_db_bindings.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAZY_ONLY = {"AsyncSessionLocal", "engine"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv"}

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def py_files():
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


# --- the premise: these really are None at module scope -------------------
db_src = open(os.path.join(HERE, "database.py"), encoding="utf-8").read()
db_tree = ast.parse(db_src)
module_level_none = set()
for node in db_tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if (isinstance(t, ast.Name) and t.id in LAZY_ONLY
                    and isinstance(node.value, ast.Constant) and node.value.value is None):
                module_level_none.add(t.id)
ok("database.py still defines these as None at module scope (the whole premise)",
   module_level_none == LAZY_ONLY)
ok("and still exposes the lazy accessors that are the correct way in",
   "def get_session_factory" in db_src and "def get_engine" in db_src)

# --- nothing imports the None names, at ANY scope -------------------------
offenders = []
for path in py_files():
    if os.path.basename(path) == "database.py":
        continue
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "database":
            for a in node.names:
                if a.name in LAZY_ONLY:
                    # An ALIAS is not an escape. `from database import engine
                    # as db_engine` binds exactly the same None, and it hid
                    # three real call sites in data_retention.py from the
                    # first sweep of this very rule - the scan required
                    # asname to be absent, so renaming the import made the
                    # bug invisible to the check written to find it.
                    alias = f" as {a.asname}" if a.asname else ""
                    offenders.append(f"{os.path.relpath(path, HERE)}:{node.lineno} "
                                     f"imports {a.name}{alias}")

ok("NOTHING imports the permanently-None names - not at module scope, and "
   "not inside a function either, where it only works by import-order luck",
   not offenders)
if offenders:
    for o in offenders[:25]:
        print("      OFFENDER:", o)

# --- and nothing calls them as if they were live --------------------------
call_sites = []
for path in py_files():
    if os.path.basename(path) in ("database.py", os.path.basename(__file__)):
        continue
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except SyntaxError:
        continue
    # A module may legitimately define its OWN AsyncSessionLocal - either a
    # thin shim over the accessor (crypto_family_tree_bot does exactly this)
    # or its own sessionmaker over its own engine. Those are not this bug:
    # the bug is specifically a name imported from database.py, which is
    # None. Flagging a local definition would push authors to delete correct
    # code to satisfy a checker, which is how a good rule gets a bad name.
    defines_own = any(
        (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "AsyncSessionLocal")
        or (isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "AsyncSessionLocal"
                    for t in n.targets))
        for n in ast.walk(tree))
    if defines_own:
        continue
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "AsyncSessionLocal"):
            call_sites.append(f"{os.path.relpath(path, HERE)}:{node.lineno}")

ok("REGRESSION: no AsyncSessionLocal() call on an IMPORTED (None) name survives",
   not call_sites)

# The one legitimate shape, kept as a worked example so the rule above reads
# as "import the accessor", not "never write this identifier".
_ft = os.path.join(HERE, "crypto_family_tree_bot.py")
if os.path.exists(_ft):
    _src = open(_ft, encoding="utf-8").read()
    ok("a local shim over the accessor is allowed, and is what the family "
       "tree uses",
       "def AsyncSessionLocal():" in _src and "get_session_factory()()" in _src)
if call_sites:
    for c in call_sites[:25]:
        print("      CALL SITE:", c)

# --- the two live bots that were really broken ----------------------------
for mod in ("alpaca_swing_bot.py", "crypto_coinbase_bot.py",
            "crypto_grid_bot.py", "profit_sweep_engine.py",
            "process_payouts.py", "bank_transfer_automation.py"):
    p = os.path.join(HERE, mod)
    if not os.path.exists(p):
        continue
    src = open(p, encoding="utf-8").read()
    ok(f"{mod}: uses the accessor", "get_session_factory()" in src)
    ok(f"{mod}: has no bare AsyncSessionLocal( left",
       "AsyncSessionLocal(" not in src)

# --- the specific governor that was reading an unwritable ledger ----------
sw = os.path.join(HERE, "alpaca_swing_bot.py")
if os.path.exists(sw):
    src = open(sw, encoding="utf-8").read()
    ok("alpaca_swing_bot's closed-trade ledger can actually be written now",
       "get_session_factory()() as db" in src)
    ok("and the governor that reads it is on the same working binding",
       "intraday_slots_allowed" in src and "AsyncSessionLocal" not in src)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
