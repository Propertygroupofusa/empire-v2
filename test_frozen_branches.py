"""FROZEN: a branch that can neither buy nor sell, and the alert for it.

THE GAP THIS CLOSES. Ten branches holding 72.8% of the fleet's allocation
sat parked across nine consecutive silent watch checks. Nothing errored,
nothing alerted, because the question "can this branch still trade?" was
never asked. Every individual fact about them was fine.

PARKED IS NOT FROZEN, and conflating them would make the alarm useless:
a full branch waiting on a reachable price is the design working.
"""
import sys
sys.path.insert(0, "/home/user/empire-v2")
import frozen_branches as FB
import alert_queue

fail = 0
def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or not detail else f"   -> {detail}"))
    if not cond: fail += 1

def br(pid, slices, levels, price, entry, alloc=500.0, step=0.03):
    return {"product_id": pid, "num_levels": levels, "allocated_usd": alloc,
            "current_price": price, "grid_pct": step,
            "slices": [{"qty": 1.0, "entry_price": entry} for _ in range(slices)]}

print("\n[1] room to buy is OK, whatever the price")
r = FB.assess_branch(br("A-USD", 2, 5, 10.0, 100.0))
ok("a branch with spare rungs is OK", r["verdict"] == "OK", r)
ok("and it reports it can buy", r["can_buy"] is True)

print("\n[2] full but the trigger is within reach -> PARKED, not FROZEN")
# entry 10, step 3% -> trigger 10.3; price 10.0 -> 3.0% away
r = FB.assess_branch(br("B-USD", 3, 3, 10.0, 10.0))
ok("full with a reachable trigger is PARKED", r["verdict"] == "PARKED", r)
ok("it cannot buy", r["can_buy"] is False)
ok("the gap is reported", abs(r["pct_to_nearest_trigger"] - 3.0) < 0.01, r)
ok("the wording calls it a wait, not a wall", "a wait, not a wall" in r["detail"], r)

print("\n[3] full and the trigger is far away -> FROZEN")
# entry 10, step 3% -> trigger 10.3; price 8.0 -> 28.75% away
r = FB.assess_branch(br("C-USD", 3, 3, 8.0, 10.0))
ok("full with an unreachable trigger is FROZEN", r["verdict"] == "FROZEN", r)
ok("it says the money is doing nothing in either direction",
   "either direction" in r["detail"], r)
ok("it does NOT claim a loss", "loss" not in r["detail"].lower(), r)

print("\n[4] the boundary is the threshold, not a guess")
near = FB.assess_branch(br("D-USD", 1, 1, 10.0, 10.0, step=FB.FAR_FROM_TRIGGER_PCT/100 - 0.001))
far  = FB.assess_branch(br("E-USD", 1, 1, 10.0, 10.0, step=FB.FAR_FROM_TRIGGER_PCT/100 + 0.001))
ok(f"just inside {FB.FAR_FROM_TRIGGER_PCT}% is PARKED", near["verdict"] == "PARKED", near)
ok(f"just outside {FB.FAR_FROM_TRIGGER_PCT}% is FROZEN", far["verdict"] == "FROZEN", far)

print("\n[5] small money does not raise an alarm")
r = FB.assess_branch(br("F-USD", 3, 3, 8.0, 10.0, alloc=FB.MATERIAL_USD - 0.01))
ok("an immaterial branch is flagged separately", r["verdict"] == "FROZEN_IMMATERIAL", r)
r = FB.assess_branch(br("G-USD", 3, 3, 8.0, 10.0, alloc=FB.MATERIAL_USD))
ok("at the floor it is FROZEN", r["verdict"] == "FROZEN", r)

print("\n[6] UNKNOWN IS A THIRD VERDICT")
b = br("H-USD", 3, 3, 8.0, 10.0); b["current_price"] = None
ok("no price -> UNKNOWN, not OK", FB.assess_branch(b)["verdict"] == "UNKNOWN")
b = br("I-USD", 3, 3, 8.0, 10.0); b["grid_pct"] = None
ok("no step -> UNKNOWN", FB.assess_branch(b)["verdict"] == "UNKNOWN")
b = br("J-USD", 3, 3, 8.0, 10.0); b["num_levels"] = 0
ok("no level count -> UNKNOWN", FB.assess_branch(b)["verdict"] == "UNKNOWN")
b = br("K-USD", 2, 3, 8.0, 10.0)
for s in b["slices"]: s["entry_price"] = None
b["num_levels"] = 2
ok("slices with no entry price -> UNKNOWN", FB.assess_branch(b)["verdict"] == "UNKNOWN")
# The property that matters is not a magic phrase - it is that an
# unreadable branch never reads as a healthy one. Assert THAT.
for _b in (
    dict(br("L-USD", 3, 3, 8.0, 10.0), current_price=None),
    dict(br("M-USD", 3, 3, 8.0, 10.0), grid_pct=None),
    dict(br("N-USD", 3, 3, 8.0, 10.0), num_levels=0),
    b,
):
    _r = FB.assess_branch(_b)
    _d = (_r.get("detail") or "").lower()
    # STRUCTURE, NOT PROSE. The first version of this grepped the detail
    # for "is fine" and failed on the string "- not that it is fine",
    # which is the module explicitly DENYING fineness. A substring match
    # cannot see a negation, so assert on the fields a caller acts on.
    ok(f"{_b['product_id']}: unreadable never reads as healthy",
       _r["verdict"] == "UNKNOWN"
       and _r.get("can_sell_now") is not True
       and _r.get("verdict") not in ("OK", "PARKED"),
       _r)
    ok(f"{_b['product_id']}: and it names what could not be read",
       any(w in _d for w in ("could not", "no readable", "no level count")),
       _r.get("detail"))

