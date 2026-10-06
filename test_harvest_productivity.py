"""Where a harvested dollar goes is ranked on the one thing that persists.

MEASURED ON THIS FLEET'S OWN LEDGER, 2026-10-05, 125 own-trading closes
since the 2026-09-26 config epoch (adopted_exit rows excluded throughout):

  a branch's NET % PER CLOSE persists     Spearman +0.643, t=+3.03, 13 df
  a branch's NUMBER OF CLOSES does not    Spearman +0.007  - noise

Walk-forward on the live ledger, ranking on the PRIOR window's net% per
close and measuring the NEXT window, three out-of-sample steps:

  top half   +2.332%   +2.166%   +2.710%   per close
  bottom half +0.859%  +0.923%   +0.714%
  spread     +1.473    +1.243    +1.996    mean +1.570 pct-pts, 3 of 3

WHY AN EARLIER REALLOCATION TEST FAILED AND THIS ONE DID NOT. That test
ranked on TOTAL DOLLARS EARNED, which is net% x closes. Closes are the half
that does not persist, so half the ranking signal was noise and it lost out
of sample. Only the net% half is carried here. That is the whole difference
and it is the reason this file exists.

THE CAPITAL THIS IS AIMED AT, measured the same day: 48.6% of allocated
capital sat in the bottom six branches by net% per close (mean -0.148%)
while 17.1% sat in the top six (mean +3.834%). XRP-USD alone held
$2,049.55 - 35.2% of trading capital - at 0.900% per close, and it was the
most stable low reading in the fleet (0.992% then 0.898% across the halves).

Run: python3 test_harvest_productivity.py
"""
import os, sys

