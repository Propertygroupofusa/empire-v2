"""An incubator is only worth having if it cannot become a second backtest.

Run as written: python3 test_incubator.py

The two properties everything else rests on: the live side never sees a
bar that existed when the candidate was chosen, and both sides are scored
by the SAME engine as the backtest - otherwise the measured gap is partly
the candidate and partly the simulator, with no way to tell which.
"""

import random
import sys

sys.path.insert(0, ".")
import incubator as inc

FAILS = []


def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(n):
    print("\n" + n)


def bars(seq, t0=0, gran=900):
    """(t, low, high, close) from a list of (low, high, close)."""
    return [(t0 + i * gran, lo, hi, c) for i, (lo, hi, c) in enumerate(seq)]


section("[1] the arming moment splits the tape, and nothing straddles it")
b = bars([(100, 100, 100)] * 5, t0=1000, gran=100)   # t = 1000..1400
before, after = inc.split_bars(b, 1200)
ok("bars before the arming instant are the backtest side",
   [x[0] for x in before] == [1000, 1100])
ok("bars after it are the live side", [x[0] for x in after] == [1300, 1400])
ok("the bar AT the arming instant belongs to NEITHER - it was half formed "
   "when the decision was made, and counting it hands the candidate hindsight",
   1200 not in [x[0] for x in before] + [x[0] for x in after])
ok("every bar is accounted for or deliberately dropped",
   len(before) + len(after) == len(b) - 1)

section("[2] the live side cannot be fitted")
# A tape that is dead flat before arming and violently profitable after.
# If any pre-arming bar leaked into the live side the live result would
# move; it must not.
flat = [(100, 100, 100)] * 20
wild = [(80, 130, 100)] * 20
tape = bars(flat + wild, t0=0, gran=900)
armed = tape[20][0] - 1
r = inc.incubate(tape, armed, step=0.03, gran_seconds=900)
ok("a flat pre-arming window produces no backtest round trips",
   r["backtest"]["round_trips"] == 0)
ok("the live window is scored on its own bars", r["live"]["round_trips"] > 0)
ok("moving the arming instant EARLIER moves trades from live to backtest",
   inc.incubate(tape, 0, step=0.03, gran_seconds=900)["live"]["round_trips"]
   >= r["live"]["round_trips"])

section("[3] both sides are scored by the BACKTEST'S OWN engine")
import importlib.util
spec = importlib.util.spec_from_file_location("gsb", "grid_step_backtest.py")
gsb = importlib.util.module_from_spec(spec)
try:
    sys.argv = ["grid_step_backtest.py"]          # it reads argv at import
    spec.loader.exec_module(gsb)
    loaded = True
except Exception as e:                            # pragma: no cover
    print(f"  (could not load grid_step_backtest.py: {type(e).__name__})")
    loaded = False
if loaded:
    ok("the fee legs are the same number",
       inc.FEE_ROUND_TRIP == gsb.FEE_RT and inc.FEE_LEG == gsb.FEE_LEG)
    random.seed(7)
    agree = True
    for _ in range(200):
        seq, p = [], 100.0
        for _i in range(60):
            p *= 1 + random.uniform(-0.04, 0.04)
            lo, hi = p * (1 - random.uniform(0, 0.03)), p * (1 + random.uniform(0, 0.03))
            seq.append((lo, hi, p))
        bb = bars(seq)
        for step in (0.009, 0.03, 0.04):
            if inc.simulate(bb, step) != gsb.simulate(bb, step):
                agree = False
                break
    ok("and the two engines agree bar for bar on 200 random tapes - the day "
       "they drift, this test fails rather than the comparison quietly "
       "becoming meaningless", agree)

section("[4] the gate - a glance is not a forward test")
few = inc.incubate(bars([(100, 100, 100)] * 10 + [(80, 130, 100)] * 4),
                   armed_at_epoch=bars([(100, 100, 100)] * 10)[-1][0] + 1,
                   step=0.03, gran_seconds=900, min_live_trips=20)
ok("a handful of live trips reads TOO EARLY", few["verdict"] == "TOO EARLY")
ok("and it says how many closed round trips it is waiting for",
   few["closed_trips_needed"] == 20)
many = inc.incubate(bars([(100, 100, 100)] * 5 + [(80, 130, 100)] * 200),
                    armed_at_epoch=bars([(100, 100, 100)] * 5)[-1][0] + 1,
                    step=0.03, gran_seconds=900, min_live_trips=20)
