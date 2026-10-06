"""The measurement that must not become a router before it has earned it.

Run as written: python3 test_capital_recycle.py

Every assertion here is on pure functions - no database, no network, no
venue. The numbers in section [8] are the real fleet's, so a change that
silently alters the arithmetic is caught against measured ground truth
rather than against a fixture somebody invented.
"""

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")
import capital_recycle as cv

FAILS = []


def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(n):
    print("\n" + n)


T0 = datetime(2026, 9, 1, 12, 0, 0)


def trade(pid, qty, entry, pnl, open_h, hold_h, reason="profit_target"):
    return {"product_id": pid, "qty": qty, "entry_price": entry, "pnl": pnl,
            "opened_at": T0 + timedelta(hours=open_h),
            "closed_at": T0 + timedelta(hours=open_h + hold_h),
            "exit_reason": reason}


section("[1] a cycle with no duration or no capital is SKIPPED, never defaulted")
rows = cv.cycle_records([
    trade("A-USD", 10, 10, 1.0, 0, 24),
    {"product_id": "B-USD", "qty": 10, "entry_price": 10, "pnl": 1.0,
     "opened_at": None, "closed_at": T0},                    # no open time
    {"product_id": "C-USD", "qty": 0, "entry_price": 10, "pnl": 1.0,
     "opened_at": T0, "closed_at": T0 + timedelta(hours=1)},  # no capital
    trade("D-USD", 10, 10, 1.0, 0, 0),                        # zero hold
])
ok("only the one usable row survives", len(rows) == 1)
ok("and it is the one with both a capital and a duration",
   rows[0]["product_id"] == "A-USD")
ok("a skipped row contributes no capital-days",
   abs(sum(r["capital_days"] for r in rows) - 100.0) < 1e-9)

section("[2] capital-days price TIME as well as size")
a = cv.cycle_records([trade("A-USD", 1, 100, 1.0, 0, 240)])[0]   # $100 x 10 days
b = cv.cycle_records([trade("B-USD", 10, 100, 1.0, 0, 24)])[0]   # $1000 x 1 day
ok("$100 held ten days and $1,000 held one day are the same capital-days",
   abs(a["capital_days"] - b["capital_days"]) < 1e-9)
ok("and the same profit over them is the same velocity",
   abs(a["pnl"] / a["capital_days"] - b["pnl"] / b["capital_days"]) < 1e-12)

section("[3] the headline that dollars-only ranking cannot produce")
# The real shape of the XRP/HBAR finding: near-identical money, wildly
# different capital-days.
slow = cv.cycle_records([trade("XRP-USD", 1000, 1, 15.69, 0, 24 * 90)])
fast = cv.cycle_records([trade("HBAR-USD", 100, 1, 15.42, 0, 24)])
ok("the slow branch earns MORE dollars",
   slow[0]["pnl"] > fast[0]["pnl"])
ok("and is nonetheless far the worse capital engine",
   cv.fleet_velocity(fast)["usd_per_capital_day"]
   > cv.fleet_velocity(slow)["usd_per_capital_day"] * 100)
ok("an empty book reports UNKNOWN, not a zero rate",
   cv.fleet_velocity([])["usd_per_capital_day"] is None
   and "unknown_reason" in cv.fleet_velocity([]))

section("[4] recycle rate is per CYCLE, not per account")
r = cv.recycle_rate(cv.cycle_records([
    trade("A-USD", 10, 10, 1.0, 0, 24),
    trade("A-USD", 10, 10, 1.0, 48, 24),
]))
ok("$200 of cumulative deployment returning $202 is 1.01x",
   abs(r["multiple"] - 1.01) < 1e-9)
ok("the same dollar recycled twice is counted twice", r["deployed_usd"] == 200.0)
ok("the note says so, where a reader will see it",
   "CUMULATIVE" in r["means"])
ok("no deployment gives no multiple rather than a divide by zero",
   cv.recycle_rate([])["multiple"] is None)

