"""Every module-level name a bot actually calls must be bound in that module.

This exists because of one real, silent, three-day money-measurement
outage. Commit 28cc92c ("Fix lazy Grid database sessions") switched
crypto_grid_bot.py from `from database import AsyncSessionLocal` to
`from database import get_session_factory` and converted the call sites -
but missed two of them:

  * _log_grid_trade()          - writes CryptoGridTradeHistory
  * the auto-rotate coin picker - reads CryptoBacktestRun

Both then raised `NameError: name 'AsyncSessionLocal' is not defined`
every single time they ran. Both were wrapped in `except Exception`, so
the NameError was swallowed and nothing surfaced. The visible symptom was
not an error at all: the dashboard's realized P&L froze at +$19.55 over
82 trades and kept presenting that stale number as the current, live
result, while real completed round trips (a real NEAR sell for +$0.23 on
2026-09-25 at 19:18:49, logged to the activity feed at the same moment)
were dropped on the floor.

A `try/except` around a database write turns a loud crash into a quiet
wrong number. That is the right trade for a logger that must never unwind
a real trade - and it is exactly why the binding has to be checked
somewhere the exception handler cannot hide it.

Run: python3 test_session_factory_binding.py
"""

import ast
import builtins
import sys

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


def module_level_bindings(tree):
    """Names bound at module scope: imports, assignments, defs, classes."""
    bound = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Try):
            # `try: from x import y / except ImportError: y = None`
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        bound.add(a.asname or a.name)
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        bound.add((a.asname or a.name).split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                bound.add(n.id)
    return bound


def locally_bound(fn):
    """Names bound anywhere inside a function body (params, assigns,
    local imports, with/for/except targets, comprehensions, nested defs)."""
    bound = set()
    args = fn.args
    for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
        bound.add(a.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            bound.add(n.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.NamedExpr):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
    return bound


def unbound_called_names(path):
    """Names that are CALLED somewhere in the module but bound nowhere.

    Deliberately narrow: only `foo(...)` call targets. A call to an
    unbound name is a guaranteed NameError the moment that line runs,
    which is exactly the failure mode being guarded against - and it
    avoids the false positives a full scope analysis would need to
    handle for a check that has to stay cheap and obvious.
    """
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    module_names = module_level_bindings(tree)
    builtin_names = set(dir(builtins))

    # Precompute the local bindings of every function, keyed by the node,
    # then attribute each call to its nearest enclosing function.
    fn_nodes = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    fn_locals = {id(n): locally_bound(n) for n in fn_nodes}

    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node

    def enclosing_scopes(node):
        scopes = []
        cur = parent.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append(fn_locals[id(cur)])
            cur = parent.get(id(cur))
        return scopes

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Name):
            continue
        name = fn.id
        if name in module_names or name in builtin_names:
            continue
        if any(name in scope for scope in enclosing_scopes(node)):
            continue
        bad.append((name, node.lineno))
    return bad


print("test_session_factory_binding.py")
print()

BOTS = [
    "crypto_grid_bot.py",
    "crypto_family_tree_bot.py",
    "crypto_mean_reversion_bot.py",
    "crypto_coinbase_bot.py",
    "alpaca_mean_reversion.py",
    "prop_bot.py",
]

print("-- no bot calls a name it never bound --")
for mod in BOTS:
    try:
        bad = unbound_called_names(mod)
    except FileNotFoundError:
        print(f"  SKIP  {mod} (not present)")
        continue
    detail = ", ".join(f"{n} (line {ln})" for n, ln in bad)
    ok(f"{mod} calls only names it has bound", not bad, detail)

print()
print("-- the exact regression: the grid bot's session factory --")
src = open("crypto_grid_bot.py", encoding="utf-8").read()
ok("crypto_grid_bot.py no longer references AsyncSessionLocal at all",
   "AsyncSessionLocal" not in src)
ok("crypto_grid_bot.py imports get_session_factory",
   "from database import get_session_factory" in src)
ok("every session in the grid bot goes through the lazy factory",
   src.count("get_session_factory()()") >= 2,
   f"found {src.count('get_session_factory()()')}")

print()
print("-- a dropped round trip can no longer be silent --")
tree = ast.parse(src)
log_fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_log_grid_trade"), None)
ok("_log_grid_trade still exists", log_fn is not None)
if log_fn is not None:
    body = ast.get_source_segment(src, log_fn) or ""
    ok("_log_grid_trade reports a failed write at ERROR, not warning",
       "log.error(" in body and "log.warning(" not in body)
    ok("the error names the P&L that did not make it into the ledger",
       "pnl" in body.split("except")[-1])
    ok("_log_grid_trade still refuses to raise into the trade path",
       any(isinstance(n, ast.Try) for n in ast.walk(log_fn)))

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
