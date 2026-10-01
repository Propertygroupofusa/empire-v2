#!/usr/bin/env python3
"""The Risk Governor decides how much money goes on the line.

Every check here exists because its failure mode is silent: the wrong
multiplier, the wrong tier, or a missing health field read as healthy
all produce a perfectly plausible-looking verdict with more money behind
it than intended.
"""
import json, math, random, statistics as st, sys
import risk_governor as R

FAILS = []
def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)

HEALTHY = {"database_healthy": True, "reconciliation_healthy": True,
           "broker_reachable": True, "circuit_breaker_tripped": False,
           "trading_blocked": False, "equity": 979.16, "equity_floor": 800.0}

print("\n[1] de-risking is fail-CLOSED on an unreadable drawdown")
# Treating a missing drawdown as 0% hands out FULL size at exactly the
# moment nobody can see how bad things are.
deepest = R.DERISK_LADDER[0][1]
ok("None -> the deepest cut, not full size", R.derisk_multiplier(None) == deepest,
   str(R.derisk_multiplier(None)))
ok("NaN -> the deepest cut", R.derisk_multiplier(float('nan')) == deepest)
ok("a string -> the deepest cut", R.derisk_multiplier("8") == deepest)
ok("a bare 0% drawdown is full size", R.derisk_multiplier(0.0) == 1.0)

print("\n[2] the ladder applies the WORST matching rung")
# A shallow-first scan with an early return gives 0.75 at a 12%
# drawdown - the mildest cut during the worst drawdown.
ok("12% -> quarter size", R.derisk_multiplier(12.0) == 0.25, str(R.derisk_multiplier(12.0)))
ok("10% -> quarter size (boundary is inclusive)", R.derisk_multiplier(10.0) == 0.25)
ok("8%  -> half", R.derisk_multiplier(8.0) == 0.50)
ok("7%  -> half (boundary)", R.derisk_multiplier(7.0) == 0.50)
ok("6%  -> three quarters", R.derisk_multiplier(6.0) == 0.75)
ok("5%  -> three quarters (boundary)", R.derisk_multiplier(5.0) == 0.75)
ok("4%  -> full size", R.derisk_multiplier(4.0) == 1.0)
# Monotonic: a deeper drawdown can never buy MORE size.
seq = [R.derisk_multiplier(x) for x in (0, 3, 5, 6, 7, 9, 10, 20, 99)]
ok("deeper drawdown never increases size",
   all(a >= b for a, b in zip(seq, seq[1:])), str(seq))

print("\n[3] MUTATION: a shallow-first ladder must be caught")
_real = R.DERISK_LADDER
R.DERISK_LADDER = tuple(sorted(_real, key=lambda t: t[0]))   # shallowest first
mutant = R.derisk_multiplier(12.0)
R.DERISK_LADDER = _real
ok("a shallowest-first ladder returns 0.75 at 12% (so [2] catches it)",
   mutant == 0.75, str(mutant))

print("\n[4] trading_approved: UNKNOWN IS BLOCKED")
ok("None state blocks", R.trading_approved(None)[0] is False)
ok("a non-dict blocks", R.trading_approved(["healthy"])[0] is False)
ok("a fully healthy state is approved", R.trading_approved(HEALTHY)[0] is True,
   str(R.trading_approved(HEALTHY)[1]))
for field in R.REQUIRED_HEALTH:
    missing = {k: v for k, v in HEALTHY.items() if k != field}
    appr, why = R.trading_approved(missing)
    ok(f"a MISSING {field} blocks", appr is False, str(why))
    ok(f"...and says it is unknown, not that it is false",
       any("unknown" in w for w in why), str(why))
    nulled = dict(HEALTHY); nulled[field] = None
    ok(f"a None {field} blocks", R.trading_approved(nulled)[0] is False)
    # The nastiest case: a truthy NON-bool. `if state[field]:` passes it.
    stringy = dict(HEALTHY); stringy[field] = "true"
    ok(f'the string "true" for {field} BLOCKS (truthy is not True)',
       R.trading_approved(stringy)[0] is False, str(R.trading_approved(stringy)[1]))

tripped = dict(HEALTHY); tripped["circuit_breaker_tripped"] = True
ok("a tripped circuit breaker blocks", R.trading_approved(tripped)[0] is False)
unread = dict(HEALTHY); unread["circuit_breaker_tripped"] = None
ok("an UNREADABLE circuit breaker blocks", R.trading_approved(unread)[0] is False)
susp = dict(HEALTHY); susp["suspended_by_user"] = True
ok("owner suspension blocks", R.trading_approved(susp)[0] is False)
low = dict(HEALTHY); low["equity"] = 799.99
ok("equity below the floor blocks", R.trading_approved(low)[0] is False,
   str(R.trading_approved(low)[1]))

print("\n[5] the edge interval is the finding, not the mean")
iv = R.edge_interval(-0.0138, 1.7847, 232)
ok("the real record's interval spans zero", iv["sign_resolved"] is False, json.dumps(iv))
ok("so it is neither positive nor negative",
   iv["positive"] is False and iv["negative"] is False)
ok("and it says how many trades would settle it", iv["trades_needed"] > 60000,
   str(iv["trades_needed"]))
ok("a genuinely positive edge IS resolved",
   R.edge_interval(1.0, 0.5, 200)["positive"] is True)