print("\n[7] the roll-up counts only what it is sure of")
rows = [br("X-USD", 3, 3, 8.0, 10.0, alloc=1000.0),      # FROZEN
        br("Y-USD", 3, 3, 10.0, 10.0, alloc=500.0),      # PARKED
        br("Z-USD", 1, 5, 10.0, 10.0, alloc=500.0)]      # OK
rows[1]["current_price"] = 10.0
blind = br("W-USD", 3, 3, 8.0, 10.0, alloc=9999.0); blind["current_price"] = None
a = FB.assess(rows + [blind])
ok("one frozen", a["frozen_count"] == 1, a["frozen_count"])
ok("its dollars are counted", a["frozen_usd"] == 1000.0, a["frozen_usd"])
ok("the unreadable branch is NOT counted as frozen", a["frozen_usd"] == 1000.0)
ok("but it IS counted as unknown", a["unknown_count"] == 1, a["unknown_count"])
ok("parked is counted separately from frozen", a["parked_count"] == 1, a["parked_count"])
ok("it is a measurement", a["is_a_measurement_not_a_change"] is True)
ok("readable is True on a working block", a["readable"] is True)

print("\n[8] the alert fires on the TRANSITION only")
W = lambda **k: {"rows": [], **k}
f = lambda asset, verdict, usd=500.0, gap=28.75: {
    "asset": asset, "verdict": verdict, "allocated_usd": usd,
    "pct_to_nearest_trigger": gap}
out = alert_queue.plan(W(frozen=[f("ZEC", "FROZEN")]), {})
ok("becoming frozen alerts", len(out) == 1 and out[0]["kind"] == "FROZEN", out)
ok("it is HIGH, not CRITICAL - nothing is being lost",
   out and out[0]["severity"] == alert_queue.HIGH)
ok("it names the idle money", out and "$500.00 idle" in out[0]["message"], out)
ok("it says nothing is wrong and nothing lost",
   out and "nothing has been lost" in out[0]["detail"], out)
st = alert_queue.next_state(W(frozen=[f("ZEC", "FROZEN")]))
ok("still frozen is silent", alert_queue.plan(W(frozen=[f("ZEC", "FROZEN")]), st) == [])
out = alert_queue.plan(W(frozen=[f("ZEC", "PARKED", gap=2.0)]), st)
ok("thawing alerts", len(out) == 1 and out[0]["severity"] == alert_queue.INFO, out)
ok("and says it is trading again", out and "trading again" in out[0]["message"])

print("\n[9] PARKED alone never alerts, or the alarm becomes noise")
ok("first sighting of a parked branch is silent",
   alert_queue.plan(W(frozen=[f("XRP", "PARKED", gap=3.54)]), {}) == [])
ok("first sighting of an OK branch is silent",
   alert_queue.plan(W(frozen=[f("XRP", "OK")]), {}) == [])

print("\n[10] a blind pass asserts nothing")
ok("an absent frozen key produces no rows",
   alert_queue.plan(W(), {"frozen:ZEC": "FROZEN"}) == [])
ok("and records no state", alert_queue.next_state(W()) == {})
ok("an UNKNOWN verdict produces no row",
   alert_queue.plan(W(frozen=[f("ZEC", "UNKNOWN")]), {}) == [])
ok("and records no state for it",
   alert_queue.next_state(W(frozen=[f("ZEC", "UNKNOWN")])) == {})
ok("so a freeze during a blind spot still fires when sight returns",
   len(alert_queue.plan(W(frozen=[f("ZEC", "FROZEN")]),
                        alert_queue.next_state(W(frozen=[f("ZEC", "UNKNOWN")])))) == 1)

print("\n[11] frozen cannot collide with a holding or a breaker")
w = {"rows": [{"asset": "ZEC", "status": "OK", "usd": 300.0}],
     "breakers": [{"asset": "ZEC", "breached": True, "usd": 300.0, "drawdown_pct": 0.3}],
     "frozen": [f("ZEC", "FROZEN", usd=300.0)]}
st = alert_queue.next_state(w)
ok("three distinct keys for one coin", len(st) == 3, st)
ok("the holding keeps its own", st.get("ZEC") == "OK")
ok("the breaker keeps its own", st.get(alert_queue.BREAKER_STATE_PREFIX + "ZEC") == "BREACHED")
ok("the frozen verdict keeps its own",
   st.get(alert_queue.FROZEN_STATE_PREFIX + "ZEC") == "FROZEN")

print("\n[12] small money does not wake anybody")
ok(f"under ${alert_queue.MIN_ALERT_USD:.0f} is silent",
   alert_queue.plan(W(frozen=[f("D", "FROZEN", usd=alert_queue.MIN_ALERT_USD - 0.01)]), {}) == [])

print("\n[13] it places nothing")
src = open("/home/user/empire-v2/frozen_branches.py").read()
for bad in ("place_order", "coinbase", "db.", "commit", "requests.", "aiohttp"):
    ok(f"no {bad} anywhere in the module", bad not in src.lower())

print()
if fail:
    print(f"{fail} FAILED"); sys.exit(1)
print("all checks passed")
