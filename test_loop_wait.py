"""A blocked loop and a dead loop are different facts. Pin the difference.

Run as written: python3 test_loop_wait.py

WHY THIS EXISTS, with the date on it. On 2026-10-08 the fleet loop placed a
post-only buy on XLM-USD at 17:26:39Z and did not cycle again until
18:30:50Z - 64 minutes, in-line, waiting for that one order. For the whole
hour /grid-status reported:

    heartbeat: {"alive": false, "age_seconds": 3316.3, "stage": "cycled"}
    silence:   {"verdict": "BROKEN", "alarm": true, ...ZEC-USD x199...}

Neither was true. The loop was not dead and ZEC was not the reason. It took
fourteen hand-polls of the activity feed over ten minutes to establish that
the loop was merely waiting, and the order then filled and paid.

So `alive: false` is kept EXACTLY as it was - things read it - and the
question "is it dead or is it waiting" gets its own answer, from a marker
the waiting code sets and clears.

THE FOOTGUN THIS MUST NOT HAVE. A marker that leaks - set and never cleared
because the wait raised - would make a genuinely dead loop read as "just
waiting" forever. That is strictly worse than the bug being fixed: it
converts a false alarm into a missed one. Hence ABANDONED: past its own
budget plus a grace, a marker stops claiming the loop is waiting and starts
saying it leaked. Sections [4] and [5] are the ones that matter most here.
"""

import sys
import time

sys.path.insert(0, ".")
import loop_wait as lw

FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


def reset():
    lw._IN_FLIGHT.clear()


section("[1] nothing in flight is the quiet case, and it says so")
reset()
ok("in_flight() is None when nothing is waiting", lw.in_flight() is None)
v = lw.verdict(age_seconds=12.0, holds_loop=True)
ok("a fresh heartbeat with no wait is ALIVE", v["verdict"] == "ALIVE")
ok("and carries no waiting_on", v["waiting_on"] is None)

section("[2] a wait in flight is reported as a wait, not as death")
reset()
tok = lw.mark("XLM-USD", "buy", 3600)
f = lw.in_flight()
ok("in_flight() names the product", f["product_id"] == "XLM-USD")
ok("and the side", f["side"] == "buy")
ok("and the budget it was given", f["budget_seconds"] == 3600)
ok("elapsed is a real number from the start", isinstance(f["elapsed_seconds"], float))
ok("and it is within budget immediately", f["within_budget"] is True)
# THE LIVE CASE: 3,316 seconds of age, which the old heartbeat called dead.
v = lw.verdict(age_seconds=3316.3, holds_loop=True)
ok("3,316s of age with a wait in flight is WAITING, not STALLED",
   v["verdict"] == "WAITING_ON_A_MAKER_ORDER")
ok("the verdict names what it is waiting on", v["waiting_on"]["product_id"] == "XLM-USD")
ok("and the detail says the loop is not dead",
   "not dead" in v["detail"].lower())
lw.clear(tok)
ok("clear() ends the wait", lw.in_flight() is None)

section("[3] a stale heartbeat with NO wait in flight is still a stall")
reset()
v = lw.verdict(age_seconds=3316.3, holds_loop=True)
ok("no wait + old heartbeat + this process holds the loop -> STALLED",
   v["verdict"] == "STALLED")
ok("and it does not pretend to know why", v["waiting_on"] is None)

section("[4] A LEAKED MARKER MUST NOT MASK A DEAD LOOP")
reset()
tok = lw.mark("ZEC-USD", "sell", 60)
# Reach in and age it past its own budget plus the grace. A real leak looks
# exactly like this: a marker nobody cleared because the wait raised.
lw._IN_FLIGHT[tok]["started_at"] -= (60 + lw.ABANDON_GRACE_SECONDS + 5)
f = lw.in_flight()
ok("a marker past budget+grace reports within_budget False",
   f["within_budget"] is False)
ok("and is flagged abandoned", f["abandoned"] is True)
v = lw.verdict(age_seconds=9999.0, holds_loop=True)
ok("an ABANDONED marker does NOT read as WAITING - this is the whole point",
   v["verdict"] != "WAITING_ON_A_MAKER_ORDER")
ok("it reads as STALLED, with the leak named",
   v["verdict"] == "STALLED" and "abandoned" in v["detail"].lower())

section("[5] a wait that is merely LONG is not abandoned")
reset()
tok = lw.mark("XLM-USD", "buy", 3600)
lw._IN_FLIGHT[tok]["started_at"] -= 3500.0     # 58 minutes in, budget 60
f = lw.in_flight()
ok("58 minutes into a 60-minute budget is still within budget",
   f["within_budget"] is True and f["abandoned"] is False)
ok("and still reads as WAITING",
   lw.verdict(age_seconds=3500.0, holds_loop=True)["verdict"]
   == "WAITING_ON_A_MAKER_ORDER")

section("[6] the marker is PROCESS-LOCAL, so absence is not evidence")
reset()
v = lw.verdict(age_seconds=3316.3, holds_loop=False)
ok("another process holds the loop -> never STALLED from here",
   v["verdict"] == "UNKNOWN_WHETHER_WAITING")
ok("and it says why the absence proves nothing",
   "this process" in v["detail"].lower())
ok("a fresh heartbeat from elsewhere is still ALIVE",
   lw.verdict(age_seconds=10.0, holds_loop=False)["verdict"] == "ALIVE")

section("[7] never raises, never blocks - it is instrumentation")
reset()
ok("mark() survives rubbish input", isinstance(lw.mark(None, None, None), str))
ok("in_flight() survives a rubbish marker", lw.in_flight() is not None)
reset()
ok("clear() of an unknown token is a no-op, not an error",
   lw.clear("no-such-token") is None)
ok("verdict() survives a missing age",
   lw.verdict(age_seconds=None, holds_loop=True)["verdict"] in
   ("NEVER_SEEN", "UNKNOWN_WHETHER_WAITING", "STALLED", "ALIVE"))
ok("verdict() survives rubbish age", isinstance(
   lw.verdict(age_seconds="nonsense", holds_loop=True)["verdict"], str))

section("[8] two waits at once report the OLDEST - the one doing the damage")
reset()
t1 = lw.mark("AAA-USD", "buy", 3600)
lw._IN_FLIGHT[t1]["started_at"] -= 900.0
t2 = lw.mark("BBB-USD", "sell", 3600)
f = lw.in_flight()
ok("the oldest wait is the one reported", f["product_id"] == "AAA-USD")
ok("and the count of concurrent waits is published", f["waits_in_flight"] == 2)
lw.clear(t1)
ok("clearing the oldest promotes the other", lw.in_flight()["product_id"] == "BBB-USD")

section("[9] clear() is safe to call twice - the finally/except overlap")
reset()
tok = lw.mark("XRP-USD", "buy", 240)
lw.clear(tok)
lw.clear(tok)
ok("a double clear leaves nothing in flight", lw.in_flight() is None)

section("[10] elapsed is measured, not remembered")
reset()
tok = lw.mark("TIA-USD", "sell", 240)
lw._IN_FLIGHT[tok]["started_at"] -= 7.0
e = lw.in_flight()["elapsed_seconds"]
ok("elapsed tracks real time", 6.5 <= e <= 8.5)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
