"""A fill joined to no attribution row is UNKNOWN, never a caller.

WHY IT EXISTS. OrderAttribution was written from 2026-09-28 and read by
NOTHING - no endpoint performed the join it was built for. That is a fact
recorded faithfully and surfaced to no one, the same defect being fixed a
layer down all week.

WHAT THESE CHECKS PIN. The join is exact where a row exists, and the whole
risk is in what happens where one does not. Three different absences look
identical in the data and mean opposite things:

  * a post-only MAKER order, which never passes through the market-order
    path that writes attribution - expected, says nothing;
  * an order that filled before the first row was ever written - expected,
    could not have been tagged;
  * a TAKER order after tagging began with no row - a caller nobody can
    name, paying real commission. The only finding.

Collapsing those into one "untagged" count would manufacture a problem out
of orders that were never eligible. The cutover that separates them is READ
FROM THE TABLE, never hardcoded, and when it is unknown nothing is accused.

Also pinned: size_in_quote. Coinbase denominates `size` in USD for a
dollar-sized market order, and multiplying that by price once reported
$96,544,199.94 of BTC on a $1,000 account.
"""
import ast
import sys

import fills_attribution as fa

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def fill(oid, side="BUY", size=1.0, price=100.0, comm=0.1, liq="TAKER",
         pid="ETH-USD", t="2026-09-29T02:00:00Z", in_quote=False):
    return {"order_id": oid, "side": side, "size": str(size), "price": str(price),
            "commission": str(comm), "liquidity_indicator": liq,
            "product_id": pid, "trade_time": t, "size_in_quote": in_quote}


CUT = "2026-09-28T00:00:00Z"

print("== orders, not fills ==")
# One order filling in three pieces is ONE order. Counting fills is how
# "three quarters of executions went unrecorded" was once claimed.
r = fa.classify([fill("A"), fill("A"), fill("A")], {"A": "grid"}, CUT)
ok("three fills of one order count as one order", r["orders"] == 1, str(r["orders"]))
ok("but the fills are still counted", r["by_source"][0]["fills"] == 3)
ok("and the commission is summed across them",
   abs(r["by_source"][0]["commission_usd"] - 0.3) < 1e-9,
   str(r["by_source"][0]["commission_usd"]))

print("== size_in_quote is not a base quantity ==")
# 1175 "BTC" that were really 1175 dollars.
r = fa.classify([fill("Q", size=1175.0, price=82166.0, in_quote=True)], {"Q": "grid"}, CUT)
ok("a quote-sized fill counts its dollars once",
   abs(r["by_source"][0]["notional_usd"] - 1175.0) < 1e-6,
   str(r["by_source"][0]["notional_usd"]))
r = fa.classify([fill("B", size=2.0, price=50.0)], {"B": "grid"}, CUT)
ok("a base-sized fill is multiplied by price",
   abs(r["by_source"][0]["notional_usd"] - 100.0) < 1e-6)

print("== unattributed is a bucket, never a source ==")
r = fa.classify([fill("U")], {}, CUT)
ok("an unjoined order lands in the unattributed bucket",
   [b["source"] for b in r["by_source"]] == [fa.UNATTRIBUTED])
ok("it is counted separately from attributed orders",
   r["attributed_orders"] == 0 and r["unattributed_orders"] == 1)
ok("and the bucket is explained as UNKNOWN",
   "UNKNOWN" in r["what_unattributed_means"])

print("== only one kind of absence is a finding ==")
# MAKER with no row: expected, because post-only never reaches the path
# that writes attribution.
r = fa.classify([fill("M", liq="MAKER")], {}, CUT)
ok("a maker order with no row is not called untagged",
   r["untagged_taker_order_count"] == 0, str(r["untagged_taker_orders"]))
# TAKER before the cutover: could not have been tagged.
r = fa.classify([fill("T0", liq="TAKER", t="2026-09-27T10:00:00Z")], {}, CUT)
ok("a taker order predating the cutover is not called untagged",
   r["untagged_taker_order_count"] == 0)
# A fill exactly AT the cutover is not after it.
r = fa.classify([fill("TE", liq="TAKER", t=CUT)], {}, CUT)
ok("a taker order exactly at the cutover is not called untagged",
   r["untagged_taker_order_count"] == 0)
# TAKER after the cutover with no row: the real finding.
r = fa.classify([fill("T1", liq="TAKER", t="2026-09-29T02:00:00Z", comm=0.5)], {}, CUT)
ok("a taker order after the cutover with no row IS the finding",
   r["untagged_taker_order_count"] == 1)
ok("and it carries its commission so the cost is visible",
   abs(r["untagged_taker_orders"][0]["commission_usd"] - 0.5) < 1e-9)
# The cutover unknown: nothing may be accused.
r = fa.classify([fill("T2", liq="TAKER", t="2026-09-29T02:00:00Z")], {}, None)
ok("with no cutover known, NOTHING is called untagged",
   r["untagged_taker_order_count"] == 0,
   "absence of attribution cannot indict a caller when nothing was attributed")
# An attributed taker order is never a finding whatever its timestamp.
r = fa.classify([fill("T3", liq="TAKER", t="2026-09-29T02:00:00Z")], {"T3": "grid"}, CUT)
ok("an attributed taker order is never a finding", r["untagged_taker_order_count"] == 0)

