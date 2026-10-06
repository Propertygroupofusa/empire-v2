"""A name read in a function that is not a local, a parameter, a module
global or a builtin. At runtime that is a NameError, and in a request
handler it is nothing but an HTTP 500 to the caller.

Run as written: python3 test_undefined_names.py

WHY THIS EXISTS. /grid-status/incubator answered for one deploy and then
returned 500 on every read. The cause was a single unbound name - `g` was
referenced to read the live fleet step and never assigned in that handler.
Every other endpoint in the file assigns it; that one did not, and because
`g` is not a module global there was nothing to fall back to.

WHY THE EXISTING TESTS COULD NOT SEE IT. The endpoint tests in this repo
read a handler's SOURCE TEXT and assert on what it contains. They never
execute it, so a name that is spelled correctly and simply never bound
reads as present to them and raises in production. Source-parsing tests
cannot catch a binding bug; a scope check can, without a database, a
network call or a running app.

HOW IT WORKS. symtable gives, per scope, whether each name is referenced
and how it resolves. A name marked GLOBAL_IMPLICIT inside a function was
never assigned there and was not found in any enclosing function, so the
only places left for it are module globals and builtins. If it is in
neither, the read cannot succeed.

WHAT IT DOES NOT DO. It does not fix the four pre-existing findings it
reports. They are recorded below as a frozen baseline with what each one
would do if its branch ran, so the count cannot grow quietly. Fixing them
means touching live trading modules, which is the account owner's call and
not this test's.
"""

import builtins
import os
import symtable
import sys

FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))

# Module-level names the interpreter supplies and symtable does not see.
IMPLICIT = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__debug__"}

# FROZEN. Each entry is a real unbound read that exists on main today, kept
# so a NEW one is unmissable. Keyed file -> {name: scope}.
#
#   crypto_coinbase_bot.py:1751  `logger` and `product_id` in the fee-floor
#     except handler. The module's logger is named `log`, and `product_id`
#     is never bound in run_crypto_cycle. If fee_floor.min_profit_usd ever
#     raises, the handler meant to downgrade it to a warning raises
#     NameError instead and takes the whole exit evaluation with it.
#   edge_test.py / fleet_capital_allocator.py  `AsyncSessionLocal` used
#     without being imported - the same import-lifecycle class as the
#     wheel-bot fix, except here there is no import at all.
#   prop_bot_options.py  `OptionPosition` referenced in the tracker's
#     __init__ with no such name in the module.
KNOWN = {
    "crypto_coinbase_bot.py": {"logger", "product_id"},
    "edge_test.py": {"AsyncSessionLocal"},
    "fleet_capital_allocator.py": {"AsyncSessionLocal"},
    "prop_bot_options.py": {"OptionPosition"},
}


