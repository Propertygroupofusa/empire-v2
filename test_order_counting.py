"""Fills are not orders, and halving fills does not give round trips.

The 2026-09-26 audit reported that three quarters of real executions never
reached the ledger. It got there by taking 1,944 fills, halving them to
972 "implied round trips", and comparing that against 249 recorded. Two
things were wrong with that denominator:

  1. It counted NON-SPOT fills. 932 of the 1,944 were Kalshi event
     contracts, which no bot here has ever traded.
  2. It counted FILLS. One order can fill in many pieces, so fills/2
     invents activity that never happened.

An order is what a bot places and what a ledger row represents. These
tests pin that distinction.
"""

import crypto_btc_compound_bot as M

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def fill(oid, pid="X-USD", side="BUY", size="1", price="10", comm="0.1"):
    return {"order_id": oid, "product_id": pid, "side": side,
            "size": size, "price": price, "commission": comm}


print("\none order that fills in pieces is ONE order")

r = M.summarise_fills([fill("A"), fill("A"), fill("A"),
                       fill("B", side="SELL", size="3", price="11", comm="0.3")])
ok("four fills are counted as four", r["fills"] == 4, r["fills"])
ok("but only two orders", r["orders"] == 2, r["orders"])
ok("which is ONE round trip, not two", r["orders"] // 2 == 1)
ok("the per-product count agrees", r["products"][0]["orders"] == 2, r["products"][0])

print("\nthe order count is a count, not a set, so it serialises")

import json
json.dumps(r)          # raises if a set leaked through
ok("the whole statement is JSON-serialisable", True)
ok("orders is an int", isinstance(r["orders"], int))
ok("per-product orders is an int", isinstance(r["products"][0]["orders"], int))

print("\nfills without an order id do not inflate the order count")

r = M.summarise_fills([fill(None), fill(None), fill("Z")])
ok("three fills", r["fills"] == 3, r["fills"])
ok("but one identifiable order", r["orders"] == 1, r["orders"])

print("\nthe two denominators give very different answers")

# The live shape: heavy partial filling on one product.
many = [fill("O%d" % (i // 5), pid="POL-USD") for i in range(315)]
r = M.summarise_fills(many)
ok("315 fills", r["fills"] == 315)
ok("but 63 orders", r["orders"] == 63, r["orders"])
ok("fills/2 would have claimed 157 round trips",
   315 // 2 == 157)
ok("orders/2 says 31 - a fifth of that",
   r["orders"] // 2 == 31, r["orders"] // 2)

print("\nnothing else about the statement changed")

r = M.summarise_fills([fill("A", side="BUY", size="2", price="100", comm="1"),
                       fill("B", side="SELL", size="2", price="110", comm="1")])
ok("bought is size x price", r["bought_usd"] == 200.0, r["bought_usd"])
ok("sold is size x price", r["sold_usd"] == 220.0, r["sold_usd"])
ok("commission sums", r["commission_usd"] == 2.0, r["commission_usd"])
ok("net cash flow is sold minus bought minus commission",
   r["net_cash_flow_usd"] == 18.0, r["net_cash_flow_usd"])

q = M.summarise_fills([{"order_id": "Q", "product_id": "X-USD", "side": "BUY",
                        "size": "50", "price": "10", "commission": "0",
                        "size_in_quote": True}])
ok("size_in_quote still means size IS the dollars",
   q["bought_usd"] == 50.0, q["bought_usd"])
ok("and is still counted", q["quote_sized_fills"] == 1)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
