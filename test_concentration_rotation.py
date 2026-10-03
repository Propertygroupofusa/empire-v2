#!/usr/bin/env python3
"""Rotate the over-weight coins out, and never at a loss.

The whole value of this module is one promise: it will not sell a slice
that is down. If that promise fails it is worse than useless, because the
owner's alternative - booking $383.31 today - is at least a known cost.

So the tests are mostly attempts to make it sell something it should not.
"""
import json, sys
import concentration_rotation as R

FAILS = []
def ok(label, cond, detail=""):
    if cond: print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

def branch(pid, alloc, price, entries):
    return {"product_id": pid, "allocated_usd": alloc, "current_price": price,
            "slices": [{"entry_price": e, "qty": q} for e, q in entries]}

# THE COST THE MODULE ACTUALLY USES, not a copy of it. A hardcoded 1.2407
# here is what let the taker-fee bug sit unnoticed: the test agreed with the
# module because both held the same wrong number.
COST = R.DEFAULT_ROUND_TRIP_COST_PCT

print("\n[1] a losing slice is never sellable")
# entry 100, price 99 -> clearly down
b = branch("ZEC-USD", 5000, 99.0, [(100.0, 1.0)])
ok("a slice at -1% is refused", R.sellable_slices(b) == [])
# entry 100, price 101 -> +1% gross, under the round trip
b = branch("ZEC-USD", 5000, 101.0, [(100.0, 1.0)])
ok(f"a +1.00% slice is refused - it does not clear the {COST}% round trip",
   R.sellable_slices(b) == [], str(R.sellable_slices(b)))
# Just past the cost but inside the churn margin.
b = branch("ZEC-USD", 5000, 100.0 * (1 + (COST + 0.05) / 100.0), [(100.0, 1.0)])
ok("a slice clearing the cost but not the margin is refused",
   R.sellable_slices(b) == [], str(R.sellable_slices(b)))
# Comfortably past cost + margin.
b = branch("ZEC-USD", 5000, 102.0, [(100.0, 1.0)])
s = R.sellable_slices(b)
ok("a +2.00% slice IS sellable", len(s) == 1, str(s))
ok("...and its net is reported after the round trip",
   abs(s[0]["net_pct"] - (2.0 - COST)) < 1e-6, str(s[0]["net_pct"]))
ok("...and the profit is positive", s[0]["profit_usd"] > 0)

print("\n[1b] THE SELL LEG IS A TAKER LEG - the bug this cost real money on")
ok("the exit fee is the TAKER rate, not the maker rate",
   R.EXIT_FEE_PCT_DEFAULT == 0.75, R.EXIT_FEE_PCT_DEFAULT)
ok("the entry fee is the maker rate the grid's rungs are bought at",
   R.ENTRY_FEE_PCT_DEFAULT == 0.35, R.ENTRY_FEE_PCT_DEFAULT)
ok("so the fee half of the round trip is 1.10%, not 0.70%",
   abs((R.ENTRY_FEE_PCT_DEFAULT + R.EXIT_FEE_PCT_DEFAULT) - 1.10) < 1e-9)
ok("and the whole round trip is 1.6407%, not the old 1.2407%",
   abs(COST - 1.6407) < 1e-9, COST)
# The exact slice the old model would have sold at a loss.
_edge = 100.0 * (1 + (1.2407 + 0.26) / 100.0)      # +0.26% net under the OLD cost
b = branch("ZEC-USD", 5000, _edge, [(100.0, 1.0)])
ok("a slice the OLD model called a +0.26% winner is now refused",
   R.sellable_slices(b) == [], str(R.sellable_slices(b)))
ok("...because its real net is negative", R.exit_net_pct(100.0, _edge) < 0,
   R.exit_net_pct(100.0, _edge))

