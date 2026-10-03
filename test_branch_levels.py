"""Does changing a level count report what it actually buys?

The failure this is written against is a quiet one: raising num_levels on a
branch with no spare allocation gives it room it cannot use, and a report
that calls that READY and stops has told the owner a change worked when it
bought nothing. NEAR-USD is exactly that case live - $1.67 unspent against
a $30.55 slice - so every "it buys nothing" assertion below is about real
money, not a hypothetical.
"""
import branch_levels as B

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def sl(qty, entry):
    return {"qty": qty, "entry_price": entry}


def br(pid, alloc, levels, slices, bot="b"):
    return {"product_id": pid, "bot_name": bot, "allocated_usd": alloc,
            "num_levels": levels, "slices": slices}


# ---- the live LINK case: room AND money -----------------------------
LINK = br("LINK-USD", 137.87, 3, [sl(2.0, 15.175), sl(2.0, 15.175), sl(2.0, 15.175)])
r = B.plan(LINK, 6)
print("\n-- LINK: has spare allocation, so levels are the constraint")
ok("it is ready", r["ok"] is True and r["status"] == "READY")
ok("slice size halves: 45.96 -> 22.98",
   r["slice_usd_before"] == 45.96 and r["slice_usd_after"] == 22.98)
ok("it was parked before", r["parked_before"] is True)
ok("it is not parked after", r["parked_after"] is False)
ok("it can open 2 rungs on $46.82 spare",
   r["unspent_usd"] == 46.82 and r["rungs_it_could_actually_open"] == 2)
ok("the new count permits 3 but the money affords 2 - the smaller wins",
   r["rungs_the_new_count_permits"] == 3 and r["rungs_the_money_affords"] == 2)
ok("it states no order is placed", r["places_no_order"] is True)
ok("it states only num_levels changes", r["changes_only_num_levels"] is True)
ok("the detail names the real rung count", "2 more rung" in r["detail"])

# ---- the live NEAR case: room WITHOUT money -------------------------
NEAR = br("NEAR-USD", 183.30, 3, [sl(10.0, 6.0543), sl(10.0, 6.0543), sl(10.0, 6.0543)])
r2 = B.plan(NEAR, 6)
print("\n-- NEAR: no spare allocation, so levels are NOT the constraint")
ok("it still reports READY (the change is legal)", r2["status"] == "READY")
ok("but it says it can open ZERO rungs",
   r2["rungs_it_could_actually_open"] == 0)
ok("and it is STILL parked after", r2["parked_after"] is True)
ok("it flags that the change buys nothing on its own",
   r2.get("buys_nothing_without_more_allocation") is True)
ok("the wording says the money becomes the constraint",
   "the money becomes it" in r2["detail"])
ok("it does NOT claim the branch can now buy",
   "can open" not in r2["detail"].split("so it still")[0])

# ---- refusals -------------------------------------------------------
print("\n-- what it refuses")
ok("below the open slice count",
   B.plan(LINK, 2)["status"] == "REFUSED")
ok("...and says why - more rungs than the config permits",
   "more rungs than its own" in B.plan(LINK, 2)["detail"])
ok("exactly the current count is NO_CHANGE",
   B.plan(LINK, 3)["status"] == "NO_CHANGE")
ok("zero levels", B.plan(LINK, 0)["status"] == "REFUSED")
ok("above the cap", B.plan(LINK, B.MAX_LEVELS + 1)["status"] == "REFUSED")
ok("at the cap is allowed", B.plan(br("X-USD", 2000.0, 3, []), B.MAX_LEVELS)["ok"] is True)
ok("not a number", B.plan(LINK, "six")["status"] == "REFUSED")
ok("a float that is not whole still parses as int(6.0)",
   B.plan(LINK, 6.0)["levels_after"] == 6)
ok("no branch at all", B.plan(None, 6)["status"] == "REFUSED")
ok("a branch with no product_id", B.plan({"num_levels": 3}, 6)["status"] == "REFUSED")
ok("no allocation -> refused, a level count changes nothing",
   B.plan(br("Z-USD", 0.0, 3, []), 6)["status"] == "REFUSED")

print("\n-- a slice too small to place is refused, not shipped")
tiny = br("T-USD", 30.0, 3, [])
r3 = B.plan(tiny, 10)
ok("$30 over 10 levels is a $3.00 slice -> REFUSED",
   r3["status"] == "REFUSED" and r3["ok"] is False)
ok("the reason names the minimum",
   f"${B.MIN_SLICE_USD:,.2f}" in r3["detail"])
ok("$30 over 5 levels is $6.00 -> allowed", B.plan(tiny, 5)["ok"] is True)

print("\n-- UNKNOWN is a third verdict")
bad = br("U-USD", 100.0, 3, [sl(None, 5.0), sl(1.0, 5.0)])
r4 = B.plan(bad, 6)
ok("a slice with no price makes the whole plan UNKNOWN",
   r4["status"] == "UNKNOWN" and r4["ok"] is False)
ok("it counts the unpriced slices", r4["unpriced_slices"] == 1)
ok("it says UNKNOWN not zero", "not zero" in r4["detail"])
ok("it quotes no rung count it cannot support",
   "rungs_it_could_actually_open" not in r4)

print("\n-- it changes nothing")
snap = dict(LINK); snap["slices"] = list(LINK["slices"])
B.plan(LINK, 6)
ok("the branch dict handed in is untouched",
   LINK["num_levels"] == 3 and LINK["allocated_usd"] == 137.87
   and len(LINK["slices"]) == 3)
ok("there is no apply/write function in this module",
   not any(n in dir(B) for n in ("apply", "set_levels", "write", "commit")))

print("\n-- plan_many")
m = B.plan_many([LINK, NEAR], {"LINK-USD": 6, "NEAR-USD": 6, "GONE-USD": 5})
ok("both live branches planned", len(m["plans"]) == 2)
ok("the missing one is named, not silently dropped", m["missing"] == ["GONE-USD"])
ok("ready lists LINK and NEAR (both legal changes)",
   set(m["ready"]) == {"LINK-USD", "NEAR-USD"})
ok("it says it is a plan, not a change", m["is_a_plan_not_a_change"] is True)
m2 = B.plan_many([LINK], {"LINK-USD": 2})
ok("a refused plan lands in refused", m2["refused"] == ["LINK-USD"])
ok("empty input does not raise", B.plan_many([], {})["plans"] == [])
ok("None input does not raise", B.plan_many(None, None)["plans"] == [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
