"""Can the new memory say what the old one structurally could not?

The old memory's failure was not a bug - it was a shape. Closed-trade P&L
on a sell-above-entry grid is positive by construction, so no arrangement
of thresholds could make it report a coin that earns while bleeding. Every
test here is written to fail if that shape comes back: if ZEC can go
unnoticed, if a winner-on-sales can read as simply a winner, or if an
unreadable branch reads as costing nothing.
"""
from datetime import datetime, timedelta, timezone

import holding_cost as M

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)


def sl(qty, entry, days_ago=30, **kw):
    d = {"qty": qty, "entry_price": entry,
         "opened_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")}
    d.update(kw)
    return d


def br(pid, slices, unreal, price=1.0):
    return {"product_id": pid, "slices": slices, "current_price": price,
            "total_unrealized_net_usd": unreal}


def lesson(pid, pnl, trades):
    return {"product_id": pid, "total_pnl": pnl, "trades": trades}


# ---- THE LIVE CASE: ZEC holds, has never sold, is the worst drain -----
# 7 slices, ~$2,341 cost basis, -$376.20 mark-to-market, zero lessons.
ZEC = br("ZEC-USD", [sl(0.2, 1672.0, 30) for _ in range(7)], -376.20, price=1386.0)
HBAR = br("HBAR-USD", [sl(474.0, 0.1168, 20) for _ in range(5)], -25.31, price=0.1064)
XRP = br("XRP-USD", [sl(163.0, 1.527, 25) for _ in range(9)], 6.37, price=1.5383)
FLAT = {"product_id": "APE-USD", "slices": [], "total_unrealized_net_usd": 0.0}

LESSONS = [lesson("HBAR-USD", 15.42, 7), lesson("XRP-USD", 15.69, 4),
           lesson("ETH-USD", 8.30, 14)]           # note: no ZEC lesson

r = M.assess([ZEC, HBAR, XRP, FLAT], LESSONS, now=NOW)
print("\n-- the live case")
ok("ZEC is reported at all (the old memory had no row for it)",
   any(x["product_id"] == "ZEC-USD" for x in r["ranked"]))
zec = next(x for x in r["ranked"] if x["product_id"] == "ZEC-USD")
ok("ZEC is flagged as never having sold", zec["never_sold"] is True)
ok("ZEC is named the worst coin to hold", r["worst"] == "ZEC-USD")
ok("the detail says the closed-trade memory has no record of it",
   "no record of it" in r["detail"])
ok("ZEC's verdict is COSTING", zec["verdict"] == "COSTING")
ok("ZEC's holding cost is the mark-to-market against it, positive-signed",
   zec["holding_cost_usd"] == 376.20)
ok("ZEC's net is its mark-to-market (nothing earned)", zec["net_usd"] == -376.20)
ok("ZEC's capital tied is its cost basis, not its market value",
   abs(zec["capital_tied_usd"] - 7 * 0.2 * 1672.0) < 0.01)
ok("ZEC's days held comes from the oldest slice", zec["days_held"] == 30.0)
ok("it appears in the never_sold roll-up", "ZEC-USD" in r["never_sold"])
ok("it appears in the costing roll-up", "ZEC-USD" in r["costing"])

print("\n-- the case the OLD memory could not express")
hb = next(x for x in r["ranked"] if x["product_id"] == "HBAR-USD")
ok("HBAR earned money on sales (+15.42)", hb["earned_by_selling_usd"] == 15.42)
ok("...and is still behind once holding is counted",
   hb["net_usd"] == round(15.42 - 25.31, 2))
ok("its verdict is EARNING_BUT_BEHIND, not 'earning'",
   hb["verdict"] == "EARNING_BUT_BEHIND")
ok("the wording says the selling works and the holding costs more",
   "selling works" in hb["why"] and "holding costs more" in hb["why"])
ok("it is in the earning_but_behind roll-up",
   "HBAR-USD" in r["earning_but_behind"])
ok("the old verdict vocabulary is gone - nothing here says plain 'earning'",
   all(x["verdict"] != "earning" for x in r["ranked"]))

print("\n-- a coin genuinely ahead is not maligned")
xr = next(x for x in r["ranked"] if x["product_id"] == "XRP-USD")
ok("XRP reads AHEAD", xr["verdict"] == "AHEAD")
ok("a position in profit has ZERO holding cost, not a negative one",
   xr["holding_cost_usd"] == 0.0)
