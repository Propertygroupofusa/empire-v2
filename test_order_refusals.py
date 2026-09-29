"""A refusal nobody can see is indistinguishable from one that never fires.

WHY IT EXISTS. 6a95d4d made an unreadable product REFUSE to size an order
instead of falling back to 8 decimals. Correct, and what §22 asks for - but
the signal it writes was exposed by no read-only endpoint for the grid
fleet, so the whole fleet could have been refusing every order and the page
would have shown nothing missing.

The checks here are mostly about the difference between "read, and there
was nothing" and "could not read". An empty result that cannot tell those
apart is the same defect in a different place.
"""
import ast
import asyncio
import inspect
import sys

import crypto_grid_bot as g

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


class _Branch:
    def __init__(self, product_id, active=True):
        self.product_id = product_id
        self.active = active


def run(errors, branches=("LINK-USD", "ETH-USD"), raise_branches=False):
    """Drive the real builder with a substituted engine map."""
    eng = g.engine
    real_errors = getattr(eng, "_last_order_error", None)
    real_get = g.get_grid_branches

    async def fake_branches():
        if raise_branches:
            raise RuntimeError("db down")
        return [_Branch(p) for p in branches]

    try:
        if errors is _MISSING:
            eng._last_order_error = None
        else:
            eng._last_order_error = errors
        g.get_grid_branches = fake_branches
        return asyncio.get_event_loop().run_until_complete(g.get_order_refusals())
    finally:
        eng._last_order_error = real_errors
        g.get_grid_branches = real_get


_MISSING = object()

print("== read-and-empty is not the same as unreadable ==")
r = run({})
ok("an empty map is available", r["available"] is True, str(r))
ok("  and reports zero refusing", r["products_refusing"] == 0, str(r))
r = run(_MISSING)
ok("an unreadable map is NOT available", r["available"] is False, str(r))
ok("  and reports no count at all", "products_refusing" not in r, str(r))
ok("  and says what could not be read", "error" in r, str(r))

print("== a refusal is reported with its product and its reason ==")
r = run({"LINK-USD": "NOTHING_TO_SELL: real balance is effectively 0"})
ok("the product is named", r["by_product"].get("LINK-USD"), str(r))
ok("the count is one", r["products_refusing"] == 1, str(r))
ok("the reason code is the part before the colon",
   r["by_reason"] == {"NOTHING_TO_SELL": 1}, str(r["by_reason"]))
r = run({"LINK-USD": "NOTHING_TO_SELL: a", "ETH-USD": "NOTHING_TO_SELL: b"})
ok("two products with one reason count as two",
   r["by_reason"] == {"NOTHING_TO_SELL": 2}, str(r["by_reason"]))
r = run({"LINK-USD": "a message with no colon at all"})
ok("a reason with no colon is kept whole, not bucketed",
   r["by_reason"] == {"a message with no colon at all": 1}, str(r["by_reason"]))

print("== the rules refusal is called out separately ==")
# This is the one the fleet was told to watch after 6a95d4d.
r = run({"LINK-USD": "product rules unreadable: refusing to size an order against a guess"})
ok("it is listed", r["product_rules_unreadable"] == ["LINK-USD"],
   str(r["product_rules_unreadable"]))
ok("  and counted", r["product_rules_unreadable_count"] == 1, str(r))
r = run({"LINK-USD": "NOTHING_TO_SELL: x"})
ok("an ordinary refusal is not counted as a rules refusal",
   r["product_rules_unreadable_count"] == 0, str(r))
r = run({"LINK-USD": "product rules unreadable: x", "ETH-USD": "product rules unreadable: y"})
ok("two are both listed, sorted",
   r["product_rules_unreadable"] == ["ETH-USD", "LINK-USD"],
   str(r["product_rules_unreadable"]))

print("== it reports the fleet, not every product the engine ever touched ==")
r = run({"LINK-USD": "NOTHING_TO_SELL: x", "DOGE-USD": "NOTHING_TO_SELL: y"},
        branches=("LINK-USD",))
ok("a product outside the fleet is excluded", r["products_refusing"] == 1, str(r))
ok("  and it is the tracked one", "LINK-USD" in r["by_product"], str(r["by_product"]))
ok("  and the scope says so", "active grid branches" in r["scope"], r["scope"])

print("== an unreadable branch list widens the scope and SAYS so ==")
r = run({"LINK-USD": "NOTHING_TO_SELL: x", "DOGE-USD": "NOTHING_TO_SELL: y"},
        raise_branches=True)
ok("it still reports the refusals", r["products_refusing"] == 2, str(r))
ok("  and does not claim fleet scope", "active grid branches" != r["scope"], r["scope"])
ok("  and names the reason the scope is wider",
   "unreadable" in r["scope"], r["scope"])

print("== an empty or absent message is not a refusal ==")
r = run({"LINK-USD": "", "ETH-USD": None})
ok("blank messages are not counted", r["products_refusing"] == 0, str(r))

print("== it is telemetry: it cannot take the payload down ==")
_tree = ast.parse(open("crypto_grid_bot.py").read())
_calls = [n for n in ast.walk(_tree) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Name) and n.func.id == "_never_fails"
          and n.args and isinstance(n.args[0], ast.Name)
          and n.args[0].id == "get_order_refusals"]
ok("it is registered through _never_fails", len(_calls) == 1, str(len(_calls)))
_src = ast.parse(inspect.getsource(g.get_order_refusals))
# NOT "add": set.add is legitimate and this caught tracked.add(). The
# verbs that matter are the ones that reach a database or the venue.
_writes = [n for n in ast.walk(_src) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute)
           and n.func.attr in ("post", "put", "patch", "delete", "commit",
                               "execute", "flush", "merge")]
ok("it writes nothing", not _writes, str([n.func.attr for n in _writes]))
_opens = [n.id for n in ast.walk(_src) if isinstance(n, ast.Name)
          and n.id in ("get_session_factory", "aiohttp", "requests")]
ok("and opens no session of its own", not _opens, str(_opens))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all order-refusal checks passed")
