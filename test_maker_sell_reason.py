"""A maker sell that was never placed must not be reported as one that
"did not fill".

WHY THIS FILE EXISTS. place_maker_sell returns None for three different
reasons:

  (a) the order rested at the ask and no buyer crossed   - a real no-fill
  (b) there was nothing sellable: the size clamped to the AVAILABLE balance
      and floored to zero at the product's decimals, so NO ORDER WAS
      CREATED
  (c) a precondition failed: the balance read errored, or the book had no
      ask - again, NO ORDER WAS CREATED

grid_sell logged all three as "maker sell did not fill and maker-ONLY mode
is on". For (b) and (c) that sentence is false: there is no order, so
nothing could fill. It describes a patient resting bid that does not exist.

That wording cost four hours. 441 QNT rows and 429 ALGO rows in one day
were read as unfilled orders in a tight book, when ALGO's available balance
was 0.046 of 1134.3 and QNT held dust - case (b) every time.

Asserted on the PARSED TREE.
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


def load(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


ENGINE_SRC, ENGINE = load("crypto_btc_compound_bot.py")
GRID_SRC, GRID = load("crypto_grid_bot.py")


def func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def returns_none_bare(node):
    return (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
            and node.value.value is None)


print("place_maker_sell: every silent exit must speak")
pmk = func(ENGINE, "place_maker_sell")
ok("it exists", pmk is not None)

if pmk is not None:
    # Find each `return None` and check that the block it sits in also
    # records a reason - either a log call or a write to the error dict.
    bare_returns = [n for n in ast.walk(pmk) if returns_none_bare(n)]
    ok("it has early returns to check", len(bare_returns) >= 2)

    def block_explains(ret):
        """Does some enclosing If also log or record a reason?"""
        for node in ast.walk(pmk):
            if not isinstance(node, ast.If):
                continue
            if not any(r is ret for r in ast.walk(node)):
                continue
            has_log = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in ("warning", "error", "info")
                for c in ast.walk(node))
            has_record = any(
                isinstance(sub, ast.Subscript)
                and isinstance(sub.value, ast.Name)
                and "error" in sub.value.id
                for sub in ast.walk(node))
            if has_log or has_record:
                return True
        return False

    unexplained = [r for r in bare_returns if not block_explains(r)]
    ok("EVERY early return explains itself", not unexplained,
       f"{len(unexplained)} of {len(bare_returns)} return None silently")

    # The two no-order-placed paths must record a machine-readable reason,
    # not only a log line, because grid_sell reads the dict.
    writes = [n for n in ast.walk(pmk) if isinstance(n, ast.Subscript)
              and isinstance(n.value, ast.Name) and "error" in n.value.id]
    ok("reasons are recorded where a caller can read them", len(writes) >= 2,
       "a log line alone cannot be read by grid_sell")

print("\ngrid_sell: report WHY, not just THAT")
gs = func(GRID, "grid_sell")
ok("it exists", gs is not None)

if gs is not None:
    seg = ast.get_source_segment(GRID_SRC, gs) or ""
    reads_reason = any(
        isinstance(n, ast.Attribute) and n.attr == "_last_order_error"
        for n in ast.walk(gs))
    ok("it reads the recorded reason", reads_reason,
       "otherwise it can only guess which of the three happened")

    # The specific false sentence must be gone.
    ok('it no longer asserts "maker sell did not fill"',
       "maker sell did not fill" not in seg,
       "false for the two paths where no order was ever created")

    # And it must still hold the slice rather than paying the taker leg.
    ok("it still holds the slice rather than crossing the spread",
       "taker leg" in seg,
       "the maker-only intent must survive the rewording")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all maker-sell reason checks passed")
