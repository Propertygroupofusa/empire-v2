"""BREAKER alerts: the kind that did not exist while three breakers tripped.

Measured 2026-10-02, QNT (-29.99%), JASMY (-28.28%) and ONDO (-27.98%) had
all breached the 25% drawdown breaker. No alert fired, because no kind of
alert covered it. The account owner found out by pasting Railway logs.

The failure to protect against here is NOT a missing alert - it is a FALSE
CALM: a pass that could not read the breakers reporting that none tripped.
"""
import sys
sys.path.insert(0, "/home/user/empire-v2")
import alert_queue

fail = 0
def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond or not detail else f"   -> {detail}"))
    if not cond: fail += 1

def br(asset, breached, dd=0.30, usd=200.0):
    return {"asset": asset, "breached": breached, "drawdown_pct": dd, "usd": usd}

W = lambda **kw: {"rows": [], **kw}

print("\n[1] a breaker tripping is news, once")
out = alert_queue.plan(W(breakers=[br("QNT-USD", True, 0.2999, 198.24)]), {})
ok("a first-seen tripped breaker alerts", len(out) == 1, out)
ok("it is kind BREAKER", out and out[0]["kind"] == "BREAKER")
ok("it is CRITICAL", out and out[0]["severity"] == alert_queue.CRITICAL)
ok("it names the coin and the drawdown",
   out and "QNT-USD" in out[0]["message"] and "30.0%" in out[0]["message"], out)
ok("it says buys only, nothing sold",
   out and "NEW BUYS" in out[0]["detail"] and "nothing has" in out[0]["detail"].lower(), out)

print("\n[2] still tripped is NOT news")
st = alert_queue.next_state(W(breakers=[br("QNT-USD", True)]))
out = alert_queue.plan(W(breakers=[br("QNT-USD", True)]), st)
ok("a breaker that was already tripped is silent", out == [], out)

print("\n[3] coming back under it is news")
out = alert_queue.plan(W(breakers=[br("QNT-USD", False, 0.18)]), st)
ok("recovery alerts", len(out) == 1, out)
ok("recovery is INFO, not CRITICAL", out and out[0]["severity"] == alert_queue.INFO)
ok("recovery says it may buy again", out and "buy again" in out[0]["detail"], out)

print("\n[4] a calm breaker first seen is NOT news")
out = alert_queue.plan(W(breakers=[br("BTC-USD", False, 0.02)]), {})
ok("first sighting of an untripped breaker is silent", out == [], out)

print("\n[5] THE FALSE-CALM GUARD: unreadable is not calm")
# No key at all - the grid status could not be read this pass.
out = alert_queue.plan(W(), {"breaker:QNT-USD": "BREACHED"})
ok("an absent breakers key produces no rows", out == [], out)
st2 = alert_queue.next_state(W())
ok("and it does NOT record the breaker as OK",
   "breaker:QNT-USD" not in st2, st2)
# The verdict itself missing on a row.
out = alert_queue.plan(W(breakers=[{"asset": "QNT-USD", "breached": None,
                                    "drawdown_pct": 0.30, "usd": 200.0}]), {})
ok("a null verdict produces no row", out == [], out)
st3 = alert_queue.next_state(W(breakers=[{"asset": "QNT-USD", "breached": None}]))
ok("a null verdict is not recorded as a state", st3 == {}, st3)
# A non-bool truthy value must not be read as True.
for bad in ("true", 1, 0, "", "False"):
    out = alert_queue.plan(W(breakers=[br("X-USD", bad)]), {})
    ok(f"{bad!r} is not a verdict and alerts nothing", out == [], out)

print("\n[6] the state a trip was MISSED in still alerts when sight returns")
# Breaker trips while unreadable, then becomes readable: it must still fire.
state = alert_queue.next_state(W(breakers=[br("ONDO-USD", False)]))
blind = alert_queue.next_state(W())                 # a pass that saw nothing
state.update(blind)                                 # worker merges forward
out = alert_queue.plan(W(breakers=[br("ONDO-USD", True, 0.2798, 50.86)]), state)
ok("the trip is reported on the first readable pass after the gap",
   len(out) == 1 and out[0]["severity"] == alert_queue.CRITICAL, out)

print("\n[7] small money does not wake anybody")
out = alert_queue.plan(W(breakers=[br("DUST-USD", True, 0.30,
                                      alert_queue.MIN_ALERT_USD - 0.01)]), {})
ok(f"under ${alert_queue.MIN_ALERT_USD:.0f} is silent", out == [], out)
out = alert_queue.plan(W(breakers=[br("OK-USD", True, 0.30,
                                      alert_queue.MIN_ALERT_USD)]), {})
ok(f"at exactly ${alert_queue.MIN_ALERT_USD:.0f} it fires", len(out) == 1, out)

print("\n[8] a breaker cannot collide with a holding of the same name")
# Both a holdings row and a breaker for QNT. They are different facts and
# must not overwrite one another in last_state.
w = {"rows": [{"asset": "QNT", "status": "OK", "usd": 300.0}],
     "breakers": [br("QNT", True, 0.30, 300.0)]}
st = alert_queue.next_state(w)
ok("the holding keeps its own key", st.get("QNT") == "OK", st)
ok("the breaker gets a prefixed key",
   st.get(alert_queue.BREAKER_STATE_PREFIX + "QNT") == "BREACHED", st)
ok("two distinct keys, not one", len(st) == 2, st)

print("\n[9] an unreadable drawdown still alerts, and says so")
out = alert_queue.plan(W(breakers=[{"asset": "Z-USD", "breached": True,
                                    "drawdown_pct": None, "usd": 100.0}]), {})
ok("it still fires without a percentage", len(out) == 1, out)
ok("and does not print a fake 0.0%",
   out and "0.0%" not in out[0]["message"] and "unreadable" in out[0]["message"], out)

print("\n[10] dedupe keys are stable and distinct")
a = alert_queue.plan(W(breakers=[br("A-USD", True)]), {})[0]["dedupe_key"]
b = alert_queue.plan(W(breakers=[br("A-USD", True)]), {})[0]["dedupe_key"]
c = alert_queue.plan(W(breakers=[br("B-USD", True)]), {})[0]["dedupe_key"]
ok("same transition, same key", a == b)
ok("different coin, different key", a != c)
ok("the key names the kind", a.startswith("BREAKER:"), a)

print("\n[11] it stays a pure function")
w = W(breakers=[br("P-USD", True)])
before = repr(w)
alert_queue.plan(w, {})
ok("plan does not mutate the watch", repr(w) == before)

print()
if fail:
    print(f"{fail} FAILED"); sys.exit(1)
print("all checks passed")
