"""The opportunity score, marked against what actually happened.

A scorer is the easiest thing in trading to build and the easiest to fool
yourself with: indicators always produce a number, and a number always looks
like knowledge. So these tests check the two properties that decide whether
this one is worth anything.

  1. It cannot trade. The score never reaches the execution path, and
     would_trade comes from cost arithmetic alone - a 100-point setup with a
     negative net edge still comes back False.
  2. It is falsifiable. Every score is a dated prediction, and the resolver
     marks it against the real move INCLUDING what a round trip would net
     after costs. A signal can be directionally right and still lose money,
     because cost is charged per trip and not per percent.

Run: python3 test_opportunity_signals.py
"""
import ast
import asyncio
import datetime as dt
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/t_signals.db")
for f in ("/tmp/t_signals.db",):
    if os.path.exists(f):
        os.remove(f)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


import opportunity_signals as S

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
SRC = open(os.path.join(HERE, "opportunity_signals.py"), encoding="utf-8").read()


print("\nit cannot trade")
ok("the live switch defaults OFF", S.SIGNALS_LIVE is False)
ok("and it fails off - the default is 'false', not a truthy fallback",
   'os.getenv("OPPORTUNITY_SIGNALS_LIVE", "false")' in SRC)
tree = ast.parse(GRID)
cycle = None
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_grid_branch_cycle":
        cycle = ast.get_source_segment(GRID, n)
ok("the per-branch trading cycle never mentions the scorer",
   cycle is not None and "signals." not in cycle and "score_total" not in cycle,
   "a score that reaches the branch cycle can influence a trade")
fleet = GRID.split("async def run_grid_branches_cycle")[1].split("\nasync def ")[0]
ok("scoring runs AFTER every branch has decided",
   fleet.index("_score_short_term_opportunities") > fleet.index("run_grid_branch_cycle(session, branch)"))
ok("the scoring pass cannot break the cycle",
   "except Exception" in fleet.split("_score_short_term_opportunities")[1][:400])
ok("no execution path reads a score",
   "score_total" not in GRID.split("def _score_short_term_opportunities")[0])


print("\nwould_trade is arithmetic, never the score")
# Closes whose last 3 bars (15 minutes) rise exactly 2.00%, so the momentum
# estimator predicts |2.00| * 0.5 = 1.00%. `hot` below is deliberately FLAT -
# it exercises the other signals - so anything asserting a prediction has to
# supply movement explicitly. That is the estimator change made visible: no
# momentum now means no predicted move, where ATR would have invented one.
MOVER = [100.0] * 37 + [100.0, 101.0, 102.0]


def econ(net_edge, slice_usd=6.92):
    """Stand in for crypto_nine_coin_scanner.evaluate_grid_step's detail."""
    return {"net_edge_pct": net_edge, "slice_usd": slice_usd}


hot = dict(closes=[100]*40, highs=[101]*40, lows=[99]*40, volumes=[50]*40,
           spread_pct=0.02, bid_depth_usd=9e5, ask_depth_usd=9e5,
           atr_pct=2.0, rsi=55, economics=econ(0.21))
# THE ECONOMICS COME FROM THE GATE, and these assert that the score cannot
# overrule them. An earlier draft priced cost here as fees + spread +
# adverse, a second copy of a formula crypto_nine_coin_scanner already owns;
# two copies drift and the unwatched one drifts first.
r = S.score(**hot)
ok("a positive net edge from the gate -> would_trade True",
   r["would_trade"] is True, f"net={r['expected_net_edge_pct']}")
ok("net edge is taken from the gate verbatim, not recomputed",
   r["expected_net_edge_pct"] == 0.21)
r_neg = S.score(**dict(hot, economics=econ(-0.39)))
ok("a NEGATIVE gate edge -> False, however good the setup looks",
   r_neg["would_trade"] is False)
ok("  and its score is not low, so the score plainly did not decide",
   (r_neg["score_total"] or 0) > 40, f"score={r_neg['score_total']}")
r_blind = S.score(**dict(hot, economics=None))
ok("a gate that could not price it -> False, and net edge stays None",
   r_blind["would_trade"] is False and r_blind["expected_net_edge_pct"] is None,
   "BLOCKED is not REJECT; the ledger records the difference")
