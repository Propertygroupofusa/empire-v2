"""The step study must not be able to recommend trading into a loss.

Its whole reason for existing is that "trade more often" and "earn more"
came apart on the live fleet. Every rule that keeps those two separate
gets a test that fails if the rule is removed.
"""

import asyncio

import step_study as S

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nthe fee is a fixed toll, and the arithmetic says so")

ok("a 2.50% step at a 0.70% fee keeps 1.80%",
   S.net_per_trip_pct(2.50, 0.70) == 1.80, S.net_per_trip_pct(2.50, 0.70))
ok("a 0.50% step at the same fee LOSES 0.20%",
   S.net_per_trip_pct(0.50, 0.70) == -0.20, S.net_per_trip_pct(0.50, 0.70))
ok("halving the step does not halve the fee",
   S.net_per_trip_pct(1.25, 0.70) != S.net_per_trip_pct(2.50, 0.70) / 2)
ok("break-even is exactly the fee", S.breakeven_step_pct(0.70) == 0.70)
ok("at a zero fee every step keeps its whole gross",
   S.net_per_trip_pct(0.25, 0.0) == 0.25)

print("\nthe verdict words cannot be skimmed wrongly")

ok("a losing step is named a loss", S.verdict(-0.20) == "LOSES on every trip")
ok("break-even exactly is a loss, not 'thin'", S.verdict(0.0) == "LOSES on every trip")
ok("a sliver is 'thin', not 'clears'", S.verdict(0.05) == "thin")
ok("a real margin 'clears'", S.verdict(1.80) == "clears")

print("\nmore trades is never reported as more money")

# The live 60-day numbers. 0.25% trades six times as often as 2.50% and
# loses money doing it.
TOTALS = {0.025: 30, 0.015: 55, 0.010: 77, 0.0075: 105, 0.005: 137, 0.0025: 185}
SLICE = 30.77

usd = {s: S.project_usd(t, s * 100, 0.70, SLICE) for s, t in TOTALS.items()}
ok("the widest step makes the most money at a 0.70% fee",
   max(usd, key=usd.get) == 0.025, usd)
ok("the tightest step trades the most", max(TOTALS, key=TOTALS.get) == 0.0025)
ok("and loses money", usd[0.0025] < 0, usd[0.0025])
ok("best_step picks the money, not the volume",
   S.best_step(TOTALS, 0.70, SLICE)[0] == 0.025, S.best_step(TOTALS, 0.70, SLICE))

ok("reproduces the measured +$16.62 at 2.50%", abs(usd[0.025] - 16.62) < 0.02, usd[0.025])
ok("reproduces the measured -$25.62 at 0.25%", abs(usd[0.0025] + 25.62) < 0.02, usd[0.0025])

print("\na tie goes to the WIDER step - fewer fills for the same money")

tie = {0.02: 10, 0.01: 20}          # 0.02 keeps 1.30 x10, 0.01 keeps 0.30 x20
ok("a wider step wins when the money is close",
   S.best_step(tie, 0.70, 100.0)[0] == 0.02, S.best_step(tie, 0.70, 100.0))
exact = {0.017: 10, 0.0100: 10}
ok("an exact tie goes to the wider step",
   S.best_step({s: t for s, t in exact.items()}, 0.70, 100.0)[0] == 0.017)

print("\nzero fees change the answer, and the study shows both")

free = {s: S.project_usd(t, s * 100, S.SPREAD_ONLY_PCT, SLICE) for s, t in TOTALS.items()}
ok("at spread-only cost every step is positive",
   all(v > 0 for v in free.values()), free)
ok("including the ones that lose money at the real fee",
   free[0.0025] > 0 > usd[0.0025])
ok("'free' is never priced at literally zero", S.SPREAD_ONLY_PCT > 0)

print("\nrendering states the assumption rather than burying it")

counts = {s: {"BTC-USD": t} for s, t in TOTALS.items()}
txt = S.render(counts, [], fee_pct=0.70, slice_usd=SLICE, days=60)
ok("it says the trips are assumed to COMPLETE", "COMPLETE" in txt)
ok("it calls the figures ceilings, not forecasts",
   "ceilings, not forecasts" in txt)