print("\n[1c] a slice's own recorded entry fee is used, and never to loosen the bar")
# entry_fee_rate is a FRACTION. A pricier recorded buy raises the bar.
# +3% gross, so BOTH the default and the dearer buy leg stay above the
# 0.25% margin and the comparison is about the net, not about the cutoff.
# At +2% the dearer leg correctly falls under the margin and returns [],
# which is right behaviour and a useless fixture for this question.
b = branch("ZEC-USD", 5000, 103.0, [(100.0, 1.0)])
b["slices"][0]["entry_fee_rate"] = 0.0060          # 0.60% buy leg, dearer than default
s_dear = R.sellable_slices(b)
base = R.exit_net_pct(100.0, 103.0)
ok("a dearer recorded buy leg lowers the reported net",
   bool(s_dear) and s_dear[0]["net_pct"] < base, str(s_dear))
ok("...by exactly the extra fee it recorded",
   bool(s_dear) and abs((base - s_dear[0]["net_pct"]) - 0.25) < 1e-6,
   str(base - s_dear[0]["net_pct"]) if s_dear else "[]")
b["slices"][0]["entry_fee_rate"] = 0.0             # a free buy leg cannot help
s_free = R.sellable_slices(b)
ok("a zero recorded fee does NOT make the bar easier",
   s_free and abs(s_free[0]["net_pct"] - base) < 1e-9, str(s_free))
b["slices"][0]["entry_fee_rate"] = None            # unknown is not free
s_unk = R.sellable_slices(b)
ok("an unknown recorded fee falls back to the default, not to zero",
   s_unk and abs(s_unk[0]["net_pct"] - base) < 1e-9, str(s_unk))

print("\n[2] every sellable slice must be profitable, never just the basket")
# A basket that is net positive overall but holds one loser.
b = branch("ZEC-USD", 5000, 102.0, [(100.0, 10.0), (130.0, 1.0)])
s = R.sellable_slices(b)
ok("the losing slice is excluded even though the basket is up",
   len(s) == 1 and s[0]["entry_price"] == 100.0,
   json.dumps(s))
ok("no returned slice has a negative net", all(x["net_pct"] > 0 for x in s))

print("\n[3] unreadable prices produce no plan, never a zero one")
for bad in (None, "", "abc", float("nan")):
    b = branch("ZEC-USD", 5000, bad, [(100.0, 1.0)])
    ok(f"price {bad!r} -> no sellable slices", R.sellable_slices(b) == [])
ok("exit_net_pct is None on an unreadable price",
   R.exit_net_pct(100.0, None) is None)
ok("...and on a zero entry price", R.exit_net_pct(0, 100.0) is None)

print("\n[4] only branches OVER the limit are touched")
fleet = [branch("ZEC-USD", 2600, 200.0, [(100.0, 1.0)]),   # 26% - over
         branch("XLM-USD", 1000, 200.0, [(100.0, 1.0)]),   # 10% - under
         branch("ETH-USD", 6400, 200.0, [(100.0, 1.0)])]   # 64% - over
over = {o["product_id"] for o in R.over_limit(fleet)}
ok("the 10% branch is left alone", "XLM-USD" not in over, str(over))
ok("both over-weight branches are picked up",
   over == {"ZEC-USD", "ETH-USD"}, str(over))
p = R.plan(fleet, [{"coin":"DOGE","edge_pct":2.4,"trades":15,"allocated_usd":0}])
sold = {m["product_id"] for m in p["moves"]}
ok("the plan never names the under-weight branch", "XLM-USD" not in sold, str(sold))

print("\n[5] an unreadable fleet total means nothing is 'over'")
ok("no allocations -> no over-limit branches",
   R.over_limit([{"product_id": "A-USD"}]) == [])
ok("a zero fleet total -> no over-limit branches",
   R.over_limit([branch("A-USD", 0, 1.0, [])]) == [])

print("\n[6] the destination has to earn it")
base = {"allocated_usd": 0.0}
cases = [
    ({"coin":"X","edge_pct":5.0,"trades":4}, "a 4-trade sample is refused"),
    ({"coin":"X","edge_pct":-1.0,"trades":50}, "a negative edge is refused"),
    ({"coin":"X","edge_pct":None,"trades":50}, "an unreadable edge is refused"),
]
for c, label in cases:
    ok(label, R.next_destination([dict(base, **c)]) is None)
