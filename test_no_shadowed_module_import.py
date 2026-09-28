"""A local import of a module-level name breaks every EARLIER use of it.

Python decides a name is function-local by looking for any binding
anywhere in the function - including an `import` on the last line. Every
use before that point then raises UnboundLocalError, at runtime, only on
the path that reaches it.

routers/trading_dashboard.py::get_live_dashboard_data_v2 had exactly
this. `import aiohttp` sat in the Alpaca block at line 7313; the
Coinbase account fetch used `aiohttp.ClientSession()` at line 7291. The
fetch could therefore never run. Its `except Exception` caught the
UnboundLocalError and substituted a literal - 483.00 - which the live
dashboard then served as the account balance. On 2026-09-28 the page
read "$483.00, total profit $0.00, growth 0%" while the account held
$10,882.46 and had banked $59.16 across 114 trades.

Nothing in the test suite could see it: the handler needs live
credentials and a database, so it was never exercised, and the bug is
invisible to the eye because the two lines are 22 apart.

This is a whole-repo check because the shape is not specific to that
file, and it is cheap: 448 files, no imports executed, pure AST.
"""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _module_level_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def _violations(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError):
        return []          # not ours to police
    module_names = _module_level_names(tree)
    found = []

    def check(fn):
        # Earliest local import of each name inside this function.
        first_import = {}
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    nm = a.asname or (a.name.split(".")[0]
                                      if isinstance(node, ast.Import) else a.name)
                    if nm not in first_import or node.lineno < first_import[nm]:
                        first_import[nm] = node.lineno
        for nm, line in first_import.items():
            if nm not in module_names:
                continue                      # no module-level name to shadow
            for use in ast.walk(fn):
                if (isinstance(use, ast.Name) and use.id == nm
                        and isinstance(use.ctx, ast.Load) and use.lineno < line):
                    found.append((path, fn.name, nm, use.lineno, line))
                    break

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, fn):
            check(fn); self.generic_visit(fn)

        def visit_AsyncFunctionDef(self, fn):
            check(fn); self.generic_visit(fn)

    V().visit(tree)
    return found


def _repo_files():
    return sorted(glob.glob(os.path.join(ROOT, "*.py"))
                  + glob.glob(os.path.join(ROOT, "routers", "*.py")))


def test_no_function_shadows_a_module_import_it_already_used():
    bad = []
    for path in _repo_files():
        bad += _violations(path)
    assert not bad, "\n".join(
        f"{os.path.relpath(p, ROOT)}::{fn}() uses {nm} at line {u} "
        f"but locally imports it at line {i} - every use before line {i} "
        f"raises UnboundLocalError at runtime"
        for p, fn, nm, u, i in bad)


def test_the_detector_catches_the_real_bug_it_was_written_for():
    # The exact shape, as it stood. If this stops failing the check above
    # has stopped checking anything, which is the failure mode that
    # matters for a linter nobody watches.
    import tempfile
    src = (
        "import aiohttp\n"
        "async def handler():\n"
        "    try:\n"
        "        async with aiohttp.ClientSession() as s:\n"
        "            pass\n"
        "    except Exception:\n"
        "        balance = 483.00\n"
        "    try:\n"
        "        import aiohttp\n"
        "    except Exception:\n"
        "        pass\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); tmp = f.name
    try:
        bad = _violations(tmp)
        assert len(bad) == 1, bad
        assert bad[0][2] == "aiohttp"
        assert bad[0][3] < bad[0][4]     # used before it is imported
    finally:
        os.unlink(tmp)


def test_an_alias_is_not_a_shadow():
    # `import aiohttp as _aiohttp` binds a DIFFERENT name, so it cannot
    # break an earlier `aiohttp.` use. Several handlers in this repo do
    # exactly that on purpose; flagging them would make the check noise.
    import tempfile
    src = ("import aiohttp\n"
           "async def handler():\n"
           "    async with aiohttp.ClientSession() as s:\n"
           "        pass\n"
           "    import aiohttp as _aiohttp\n"
           "    return _aiohttp\n")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); tmp = f.name
    try:
        assert _violations(tmp) == []
    finally:
        os.unlink(tmp)


def test_a_local_import_used_only_after_itself_is_fine():
    import tempfile
    src = ("import json\n"
           "def handler():\n"
           "    import json\n"
           "    return json.dumps({})\n")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); tmp = f.name
    try:
        assert _violations(tmp) == []
    finally:
        os.unlink(tmp)


def test_a_local_import_of_a_name_never_imported_at_module_level_is_fine():
    import tempfile
    src = ("def handler():\n"
           "    x = uuid\n"          # NameError, but not THIS bug
           "    import uuid\n")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); tmp = f.name
    try:
        assert _violations(tmp) == []
    finally:
        os.unlink(tmp)


def test_it_actually_reads_the_repo_and_is_not_vacuous():
    files = _repo_files()
    assert len(files) > 200, len(files)
    assert any(p.endswith(os.path.join("routers", "trading_dashboard.py")) for p in files)


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