ok("a genuinely negative edge IS resolved",
   R.edge_interval(-1.0, 0.5, 200)["negative"] is True)
ok("n under 2 is unknowable, not zero", R.edge_interval(0.5, 1.0, 1) is None)
ok("a missing sd is unknowable", R.edge_interval(0.5, None, 200) is None)

print("\n[6] the REAL 232-trade record must not promote")
# These are this account's own measured numbers.
real = {"trades": 232, "mean_pnl": -0.0138, "sd_pnl": 1.7847,
        "drawdown_pct": 0.0, "rule_compliance_pct": 100.0,
        "at_equity_high": True, "state": HEALTHY}
v = R.promotion_verdict(real)
ok("promotion is refused", v["promote"] is False, json.dumps(v["blocked_by"]))
ok("and the reason is the confidence interval, not a missing checkbox",
   any("spans zero" in b for b in v["blocked_by"]), json.dumps(v["blocked_by"]))

print("\n[7] the ORIGINAL spec's gates alone would have promoted on noise")
# Everything the original design asked for, satisfied, with an edge that
# is pure noise: 30 trades, at an equity high, no drawdown, compliant.
# $0.1979 is the MEDIAN per-trade mean of a *profitable* 30-trade window
# resampled from this account's own 232 trades - i.e. exactly what a
# lucky streak looks like here. (An earlier draft of this test used
# $0.90, which over 30 trades at this dispersion is genuinely
# significant - the governor was right to promote it and the test was
# wrong to demand a refusal. A gate that refuses a real edge is as
# broken as one that passes noise.)
noisy = {"trades": 30, "mean_pnl": 0.1979, "sd_pnl": 1.7847,
         "drawdown_pct": 0.0, "rule_compliance_pct": 100.0,
         "at_equity_high": True, "state": HEALTHY}
v2 = R.promotion_verdict(noisy)
ok("a typical LUCKY 30-trade streak is refused", v2["promote"] is False,
   json.dumps(v2["blocked_by"]))
ok("...because the interval still spans zero",
   any("spans zero" in b for b in v2["blocked_by"]), json.dumps(v2["blocked_by"]))
# And the converse: a real edge must NOT be refused, or the governor is
# just an obstacle.
genuine = dict(noisy, trades=400, mean_pnl=0.40, sd_pnl=1.7847)
ok("a genuine, well-sampled edge IS promoted",
   R.promotion_verdict(genuine)["promote"] is True,
   json.dumps(R.promotion_verdict(genuine)["blocked_by"]))
# Prove the premise rather than asserting it: resample the real trades.
pnl_real = None
try:
    data = json.load(open("/tmp/ct.json"))
    pnl_real = [float(t["pnl"]) for t in data["trades"]]
except Exception:
    pass
if pnl_real:
    random.seed(7)
    wins = sum(1 for _ in range(20000)
               if sum(random.choice(pnl_real) for _ in range(30)) > 0)
    rate = wins / 20000 * 100
    ok(f"a 30-trade window shows a profit {rate:.1f}% of the time "
       f"(so a 30-trade gate is a coin flip)", 35 < rate < 55, f"{rate:.1f}%")
else:
    print("  SKIP  resampling check (no local trade file); the gate test above still holds")

print("\n[8] an unknown tier falls back to the FLOOR, never upward")
d = R.decide("STAGE_9", {"state": HEALTHY, "drawdown_pct": 0.0})
ok("unknown tier -> base tier", d["tier"] == R.BASE_TIER, d["tier"])
ok("and base risk, not the top tier",
   d["tier_risk_pct"] == R.RISK_TIERS[R.BASE_TIER])
ok("and it says so", any("not recognised" in n for n in d["notes"]), str(d["notes"]))
ok("None tier also falls back", R.decide(None, {"state": HEALTHY,
   "drawdown_pct": 0.0})["tier"] == R.BASE_TIER)

print("\n[9] decide(): not approved means ZERO risk, not reduced risk")
bad = R.decide("STAGE_4", {"state": {"database_healthy": False},
                           "drawdown_pct": 0.0})
ok("effective risk is exactly 0", bad["effective_risk_pct"] == 0.0,
   str(bad["effective_risk_pct"]))
ok("approved is False", bad["approved"] is False)
ok("and the reasons are carried, not swallowed", len(bad["blocked_because"]) > 0)
ok("a totally missing state blocks",
   R.decide("STAGE_1", {})["effective_risk_pct"] == 0.0)

print("\n[10] de-risking multiplies the tier, and never raises it")
for tier in R.TIER_ORDER:
    for dd in (0, 5, 7, 10, 25):
        d = R.decide(tier, {"state": HEALTHY, "drawdown_pct": dd})
        ok(f"{tier} @ {dd}% dd never exceeds its tier",
           d["effective_risk_pct"] <= R.RISK_TIERS[tier] + 1e-12,
           f"{d['effective_risk_pct']} vs {R.RISK_TIERS[tier]}")

print("\n[11] the module cannot move money")
src = open("risk_governor.py").read()
for bad_call in ("requests.", "aiohttp", "urllib", "submit_order", "place_order",
                 "session.post", "await "):
    ok(f"no {bad_call!r} anywhere in the module", bad_call not in src)
ok("and it declares it is not wired to live sizing",
   R.decide("STAGE_1", {"state": HEALTHY, "drawdown_pct": 0})["applied_to_live_sizing"] is False)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