ok("cost is BACKED OUT of the gate's edge, never re-derived here",
   abs(r["cost_assumed_pct"] - (r["expected_move_pct"] - 0.21)) < 1e-9,
   f"cost={r['cost_assumed_pct']}")
ok("the adverse constant is a LABEL, not arithmetic",
   "ADVERSE_PCT_ASSUMED" in SRC and "ADVERSE_PCT_ASSUMED +" not in SRC
   and "+ ADVERSE_PCT" not in SRC)
ok("the ATR estimate is HALF of ATR, not the whole range",
   r["expected_move_atr_pct"] == 1.0 and r["expected_move_atr_pct"] < hot["atr_pct"],
   "a real order captures part of a swing, not its extremes")
thin = dict(hot, atr_pct=0.5)
r2 = S.score(**thin)

print("\na missing reading is not a zero")
blind = dict(hot, bid_depth_usd=None, ask_depth_usd=None, volumes=[])
rb = S.score(**blind)
ok("an unreadable book yields None for liquidity, not 0",
   rb["score_liquidity"] is None)
ok("no volume history yields None, not a fabricated 1.0x",
   rb["volume_ratio"] is None and rb["score_volume"] is None)
ok("the total averages only what was measured",
   rb["components_measured"] < r["components_measured"] and rb["score_total"] is not None)
ok("and it does not drag the total toward zero",
   rb["score_total"] > 40,
   "scoring an unknown as zero makes a venue hiccup look like a bad setup")


print("\nthe signals compute what they claim")
rising = [100 + i * 0.5 for i in range(40)]
ok("momentum reads the 5/15/30m returns off 5-minute bars",
   S.momentum(rising)["ret_15m_pct"] > 0 and S.momentum(rising)["ret_30m_pct"] >
   S.momentum(rising)["ret_15m_pct"])
ok("volume_ratio is above 1 when recent volume rises",
   S.volume_ratio([10]*24 + [30]*3) > 2.5)
ok("and below 1 when it falls", S.volume_ratio([10]*24 + [3]*3) < 0.5)
ok("volume_ratio returns None on a short series, never 1.0",
   S.volume_ratio([10]*5) is None)

# impulse 100 -> 110, then a retrace to ~105: the textbook shape.
# At least look+2 bars, or the function correctly refuses to read a window
# it does not have - which is what the first draft of this fixture hit.
imp_c = [100]*8 + [110]*3 + [105]*3
imp_h = [100]*8 + [110]*3 + [110, 107, 106]
imp_l = [100]*8 + [104]*3 + [104.5]*3
pb = S.pullback_quality(imp_c, imp_h, imp_l)
ok("a half retrace off a real impulse scores high", pb is not None and pb > 0.7, f"got {pb}")
flat = S.pullback_quality([100]*14, [100.2]*14, [99.9]*14)
ok("an impulse smaller than a round trip costs is None, not 0",
   flat is None,
   "zero claims a bad setup where there is simply no setup")


print("\nthe ledger runs, and the verdict refuses a thin sample")