section("[5] a sell with no buy after it is CENSORED, never zero")
sells = {"A-USD": [T0, T0 + timedelta(hours=100)]}
buys = {"A-USD": [T0 + timedelta(hours=2)]}
g = cv.redeploy_gaps(sells, buys)
ok("the one followed by a buy is measured", g["measured"] == 1)
ok("the one still waiting is counted separately", g["still_waiting"] == 1)
ok("it does not drag the median toward zero", g["median_hours"] == 2.0)
ok("a buy BEFORE the sell is never credited to it",
   cv.redeploy_gaps({"A-USD": [T0 + timedelta(hours=5)]},
                    {"A-USD": [T0]})["measured"] == 0)
ok("one branch's buy is never credited to another branch's sell",
   cv.redeploy_gaps({"A-USD": [T0]},
                    {"B-USD": [T0 + timedelta(hours=1)]})["measured"] == 0)

section("[6] thin per-branch samples are withheld, not ranked")
recs = cv.cycle_records([trade("A-USD", 10, 10, 1.0, i * 48, 24) for i in range(4)]
                        + [trade("B-USD", 10, 10, 9.0, 0, 24)])
bb = cv.by_branch(recs, min_cycles=3)
ok("the four-cycle branch is ranked",
   [r["product_id"] for r in bb["ranked"]] == ["A-USD"])
ok("the one-cycle branch is held back even though it looks far better",
   [r["product_id"] for r in bb["too_few_cycles"]] == ["B-USD"])
ok("and the reason is stated", "evidence" in bb["withheld_note"])

section("[7] inventory outranks price, and the trigger gap is named")
branches = [{
    "product_id": "ZEC-USD", "current_price": 100.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 1, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}, {
    "product_id": "SOL-USD", "current_price": 100.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 10, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}, {
    # target 103 sits BELOW this slice's break-even of 120 x 1.007
    "product_id": "OLD-USD", "current_price": 100.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 1, "entry_price": 120.0, "unrealized_net_pct": -0.17}],
}, {
    "product_id": "DUSTY-USD", "current_price": 100.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 0.001, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}]
cls = {c["product_id"]: c["state"] for c in
       cv.classify_slices(branches, short_products={"ZEC-USD"},
                          available_units={"SOL-USD": 2.0})}

# THE ORDER IS THE POINT, so it is tested where the orders DISAGREE.
# Both of these branches are past their trigger and in profit, so a
# price-first reading calls them sellable. One holds no coin and one has
# its units behind a resting order; neither can sell at any price. A first
# draft of this test used fixtures that were below their trigger, so
# reordering the function changed nothing and the test passed on a
# classifier that had price outrank inventory. Found by mutation.
conflict = [{
    "product_id": "SHORT-USD", "current_price": 110.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 1, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}, {
    "product_id": "LOCKED-USD", "current_price": 110.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 10, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}, {
    "product_id": "FREE-USD", "current_price": 110.0, "reference_price": 100.0,
    "grid_pct": 0.03,
    "slices": [{"qty": 1, "entry_price": 50.0, "unrealized_net_pct": 0.5}],
}]
cc = {c["product_id"]: c["state"] for c in
      cv.classify_slices(conflict, short_products={"SHORT-USD"},
                         available_units={"LOCKED-USD": 2.0})}
ok("past its trigger and IN PROFIT, a branch holding no coin is still "
   "ACCOUNTING_HOLD - inventory outranks price",
   cc["SHORT-USD"] == "ACCOUNTING_HOLD")
ok("past its trigger and IN PROFIT, coin behind a resting order is still "
   "INVENTORY_LOCKED", cc["LOCKED-USD"] == "INVENTORY_LOCKED")
ok("and the branch with neither problem is the only one that CAN_SELL_NOW",
   cc["FREE-USD"] == "CAN_SELL_NOW")
ok("a branch short of coin reads ACCOUNTING_HOLD, not a price wait",
   cls["ZEC-USD"] == "ACCOUNTING_HOLD")
ok("coin behind a resting order reads INVENTORY_LOCKED",
   cls["SOL-USD"] == "INVENTORY_LOCKED")
ok("a slice above its own branch trigger is NAMED, not called 'waiting'",
   cls["OLD-USD"] == "ABOVE_ITS_OWN_TRIGGER")
ok("a slice too small to sell reads DUST", cls["DUSTY-USD"] == "DUST")