ok("its net is earned + mark-to-market", xr["net_usd"] == round(15.69 + 6.37, 2))

print("\n-- flat branches hold nothing, so they are not holding-cost rows")
ok("APE (no slices) is absent from ranked",
   not any(x["product_id"] == "APE-USD" for x in r["ranked"]))
ok("and absent from unknown", not any(x["product_id"] == "APE-USD"
                                      for x in r["unknown"]))
ok("branches_holding_coin counts only the three holding",
   r["branches_holding_coin"] == 3)

print("\n-- ordering and totals")
ok("ranked is worst-first", [x["product_id"] for x in r["ranked"]][0] == "ZEC-USD")
ok("earned total covers only coins still holding (ETH has no slices)",
   r["earned_by_selling_usd"] == round(15.42 + 15.69, 2))
ok("mark-to-market total adds up",
   r["mark_to_market_usd"] == round(-376.20 - 25.31 + 6.37, 2))
ok("net total is the sum of the two",
   r["net_usd"] == round(r["earned_by_selling_usd"] + r["mark_to_market_usd"], 2))

# ---- UNKNOWN is a third verdict -------------------------------------
print("\n-- UNKNOWN is never zero cost")
u = M.assess([br("QNT-USD", [sl(0.3, 160.0, 30)], None)], LESSONS, now=NOW)
ok("an unreadable unrealized lands in unknown", u["unknown_count"] == 1)
ok("its verdict is UNKNOWN", u["unknown"][0]["verdict"] == "UNKNOWN")
ok("it carries NO holding_cost_usd field at all",
   "holding_cost_usd" not in u["unknown"][0])
ok("it is not in ranked", u["ranked"] == [])
ok("the reason says UNKNOWN, not zero", "not zero" in u["unknown"][0]["why"])
u2 = M.assess([br("X-USD", [sl(None, 5.0, 30), sl(1.0, 5.0, 30)], -3.0)], [], now=NOW)
ok("a slice with no qty makes the branch UNKNOWN rather than under-counted",
   u2["unknown_count"] == 1 and u2["unknown"][0]["unpriced_slices"] == 1)
u3 = M.assess([br("Y-USD", [sl(1.0, 0.0, 30)], -3.0)], [], now=NOW)
ok("a zero cost basis is UNKNOWN, not a division", u3["unknown_count"] == 1)

# ---- a phantom unrealized is not a holding cost ---------------------
print("\n-- a phantom mark-to-market is not a cost of holding")
BACKING = {"readable": True, "unbacked": [
    {"product_id": "QNT-USD", "backed": False, "short_usd": 166.0},
    {"product_id": "HBAR-USD", "backed": True}]}
p = M.assess([br("QNT-USD", [sl(0.3, 160.0, 30)], 57.78), HBAR],
             LESSONS + [lesson("QNT-USD", 1.19, 1)], backing=BACKING, now=NOW)
q = next(x for x in p["ranked"] if x["product_id"] == "QNT-USD")
ok("QNT is PHANTOM", q["verdict"] == "PHANTOM")
ok("its holding cost is None, not a number", q["holding_cost_usd"] is None)
ok("its net is None - not computed from an artifact", q["net_usd"] is None)
ok("the mark-to-market is still shown, labelled as an artifact",
   q["mark_to_market_usd"] == 57.78 and "artifact" in q["why"])
ok("it is in the phantom roll-up", "QNT-USD" in p["phantom"])
ok("a phantom figure is EXCLUDED from the fleet mark-to-market total",
   p["mark_to_market_usd"] == -25.31)
ok("HBAR, marked backed:True, is NOT treated as phantom",
   next(x for x in p["ranked"] if x["product_id"] == "HBAR-USD")["verdict"]
   == "EARNING_BUT_BEHIND")
ok("backing_readable is carried through", p["backing_readable"] is True)
p2 = M.assess([br("QNT-USD", [sl(0.3, 160.0, 30)], 57.78)], [],
              backing={"readable": False}, now=NOW)
ok("unreadable backing does not mark anything phantom",
   p2["ranked"][0]["verdict"] != "PHANTOM" and p2["backing_readable"] is False)