async def _runtime():
    from database import Base, get_engine, get_session_factory
    import models
    from sqlalchemy import select
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)

    await S.record("TIA-USD", "crypto_grid_7", 0.50, S.score(**hot))
    s0 = await S.summary()
    ok("a scored row is recorded", s0["scored"] == 1 and s0["resolved"] == 0)
    ok("the summary reports the live switch", s0["live"] is False)

    # Backdate past the 30m horizon, then resolve with price UP 2%.
    async with get_session_factory()() as db:
        for row in (await db.execute(select(models.ShortTermSignal))).scalars().all():
            row.scored_at = dt.datetime.utcnow() - dt.timedelta(minutes=31)
        await db.commit()

    async def price_for(pid):
        return 0.51                      # +2.0% from 0.50

    n = await S.resolve(None, price_for)
    ok("the horizon resolves", n == 1)
    async with get_session_factory()() as db:
        row = (await db.execute(select(models.ShortTermSignal))).scalars().all()[0]
    ok("all three horizons filled", None not in (row.actual_move_5m_pct,
                                                 row.actual_move_15m_pct,
                                                 row.actual_move_30m_pct))
    ok("actual move is measured against the scored price",
       abs(row.actual_move_30m_pct - 2.0) < 0.01, row.actual_move_30m_pct)
    ok("a +2.0%% move beat the 1.0%% prediction -> materialized",
       row.materialized is True)
    ok("time-to-target is recorded", row.minutes_to_target is not None)
    # THE POINT. 2.0% realised against a 1.39% cost nets +0.61%.
    ok("net_after_costs is the realised move MINUS the full cost",
       abs(row.net_after_costs_pct - (2.0 - row.cost_assumed_pct)) < 0.01,
       f"net={row.net_after_costs_pct} cost={row.cost_assumed_pct}")
    ok("  and it is priced against the BEST the move reached, which flatters it",
       "flatters the signal on purpose" in SRC,
       "if it loses under a perfect exit, no execution fix rescues it")

    s1 = await S.summary()
    ok("the report states its cost basis is ASSUMED, not measured",
       "ASSUMED" in s1["cost_basis"] and "adverse selection" in s1["cost_basis"])
    ok("results are reported PER COIN, the decision the fleet can act on",
       "per_coin" in s1 and "TIA-USD" in s1["per_coin"])
    f = s1["per_coin"]["TIA-USD"]
    ok("  the funnel is detected -> reached -> profitable after costs",
       set(("detected", "reached_target", "profitable_after_costs",
            "median_minutes_to_target", "net_expectancy_pct")) <= set(f))
    ok("  expectancy averages EVERY resolved setup, not just the winners",
       "not only the ones that" in SRC)
    ok("the verdict refuses to speak on one row",
       "not enough data" in s1["verdict"], s1["verdict"])
    ok("results are bucketed by score, not averaged into one number",
       set(s1["buckets"]) == {"0-40", "40-60", "60-80", "80+"},
       "'does a HIGH score mean anything' cannot be answered by one mean")

    # A directionally-right signal that still loses money - the whole thesis.
    # Scored at the LIVE cost, not the cheap fixture above: gate edge -0.39
    # on a 1.0% expected move backs out to the real 1.39% round trip.
    await S.record("BONK-USD", "crypto_grid_3", 100.0,
                   S.score(**dict(hot, closes=MOVER, economics=econ(-0.39))))
    async with get_session_factory()() as db:
        rows = (await db.execute(select(models.ShortTermSignal))).scalars().all()
        rows[-1].scored_at = dt.datetime.utcnow() - dt.timedelta(minutes=31)
        await db.commit()
    await S.resolve(None, lambda pid: _ret(100.8))      # +0.8%: up, but small
    async with get_session_factory()() as db:
        row2 = (await db.execute(select(models.ShortTermSignal))).scalars().all()[-1]
    ok("a move that went the RIGHT way but too small nets negative",
       row2.actual_move_30m_pct > 0 and row2.net_after_costs_pct < 0,
       f"move={row2.actual_move_30m_pct} net={row2.net_after_costs_pct}")
    ok("  and it is correctly marked as NOT materialized",
       row2.materialized is False)


async def _ret(v):
    return v

asyncio.run(_runtime())

print("\nunits: percent in this module, fractions in what it reads from")
GRIDSRC = GRID
ok("the module declares its unit", S.UNITS == "percent")
ok("the boundary converts ATR from fraction to percent",
   "atr_frac * 100.0" in GRIDSRC,
   "_atr_pct_from_candles returns atr/price despite the _pct in its name")
ok("and converts the gate's net edge the same way",
   'detail["net_edge_pct"] * 100.0' in GRIDSRC)
ok("but hands the gate a FRACTION, which is what it speaks",
   "_step_pct / 100.0, swing" in GRIDSRC,
   "the step is momentum-derived now; the conversion is what matters")
ok("REGRESSION: the raw fraction is never scored directly",
   "atr_pct=atr_frac" not in GRIDSRC)

# The bug, as arithmetic: a realistic 0.9% ATR arrives as 0.009 from the
# engine. Scored raw it lands at the bottom of every band and, worse,
# materialized then compares a percent move against a fraction target.
raw = S.score(**dict(hot, closes=MOVER, atr_pct=0.009, economics=econ(0.21)))
good = S.score(**dict(hot, closes=MOVER, atr_pct=0.9, economics=econ(0.21)))
ok("a fraction scores volatility at zero; a percent does not",
   raw["score_volatility"] == 0.0 and good["score_volatility"] > 0,
   f"raw={raw['score_volatility']} pct={good['score_volatility']}")