t = cv.trapped_capital(cv.classify_slices(branches, {"ZEC-USD"}, {"SOL-USD": 2.0}))
ok("trapped counts the four states price will not release",
   t["by_state"].get("WAITING_ON_PRICE") is None
   and t["trapped_usd"] == t["deployed_usd"])
waiting = cv.trapped_capital(cv.classify_slices(
    [{"product_id": "W-USD", "current_price": 100.0, "reference_price": 100.0,
      "grid_pct": 0.03,
      "slices": [{"qty": 1, "entry_price": 99.0, "unrealized_net_pct": -0.01}]}]))
ok("a slice merely waiting on price is NOT counted as trapped",
   waiting["trapped_usd"] == 0.0 and waiting["deployed_usd"] == 99.0)

section("[8] the real fleet's measured numbers, as ground truth")
ok("fleet velocity is a profit-per-dollar-day RATE, not a percentage of account",
   "capital_days" in cv.fleet_velocity(cv.cycle_records([trade("A", 10, 10, 1, 0, 24)])))
tree = cv.capital_tree(
    {"cash_usd": 3772.47, "coin_usd": 6267.24, "total_usd": 10039.72,
     "assets_unpriced": 4},
    {"reserve_usd": 2000.0, "free_cash_usd": 70.67, "deployed_usd": 3942.72,
     "earmarked_behind_slices_usd": 7625.11})
ok("deployable now is the FREE cash, not the claim", tree["deployable_now_usd"] == 70.67)
ok("claim exceeding cash is stated as its own number",
   abs(tree["claim_minus_cash_usd"] - 3852.64) < 0.01)
ok("the coin no branch holds is reported", abs(tree["coin_no_branch_holds_usd"] - 2324.52) < 0.01)
ok("cash that is neither free nor reserve is named, not folded into available",
   abs(tree["other_cash_usd"] - 1701.80) < 0.01)
ok("unpriced assets are flagged as never-deploy", "never be deployed" in tree["unpriced_note"])
ok("an unreadable input yields None, never a zero that reads as 'none trapped'",
   cv.capital_tree({}, {})["deployable_now_usd"] is None)

section("[9] THE GATE - velocity may not route capital until it earns it")
# Six branches, ranks perfectly preserved across the split: the strongest
# case the real book could produce.
perfect = []
for i, pid in enumerate(["A", "B", "C", "D", "E", "F"]):
    for k in range(3):
        perfect.append(trade(f"{pid}-USD", 10, 10, (i + 1) * 0.1, k * 24, 24))
    for k in range(3):
        perfect.append(trade(f"{pid}-USD", 10, 10, (i + 1) * 0.1, 500 + k * 24, 24))
p = cv.predictive_check(cv.cycle_records(perfect))
ok("a perfectly preserved ranking at n=6 still clears its bar",
   p["spearman_rho"] == 1.0 and p["routing_allowed"] is True)
ok("the bar at n=6 is strict", p["critical_value_5pct"] == 0.886)

# The real book: rho +0.771 at n=6, which LOOKS convincing and is not.
ok("rho below the critical value refuses routing",
   cv._spearman_critical(6) > 0.771)

# A COMPUTED rho that falls short must set the flag False. The first draft
# only checked the perfect case (True) and the too-thin case (which short-
# circuits before the flag is computed at all), so a classifier hardwired
# to return True passed. Found by mutation. This book ranks six branches
# one way in the first half and swaps a middle pair in the second: a
# genuine, high, insufficient correlation - the live shape exactly.
shuffled = []
order_a = ["A", "B", "C", "D", "E", "F"]
# Swapping one ADJACENT pair is not enough: at n=6 that is rho 0.943,
# which clears the bar. Moving a branch two places produces rho +0.771 -
# the exact value the live book shows, and the exact value that looks
# convincing and is not significant.
order_b = ["C", "B", "A", "D", "E", "F"]
for i, pid in enumerate(order_a):
    for k in range(3):
        shuffled.append(trade(f"{pid}-USD", 10, 10, (i + 1) * 0.1, k * 24, 24))
for i, pid in enumerate(order_b):
    for k in range(3):
        shuffled.append(trade(f"{pid}-USD", 10, 10, (i + 1) * 0.1, 500 + k * 24, 24))
