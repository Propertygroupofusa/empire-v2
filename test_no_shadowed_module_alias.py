"""A module alias must never be rebound as a local inside a function.

WHAT THIS COSTS WHEN IT IS WRONG, measured, not imagined. At 15:31:03Z on
2026-09-29 a real BTC-USD buy filled on the live fleet. The slice insert
raised:

    UnboundLocalError: cannot access local variable '_sl' where it is not
    associated with a value

`import slice_lifecycle as _sl` sits at module level, but
run_grid_branch_cycle also wrote `for _sl in slices:` twice. In Python a
name assigned ANYWHERE in a function is local for the WHOLE function, so
those loops shadowed the module for every line in the function - including
the lines ABOVE them. The coin was really bought, the row was never
written, the outer handler swallowed the error, and the only reason anyone
found out is that the same commit had just started recording CYCLE_ERROR.

The damage ran both ways, which is why a single-site fix would not have
been enough: the insert ABOVE the loops raised UnboundLocalError, and the
PARTIAL write BELOW them would have read a CryptoGridSlice as if it were
the module and raised AttributeError instead.

WHY 245 EXISTING TESTS MISSED IT. test_slice_state_writes.py asserts on the
parsed tree that the field is PRESENT. test_slice_state_roundtrip.py
EXECUTES the insert - but standalone, where `_sl` resolves to the module
just fine. Neither asks the only question that mattered: inside THAT
function, does this name still mean the module? Presence is not resolution,
and executing a statement out of its function is not executing the
function.

This checks every function in the file against every module alias, so it
fails for the next alias too, not just this one.
"""
import ast
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


TARGET = "crypto_grid_bot.py"
TREE = ast.parse(open(TARGET).read())

# Every name bound by a module-level import: `import x as y` -> y, `import x` -> x.
aliases = set()
for node in TREE.body:
    if isinstance(node, ast.Import):
        for a in node.names:
            aliases.add(a.asname or a.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for a in node.names:
            aliases.add(a.asname or a.name)

print(f"\n-- {len(aliases)} module-level import aliases in {TARGET} --")


def local_bindings(fn):
    """Names this function binds, which therefore shadow the module scope.

    Only the function's OWN body counts: a nested def or lambda has its own
    scope and cannot shadow a name in this one. Comprehensions likewise get
    their own scope in Python 3, so their targets are excluded.
    """
    bound = set()
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                ast.Lambda, ast.ListComp, ast.SetComp,
                                                ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
    return bound


def uses_as_module(fn, name):
    """True if the function reads `name.something` - i.e. treats it as a module."""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == name and isinstance(node.value.ctx, ast.Load)):
            return node.lineno
    return None


clashes = []
for fn in [n for n in ast.walk(TREE)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    bound = local_bindings(fn)
    # A function that re-imports the alias itself is fine: the name still
    # means the module. Only a NON-import rebinding is a shadow.
    reimported = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                reimported.add(a.asname or a.name.split(".")[0])
    for name in sorted(aliases & bound):
        if name in reimported:
            continue
        line = uses_as_module(fn, name)
        if line is not None:
            clashes.append((fn.name, name, line))

ok("no function rebinds a module alias it also uses as a module",
   not clashes,
   "; ".join(f"{f}() rebinds {n}, still used as a module at line {l}"
             for f, n, l in clashes))

# The specific regression, pinned by name so a future edit cannot quietly
# reintroduce it under a different loop variable.
FN = next((n for n in ast.walk(TREE)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "run_grid_branch_cycle"), None)
ok("run_grid_branch_cycle is present to check", FN is not None)

if FN is not None:
    bound = local_bindings(FN)
    ok("_sl is NOT rebound in run_grid_branch_cycle - it must stay the module",
       "_sl" not in bound,
       "a `for _sl in ...` or `_sl = ...` shadows slice_lifecycle for the "
       "WHOLE function, including the lines above it")
    # And prove the name is actually still needed there, so this test cannot
    # pass vacuously if the §1 writes are ever removed.
    ok("run_grid_branch_cycle really does use _sl as a module",
       uses_as_module(FN, "_sl") is not None,
       "nothing reads _sl.<attr> any more - if §1's writes were removed on "
       "purpose, delete this assertion; otherwise they have been lost")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    sys.exit(1)
print("No module alias is shadowed by a local binding.")