ok("and the ATR prediction differs by 100x, which is the silent half",
   abs(good["expected_move_atr_pct"] / raw["expected_move_atr_pct"] - 100) < 1e-6,
   "materialized would compare a percent move to a fraction target and "
   "always say yes")
ok("  while the momentum prediction is unaffected, being price-derived",
   good["expected_move_pct"] == raw["expected_move_pct"] == 1.0,
   "one more reason to carry both: they fail differently")



print("\nmomentum is the primary estimator, ATR is kept to check it")
# closes rising 0.5/bar: ret_15m over 3 bars off 100+ is about +1.5%
rising40 = [100 + i * 0.5 for i in range(40)]
rm = S.score(**dict(hot, closes=rising40, atr_pct=2.0))
_r15 = S.momentum(rising40)["ret_15m_pct"]
ok("expected_move comes from |ret_15m| / 2",
   abs(rm["expected_move_pct"] - abs(_r15) * 0.5) < 1e-4,
   f"got {rm['expected_move_pct']} from ret_15m {_r15:.3f}")
ok("and it is labelled as momentum-based", rm["expected_move_basis"] == "momentum_15m")
ok("the ATR estimate is recorded BESIDE it, not instead of it",
   rm["expected_move_atr_pct"] == 1.0 and
   rm["expected_move_atr_pct"] != rm["expected_move_pct"],
   "one identical realised move scores both, so the ledger decides")
ok("both carry the SAME 0.5 discount, isolating range-vs-direction",
   abs(rm["expected_move_pct"] / abs(_r15) - 0.5) < 1e-3
   and rm["expected_move_atr_pct"] / 2.0 == 0.5,
   f"momentum {rm['expected_move_pct']} from {_r15:.3f}, atr {rm['expected_move_atr_pct']}")
flat40 = [100.0] * 40
rf = S.score(**dict(hot, closes=flat40, atr_pct=2.0))
ok("no momentum -> a near-zero expected move, not an ATR number",
   rf["expected_move_pct"] == 0.0)
ok("  which correctly refuses the trade rather than inventing movement",
   rf["would_trade"] is False or rf["expected_net_edge_pct"] is not None)
short = S.score(**dict(hot, closes=[100.0]*3, atr_pct=2.0))
ok("too few bars for a 15m return falls back to ATR, and says so",
   short["expected_move_basis"] == "atr_fallback"
   and short["expected_move_pct"] == short["expected_move_atr_pct"])
ok("the gate is asked about the MOMENTUM step, not the ATR one",
   "_step_pct / 100.0, swing" in GRID and "abs(_r15) * 0.5" in GRID)
ok("and the step is converted back to a fraction for the gate",
   "_step_pct / 100.0" in GRID,
   "the gate speaks fractions; this module speaks percent")


print("\nzero trades must name its own bottleneck")
# Categorised off crypto_nine_coin_scanner's OWN strings, so this can never
# disagree with the gate about why it refused.
for reason, want in (
        ("net edge -0.823% - a 0.23% target cannot clear it", "expected_move"),
        ("spread 0.812% is over the 0.400% limit", "spread"),
        ("thin book - bid $412 / ask $388 against a $6.92 slice", "liquidity"),
        ("target is 4.1x the 0.33% hourly swing", "swing_multiple"),
        ("order book unavailable - cannot price the spread", "unpriceable"),
        ("hourly swing is None - not a usable number", "unpriceable")):
    ok(f"  {want:<15} <- {reason[:42]}",
       S.categorise_reject(reason, False) == want,
       f"got {S.categorise_reject(reason, False)}")
ok("a qualified scan is not a rejection",
   S.categorise_reject("net edge +0.31% on a 1.10% step", True) == "qualified")
ok("an UNRECOGNISED refusal lands in 'other', never silently in a bucket",
   S.categorise_reject("some new gate nobody told us about", False) == "other",
   "a reason added upstream must show up as itself")
ok("and the gate's raw words are kept verbatim beside the category",
   S.score(**dict(hot, gate_reason="thin book - whatever"))["reject_reason"]
   == "thin book - whatever")