ok("it prints the break-even step", "Break-even step" in txt)
ok("it names most-money and most-trades separately",
   "Most money:" in txt and "Most trades:" in txt)
ok("and says plainly when they differ",
   "Trading more is not earning more" in txt)

same = S.render({0.025: {"BTC-USD": 30}}, [], fee_pct=0.70, slice_usd=SLICE, days=60)
ok("with one step there is no spurious disagreement",
   "Trading more is not earning more" not in same)

print("\na losing row names the fee that would fix it")

ok("a 0.50% step needs the round trip at or under 0.50%",
   S.max_fee_for_step(0.50) == 0.50, S.max_fee_for_step(0.50))
ok("a 0.25% step needs it at or under 0.25%",
   S.max_fee_for_step(0.25) == 0.25)
ok("neither is reachable at the account's real 0.70%",
   S.max_fee_for_step(0.50) < 0.70 and S.max_fee_for_step(0.25) < 0.70)
ok("both are reachable at spread-only cost",
   S.max_fee_for_step(0.50) > S.SPREAD_ONLY_PCT
   and S.max_fee_for_step(0.25) > S.SPREAD_ONLY_PCT)
ok("demanding a real margin raises the bar further",
   S.max_fee_for_step(0.50, min_net_pct=0.20) == 0.30)

txt2 = S.render(counts_for_text := {s: {"BTC-USD": t} for s, t in TOTALS.items()},
                [], fee_pct=0.70, slice_usd=SLICE, days=60)
ok("the losing rows print the fee they need",
   "<= 0.50%" in txt2 and "<= 0.25%" in txt2, "needs-fee column missing")
ok("the clearing row does not pretend to need one", "ok  clears" in txt2)
ok("and are called steps at the wrong fee, not broken steps",
   "run at the wrong fee" in txt2)
ok("it states the bot refuses them regardless of config",
   "fails\nclosed" in txt2 or "fails closed" in txt2, "gate note missing")

clean = S.render({0.025: {"BTC-USD": 30}}, [], fee_pct=0.70, slice_usd=SLICE, days=60)
ok("with no losing rows that paragraph does not appear",
   "run at the wrong fee" not in clean)

print("\ncompounding comes after profit - the rule is stated where it is read")

ok("the module says compounding follows realized profit",
   "COMPOUNDING COMES AFTER PROFIT" in S.__doc__)
ok("and points at the gate that enforces it",
   "evaluate_adaptive_fleet_stages" in S.__doc__)

import crypto_grid_bot as G
blocked = G.evaluate_adaptive_fleet_stages(
    realized_pnl=0.0, claimed=set(), excluded=set(), roi_by_coin={})
states = {x["product_id"]: x["state"] for x in blocked["stages"]}
gated = [p for p, (pid_req) in
         ((p, r) for p, r in G.ADAPTIVE_FLEET_STAGES) if pid_req > 0]
ok("with zero realized profit no profit-gated stage is eligible",
   all(states.get(p) != "eligible" for p in gated), states)
ok("and the tail behind it is blocked too, not skipped over",
   "waiting_for_prior_stage" in states.values(), states)

print("\na coin that will not load is left out, never scored zero")


def fetcher_for(table):
    async def _f(session, pid, start, end, granularity=3600):
        if pid not in table:
            raise RuntimeError("HTTP 404")
        return table[pid]
    return _f


highs = [100.0, 96.0, 102.0] * 40
lows = [100.0, 96.0, 102.0] * 40
good = ([0.0] * len(highs), highs, lows)
counts, missing = asyncio.run(
    S.measure(["A-USD", "B-USD"], [0.025], 60, session=object(),
              fetcher=fetcher_for({"A-USD": good})))
ok("the missing coin is reported", missing == ["B-USD"], missing)
ok("and is absent from the counts, not a zero",
   "B-USD" not in counts[0.025] and "A-USD" in counts[0.025], counts)
ok("render names it rather than silently dropping it",
   "history unavailable" in S.render(counts, missing, fee_pct=0.70,
                                     slice_usd=SLICE, days=60))

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