def undefined_names(path):
    """-> {name: [scope, ...]} for every name read that cannot resolve."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    top = symtable.symtable(src, path, "exec")
    module_names = {s.get_name() for s in top.get_symbols()
                    if s.is_assigned() or s.is_imported() or s.is_parameter()}
    resolvable = module_names | set(dir(builtins)) | IMPLICIT
    found = {}

    def walk(scope):
        for sym in scope.get_symbols():
            name = sym.get_name()
            # is_global() here means GLOBAL_IMPLICIT or GLOBAL_EXPLICIT:
            # not a local of this scope and not free in an enclosing one.
            if sym.is_referenced() and sym.is_global() and name not in resolvable:
                found.setdefault(name, []).append(scope.get_name())
        for child in scope.get_children():
            walk(child)

    for child in top.get_children():
        walk(child)
    return found


def modules():
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "node_modules", "venv",
                                ".venv", "site-packages")]
        for f in sorted(files):
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)


# [1] The handler whose 500 this is. Zero, with no baseline allowance.
ROUTER = os.path.join(HERE, "routers", "trading_dashboard.py")
router_found = undefined_names(ROUTER)
ok("routers/trading_dashboard.py reads no name it has not bound - this is "
   "the file the incubator 500 came from, so it gets no baseline allowance",
   router_found == {})
if router_found:
    for n, scopes in sorted(router_found.items()):
        print("        " + n + " in " + ", ".join(sorted(set(scopes))))

# [2] The exact bug, reconstructed. If this check could not catch the
#     original defect it is decoration, so prove it on the real shape.
BUG = (
    "import crypto_grid_bot as crypto_grid_bot_module\n"
    "async def grid_incubator_endpoint():\n"
    "    if crypto_grid_bot_module is None:\n"
    "        raise RuntimeError('unavailable')\n"
    "    status = await g.get_grid_status()\n"
    "    return status\n"
)
bug_top = symtable.symtable(BUG, "<bug>", "exec")
bug_mod = {s.get_name() for s in bug_top.get_symbols()
           if s.is_assigned() or s.is_imported() or s.is_parameter()}
bug_scope = [c for c in bug_top.get_children()
             if c.get_name() == "grid_incubator_endpoint"][0]
bug_hits = {s.get_name() for s in bug_scope.get_symbols()
            if s.is_referenced() and s.is_global()
            and s.get_name() not in (bug_mod | set(dir(builtins)) | IMPLICIT)}
ok("the check catches the shipped defect: `g` read in the handler with no "
   "binding anywhere, which is the 500 exactly as it happened",
   bug_hits == {"g"})

FIXED = BUG.replace("    status = await g",
                    "    g = crypto_grid_bot_module\n    status = await g")
fixed_top = symtable.symtable(FIXED, "<fixed>", "exec")
fixed_mod = {s.get_name() for s in fixed_top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_parameter()}
fixed_scope = [c for c in fixed_top.get_children()
               if c.get_name() == "grid_incubator_endpoint"][0]
fixed_hits = {s.get_name() for s in fixed_scope.get_symbols()
              if s.is_referenced() and s.is_global()
              and s.get_name() not in (fixed_mod | set(dir(builtins)) | IMPLICIT)}
ok("and clears once the name is bound, so it is reporting the binding and "
   "not merely the spelling", fixed_hits == set())

# [3] A name that IS a module global must not be reported. Without this the
#     check would fail every module in the repo and get switched off.
CLEAN = ("import os\n"
         "LIMIT = 3\n"
         "def f(a):\n"
         "    b = a + LIMIT\n"
         "    return os.path.join(str(b), __file__)\n")
clean_top = symtable.symtable(CLEAN, "<clean>", "exec")
clean_mod = {s.get_name() for s in clean_top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_parameter()}
clean_scope = clean_top.get_children()[0]
clean_hits = {s.get_name() for s in clean_scope.get_symbols()
              if s.is_referenced() and s.is_global()
              and s.get_name() not in (clean_mod | set(dir(builtins)) | IMPLICIT)}
ok("a module global, an import and __file__ are all resolvable - no false "
   "positive on the ordinary case", clean_hits == set())

# [4] Nothing new, anywhere in the repo.
new_findings = []
for path in modules():
    rel = os.path.relpath(path, HERE)
    try:
        found = undefined_names(path)
    except SyntaxError as exc:
        # A module that does not parse is a different failure, reported
        # rather than swallowed into a pass.
        new_findings.append(rel + ": does not parse: " + str(exc))
        continue
    allowed = KNOWN.get(rel, set())
    for name, scopes in sorted(found.items()):
        if name not in allowed:
            new_findings.append(rel + ": " + name + " in "
                                + ", ".join(sorted(set(scopes))))

ok("no module reads a name it has not bound, beyond the four findings "
   "already on main", not new_findings)
for line in new_findings:
    print("        " + line)

# [5] The baseline is a record of real defects, not a mute button. If one is
#     fixed this fails and the entry comes out - a stale allowance is how a
#     frozen list rots into permission.
stale = []
for rel, names in sorted(KNOWN.items()):
    path = os.path.join(HERE, rel)
    if not os.path.exists(path):
        stale.append(rel + ": file is gone")
        continue
    still = set(undefined_names(path))
    for name in sorted(names - still):
        stale.append(rel + ": " + name + " is fixed - remove it from KNOWN")
ok("every baselined finding is still really there, so the list cannot rot "
   "into blanket permission", not stale)
for line in stale:
    print("        " + line)

print("\nALL PASS" if not FAILS else "\n%d FAILED: " % len(FAILS) + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