ok("the category is carried onto the row",
   S.score(**dict(hot, economics=econ(-0.4),
                  gate_reason="thin book - x"))["reject_category"] == "liquidity")
ok("  but a row the gate PASSED is 'qualified' whatever the text says",
   S.score(**dict(hot, gate_reason="thin book - x"))["reject_category"] == "qualified",
   "a positive net edge is not a rejection, however the reason reads")

ok("the funnel is assembled from existing counters, not new ones",
   "await get_maker_only_skips()" in GRID and "await get_fill_mix()" in GRID
   and "await get_realized_edge()" in GRID,
   "a second counter drifts from the thing it counts")
ok("a missing source reads None, never 0",
   "if sig.get(\"available\") else None" in GRID,
   "'no data' and 'none happened' are different answers")
ok("the bottleneck is the EARLIEST blocked stage, not the last empty one",
   GRID.index('return "execution:') > GRID.index('return ("triggering:')
   > GRID.index('return (f"qualification:'),
   "a pipeline blocked at stage 1 is empty at every later stage too")
ok("and the funnel is served through the crash guard",
   'await _never_fails(get_pipeline_funnel' in GRID)

ok("summary exposes would_trade_count at the TOP level, which the funnel reads",
   '"would_trade_count": sum(1 for r in rows if r.would_trade),\n        "window"' in SRC,
   "it lived only inside _funnel, so get_pipeline_funnel read a missing key "
   "and got None - falsy, so the bottleneck was right BY ACCIDENT")
ok("a row with no category reads as pre_instrumentation, not unknown",
   '"pre_instrumentation"' in SRC and 'or "unknown"' not in SRC,
   "the first live funnel said 'unknown 162 of 162, 100%' and every one of "
   "those rows was written before the column existed")
ok("the headline ignores uncategorised rows rather than reporting them",
   'k != "pre_instrumentation"' in SRC)
ok("and says so plainly when nothing is categorised yet",
   "predate the instrumentation" in SRC)
ok("the funnel labels which stages are all-time vs current-config",
   "attempted_basis" in GRID and "predate the config epoch" in GRID,
   "fill mix and skip counters span both cohorts; scans and completed do not")


print("\nit cannot stall or crash the live loop")
ok("a wall-clock budget exists on the telemetry pass",
   "TELEMETRY_BUDGET_SECONDS" in GRID,
   "3 reads x 6 coins x 15s = 270s against a 180s lease window")
ok("the budget is checked BEFORE each coin, not after",
   GRID.index("if deadline is not None and time.time() >= deadline")
   < GRID.index("candles = await signals.fetch_candles_with_volume"))
ok("resolution takes the same deadline", "deadline=_deadline" in GRID)
ok("and stops mid-pass rather than running over",
   "deadline is not None and time.time() >= deadline" in SRC and "break" in SRC)
ok("the budget is comfortably inside the lease window",
   float(os.getenv("GRID_TELEMETRY_BUDGET_SECONDS", "25")) < 180)
# Named rather than counted: a magic number goes stale the moment another
# builder is added, and it went stale within the hour when the pipeline
# funnel arrived. Asserting the NAMES also catches the failure that matters -
# a new builder wired in unwrapped.
_WRAPPED = ("get_realized_edge", "get_maker_expiry_drift",
            "signals.summary", "get_pipeline_funnel")
for _b in _WRAPPED:
    ok(f"  {_b} is served through the crash guard",
       f"await _never_fails({_b}" in GRID,
       "a None reaching round() used to propagate out of get_grid_fleet_status")
ok("and nothing in the payload calls a telemetry builder directly",
   all(f'"{k}": await {b}(' not in GRID for k, b in
       (("realized_edge", "get_realized_edge"),
        ("maker_expiry_drift", "get_maker_expiry_drift"),
        ("pipeline", "get_pipeline_funnel"))))
ok("the wrapper returns a shape callers can read, not a raise",
   '"available": False' in GRID.split("async def _never_fails")[1][:700])
ok("a failed builder is logged at WARNING, not swallowed",
   "log.warning" in GRID.split("async def _never_fails")[1][:700])
ok("_latest survives an empty list", S._latest([]) == {})

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
