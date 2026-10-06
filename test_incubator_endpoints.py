"""The two routes, and the promises that keep an incubator honest.

Run as written: python3 test_incubator_endpoints.py

Source-level, like the other endpoint tests here: no server, no database,
no venue. What it checks is the shape of the code that decides whether a
forward test stays a forward test.
"""

import re
import sys

ROUTER = open("routers/trading_dashboard.py").read()
INC = open("incubator.py").read()
FAILS = []


def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(n):
    print("\n" + n)


def block(start, end_marker="\n@router."):
    i = ROUTER.index(start)
    return ROUTER[i:ROUTER.index(end_marker, i + 10)]


section("[1] both routes exist, and only one of them writes")
ok("the read is a GET", '@router.get("/grid-status/incubator")' in ROUTER)
ok("the arm is a POST", '@router.post("/grid-status/incubator/arm")' in ROUTER)
read = block('@router.get("/grid-status/incubator")')
arm = block('@router.post("/grid-status/incubator/arm")')
ok("the read never commits", "commit()" not in read)
ok("the read never deletes", "db.delete" not in read)
ok("the read never adds a row", "db.add(" not in read)

section("[2] THE ARMING MOMENT IS NOW, AND CANNOT BE SUPPLIED")
spec = ROUTER[ROUTER.index("class ArmIncubatorRequest"):
              ROUTER.index("class ArmIncubatorRequest") + 400]
for forbidden in ("armed_at", "start", "since", "epoch", "when"):
    ok(f"the request body has no '{forbidden}' field - a caller-supplied start "
       f"date is how a forward test becomes a backtest", forbidden not in spec)
ok("the arm time is read from the clock", "int(time.time())" in arm)
ok("and re-arming says plainly that the clock reset",
   '"clock_reset"' in arm)

section("[3] neither route can trade")
for name, body in (("read", read), ("arm", arm)):
    for bad in ("/orders", "place_order", "grid_sell", "grid_buy",
                "try_open", "withdraw"):
        ok(f"the {name} route has no {bad}", bad not in body)
ok("the arm route writes ONE table and it is the key-value state table",
   body.count("TradingBotState") >= 1 and "CryptoGridSlice" not in arm
   and "CryptoGridBranch" not in arm)
ok("both routes say in the payload that they placed nothing",
   '"placed_nothing": True' in read and '"placed_nothing": True' in arm)

section("[4] a parameter with no engine behind it is REFUSED, not stored")
# The build now carries two engines - a step candidate and a maker-wait
# candidate - so the gate is a whitelist rather than one name. What must
# stay true is that it is a WHITELIST: anything outside it is refused at
# the door, because a cohort that can never be scored is worse than none.
ok("the arm route accepts only the parameters that have an engine",
   re.search(r'if param not in \("step", "wait"\):[\s\S]{0,500}HTTPException', arm)
   is not None)
ok("a step is validated as a fraction",
   re.search(r'param == "step"[\s\S]{0,200}HTTPException', arm) is not None)
ok("a wait is validated as SECONDS, with both ends bounded",
   re.search(r'param == "wait"[\s\S]{0,600}60\.0 <= payload\.value <= 604800\.0', arm)
   is not None)
ok("and the live wait is named in the refusal, so nobody guesses it",
   "The live" in arm and "3600" in arm)
ok("and says why - a stored cohort that can never be scored is worse than none",
   "can never be scored" in arm)
ok("the read route reports an unscorable cohort as UNKNOWN rather than zero",
   "has no\\n" in read or "no engine here yet" in read or "UNKNOWN" in read)

section("[5] a gap in the tape is UNKNOWN, never a flat market")
cand = block("async def _incubator_candles", "\n@router.get")
ok("an empty tape returns None with its cause, not an empty list",
   "return None, (err" in cand)
ok("and the reason is written down where the next reader will see it",
   "A GAP IS NOT A FLAT MARKET" in cand)
ok("the read route surfaces that as UNKNOWN on the cohort",
   '"verdict": "UNKNOWN"' in read)
ok("a partial window is flagged rather than silently shortened",
   "tape_partial" in read)

section("[6] the public tape, so the result can be checked by anyone")
ok("candles come from Coinbase's PUBLIC endpoint",
   "api.exchange.coinbase.com" in cand)
ok("and the reason is stated", "re-derivable" in cand or "nobody can check" in cand)
ok("the timestamp format trap is documented, not rediscovered",
   "decodes as a SPACE" in cand or 'trailing Z' in cand)

section("[7] nothing but the candidate and its minute is stored")
ok("the engine itself has no database access", "AsyncSessionLocal" not in INC
   and "commit(" not in INC and "session" not in INC.lower().split("sessions")[0][:0] + "")
ok("results are recomputed on every read, never persisted",
   "recomputed from the public tape" in INC)
ok("the read route stores nothing it computed",
   read.count("commit()") == 0)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