ok("a real candidate is accepted",
   (R.next_destination([{"coin":"DOGE","edge_pct":2.4,"trades":15,
                         "allocated_usd":0.0}]) or {}).get("coin") == "DOGE")
ok("the highest edge wins",
   R.next_destination([{"coin":"A","edge_pct":1.0,"trades":9,"allocated_usd":0},
                       {"coin":"B","edge_pct":3.0,"trades":9,"allocated_usd":0}]
                      )["coin"] == "B")
ok("a coin already at the limit is refused",
   R.next_destination([{"coin":"A","edge_pct":9.0,"trades":9,
                        "allocated_usd":2500}], fleet_total_usd=10000) is None)
ok("the coin being SOLD is never the destination",
   R.next_destination([{"coin":"ZEC","edge_pct":9.0,"trades":9,
                        "allocated_usd":0}], taken=["ZEC"]) is None)

print("\n[7] the real fleet today: both over the limit, both WAIT")
g = json.load(open('/tmp/gsr.json'))
p = R.plan(g["branches"],
           [{"coin":"HBAR","edge_pct":4.197,"trades":7,"allocated_usd":332.71},
            {"coin":"DOGE","edge_pct":2.414,"trades":15,"allocated_usd":0.0}])
ok("ZEC and XRP are the over-limit pair",
   {o["product_id"] for o in p["over_limit"]} == {"ZEC-USD","XRP-USD"},
   str(p["over_limit"]))
ok("every move is WAIT", all(m["action"] == "WAIT" for m in p["moves"]))
ok("nothing is freed", p["frees_usd"] == 0.0)
ok("nothing is booked", p["profit_booked_usd"] == 0.0)
ok("no destination is chosen when nothing was freed", p["destination"] is None)

print("\n[8] it fires once price recovers - the point of waiting")
# XRP's worst slice is entry 1.5383. Lift price until it clears.
g2 = json.loads(json.dumps(g))
xrp = next(b for b in g2["branches"] if b["product_id"] == "XRP-USD")
xrp["current_price"] = 1.5383 * (1 + (COST + 1.0)/100)   # ~+2.24% on the worst
p2 = R.plan(g2["branches"],
            [{"coin":"DOGE","edge_pct":2.414,"trades":15,"allocated_usd":0.0}])
m = next(x for x in p2["moves"] if x["product_id"] == "XRP-USD")
ok("XRP now SELLS", m["action"] == "SELL", m["action"])
ok("...every slice it sells is profitable",
   all(s["net_pct"] > 0 for s in m["sell"]), json.dumps(m["sell"])[:200])
ok("...it frees real capital", p2["frees_usd"] > 0, str(p2["frees_usd"]))
ok("...booking a PROFIT, not a loss", p2["profit_booked_usd"] > 0)
ok("...and a destination is chosen",
   (p2["destination"] or {}).get("coin") == "DOGE", str(p2["destination"]))
zec = next(x for x in p2["moves"] if x["product_id"] == "ZEC-USD")
ok("ZEC still waits - it has not recovered", zec["action"] == "WAIT")

print("\n[9] it cannot place an order")
src = open("concentration_rotation.py").read()
import re
code = re.sub(r'""".*?"""', "", src, flags=re.S)
code = re.sub(r"#.*", "", code)
for bad in ("requests", "aiohttp", "urllib", "place_order", "submit",
            "session.post", "await ", "async "):
    ok(f"no {bad!r} in the executable code", bad not in code)
ok("and the plan says so", R.plan([], [])["is_a_plan_not_an_order"] is True)

print("\n[10] the owner's limit is not lowered here")
ok("the concentration limit is 20.0", R.CONCENTRATION_LIMIT_PCT == 20.0)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