sh = cv.predictive_check(cv.cycle_records(shuffled))
ok("a near-miss rho is REPORTED, not rounded up",
   0.0 < sh["spearman_rho"] < sh["critical_value_5pct"])
ok("and a near-miss REFUSES routing - the flag is computed, not hardwired",
   sh["routing_allowed"] is False)
ok("its verdict says so in words", sh["verdict"] == "NOT STATISTICALLY SUPPORTED")
thin = cv.predictive_check(cv.cycle_records(
    [trade("A-USD", 10, 10, 1.0, i * 48, 24) for i in range(8)]))
ok("too few branches to test reads UNKNOWN and still refuses routing",
   thin["verdict"] == "UNKNOWN" and thin["routing_allowed"] is False)
ok("an empty book refuses routing", cv.predictive_check([]).get("routing_allowed") in (False, None))
ok("the flag a caller must read is named plainly", "routing_allowed" in p)

section("[10] this module cannot trade")
src = open("capital_recycle.py").read()
for bad in ("requests", "aiohttp", "place_order", "grid_sell", "grid_buy",
            "session.post", "import os"):
    ok(f"no {bad}", bad not in src)

section("[11] the recycle TAIL, which a median hides")
# Eight fast exits and two slow ones. With nine 1s and a single 100 the
# 90th percentile is 1, correctly - only the top tenth exceeds it - so the
# fixture needs a tail wide enough for p90 to land in.
d = cv.recycle_distribution([1, 1, 1, 1, 1, 1, 1, 1, 50, 100])
ok("the median stays near the fast cluster", d["p50_hours"] <= 2)
ok("and the 90th percentile shows the tail the median hid", d["p90_hours"] >= 50)
ok("p75 sits between them", d["p50_hours"] <= d["p75_hours"] <= d["p90_hours"])
ok("an empty sample is UNKNOWN, not a zero-hour recycle",
   cv.recycle_distribution([])["measured"] == 0
   and "unknown_reason" in cv.recycle_distribution([]))
ok("a None in the sample is dropped, never read as instant",
   cv.recycle_distribution([None, 5])["measured"] == 1)

section("[12] an OPEN slice accrues capital-days and has NO rate")
cl = cv.classify_slices(
    [{"product_id": "A-USD", "current_price": 100.0, "reference_price": 100.0,
      "grid_pct": 0.03,
      "slices": [{"qty": 1, "entry_price": 99.0, "unrealized_net_pct": -0.01,
                  "opened_at": "2026-10-01T00:00:00Z"}]}],
    now_epoch=1790899200.0)   # 2026-10-02T00:00:00Z, one day later
rec = cv.open_slice_records(cl)[0]
ok("it carries an age", rec["age_days"] is not None and abs(rec["age_days"] - 1.0) < 0.05)
ok("and the capital-days it has already cost",
   abs(rec["capital_days_accrued"] - 99.0) < 1.0)
ok("but NO $/capital-day - an unrealised mark is not a measurement",
   rec["usd_per_capital_day"] is None)
ok("and it says why, where a reader will see it",
   "unrealised price move" in rec["why_no_rate"])
no_ts = cv.classify_slices(
    [{"product_id": "B-USD", "current_price": 100.0, "reference_price": 100.0,
      "grid_pct": 0.03, "slices": [{"qty": 1, "entry_price": 99.0}]}],
    now_epoch=1790899200.0)
ok("an unreadable timestamp gives age None, never 0 - a zero age would make "
   "an old stuck slice report no capital cost at all",
   cv.open_slice_records(no_ts)[0]["age_days"] is None)
# A timestamp that is PRESENT but unparseable takes a different code path
# from one that is absent, and a first draft of this test only covered the
# absent case - so setting the except branch to 0.0 passed. Found by
# mutation. A zero age makes a stuck slice report no capital cost at all,
# which is the opposite of the truth and exactly the wrong way round.
for junk in ("not-a-date", "", "2026-13-45T99:99:99Z", 12345):
    bad = cv.classify_slices(
        [{"product_id": "C-USD", "current_price": 100.0, "reference_price": 100.0,
          "grid_pct": 0.03,
          "slices": [{"qty": 1, "entry_price": 99.0, "opened_at": junk}]}],
        now_epoch=1790899200.0)
    r = cv.open_slice_records(bad)[0]
    ok(f"an unparseable timestamp ({junk!r}) gives age None, never 0",
       r["age_days"] is None and r["capital_days_accrued"] is None)