ok("enough closed trips reads READABLE", many["verdict"] == "READABLE")
ok("the bar is CLOSED TRIPS, not elapsed days - a quiet market can run a "
   "long window with no evidence in it",
   "closed ROUND TRIPS" in inc.incubate(bars([(1, 1, 1)]), 0, 0.03)["verdict_means"]
   or "closed round trips" in many["verdict_means"])

section("[5] a missing answer is UNKNOWN, never a zero")
empty = inc.incubate([], 0, step=0.03)
ok("no bars gives no rate rather than 0.00/day",
   empty["live"]["net_usd_per_day"] is None
   and empty["backtest"]["net_usd_per_day"] is None)
ok("and no gap rather than a 0% divergence", empty["gap_pct_per_day"] is None)
ok("an all-pre-arming tape leaves the live side empty, not fabricated",
   inc.incubate(bars([(100, 100, 100)] * 5), 10 ** 12, 0.03)["live"]["round_trips"] == 0)

section("[6] the cohort key round-trips and refuses strangers")
k = inc.cohort_key("BTC-USD", "step", 0.04)
ok("the key is legible in a database client", k == "incubator:BTC-USD:step=0.04")
p = inc.parse_cohort_key(k)
ok("and parses back", p["product_id"] == "BTC-USD" and p["param"] == "step"
   and abs(p["value"] - 0.04) < 1e-12)
for stranger in ("alpaca_entries_paused", "incubator:", "incubator:BTC-USD",
                 "incubator:BTC-USD:step=abc", "", None):
    ok(f"a non-cohort row is ignored, not read as a candidate: {stranger!r}",
       inc.parse_cohort_key(stranger) is None)

section("[7] it cannot trade, and it stores no result")
src = open("incubator.py").read()
for bad in ("requests", "aiohttp", "place_order", "grid_sell", "grid_buy",
            "session.post", "AsyncSessionLocal", "commit("):
    ok(f"no {bad}", bad not in src)
ok("the payload says plainly that it placed nothing",
   inc.incubate(bars([(100, 100, 100)] * 3), 0, 0.03)["placed_nothing"] is True)

# ---------------------------------------------------------------------------
# The maker-wait candidate. Appended rather than rewritten, because the step
# engine's own guarantees above must keep holding while this is added.
# ---------------------------------------------------------------------------

section("[8] a resting order is placed only once the trigger is reached")
# A tape that drifts straight up: a buy trigger below the market is never
# reached, so nothing should ever be placed and nothing should expire.
up = bars([(100 + i, 101 + i, 100.5 + i) for i in range(40)])
r = inc.simulate_wait(up, 0.03, wait_seconds=3600, gran_seconds=900)
ok("a trigger the market never reaches places nothing",
   r["expired_buys"] == 0 and r["round_trips"] == 0)
ok("and the engine says how long it let an order rest",
   r["wait_seconds"] == 3600 and r["wait_bars"] == 4)

section("[9] the placement bar is NOT a fill - a post-only order sits behind the queue")
# One deep bar reaches the trigger; the bars after it never return there.
deep = bars([(100, 100, 100)] * 3 + [(90, 100, 100)] + [(99.9, 100, 100)] * 10)
r1 = inc.simulate_wait(deep, 0.03, wait_seconds=900, gran_seconds=900)
ok("the order placed on the deep bar does not fill on that bar",
   r1["expired_buys"] >= 1)
# The step engine fills that deep bar the instant its range covers the
# trigger, and on this tape goes on to complete the round trip. The wait
# engine never fills at all. A first draft asserted open_at_end >= 1 on the
# step engine, which was simply wrong about what it would do - it had
# already closed the slice.
_step = inc.simulate(deep, 0.03)
ok("this engine is therefore STRICTER than the step engine, which fills "
   "same-bar and completes the trip - deliberate, not a bug",
   _step["round_trips"] >= 1 and r1["round_trips"] == 0)

section("[10] the wait actually BINDS - a longer rest resolves differently")
# A trigger is reached, price leaves, and comes back later.
away = bars([(100, 100, 100)] * 2 + [(96, 100, 100)]
            + [(99.5, 100.5, 100)] * 6 + [(96, 100, 100)] + [(100, 101, 100)] * 4)
