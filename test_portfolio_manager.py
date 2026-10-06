"""The portfolio manager scales and never sells.

THE OWNER'S INSTRUCTION, 2026-10-05: "Scaling, not selling." - "Do not sell
anything that I have as a loss." - "Just use what's in the account and
build it up."

That makes ONE property load-bearing above every other test in this file:
no input, however shaped, may produce a plan that needs a sale to execute.
A plan needs a sale exactly when it takes a branch below the cost basis of
the coin that branch is holding, or when it takes budget from a branch that
still has a rung to buy. Both are tested directly and both are fuzzed.

MEASURED CONTEXT, same day: $1,309.90 of claim is releasable without any
sale (ZEC $1,186.32, LTC $123.58 - both full on rungs, both above their coin
basis), plus $75.80 of genuinely loose cash. $1,385.70 total, 18.1% of the
$7,639.11 book. Everything else is either coin or a branch's own dry powder.

Run: python3 test_portfolio_manager.py
"""
import os, random, sys

os.environ.pop("GRID_REGIME_COST_BAR_PCT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_manager as pm
import harvest_redirect as hr

FAIL = []
def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAIL.append(name)

BAR = hr.REGIME_COST_BAR_PCT


def B(name, *, alloc, basis=0.0, levels=3, slices=0, net=None, closes=None,
      px=100.0, ref=100.0, gp=0.025):
    b = {"bot_name": name, "product_id": name + "-USD",
         "allocated_usd": alloc, "coin_basis_usd": basis,
         "num_levels": levels, "slices": [{}] * slices,
         "current_price": px, "reference_price": ref, "grid_pct": gp}
    if net is not None: b["net_pct_per_close"] = net
    if closes is not None: b["closes"] = closes
    return b


print("\n=== 1. releasable: the two conditions, both required ===")
ok("a branch FULL on rungs, above its coin basis, releases the difference",
   pm.releasable(B("a", alloc=1808.37, basis=622.05, levels=3, slices=3))
   == 1186.32)
ok("a branch with an EMPTY RUNG releases nothing, however much it holds",
   pm.releasable(B("a", alloc=1808.37, basis=622.05, levels=3, slices=2)) == 0.0)
ok("a branch at its coin basis releases nothing",
   pm.releasable(B("a", alloc=622.05, basis=622.05, levels=3, slices=3)) == 0.0)
ok("a branch BELOW its coin basis releases nothing - never a negative",
   pm.releasable(B("a", alloc=100.0, basis=622.05, levels=3, slices=3)) == 0.0)
ok("a sliver under the live order minimum is not worth moving",
   pm.releasable(B("a", alloc=104.0, basis=100.0, levels=3, slices=3)) == 0.0)
ok("an unreadable basis releases nothing rather than guessing zero",
   pm.releasable({"allocated_usd": 500.0, "num_levels": 3, "slices": [{}]*3})
   == 0.0)

print("\n=== 2. THE LOAD-BEARING PROPERTY: no plan can need a sale ===")
def needs_a_sale(branches, result):
    by = {b["bot_name"]: b for b in branches}
    for s in result["sources"]:
        b = by[s["bot_name"]]
        after = b["allocated_usd"] - s["releases_usd"]
        if after < b["coin_basis_usd"] - 1e-9:
            return True                      # would have to sell coin
        if len(b["slices"]) < b["num_levels"]:
            return True                      # took a branch's live dry powder
    return False

random.seed(1105)
worst = None
for trial in range(4000):
    n = random.randint(1, 7)
    rows = []
    for i in range(n):
        alloc = round(random.uniform(0.0, 3000.0), 2)
        rows.append(B(f"b{i}", alloc=alloc,
                      basis=round(random.uniform(0.0, alloc + 500.0), 2),
                      levels=random.randint(1, 10),
                      slices=random.randint(0, 10),
                      net=random.choice([None, -5.0, 0.0, 0.5, 1.3, 4.0, 9.0]),
                      closes=random.choice([None, 0, 1, 2, 3, 9])))
    r = pm.plan(rows, loose_cash_usd=round(random.uniform(0, 500), 2))
    if needs_a_sale(rows, r):
        worst = (trial, rows, r); break
ok("4,000 randomised fleets: not one plan required a sale", worst is None)

print("\n=== 3. it never breaches the concentration ceiling ===")
random.seed(406)
breach = None
for trial in range(4000):
    rows = [B(f"b{i}", alloc=round(random.uniform(0.0, 2000.0), 2),
              basis=round(random.uniform(0.0, 300.0), 2),
              levels=random.randint(1, 10), slices=random.randint(0, 6),
              net=random.choice([None, 0.3, 1.5, 4.0, 8.0]),
              closes=random.choice([None, 1, 3, 8]))
            for i in range(random.randint(2, 6))]
    fleet = sum(b["allocated_usd"] for b in rows)
    r = pm.plan(rows, loose_cash_usd=round(random.uniform(0, 400), 2))
    for m in r["plan"]:
        # 0.20 is written out, NOT read from pm.MAX_SHARE_OF_FLEET. A test
        # that takes its expectation from the same constant it is checking
        # cannot fail when that constant is wrong.
        if fleet > 0 and m["allocated_usd_after"] > 0.20 * fleet + 0.011:
            breach = (trial, m, fleet); break
    if breach: break
ok("4,000 randomised fleets: the 20% ceiling was never breached", breach is None)
ok("and the ceiling really is 20% - pinned, not read from the module",
   pm.MAX_SHARE_OF_FLEET == 0.20)

print("\n=== 4. the regime gate stops the whole plan, not just one move ===")
dead = [B("src", alloc=1000.0, basis=100.0, levels=2, slices=2, net=0.9, closes=5),
        B("dst", alloc=100.0, basis=0.0, levels=3, slices=0, net=0.4, closes=5)]
r = pm.plan(dead, loose_cash_usd=500.0)
ok("nothing clearing the cost bar means no moves at all", r["plan"] == [])
ok("and the verdict says HOLD", r["verdict"] == "HOLD")
ok("the free capital is still reported, so the refusal is legible",
   r["capital_available_usd"] > 0)
ok("and nothing is moved", r["would_move_usd"] == 0.0)
# The gate is DEFENCE IN DEPTH for the moves: tier 0 requires beating the
# cost bar, so a dead regime has no tier-0 branch and the move list would be
# empty anyway. What the gate uniquely provides is the REASON - without it
# the caller is told "no branch has room" when the truth is "trading does
# not pay right now". Those are different problems with different fixes.
ok("the explanation names the dead regime, not a phantom room problem",
   "cost bar" in r["detail"] and "room" not in r["detail"])
ok("and it quotes the bar it measured against", f"{BAR:.4f}%" in r["detail"])

print("\n=== 5. a live regime does produce the move ===")
rows = [B("zec", alloc=1808.37, basis=622.05, levels=3, slices=3, net=0.8, closes=4),
        B("algo", alloc=168.66, basis=91.15, levels=3, slices=2, net=4.49, closes=7),
        B("hbar", alloc=329.28, basis=310.18, levels=10, slices=6, net=4.197, closes=7),
        B("xrp", alloc=2049.55, basis=1000.0, levels=10, slices=7, net=0.9, closes=4)]
r = pm.plan(rows, loose_cash_usd=75.80)
ok("ZEC is the source - full on rungs, above its coin basis",
   [s["product_id"] for s in r["sources"]] == ["zec-USD"])
ok("the released amount is exactly alloc minus coin basis",
   r["sources"][0]["releases_usd"] == 1186.32)
ok("capital available is the release plus the loose cash",
   r["capital_available_usd"] == round(1186.32 + 75.80, 2))
ok("the verdict is MOVE", r["verdict"] == "MOVE")
dests = {m["product_id"] for m in r["plan"]}
ok("XRP receives nothing - it is bottom tier AND over the ceiling",
   "xrp-USD" not in dests)
ok("ZEC does not fund itself", "zec-USD" not in dests)
ok("every destination clears the cost bar",
   all(m["net_pct_per_close"] > BAR for m in r["plan"]))
ok("every destination has somewhere to put it",
   all(m["empty_rungs"] > 0 for m in r["plan"]))
ok("the plan says plainly that it sells nothing",
   r["sells_nothing"] is True and "sale" in r["what_this_is_not"])

print("\n=== 5b. a bottom-tier branch with room is still refused funding ===")
# "meh" is eligible on every structural gate - empty rungs, tiny, well under
# the ceiling - and is refused on productivity ALONE. Without such a row the
# tier filter can be deleted without any test noticing.
rows_b = [B("zec",  alloc=1808.37, basis=622.05, levels=3, slices=3, net=0.8, closes=4),
          B("algo", alloc=100.0, basis=50.0, levels=3, slices=1, net=4.49, closes=7),
          B("meh",  alloc=20.0,  basis=5.0,  levels=5, slices=1, net=1.30, closes=9)]
rb = pm.plan(rows_b, loose_cash_usd=0.0)
_d = {m["product_id"] for m in rb["plan"]}
ok("the productive branch is funded", "algo-USD" in _d)
ok("the eligible-but-unproductive branch is NOT, though it has empty rungs "
   "and masses of ceiling room", "meh-USD" not in _d)
ok("and it really was structurally eligible - it was refused on tier alone",
   any(e["bot_name"] == "meh" for e in
       hr.candidates(rows_b, amount_usd=0.0,
                     fleet_allocated_usd=sum(b["allocated_usd"] for b in rows_b))[0]))

print("\n=== 5c. an UNPROVEN branch is not funded with blocks of claim ===")
# Stricter than harvest_redirect on purpose. The redirect places a few
# harvested dollars, where funding an unproven branch is cheap exploration.
# This moves blocks - $1,186.32 from one branch on the live fleet - and the
# same exploration costs a thousand times more for the same data point.
rows_c = [B("zec",  alloc=1808.37, basis=622.05, levels=3, slices=3, net=0.8, closes=4),
          B("algo", alloc=100.0, basis=50.0, levels=3, slices=1, net=4.49, closes=7),
          B("new",  alloc=20.0,  basis=5.0,  levels=5, slices=1, net=9.90, closes=2)]
rc = pm.plan(rows_c, loose_cash_usd=0.0)
_dc = {m["product_id"] for m in rc["plan"]}
ok("a branch with only 2 closes is NOT funded, however good those 2 look",
   "new-USD" not in _dc)
ok("the proven branch is funded instead", "algo-USD" in _dc)
ok("and harvest_redirect WOULD have ranked it above a bad branch - the two "
   "modules differ on purpose",
   hr.productivity_tier(None, 2.0) < hr.productivity_tier(0.1, 2.0))

print("\n=== 5d. no branch is filled to the ceiling on rank alone ===")
# The regression this pins: with only the 20% ceiling, the live fleet sent
# $1,299.48 of $1,385.70 into APE-USD - 20.00% of the book - because APE was
# nearest its buy line, while LINK-USD at 3.343%/close against APE's 2.044%
# got $86.22. A branch can only usefully take what its empty rungs can buy.
APE  = B("ape",  alloc=228.34, basis=100.0, levels=3,  slices=1, net=2.044, closes=7)
LINK = B("link", alloc=137.00, basis=50.0,  levels=10, slices=4, net=3.343, closes=6)
ZEC  = B("zec",  alloc=1808.37, basis=622.05, levels=3, slices=3, net=0.8, closes=4)
# A fourth row so the median sits BELOW APE and both top branches are in
# the plan. With three rows the median is APE itself, which would drop it to
# tier 2 and test nothing about sizing.
SOL  = B("sol",  alloc=129.60, basis=60.0, levels=4, slices=3, net=0.5, closes=5)
_rows_d = [APE, LINK, ZEC, SOL]
ok("the four rows really do put APE in the top tier",
   hr.productivity_tier(2.044, hr.fleet_median_net_pct(_rows_d)) == 0)
rd = pm.plan(_rows_d, loose_cash_usd=75.80, fleet_allocated_usd=7639.11)
mv = {m["product_id"]: m for m in rd["plan"]}
ok("APE's intake is capped by its 2 empty rungs, not by the ceiling",
   mv["ape-USD"]["add_usd"] <= pm.rung_capacity(
       {"allocated_usd": 228.34, "num_levels": 3, "empty_rungs": 2}) + 0.011)
ok("APE is nowhere near the 20% ceiling any more",
   mv["ape-USD"]["share_after_pct"] < 10.0)
ok("no branch in the plan lands at the ceiling",
   all(m["share_after_pct"] < 20.0 for m in rd["plan"]))
ok("every move says what limited it", all(m["limited_by"] for m in rd["plan"]))
ok("capital that cannot be deployed stays as cash, not as idle claim",
   rd["unplaced_usd"] > 0 and "stays as cash" in rd["detail"])
ok("and conservation still holds after capping",
   abs((rd["would_move_usd"] + rd["unplaced_usd"])
       - rd["capital_available_usd"]) < 0.011)
ok("rung capacity is zero when there are no empty rungs",
   pm.rung_capacity({"allocated_usd": 100.0, "num_levels": 3, "empty_rungs": 0}) == 0.0)
ok("and zero on unreadable inputs rather than a divide",
   pm.rung_capacity({"allocated_usd": 100.0, "num_levels": 0, "empty_rungs": 2}) == 0.0)

print("\n=== 6. capital is conserved - nothing is invented or lost ===")
ok("moved plus unplaced equals available",
   abs((r["would_move_usd"] + r["unplaced_usd"]) - r["capital_available_usd"]) < 0.011)
ok("no single move exceeds the pot",
   all(m["add_usd"] <= r["capital_available_usd"] + 0.011 for m in r["plan"]))
ok("every move is at least the live order minimum",
   all(m["add_usd"] >= pm.MIN_MOVE_USD for m in r["plan"]))

print("\n=== 7. an empty or unreadable fleet is handled, never crashes ===")
for bad in ([], None, [{}], [{"bot_name": "x"}]):
    try:
        rr = pm.plan(bad, loose_cash_usd=100.0)
        ok(f"{str(bad)[:18]:<18} -> no crash, no moves", rr["plan"] == [])
    except Exception as e:
        ok(f"{str(bad)[:18]:<18} -> no crash", False)

print("\n=== 8. the module cannot move money even if asked to ===")
CODE = "\n".join(l for l in open("portfolio_manager.py").read().splitlines()
                 if not l.strip().startswith("#"))
for forbidden in ("place_order", "place_market_buy", "place_market_sell",
                  "close_all_grid_slices", "withdraw_from_grid_branch",
                  "db.commit", "session.post", "await ", "requests."):
    ok(f"never references {forbidden}", forbidden not in CODE)

print(f"\n{'ALL PASSED' if not FAIL else str(len(FAIL)) + ' FAILED'}")
for f in FAIL: print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