print("== liquidity that cannot be read is its own bucket ==")
r = fa.classify([fill("X", liq="")], {}, CUT)
b = r["by_source"][0]
ok("an unreadable liquidity is not counted as maker",
   b["maker_fills"] == 0 and b["unknown_liquidity_fills"] == 1)
ok("nor as taker", b["taker_fills"] == 0)
ok("and it is therefore not accused of being untagged",
   r["untagged_taker_order_count"] == 0,
   "a taker fill must be CONFIRMED before its absence of a row is a finding")

print("== a fill with no order id can never be joined ==")
r = fa.classify([{"order_id": None, "size": "1", "price": "1", "commission": "0"}], {}, CUT)
ok("it is counted, not silently dropped", r["fills_without_an_order_id"] == 1)
ok("and it does not invent an order", r["orders"] == 0)

print("== truncation makes every count a floor ==")
r = fa.classify([fill("A")], {"A": "grid"}, CUT, truncated=True)
ok("truncation is carried through", r["truncated"] is True)
ok("and is named as making the counts floors", r["counts_are_floors"] is True)
r = fa.classify([fill("A")], {"A": "grid"}, CUT, truncated=False)
ok("an untruncated window does not claim to be a floor", r["counts_are_floors"] is False)

print("== the buckets add up ==")
r = fa.classify([fill("A"), fill("B"), fill("C", liq="MAKER")],
                {"A": "grid", "B": "market_brain"}, CUT)
ok("attributed + unattributed equals total orders",
   r["attributed_orders"] + r["unattributed_orders"] == r["orders"] == 3)
ok("each source keeps its own bucket",
   sorted(b["source"] for b in r["by_source"]) == ["grid", "market_brain", fa.UNATTRIBUTED])
ok("sources are ordered by commission, costliest first",
   [b["commission_usd"] for b in r["by_source"]]
   == sorted((b["commission_usd"] for b in r["by_source"]), reverse=True))
ok("the attributed percentage uses orders as its denominator",
   abs(r["attributed_pct_of_orders"] - (2 / 3 * 100)) < 0.01,
   str(r["attributed_pct_of_orders"]))
r = fa.classify([], {}, CUT)
ok("an empty window reports no percentage rather than 0%",
   r["attributed_pct_of_orders"] is None,
   "0% attributed and nothing to attribute are different facts")

print("== the endpoint fails closed on an unreadable exchange ==")
# fastapi is not installed here, so the route is read from its own AST
# rather than called. Anchored to the function's OWN node - a substring
# search over a 10k-line router proves almost nothing.
SRC = open("routers/trading_dashboard.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
fn = next((n for n in ast.walk(TREE)
           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
           and n.name == "fills_by_source"), None)
ok("the route function exists", fn is not None)
if fn is None:
    print("\nFAILED: cannot inspect the route")
    sys.exit(1)
SEG = ast.get_source_segment(SRC, fn) or ""

ok("it is registered on the router",
   any(isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "get"
       and d.args and isinstance(d.args[0], ast.Constant)
       and d.args[0].value == "/fills-by-source"
       for d in fn.decorator_list),
   "expected @router.get('/fills-by-source')")
# The key refusal. An unreadable fills feed must not produce empty buckets,
# which would render as "nobody traded" - the most misleading thing this
# endpoint could say.
ok("an unavailable fills feed returns readable:false",
   '"readable": False' in SEG and 'raw.get("available")' in SEG)
ok("and says UNKNOWN rather than zero",
   "do not read this as 'no " in SEG)
ok("a fetch exception is called a GAP, not an empty result",
   "This is a GAP" in SEG)

print("== an unreadable attribution table is not a finding about callers ==")
# Empty and unreadable produce IDENTICAL buckets and mean opposite things.
ok("a database failure is named, not swallowed",
   "attribution_table_error" in SEG and "attribution_is_unreadable" in SEG)
ok("and it rewrites the unattributed explanation",
   "attr_error is not None" in SEG
   and "Nothing here is evidence about any caller." in SEG)
ok("the attribution map is cleared when the read failed, not left partial",
   "attribution, started_at = {}, None" in SEG,
   "a half-read map would attribute some orders and silently orphan the rest")

print("== the cutover is derived, never hardcoded ==")
ok("it comes from the table's own oldest row",
   "func.min(OrderAttribution.placed_at)" in SEG)
_lits = [n.value for n in ast.walk(fn)
         if isinstance(n, ast.Constant) and isinstance(n.value, str)
         and n.value.startswith("2026-") and len(n.value) > 8]
ok("no ISO timestamp literal is used as the cutover", not _lits, str(_lits))

print("== the order-id query is chunked ==")
ok("the IN() is chunked so a large window cannot fail as 'nothing attributed'",
   "range(0, len(order_ids), 400)" in SEG)

print("== bounds ==")
ok("hours is bounded", "min(int(hours), 720)" in SEG)
ok("max_pages is bounded", "min(int(max_pages), 40)" in SEG)
ok("a bad hours value degrades to a default rather than raising",
   "except (TypeError, ValueError)" in SEG)
ok("the endpoint places no order",
   not any(w in SEG for w in ("place_market", "place_maker", "_place_and_confirm",
                              "session.post", "cancel")))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all fills-by-source checks passed")
