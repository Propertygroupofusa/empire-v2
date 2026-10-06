"""The re-ranking loop must be able to invalidate itself.

THE OWNER'S INSTRUCTION, 2026-10-06: "Close the loop, make the re-ranking
run on a schedule."

THE CENTRAL PROPERTY HERE IS THE REFUSAL, not the ranking. A scheduled
ranker that always produces a ranking is worse than no ranker: it launders
a stale ordering as a fresh finding, every hour, with increasing authority.
The ranking rests on one empirical claim - that a branch's net % per close
predicts its own next window. That was true when measured (Spearman +0.643,
t=+3.03 on 13 df) and it is not a law. The market where it stops being true
is exactly the market where acting on it loses money, so every pass re-tests
it and publishes nothing when it fails.

Most of this file is therefore about the ways the test must FAIL CLOSED:
  - a correlation that no longer clears its critical value   -> WITHHELD
  - too few branches in both windows                         -> WITHHELD, and
                                                                UNKNOWN, not
                                                                disproved
  - a NEGATIVE correlation, however strong                   -> WITHHELD
  - a normal approximation standing in for a small-sample t  -> rejected

Run: python3 test_portfolio_plan.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_plan as pp

FAIL = []
def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAIL.append(name)

SPLIT = "2026-10-01"

def T(bot, pnl, notional, day, reason="profit_target"):
    return {"bot_name": bot, "pnl": pnl, "entry_price": notional, "qty": 1.0,
            "closed_at": f"{day}T12:00:00Z", "exit_reason": reason,
            "product_id": bot + "-USD"}

def B(bot, *, alloc=100.0, basis=20.0, levels=3, slices=0,
      px=100.0, ref=100.0, gp=0.025):
    return {"bot_name": bot, "product_id": bot + "-USD",
            "allocated_usd": alloc, "coin_basis_usd": basis,
            "num_levels": levels, "slices": [{}] * slices,
            "current_price": px, "reference_price": ref, "grid_pct": gp}

def ledger(pairs, *, early="2026-09-28", late="2026-10-03"):
    """pairs: [(bot, early_net_pct, late_net_pct)] -> 3 closes each window."""
    out = []
    for bot, e, l in pairs:
        for _ in range(3):
            out.append(T(bot, e, 100.0, early))
            out.append(T(bot, l, 100.0, late))
    return out

# 10 branches whose ordering is preserved across the split -> strong rho
AGREE = [(f"b{i}", float(i), float(i) + 0.5) for i in range(10)]
# 10 branches whose ordering is exactly reversed -> strong NEGATIVE rho
REVERSE = [(f"b{i}", float(i), float(9 - i)) for i in range(10)]
# 10 branches whose later window is unrelated to the earlier
SHUFFLE = [("b0",0.0,4.0),("b1",1.0,9.0),("b2",2.0,1.0),("b3",3.0,7.0),
           ("b4",4.0,0.0),("b5",5.0,6.0),("b6",6.0,2.0),("b7",7.0,8.0),
           ("b8",8.0,3.0),("b9",9.0,5.0)]

print("\n=== 1. the premise holds -> a ranking is published ===")
p = pp.persistence(ledger(AGREE), split_at=SPLIT)
ok("a preserved ordering is significant", p["holds"] is True)
ok("rho is strongly positive", p["rho"] > 0.9)
ok("t clears its critical value",
   p["t_is_infinite"] or p["t"] >= p["t_critical_95"])
ok("a PERFECT ordering is the strongest result, not a rejected one",
   p["rho"] == 1.0 and p["t_is_infinite"] is True and p["holds"] is True)
ok("the detail says it persists", "persists" in p["detail"])

print("\n=== 2. THE REFUSAL: the premise fails -> nothing is published ===")
p2 = pp.persistence(ledger(REVERSE), split_at=SPLIT)
ok("a REVERSED ordering does not license the ranking", p2["holds"] is False)
ok("even though the correlation is strong", abs(p2["rho"]) > 0.9)
ok("and it is negative", p2["rho"] < 0)
ok("a perfect REVERSAL still fails, infinite or not", p2["holds"] is False)
ok("the detail says it NO LONGER persists", "NO LONGER" in p2["detail"])
p3 = pp.persistence(ledger(SHUFFLE), split_at=SPLIT)
ok("a shuffled ordering does not reach significance", p3["holds"] is False)

print("\n=== 3. too few branches is UNKNOWN, never disproved ===")
few = pp.persistence(ledger(AGREE[:4]), split_at=SPLIT)
ok("4 branches withholds", few["holds"] is False)
ok("and is marked unreadable, not a failed test", few["readable"] is False)
ok("it says UNKNOWN, not disproved", "UNKNOWN" in few["detail"])
ok("and says nothing was measured", "nothing was measured" in few["detail"])
ok("no rho is invented", few["rho"] is None)
ok("an empty ledger withholds too",
   pp.persistence([], split_at=SPLIT)["holds"] is False)

print("\n=== 4. a branch must trade in BOTH windows to count ===")
only_early = [T("x", 5.0, 100.0, "2026-09-28") for _ in range(3)]
p4 = pp.persistence(ledger(AGREE) + only_early, split_at=SPLIT)
ok("a branch present in one window only is excluded",
   p4["branches_compared"] == 10)
ok("the window close counts are both reported",
   p4["early_closes"] > 0 and p4["late_closes"] > 0)

print("\n=== 4b. BOTH windows are required, at whatever the floor is set to ===")
# At MIN_CLOSES_PER_HALF = 1 the late-window check is unreachable: being
# present in the later window at all already means one close. That makes it
# untested defensive code, and a mutation dropping it survived. Raising the
# floor for one assertion exercises the branch that will matter the moment
# the constant is raised for real.
_orig = pp.MIN_CLOSES_PER_HALF
try:
    pp.MIN_CLOSES_PER_HALF = 3
    thin_late = []
    for bot, e, l in AGREE:
        for _ in range(3):
            thin_late.append(T(bot, e, 100.0, "2026-09-28"))
        thin_late.append(T(bot, l, 100.0, "2026-10-03"))      # ONE late close
    r = pp.persistence(thin_late, split_at=SPLIT)
    ok("a branch thin in the LATE window is excluded, not counted",
       r["branches_compared"] == 0)
    ok("and with none left the verdict is UNKNOWN, not a failed test",
       r["holds"] is False and r["readable"] is False)
    # symmetric: thin EARLY, fat late
    thin_early = []
    for bot, e, l in AGREE:
        thin_early.append(T(bot, e, 100.0, "2026-09-28"))      # ONE early close
        for _ in range(3):
            thin_early.append(T(bot, l, 100.0, "2026-10-03"))
    r2 = pp.persistence(thin_early, split_at=SPLIT)
    ok("a branch thin in the EARLY window is excluded too",
       r2["branches_compared"] == 0)
    # and a fleet fat in both still counts
    r3 = pp.persistence(ledger(AGREE), split_at=SPLIT)
    ok("branches with 3 closes each side still qualify at a floor of 3",
       r3["branches_compared"] == 10)
finally:
    pp.MIN_CLOSES_PER_HALF = _orig
ok("the floor is restored for the rest of the file",
   pp.MIN_CLOSES_PER_HALF == _orig)

print("\n=== 5. the small-sample critical value is NOT the normal one ===")
ok("df=8 uses 2.306, not 1.96", pp._t_crit(8) == 2.306)
ok("df=13 uses 2.160", pp._t_crit(13) == 2.160)
ok("df under 6 is refused outright", pp._t_crit(5) is None)
ok("a large df falls back conservatively", pp._t_crit(200) == 1.96)
ok("2.306 really is stricter than the normal 1.96", pp._t_crit(8) > 1.96)
# A correlation that a normal approximation would pass and the real t rejects
import math
n, rho = 10, 0.60
t = rho * math.sqrt((n - 2) / (1 - rho * rho))
ok("a rho that 1.96 would pass is correctly rejected at df=8",
   t > 1.96 and t < pp._t_crit(8))

print("\n=== 6. adopted exits never enter any statistic ===")
led = ledger(AGREE) + [T("b0", -123.24, 1650.0, "2026-10-04", "adopted_exit")]
ok("own_closes drops them", len(pp.own_closes(led)) == len(ledger(AGREE)))
a = pp.aggregate(pp.own_closes(led))
ok("and they do not reach the per-branch aggregate",
   abs(a["b0"]["realized_usd"]) < 100)

print("\n=== 7. aggregate arithmetic ===")
ag = pp.aggregate([T("x", 2.0, 100.0, "2026-10-02"),
                   T("x", 4.0, 100.0, "2026-10-02")])
ok("closes counted", ag["x"]["closes"] == 2)
ok("realized summed", ag["x"]["realized_usd"] == 6.0)
ok("net% is realized over notional", abs(ag["x"]["net_pct_per_close"] - 3.0) < 1e-9)
ok("zero notional gives None, never a divide",
   pp.aggregate([{"bot_name": "y", "pnl": 1.0, "entry_price": 0, "qty": 0,
                  "closed_at": "2026-10-02T00:00:00Z"}])["y"]["net_pct_per_close"]
   is None)

print("\n=== 8. build(): WITHHELD publishes no ordering at all ===")
brs = [B(f"b{i}", alloc=100.0 + i, slices=1) for i in range(10)]
w = pp.build(trades=ledger(REVERSE), branches=brs, split_at=SPLIT,
             loose_cash_usd=500.0, fleet_allocated_usd=5000.0)
ok("verdict is WITHHELD", w["verdict"] == "WITHHELD")
ok("no plan", w["plan"] == [])
ok("no ranking key at all - not an empty one to be misread",
   "ranking" not in w)
ok("nothing would move", w["would_move_usd"] == 0.0)
ok("it explains itself as deliberate", "deliberate refusal" in w["what_this_is"])

print("\n=== 9. build(): the premise holding produces a real plan ===")
# one source branch, full on rungs and above its coin basis
srcs = [B("src", alloc=1000.0, basis=200.0, levels=2, slices=2)]
dests = [B(f"b{i}", alloc=100.0, basis=20.0, levels=5, slices=1)
         for i in range(10)]
g = pp.build(trades=ledger(AGREE) + ledger([("src", 0.1, 0.1)]),
             branches=dests + srcs, split_at=SPLIT,
             loose_cash_usd=0.0, fleet_allocated_usd=2000.0)
ok("the premise holds on this ledger", g["persistence"]["holds"] is True)
ok("a ranking is published", len(g["ranking"]) > 0)
ok("it is sorted best first",
   all(g["ranking"][i]["net_pct_per_close"] >= g["ranking"][i+1]["net_pct_per_close"]
       for i in range(len(g["ranking"]) - 1)))
ok("src is the capital source", [s["bot_name"] for s in g["sources"]] == ["src"])
ok("and it still sells nothing", g["sells_nothing"] is True)

print("\n=== 10. it cannot execute ===")
CODE = "\n".join(l for l in open("portfolio_plan.py").read().splitlines()
                 if not l.strip().startswith("#"))
for forbidden in ("place_order", "place_market_buy", "db.commit", "requests.",
                  "aiohttp", "await ", "session.post", "withdraw"):
    ok(f"never references {forbidden}", forbidden not in CODE)

print(f"\n{'ALL PASSED' if not FAIL else str(len(FAIL)) + ' FAILED'}")
for f in FAIL: print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