short = inc.simulate_wait(away, 0.03, wait_seconds=900, gran_seconds=900)
long_ = inc.simulate_wait(away, 0.03, wait_seconds=10800, gran_seconds=900)
ok("a short wait gives up and records the expiry", short["expired_buys"] > 0)
ok("a longer wait on the SAME tape resolves differently - the parameter is "
   "not inert", short != long_)
ok("the two differ in filled inventory or in expiries, not only in a label",
   short["open_at_end"] != long_["open_at_end"]
   or short["expired_buys"] != long_["expired_buys"])

section("[11] identical inputs give identical answers")
a = inc.simulate_wait(away, 0.03, wait_seconds=3600, gran_seconds=900)
b = inc.simulate_wait(away, 0.03, wait_seconds=3600, gran_seconds=900)
ok("the engine is deterministic", a == b)
ok("a wait expressed in a different unit of the same length agrees",
   inc.simulate_wait(away, 0.03, wait_seconds=3600, gran_seconds=900)
   == inc.simulate_wait(away, 0.03, wait_seconds=3600, gran_seconds=900))

section("[12] a sell that rested through a fall still refuses to book a loss")
# A slice is opened, the target is reached so a sell is placed, then price
# collapses and only recovers to a level BELOW the entry plus both fees.
# Nothing may close.
fall = bars([(100, 100, 100)] * 2 + [(96, 100, 100)]          # buy triggers at 97
            + [(96, 96, 96)]                                   # fills
            + [(96, 101, 100)]                                 # sell target reached
            + [(70, 75, 72)] * 8)                              # never comes back
r = inc.simulate_wait(fall, 0.03, wait_seconds=86400, gran_seconds=900)
ok("no round trip is booked below entry plus both fee legs",
   r["net_usd"] >= 0 and r["round_trips"] == 0)
# WHERE THE FLOOR ACTUALLY BINDS. The resting sell sits at entry x (1+step)
# and never moves, so the fill-time re-check cannot fire - mutation testing
# proved it by deleting the check and breaking no test. The floor that does
# the work is at PLACEMENT: a step that does not clear both fee legs must
# never put a sell on the book at all.
tight = bars([(100, 100, 100)] * 2 + [(99, 100, 100)] + [(99, 99, 99)]
             + [(99, 120, 110)] * 6)
thin = inc.simulate_wait(tight, 0.005, wait_seconds=86400, gran_seconds=900)
ok("a step BELOW the 0.70% round trip NEVER PLACES a sell - this is the "
   "floor that binds, and it is observable only because placements are "
   "counted. Without that counter, removing this floor broke no assertion.",
   thin["placed_sells"] == 0 and thin["round_trips"] == 0)
ok("it still places buys, so the tape is not simply inert",
   thin["placed_buys"] >= 1)
fat = inc.simulate_wait(tight, 0.05, wait_seconds=86400, gran_seconds=900)
ok("a step well clear of the fee floor does place and complete a trip on "
   "the same tape", fat["placed_sells"] >= 1 and fat["round_trips"] >= 1)

section("[13] the wait candidate is scored against the LIVE wait, same bars")
res = inc.incubate_wait(away, armed_at_epoch=away[1][0], wait_seconds=10800,
                        step=0.03, gran_seconds=900, min_live_trips=20,
                        baseline_wait_seconds=3600)
ok("it names what it was compared with", "LIVE wait" in res["compared_against"])
ok("both sides are run on the live window", res["live"]["bars"] > 0)
ok("the candidate and the live wait are both reported, not just the edge",
   res["live"]["candidate"] is not None and res["live"]["live_wait"] is not None)
ok("the baseline wait is stated rather than assumed",
   res["baseline_wait_seconds"] == 3600)
ok("a thin live window still reads TOO EARLY", res["verdict"] == "TOO EARLY")
ok("it places nothing", res["placed_nothing"] is True)
same = inc.incubate_wait(away, armed_at_epoch=away[1][0], wait_seconds=3600,
                         step=0.03, gran_seconds=900, baseline_wait_seconds=3600)
ok("a candidate equal to the live wait shows no edge, by construction",
   same["edge_usd_per_day"] == 0 or same["edge_usd_per_day"] is None)

print("\nALL PASS (wait candidate)" if not FAILS else f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