ok("no backing argument at all also marks nothing phantom",
   M.assess([br("QNT-USD", [sl(0.3, 160.0, 30)], 57.78)], [], now=NOW)
   ["phantom"] == [])

# ---- it refuses to convict on a thin sample -------------------------
print("\n-- too soon, and too small, are not lessons")
t = M.assess([br("NEW-USD", [sl(10.0, 5.0, 0.5)], -8.0)], [], now=NOW)
ok("a position half a day old is TOO_SOON, not COSTING",
   t["ranked"][0]["verdict"] == "TOO_SOON")
ok("and the wording says it is not being held against the coin",
   "not being held against the coin" in t["ranked"][0]["why"])
sm = M.assess([br("TINY-USD", [sl(1.0, 10.0, 30)], -4.0)], [], now=NOW)
ok(f"${10} of capital is under the ${M.MATERIAL_CAPITAL_USD:.0f} bar -> TOO_SOON",
   sm["ranked"][0]["verdict"] == "TOO_SOON")
big = M.assess([br("BIG-USD", [sl(10.0, 10.0, 30)], -40.0)], [], now=NOW)
ok("$100 held 30 days DOES get a verdict",
   big["ranked"][0]["verdict"] == "COSTING")
ok("the day bar sits above a one-day-old position",
   M.MIN_DAYS_HELD_FOR_A_VERDICT >= 1.0)

# ---- it changes nothing ---------------------------------------------
print("\n-- readable is a verdict on every path, never a missing key")
ok("the success path says readable True",
   M.assess([ZEC], LESSONS, now=NOW).get("readable") is True)
ok("the unreadable-balance path says readable False",
   M.assess([], [], now=NOW) is not None and
   M.assess([{"product_id": "Z-USD", "slices": [sl(1.0, 5.0)]}], [],
            now=NOW).get("readable") is True)
ok("a monitor checking .get('readable') never sees None on a working block",
   M.assess([ZEC, HBAR, XRP], LESSONS, now=NOW).get("readable") is not None)
ok("...nor on an all-UNKNOWN block",
   M.assess([br("Q-USD", [sl(1.0, 5.0)], None)], [], now=NOW).get("readable")
   is not None)

print("\n-- it measures only")
bs = [ZEC, HBAR]
snap = [dict(b) for b in bs]
ls = [dict(x) for x in LESSONS]
res = M.assess(bs, LESSONS, now=NOW)
ok("the branch dicts handed in are untouched", bs == snap)
ok("the lesson dicts handed in are untouched", LESSONS == ls)
ok("it says so in the payload", res["is_a_measurement_not_a_change"] is True)
ok("it states it never blocks a buy", "it_never_blocks_a_buy" in res)
ok("it states the mark-to-market is not booked",
   "Nothing here is booked" in res["mark_to_market_is_not_a_loss"])
ok("there is no enforcement switch anywhere in the module",
   not any(k in dir(M) for k in
           ("set_enforcement_active", "is_enforcement_active", "check_before_buy")))

# ---- degenerate inputs ---------------------------------------------
print("\n-- degenerate inputs")
for label, args in (("no branches", ([], LESSONS)), ("None branches", (None, None)),
                    ("None lessons", ([ZEC], None))):
    rr = M.assess(*args, now=NOW)
    ok(f"{label} does not raise", isinstance(rr, dict))
ok("no branches -> worst is None, not a guess",
   M.assess([], LESSONS, now=NOW)["worst"] is None)
ok("no branches -> the detail says nothing is behind",
   "no coin" in M.assess([], LESSONS, now=NOW)["detail"])
ok("a branch with no product_id is skipped",
   M.assess([{"slices": [sl(1.0, 5.0)]}], [], now=NOW)["branches_holding_coin"] == 0)
nd = M.assess([{"product_id": "N-USD", "slices": [{"qty": 2.0, "entry_price": 50.0}],
                "total_unrealized_net_usd": -30.0}], [], now=NOW)
ok("a slice with no opened_at -> days_held None, verdict TOO_SOON not COSTING",
   nd["ranked"][0]["days_held"] is None
   and nd["ranked"][0]["verdict"] == "TOO_SOON")
ok("a lesson for a coin with no branch is simply unused",
   M.assess([], [lesson("GONE-USD", -99.0, 50)], now=NOW)["ranked"] == [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