section("[13] rungs free is not dollars available")
roll = cv.branch_rollup(
    cv.cycle_records([trade("A-USD", 10, 10, 1.0, i * 48, 24) for i in range(3)]),
    [{"product_id": "A-USD", "num_levels": 3,
      "slices": [{"qty": 1, "entry_price": 50.0}]}],
    cv.classify_slices([{"product_id": "A-USD", "current_price": 100.0,
                         "reference_price": 100.0, "grid_pct": 0.03,
                         "slices": [{"qty": 1, "entry_price": 50.0,
                                     "unrealized_net_pct": 0.5}]}]))[0]
ok("a branch with one of three rungs filled has two free",
   roll["available_rung_capacity"] == 2)
ok("closed cycles and open slices are counted separately",
   roll["completed_cycles"] == 3 and roll["open_slices"] == 1)
ok("a frozen branch is flagged rather than shown as able to buy",
   cv.branch_rollup([], [{"product_id": "F-USD", "num_levels": 3, "slices": [],
                          "drawdown_breached": True}], [])[0]["buys_frozen"] is True)

section("[14] THE RULE - unresolved capital never counts as available")
census = {"cash_usd": 3772.47, "coin_usd": 6267.24, "total_usd": 10039.72,
          "untracked_usd": 6026.33, "assets_unpriced": 4}
money = {"reserve_usd": 2000.0, "free_cash_usd": 70.67, "deployed_usd": 3942.72,
         "earmarked_behind_slices_usd": 7625.11}
nd = cv.never_deployable(census, money)
ok("coin is named as never-deployable", nd["buckets"]["coin_usd"] == 6267.24)
ok("unbranched capital is named", nd["buckets"]["unbranched_usd"] == 6026.33)
ok("the reserve is named", nd["buckets"]["reserved_cash_usd"] == 2000.0)
ok("unpriced assets are named", nd["buckets"]["unpriced_asset_count"] == 4)
ok("the rule is stated in the payload, not only in a comment",
   "never buy, never route, never count as available" in nd["rule"])
ok("the live numbers PASS the check", cv.assert_deployable_is_clean(census, money) == [])
ok("deployable larger than the cash that exists is caught",
   any(p["check"] == "deployable_exceeds_cash" for p in
       cv.assert_deployable_is_clean(census, {**money, "free_cash_usd": 5000.0})))
ok("treating the whole account as spendable is caught",
   any(p["check"] == "whole_account_treated_as_cash" for p in
       cv.assert_deployable_is_clean(census, {**money, "free_cash_usd": 10039.72})))
ok("a negative deployable is caught, not used as a floor to buy from",
   any(p["check"] == "negative_deployable" for p in
       cv.assert_deployable_is_clean(census, {**money, "free_cash_usd": -5.0})))
ok("an unreadable input is UNKNOWN, never a pass",
   cv.assert_deployable_is_clean({}, {}) != [])
ok("the check RETURNS findings rather than raising - a check that took the "
   "panel down would blind the owner at the moment he needs it",
   isinstance(cv.assert_deployable_is_clean(census, {**money, "free_cash_usd": 9e9}), list))

section("[15] every dollar lands in exactly one bucket, and they sum")
census = {"cash_usd": 3772.47, "coin_usd": 6267.24, "total_usd": 10039.72,
          "untracked_usd": 6026.33, "assets_unpriced": 4}
money = {"reserve_usd": 2000.0, "free_cash_usd": 70.67, "deployed_usd": 3942.72,
         "earmarked_behind_slices_usd": 7625.11}
dc = cv.classify_every_dollar(census, money, short_usd=0.0)
ok("there are exactly five buckets", set(dc["buckets"]) == set(cv.DOLLAR_BUCKETS))
ok("and none of them is called 'unallocated'",
   not any("UNALLOC" in b.upper() for b in dc["buckets"]))
ok("they sum to the venue total within a few cents of rounding", dc["balances"])
ok("branch-allocated cash is a RESIDUAL of cash, not the claim figure - "
   "claim exceeds the cash that exists by thousands",
   abs(dc["buckets"]["BRANCH_ALLOCATED"] - (3772.47 - 70.67 - 2000.0)) < 0.01)