os.environ.pop("GRID_REGIME_COST_BAR_PCT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harvest_redirect as hr

FAIL = []
def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAIL.append(name)

BAR = hr.REGIME_COST_BAR_PCT          # 1.2003
FLEET = 10000.0

def B(name, *, net=None, closes=None, alloc=100.0, levels=3, slices=0,
      px=100.0, ref=100.0, gp=0.025, pnl=None, notional=None):
    """A branch row. px==ref and gp=2.5% puts it 2.5% from its buy line."""
    b = {"bot_name": name, "product_id": name + "-USD", "allocated_usd": alloc,
         "num_levels": levels, "slices": [{}] * slices,
         "current_price": px, "reference_price": ref, "grid_pct": gp}
    if net is not None: b["net_pct_per_close"] = net
    if closes is not None: b["closes"] = closes
    if pnl is not None: b["realized_usd"] = pnl
    if notional is not None: b["closed_notional_usd"] = notional
    return b

print("\n=== 1. the tier is BOTH bars, never just the median ===")
# median of (0.5, 0.9) is 0.7 -> 0.9 is "above median" but under the 1.2003 bar
ok("above the median but under the cost bar is NOT top tier",
   hr.productivity_tier(0.9, 0.7) == 2)
ok("above the median AND above the cost bar is top tier",
   hr.productivity_tier(3.5, 0.7) == 0)
ok("below the median is bottom tier even when it clears the bar",
   hr.productivity_tier(1.5, 2.0) == 2)
ok("exactly AT the cost bar is not above it",
   hr.productivity_tier(BAR, 0.1) == 2)
ok("exactly AT the median is not above it",
   hr.productivity_tier(3.0, 3.0) == 2)

print("\n=== 2. unknown is its own tier, between the two, never the bottom ===")
ok("no figure at all is tier 1", hr.productivity_tier(None, 1.0) == 1)
ok("no median to compare against is tier 1", hr.productivity_tier(5.0, None) == 1)
ok("tier 1 sorts ABOVE tier 2, so an unproven branch beats a bad one",
   hr.productivity_tier(None, 1.0) < hr.productivity_tier(0.1, 1.0))
ok("tier 1 sorts BELOW tier 0, so it never outranks a proven one",
   hr.productivity_tier(None, 1.0) > hr.productivity_tier(3.5, 1.0))

print("\n=== 3. a thin sample is not evidence ===")
ok(f"under {hr.MIN_CLOSES_FOR_PRODUCTIVITY} closes reads as no figure",
   hr._net_pct_per_close(B("x", net=9.9, closes=2)) is None)
ok("at the floor it is read",
   hr._net_pct_per_close(B("x", net=9.9, closes=3)) == 9.9)
ok("a missing close count is no figure",
   hr._net_pct_per_close(B("x", net=9.9)) is None)

print("\n=== 4. None and 0.0 stay different claims ===")
ok("a branch with enough closes that netted zero HAS a figure of 0.0",
   hr._net_pct_per_close(B("x", net=0.0, closes=10)) == 0.0)
ok("and 0.0 is bottom tier, not unknown",
   hr.productivity_tier(hr._net_pct_per_close(B("x", net=0.0, closes=10)), 1.0) == 2)
ok("net% can be derived from pnl and notional when not given directly",
   abs(hr._net_pct_per_close(B("x", closes=5, pnl=3.0, notional=100.0)) - 3.0) < 1e-9)
ok("zero notional is unknown, never a divide",
   hr._net_pct_per_close(B("x", closes=5, pnl=3.0, notional=0.0)) is None)

print("\n=== 5. the median is taken over EVERY branch handed in ===")
# FULL is ineligible (no empty rung) but must still count toward the median.
rows = [B("a", net=4.0, closes=5), B("b", net=0.2, closes=5),
        B("full", net=9.9, closes=5, levels=1, slices=1)]
ok("a branch that cannot take money still shapes the median",
   hr.fleet_median_net_pct(rows) == 4.0)
ok("branches with no figure are left out of the median entirely",
   hr.fleet_median_net_pct([B("a", net=4.0, closes=5), B("b", net=0.1, closes=1)]) == 4.0)
ok("no measurable branch means no median", hr.fleet_median_net_pct([B("a")]) is None)
ok("an even count averages the middle pair",
   hr.fleet_median_net_pct([B("a", net=1.0, closes=5), B("b", net=3.0, closes=5)]) == 2.0)

print("\n=== 6. the regime gate: a dead market leaves the money as cash ===")
dead = [B("a", net=0.9, closes=5, alloc=100.0), B("b", net=0.4, closes=5, alloc=100.0)]
rv = hr.regime_verdict(dead)
ok("nobody clearing the cost bar is NOT tradeable", rv["tradeable"] is False)
ok("and it says so with the bar in it", f"{BAR:.4f}%" in rv["detail"])
t, why = hr.pick_target(dead, amount_usd=5.0, fleet_allocated_usd=FLEET)
ok("pick_target declines in a dead regime", t is None)
ok("and it names a branch it WOULD have picked, so the refusal is legible",
   why.get("would_have_picked") is not None)
ok("the regime block travels with the answer", "regime" in why)

alive = [B("a", net=3.5, closes=5), B("b", net=0.4, closes=5)]
ok("one branch clearing the bar is enough to be tradeable",
   hr.regime_verdict(alive)["tradeable"] is True)
# EXACTLY at the bar is break-even, not profit: the cost is already in it.
at_bar = [B("a", net=BAR, closes=5), B("b", net=0.4, closes=5)]
ok("a branch netting EXACTLY the cost bar does not clear it",
   hr.regime_verdict(at_bar)["tradeable"] is False)
ok("and it is counted as zero clearing, not one",
   hr.regime_verdict(at_bar)["clearing_count"] == 0)
ok("it counts how many cleared", hr.regime_verdict(alive)["clearing_count"] == 1)

print("\n=== 7. unreadable is NOT dead - never refuse on an unread number ===")
blind = [B("a"), B("b")]
rb = hr.regime_verdict(blind)
ok("no measurable branch is still tradeable", rb["tradeable"] is True)
ok("and is flagged unreadable rather than passed off as measured",
   rb["readable"] is False)
ok("clearing_count is None, not 0, when nothing could be read",
   rb["clearing_count"] is None)
t, why = hr.pick_target(blind, amount_usd=5.0, fleet_allocated_usd=FLEET)
ok("a target is still chosen when the regime cannot be read", t is not None)

print("\n=== 8. tier outranks distance; distance still decides inside a tier ===")
# NEAR is 0.5% from buying but bottom tier; GOOD is 2.5% away and top tier.
NEAR = B("near", net=0.3, closes=5, px=100.0, ref=100.0, gp=0.005)
GOOD = B("good", net=4.0, closes=5, px=100.0, ref=100.0, gp=0.025)
t, why = hr.pick_target([NEAR, GOOD], amount_usd=5.0, fleet_allocated_usd=FLEET)
ok("the productive branch wins even though the other buys sooner", t == "good")
ok("and the reason says which side of the median it was on",
   "above the fleet median" in why["reason"])

# Two branches can never BOTH be above their own median - the median
# splits them - so a third row is needed to put two of them in one tier.
# With three rows the median IS the middle row, which would push g1 into
# the bottom tier and test nothing. Four rows put two cleanly above it:
# values 0.1, 0.2, 4.0, 4.2 -> median 2.1, so g1 and g2 share tier 0.
LO1 = B("lo1", net=0.1, closes=5, px=100.0, ref=100.0, gp=0.025)
LO2 = B("lo2", net=0.2, closes=5, px=100.0, ref=100.0, gp=0.025)
# g2 is given the SMALLER allocation on purpose. Allocation is the last
# tiebreak and prefers the smaller branch, so if distance-to-buy-line were
# ever dropped from the sort key, g2 would win - and this test would catch
# it. With equal allocations a stable sort returns g1 by input order alone
# and the test proves nothing.
G1  = B("g1",  net=4.0, closes=5, alloc=500.0, px=100.0, ref=100.0, gp=0.005)
G2  = B("g2",  net=4.2, closes=5, alloc=100.0, px=100.0, ref=100.0, gp=0.025)
_med = hr.fleet_median_net_pct([LO1, LO2, G1, G2])
ok("the four rows really do put g1 and g2 in the same tier",
   hr.productivity_tier(4.0, _med) == 0 and hr.productivity_tier(4.2, _med) == 0)
t2, why2 = hr.pick_target([LO1, LO2, G1, G2], amount_usd=5.0,
                          fleet_allocated_usd=FLEET)
ok("inside one tier the nearer-to-buying branch wins, not the higher net%",
   t2 == "g1")
ok("and it won on distance alone - it is both less productive AND bigger",
   G1["net_pct_per_close"] < G2["net_pct_per_close"]
   and G1["allocated_usd"] > G2["allocated_usd"])
ok("and the branch it beat really was more productive",
   G2["net_pct_per_close"] > G1["net_pct_per_close"])

print("\n=== 9. every gate the module already had is untouched ===")
SRC_ONLY = B("src", net=9.9, closes=9)
t3, _ = hr.pick_target([SRC_ONLY], exclude_bot_name="src", amount_usd=5.0,
                       fleet_allocated_usd=FLEET)
ok("the source branch is still refused however productive it is", t3 is None)
FULLB = B("full", net=9.9, closes=9, levels=2, slices=2)
t4, _ = hr.pick_target([FULLB], amount_usd=5.0, fleet_allocated_usd=FLEET)
ok("a branch with no empty rung is still refused", t4 is None)
BIG = B("big", net=9.9, closes=9, alloc=1900.0)
t5, _ = hr.pick_target([BIG], amount_usd=200.0, fleet_allocated_usd=10000.0)
ok("the 20% concentration ceiling still refuses a productive branch", t5 is None)
NOPX = B("nopx", net=9.9, closes=9); NOPX["current_price"] = None
t6, _ = hr.pick_target([NOPX], amount_usd=5.0, fleet_allocated_usd=FLEET)
ok("an unreadable price is still refused", t6 is None)

print("\n=== 10. the real fleet rows, 2026-10-05 ===")
XRP  = B("crypto_grid_6",  net=0.900, closes=4,  alloc=2049.55, levels=10, slices=7)
ALGO = B("crypto_grid_11", net=4.490, closes=7,  alloc=168.66,  levels=3,  slices=2)
HBAR = B("crypto_grid_13", net=4.197, closes=7,  alloc=329.28,  levels=10, slices=6)
LINK = B("crypto_grid_16", net=3.343, closes=6,  alloc=137.00,  levels=10, slices=4)
SOL  = B("crypto_grid_15", net=0.943, closes=2,  alloc=129.60,  levels=4,  slices=3)
LIVE = [XRP, ALGO, HBAR, LINK, SOL]
med = hr.fleet_median_net_pct(LIVE)
# measurable: XRP 0.900, LINK 3.343, HBAR 4.197, ALGO 4.490 -> median 3.770
ok("SOL's 2 closes are excluded from the median as a thin sample",
   abs(med - 3.770) < 1e-9)
FLEET_REAL = 7639.11          # total allocated across all 21 branches
elig, rej, reg = hr.candidates(LIVE, amount_usd=5.0,
                               fleet_allocated_usd=FLEET_REAL)
by = {e["bot_name"]: e for e in elig}
why_rej = {r["bot_name"]: r["why"] for r in rej}
ok("the live regime is tradeable", reg["tradeable"] is True)

# XRP never even reaches the ranking: $2,049.55 of $7,639.11 is 26.83%,
# already past the 20% ceiling, so the redirect cannot add to it at all.
ok("XRP is refused outright - it is already over the concentration ceiling",
   "crypto_grid_6" in why_rej and "ceiling" in why_rej["crypto_grid_6"])
ok("and that refusal happens without reading its productivity at all",
   "crypto_grid_6" not in by)

ok("ALGO at 4.490%/close is top tier", by["crypto_grid_11"]["productivity_tier"] == 0)
ok("LINK at 3.343%/close is below the 3.770 median, so bottom tier",
   by["crypto_grid_16"]["productivity_tier"] == 2)
ok("SOL is UNPROVEN on 2 closes, not bottom-tiered",
   by["crypto_grid_15"]["productivity_tier"] == 1)
ok("the harvested dollar goes to a top-tier branch",
   elig[0]["productivity_tier"] == 0)
ok("LINK ranks BELOW the unproven branch, because 3.343% is measured and under",
   [e["bot_name"] for e in elig].index("crypto_grid_16") >
   [e["bot_name"] for e in elig].index("crypto_grid_15"))

# A smaller XRP would be ranked rather than refused - and ranked last.
XRP_SMALL = dict(XRP, allocated_usd=140.0)
e2, _r2, _g2 = hr.candidates([XRP_SMALL, ALGO, HBAR, LINK, SOL],
                             amount_usd=5.0, fleet_allocated_usd=FLEET_REAL)
ok("under the ceiling XRP IS ranked, and lands in the bottom tier",
   {e["bot_name"]: e for e in e2}["crypto_grid_6"]["productivity_tier"] == 2)

print("\n=== 11. still moves nothing by itself ===")
CODE = "\n".join(l for l in open("harvest_redirect.py").read().splitlines()
                 if not l.strip().startswith("#"))
for forbidden in ("create_grid_branch", "place_market_buy", "place_order",
                  "close_all_grid_slices", "withdraw_from_grid_branch",
                  "db.commit", "session.post"):
    ok(f"never calls {forbidden}", forbidden not in CODE)

print(f"\n{'ALL PASSED' if not FAIL else str(len(FAIL)) + ' FAILED'}")
for f in FAIL: print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
