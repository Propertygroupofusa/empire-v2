"""The grid sell path must not retire a slice the venue only partly sold.

WHY THIS FILE EXISTS. grid_sell() returns what ACTUALLY filled - its own
docstring says "Returns (filled_qty, price, leg_fee_rate)". The rise-trigger
sell priced the P&L with that number and then deleted the whole slice row
regardless of it. Two things followed, and between them they explain why the
same branch's shortfall was seen moving in BOTH directions:

  PARTIAL FILL      the row vanished while the wallet had sold only part of
                    it, so tracked units fell faster than held units.
  SELL LANDS BUT    the venue call and the DB write are not one transaction
  THE WRITE DOESN'T and the write had no retry, so one transient failure left
                    the coin gone and the slice still claiming it.

Eight live branches were short $510.47 against the wallet when this was found,
and the drift is self-reinforcing: a branch already short cannot fill the next
sell for its full qty either.

crypto_grid_bot imports the database, the models and three sibling bots at
module scope, none of which exist in this environment, so the unit tests below
lift the helper out of the file with AST and exec only it. The shape tests
assert against the PARSED TREE, never against source text - a guard that
matches strings fires on the comment that explains the bug.

A missing helper is recorded as a failure rather than raised, so that reverting
the fix fails this file on WHAT IS WRONG WITH THE SELL PATH and not merely on
"that name is new", which would prove nothing.
"""
import ast
import sys

SRC_PATH = "crypto_grid_bot.py"
with open(SRC_PATH, encoding="utf-8") as fh:
    SOURCE = fh.read()
TREE = ast.parse(SOURCE)

failures = []
MISSING = "<helper missing>"


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def _load_helper():
    """Exec ONLY the helper and the constants it names, derived from its own
    AST rather than a hand-written list that would go stale silently."""
    fn = next((n for n in TREE.body
               if isinstance(n, ast.FunctionDef) and n.name == "grid_sell_residual"),
              None)
    if fn is None:
        return None
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    segments = []
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & names:
                segments.append(ast.get_source_segment(SOURCE, node))
    segments.append(ast.get_source_segment(SOURCE, fn))
    ns = {}
    exec("\n".join(segments), ns)
    return ns["grid_sell_residual"]


RESIDUAL = _load_helper()


def call(*args):
    """Call the helper if it exists; otherwise hand back a sentinel so every
    assertion below fails on its own merits instead of aborting the file."""
    if RESIDUAL is None:
        return MISSING, MISSING
    return RESIDUAL(*args)


print("grid_sell_residual")
check("grid_sell_residual exists as a module-level function", RESIDUAL is not None,
      "the sell path has no way to tell a partial fill from a whole one")

r, retire = call(100.0, 100.0)
check("a full fill retires the slice", retire is True and r == 0.0, f"{r!r},{retire!r}")

r, retire = call(100.0, 40.0)
check("a partial fill KEEPS the slice with the remainder",
      retire is False and r != MISSING and abs(r - 60.0) < 1e-12, f"{r!r},{retire!r}")

r, retire = call(100.0, 100.0 - 1e-5)
check("dust below the relative epsilon retires", retire is True and r == 0.0,
      f"{r!r},{retire!r}")

r, retire = call(100.0, 99.9)
check("a real remainder above the epsilon is kept",
      retire is False and r != MISSING and abs(r - 0.1) < 1e-9, f"{r!r},{retire!r}")

r, retire = call(100.0, 120.0)
check("an overfill retires and never reports a NEGATIVE residual",
      retire is True and r == 0.0, f"{r!r},{retire!r}")

r, retire = call(100.0, 0.0)
check("a zero fill keeps the whole slice", retire is False and r == 100.0,
      f"{r!r},{retire!r}")

r, retire = call(100.0, None)
check("an unreadable fill keeps the whole slice", retire is False and r == 100.0,
      f"{r!r},{retire!r}")

r, retire = call(100.0, float("nan"))
check("a NaN fill keeps the whole slice", retire is False and r == 100.0,
      f"{r!r},{retire!r}")

r, retire = call(0.0, 5.0)
check("an already-empty slice retires", retire is True and r == 0.0,
      f"{r!r},{retire!r}")

r, retire = call(None, 5.0)
check("an unreadable slice qty is KEPT, never deleted on a guess",
      retire is False and r is None, f"{r!r},{retire!r}")

# This fleet's quantities span eleven orders of magnitude - PEPE slices are
# ~8,000,000 units and BTC slices ~0.0005 - so one ABSOLUTE epsilon is either
# meaningless at one end or destructive at the other.
r, retire = call(8296372.54005447, 8296372.53005447)
check("PEPE-scale dust retires", retire is True and r == 0.0, f"{r!r},{retire!r}")

r, retire = call(0.0005, 0.0004)
check("BTC-scale remainder is KEPT, not swallowed by an epsilon",
      retire is False and r != MISSING and abs(r - 0.0001) < 1e-12, f"{r!r},{retire!r}")


def _enclosing_sell_function():
    """The function that calls grid_sell() - found, not named, so renaming it
    does not silently disable every assertion below."""
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "grid_sell"):
                return node
    return None


sell_fn = _enclosing_sell_function()
print("\nthe sell path")
check("a function calling grid_sell() exists", sell_fn is not None)

if sell_fn is not None:
    names_used = {n.id for n in ast.walk(sell_fn) if isinstance(n, ast.Name)}
    check("it calls grid_sell_residual", "grid_sell_residual" in names_used,
          "the fill quantity is being discarded")

    deletes = [n for n in ast.walk(sell_fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "delete"]
    check("the sell path still deletes a slice somewhere", len(deletes) >= 1)

    def guarded_by_retire(target):
        for node in ast.walk(sell_fn):
            if not isinstance(node, ast.If):
                continue
            test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "_retire" not in test_names:
                continue
            if any(d is target for d in ast.walk(node)):
                return True
        return False

    check("EVERY slice delete is guarded by the retire decision",
          bool(deletes) and all(guarded_by_retire(d) for d in deletes),
          "an unguarded delete retires coin the wallet still holds")

    qty_writes = [n for n in ast.walk(sell_fn) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Attribute) and t.attr == "qty"
                          for t in n.targets)]
    check("the partial branch writes the remainder back to the slice's qty",
          len(qty_writes) >= 1)

    retry_loops = [n for n in ast.walk(sell_fn) if isinstance(n, ast.For)
                   and any(isinstance(s, ast.Try) for s in ast.walk(n))]
    check("the ledger write is inside a retrying loop", len(retry_loops) >= 1,
          "a venue sell followed by a single unguarded write is a gap the size "
          "of one restart")

    log_errors = [n for n in ast.walk(sell_fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "error"]
    check("a sale that could not be persisted is logged at error level",
          len(log_errors) >= 1, "a silent skip on a money path is the worst gap")

    len_tests = [n for n in ast.walk(sell_fn) if isinstance(n, ast.If)
                 and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                         and c.func.id == "len" for c in ast.walk(n.test))]
    check("the post-sale settle also consults the retire decision",
          any("_retire" in {x.id for x in ast.walk(t.test) if isinstance(x, ast.Name)}
              for t in len_tests),
          "a kept residual means the branch is not flat")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all grid sell partial-fill checks passed")