ok("only the available bucket can buy",
   dc["notes"]["VERIFIED_AVAILABLE"].endswith("the only bucket that can buy"))
ok("unresolved carries the rule, not just a label",
   "NEVER buys" in dc["notes"]["UNRESOLVED"])
short = cv.classify_every_dollar(census, money, short_usd=622.05)
ok("coin a branch claims but does not hold leaves VERIFIED_COIN",
   short["buckets"]["VERIFIED_COIN"] < dc["buckets"]["VERIFIED_COIN"])
ok("and lands in UNRESOLVED instead",
   short["buckets"]["UNRESOLVED"] > dc["buckets"]["UNRESOLVED"])
ok("the totals still balance once it moves", short["balances"])
bad = cv.classify_every_dollar({**census, "total_usd": 99999.0}, money)
ok("a residual the buckets cannot explain is REPORTED, never absorbed",
   bad["balances"] is False and "residual_warning" in bad)
ok("a missing input is UNKNOWN rather than a bucket built on a guess",
   cv.classify_every_dollar({}, {})["readable"] is False)

section("[16] the recycle ledger - one row per completed sell")
T1 = datetime(2026, 10, 1, 12, 0, 0)
tr = [{"product_id": "A-USD", "qty": 2.0, "entry_price": 10.0, "exit_price": 11.0,
       "pnl": 1.8, "opened_at": T1, "closed_at": T1 + timedelta(hours=24)},
      {"product_id": "B-USD", "qty": 1.0, "entry_price": 50.0, "exit_price": 51.0,
       "pnl": 0.6, "opened_at": T1, "closed_at": T1 + timedelta(hours=48)}]
buysb = {"A-USD": [T1 + timedelta(hours=24, minutes=30)]}
fleet = [(T1 + timedelta(hours=24, minutes=5), "C-USD", 33.0),
         (T1 + timedelta(hours=49), "D-USD", 77.0)]
led = cv.recycle_ledger(tr, buysb, fleet)
ok("one row per sell", led["total_sells"] == 2)
ok("newest first, like every other feed here",
   led["rows"][0]["product"] == "B-USD")
a = [r for r in led["rows"] if r["product"] == "A-USD"][0]
ok("cash released is the exit price times quantity", a["cash_released_usd"] == 22.0)
ok("and says which basis it used, so nobody finds a phantom gap "
   "reconciling it against cost-plus-P&L",
   "exit price" in a["cash_released_from"])
ok("the hold is recorded in hours", a["hold_hours"] == 24.0)
ok("capital-days are per cycle", abs(a["capital_days"] - 20.0) < 1e-9)
ok("and so is the rate", abs(a["usd_per_capital_day"] - 0.09) < 1e-6)
ok("sell to next buy is in MINUTES, where the interesting end of this "
   "distribution lives", a["sell_to_next_buy_minutes"] == 30.0)
ok("the FLEET's next buy is recorded separately from the branch's own - "
   "cash is shared and a branch has no first claim on its own proceeds",
   a["next_branch"] == "C-USD" and a["redeployed_amount_usd"] == 33.0)
b_ = [r for r in led["rows"] if r["product"] == "B-USD"][0]
ok("a sell with no buy after it in its own branch is carried, not dropped",
   b_["still_waiting"] is True and b_["sell_to_next_buy_minutes"] is None)
ok("cumulative recycled sums every dollar that came back",
   abs(led["cumulative_recycled_usd"] - 73.0) < 1e-9)
ok("and says plainly that it is NOT the size of the account",
   "NOT the size of the account" in led["means"])
no_exit = cv.recycle_ledger(
    [{"product_id": "A-USD", "qty": 2.0, "entry_price": 10.0, "exit_price": None,
      "pnl": 1.8, "opened_at": T1, "closed_at": T1 + timedelta(hours=1)}], {}, [])
ok("a missing exit price falls back to basis plus P&L and SAYS so",
   no_exit["rows"][0]["cash_released_usd"] == 21.8
   and "cost basis plus" in no_exit["rows"][0]["cash_released_from"])

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
