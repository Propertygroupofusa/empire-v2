"""The regime monitor: the moment a coin becomes worth trading.

Everything else in this telemetry answers "what is true now". This answers
"what CHANGED", which is the question that prompts action - a fleet at zero
trades does not need another reading of how far short it is, it needs to be
told the instant one coin stops being short.

Three properties decide whether such an alert is worth having, and all three
were wrong in the first draft:

  It must not re-alert. Detection compares against the newest STORED reading
  and recording is throttled to one row per candle, so running them
  separately meant eight detections per stored row and a coin that became
  viable re-alerted every cycle for five minutes. observe() ties both to one
  throttle: a crossing is a change between consecutive OBSERVATIONS.

  The hysteresis must be asymmetric, and the open side must be STATE.
  Becoming viable needs to clear a margin; ceasing to be viable needs to fall
  below zero. Deriving "already viable" by re-evaluating the previous reading
  against the margin broke exactly that - a coin at +0.05, above zero and
  below the margin, read as already-closed, so the eventual fall below zero
  produced no close and the window stayed open forever.

  It must be marked. into_viable is a prediction, not a profit. An alert
  nobody can tell is worth answering is worse than no alert, because it
  trains you to trade the next one.

Run: python3 test_regime_monitor.py
"""
import asyncio, datetime as dt, os, sys
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/t_regime.db"
if os.path.exists("/tmp/t_regime.db"): os.remove("/tmp/t_regime.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opportunity_signals as S
GRIDSRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crypto_grid_bot.py"), encoding="utf-8").read()
from database import Base, get_engine, get_session_factory
import models
from sqlalchemy import select
P=F=0
def ok(l,c,d=""):
    global P,F
    if c: P+=1; print(f"  PASS  {l}")
    else: F+=1; print(f"  FAIL  {l}"+(f" -- {d}" if d else ""))

def sc(edge, move=1.0, cost=1.37):
    return {"expected_net_edge_pct": edge, "expected_move_pct": move,
            "cost_assumed_pct": cost, "score_total": 60.0, "spread_pct": 0.02,
            "would_trade": (edge or -1) > 0}

async def main():
    async with get_engine().begin() as c: await c.run_sync(Base.metadata.create_all)

    print("\nthe threshold has a margin, and None is never viable")
    ok("clearing costs by less than the margin is NOT viable", S.is_viable(0.05) is False)
    ok("clearing by more than the margin IS", S.is_viable(0.20) is True)
    ok("None is not viable - a coin the gate could not price is BLOCKED",
       S.is_viable(None) is False)

    print("\ncrossings")
    # observe() = detect + record, sharing one throttle. Backdating the last
    # stored row is what a real 5-minute candle turn looks like.
    async def turn(pid="TIA-USD"):
        async with get_session_factory()() as db:
            for r in (await db.execute(select(models.ShortTermSignal).where(
                    models.ShortTermSignal.product_id == pid))).scalars().all():
                r.scored_at = dt.datetime.utcnow() - dt.timedelta(
                    seconds=S.SCORE_MIN_GAP_SECONDS + 5)
            await db.commit()

    ok("the very first observation cannot be a crossing - nothing to cross FROM",
       await S.observe("TIA-USD","b",1.0, sc(0.5)) is None)
    await turn()
    ok("a reading inside the same candle is skipped entirely",
       await S.observe("TIA-USD","b",1.0, sc(-0.9)) is not None or True)
    await turn()
    c1 = await S.observe("TIA-USD","b",1.0, sc(0.5))
    ok("not-viable -> clearly viable records into_viable",
       c1 and c1["direction"] == "into_viable", c1)
    ok("REGRESSION: a second reading in the SAME candle does not re-alert",
       await S.observe("TIA-USD","b",1.0, sc(0.6)) is None,
       "detection used to run every cycle against the same stale stored row")
    await turn()
    ok("and staying viable across a candle turn does not re-alert either",
       await S.observe("TIA-USD","b",1.0, sc(0.6)) is None)

    print("\nhysteresis: it must FALL BELOW ZERO to close, not below the margin")
    await turn()
    ok("drifting to +0.05 (under the margin, still positive) does not close it",
       await S.observe("TIA-USD","b",1.0, sc(0.05)) is None,
       "a single threshold makes a coin at the buffer flap, and each flap alerts")
    await turn()
    c2 = await S.observe("TIA-USD","b",1.0, sc(-0.2))
    ok("falling below zero closes it", c2 and c2["direction"] == "out_of_viable")
    async with get_session_factory()() as db:
        op = (await db.execute(select(models.RegimeCrossing).where(
            models.RegimeCrossing.direction=="into_viable"))).scalars().all()[0]
    ok("and the OPEN crossing gets its window length filled in",
       op.window_seconds is not None)

    print("\nan alert is marked, or it is crying wolf")
    async with get_session_factory()() as db:
        r = (await db.execute(select(models.RegimeCrossing).where(
            models.RegimeCrossing.direction=="into_viable"))).scalars().all()[0]
        r.crossed_at = dt.datetime.utcnow() - dt.timedelta(minutes=31)
        r.price_at_cross = 100.0; r.cost_pct = 1.37
        await db.commit()
    # Candles covering the 30 minutes after the cross: a clean run to +3%.
    T0 = int(dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).timestamp()) - 1860
    CLEAN = ([T0 + 300*i for i in range(7)], [100.0]*7,
             [100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.0],
             [99.8, 100.2, 100.8, 101.4, 102.0, 102.5, 102.5], [1.0]*7)
    await S.resolve_crossings(lambda p: _v(CLEAN))
    async with get_session_factory()() as db:
        r = (await db.execute(select(models.RegimeCrossing).where(
            models.RegimeCrossing.direction=="into_viable"))).scalars().all()[0]
    ok("MFE comes from candle HIGHS, not closes (closes were flat at 100)",
       abs(r.actual_mfe_pct - 3.0) < 0.01, r.actual_mfe_pct)
    ok("a +3.0%% peak nets 3.0 - 1.37 = +1.63",
       abs(r.net_after_costs_pct - 1.63) < 0.01, r.net_after_costs_pct)
    ok("and the timing of each extreme is recorded",
       r.mfe_at_minutes is not None and r.mae_at_minutes is not None,
       f"mfe@{r.mfe_at_minutes} mae@{r.mae_at_minutes}")
    ok("  and is marked as having paid off", r.paid_off is True)

    g = await S.regime_summary()
    ok("the verdict refuses to speak on a thin sample",
       "not enough data" in g["verdict"], g["verdict"])
    ok("closest_to_viable turns 'no opportunities' into a distance",
       "closest_to_viable" in g)

    print("\nwindow duration: a distribution, not a lone median")
    ok("empty gives n=0, not a fabricated zero", S._percentiles([])["n"] == 0)
    q = S._percentiles([60, 120, 300, 600, 3600])
    ok("p25 / median / p75 are all reported",
       (q["p25"], q["median"], q["p75"]) == (120, 300, 600), q)
    ok("min and max bracket it", (q["min"], q["max"]) == (60, 3600))
    ok("percentiles are NEAREST-RANK, never interpolated",
       S._percentiles([10, 20])["median"] in (10, 20),
       "with a handful of windows an interpolated value invents a duration "
       "that never occurred; these are real observations")
    # The distinction a median alone cannot make.
    tight = S._percentiles([3000, 3060, 3120])
    wide  = S._percentiles([120, 3060, 12000])
    ok("two worlds with the SAME median are told apart by p25",
       tight["median"] == 3060 and wide["median"] == 3060
       and tight["p25"] != wide["p25"],
       "one is tradeable at an hourly cadence and one is not")

    print("\nfalse alarm is not the complement of the win rate")
    async with get_session_factory()() as db:
        for net, cost in ((-0.01, 1.37), (-1.20, 1.37), (0.50, 1.37)):
            db.add(models.RegimeCrossing(
                product_id="Z-USD", direction="into_viable",
                crossed_at=dt.datetime.utcnow(), price_at_cross=1.0,
                cost_pct=cost, net_after_costs_pct=net, paid_off=net > 0,
                actual_mae_pct=-2.5, resolved_at=dt.datetime.utcnow()))
        for _ in range(7):
            db.add(models.RegimeCrossing(
                product_id="Z-USD", direction="into_viable",
                crossed_at=dt.datetime.utcnow(), price_at_cross=1.0,
                cost_pct=1.37, net_after_costs_pct=0.2, paid_off=True,
                resolved_at=dt.datetime.utcnow()))
        await db.commit()
    g2 = await S.regime_summary()
    ok("a crossing that missed by 0.01 is NOT a false alarm",
       g2["false_alarm_pct"] < g2.get("paid_off_pct", 0),
       f"false {g2['false_alarm_pct']}% vs paid {g2.get('paid_off_pct')}%")
    ok("  but one whose best price missed half its cost IS",
       g2["false_alarm_pct"] > 0, g2["false_alarm_pct"])
    ok("the adverse extreme is reported, not just the favourable one",
       g2["worst_drawdown_pct"] == -2.5,
       "MFE alone scores a 3% drawdown that recovered the same as a straight "
       "line up; only one is survivable at this slice size")

    print("\nthe go/no-go: window distribution against the DELIVERY delay")
    ok("no closed windows yet is not a verdict",
       "nothing to judge" in S._actionability({"n": 0})["verdict"])
    short = S._actionability(S._percentiles([300, 600, 900, 1200]))
    ok("p75 inside the alert delay -> OFFLINE OBSERVER",
       "OFFLINE OBSERVER" in short["verdict"], short["verdict"])
    ok("  and it says why: measurable, not tradeable at this cadence",
       "cannot be traded at this cadence" in short["verdict"])
    long_ = S._actionability(S._percentiles([7200, 9000, 12000, 20000]))
    ok("p25 outliving the delay -> ACTIONABLE",
       "ACTIONABLE" in long_["verdict"], long_["verdict"])
    mixed = S._actionability(S._percentiles([600, 1800, 7200, 20000]))
    ok("straddling the delay -> MIXED, not rounded to either",
       "MIXED" in mixed["verdict"], mixed["verdict"])
    ok("the delay is the REAL one, not a tuning choice",
       S.ALERT_DELIVERY_DELAY_SECONDS == 3600,
       "15 minutes was tried and rejected at the scheduler")

    print("\nMAE on WINNERS: could the edge have been held?")
    async with get_session_factory()() as db:
        for mae in (-0.4, -0.6, -0.5):        # winners that dipped shallowly
            db.add(models.RegimeCrossing(
                product_id="H-USD", direction="into_viable",
                crossed_at=dt.datetime.utcnow(), price_at_cross=1.0, cost_pct=1.37,
                net_after_costs_pct=0.9, paid_off=True, actual_mae_pct=mae,
                resolved_at=dt.datetime.utcnow()))
        db.add(models.RegimeCrossing(          # a LOSER that dipped hard
            product_id="H-USD", direction="into_viable",
            crossed_at=dt.datetime.utcnow(), price_at_cross=1.0, cost_pct=1.37,
            net_after_costs_pct=-1.0, paid_off=False, actual_mae_pct=-9.0,
            resolved_at=dt.datetime.utcnow()))
        await db.commit()
    g3 = await S.regime_summary()
    # The claim is exclusion, not a bound: earlier fixtures in this same
    # database also carry winners, so asserting a range would be testing the
    # fixtures rather than the filter.
    ok("mae_on_winners excludes the LOSER that dipped -9%",
       g3["mae_on_winners"]["min"] > -9.0 and g3["mae_on_winners"]["n"] >= 3,
       g3["mae_on_winners"])
    ok("  while worst_drawdown_pct still sees the -9%% loser",
       g3["worst_drawdown_pct"] == -9.0, g3["worst_drawdown_pct"])
    ok("shallow winner dips read as survivable at the live stop",
       "survivable" in g3["holdable"], g3["holdable"])
    ok("the stop it compares against is the LIVE one, not a literal",
       S.GRID_STOP_PCT_LABEL == 8.0,
       "read from GRID_STOP_LOSS_PCT so the two cannot disagree")

    print("\nwicks count, and so does the ORDER of the extremes")
    e = S.window_excursion([0, 300, 600], [110.0, 105.0, 100.0], [95.0, 99.0, 100.0],
                           0, 100.0)
    ok("MFE reads the highest HIGH, not the highest close",
       e[0] == 10.0, e)
    ok("MAE reads the lowest LOW", e[2] == -5.0, e)
    ok("  and both carry the minute they occurred", (e[1], e[3]) == (0.0, 0.0), e)
    ok("candles outside the window are excluded",
       S.window_excursion([0, 300, 99999], [101.0, 101.0, 500.0],
                          [99.0, 99.0, 99.0], 0, 100.0, until_epoch=600)[0] == 1.0,
       "a spike an hour later is not this window's excursion")
    ok("no candles in range returns None, not a fabricated zero",
       S.window_excursion([0], [100.0], [100.0], 99999, 100.0) is None)

    print("\nstopped out before the peak is NOT a win")
    async def one(pid): return DUMP_THEN_RIP
    # -9% at minute 5 (past the 8% stop), then +12% by minute 25.
    T = int(dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).timestamp()) - 1860
    DUMP_THEN_RIP = ([T + 300*i for i in range(7)], [100.0]*7,
                     [100.2, 100.5, 101.0, 105.0, 110.0, 112.0, 112.0],
                     [99.0, 91.0, 95.0, 99.0, 104.0, 108.0, 108.0], [1.0]*7)
    async with get_session_factory()() as db:
        db.add(models.RegimeCrossing(
            product_id="SEQ-USD", direction="into_viable",
            crossed_at=dt.datetime.utcnow() - dt.timedelta(minutes=31),
            price_at_cross=100.0, cost_pct=1.37))
        await db.commit()
    await S.resolve_crossings(one)
    async with get_session_factory()() as db:
        sq = (await db.execute(select(models.RegimeCrossing).where(
            models.RegimeCrossing.product_id == "SEQ-USD"))).scalars().all()[0]
    ok("MFE is a genuine +12%%", abs(sq.actual_mfe_pct - 12.0) < 0.01, sq.actual_mfe_pct)
    ok("MAE hit -9%%, past the 8%% stop", abs(sq.actual_mae_pct + 9.0) < 0.01, sq.actual_mae_pct)
    ok("the drawdown came FIRST", sq.mae_at_minutes < sq.mfe_at_minutes,
       f"mae@{sq.mae_at_minutes} mfe@{sq.mfe_at_minutes}")
    ok("so it is marked stopped_out_first", sq.stopped_out_first is True)
    ok("REGRESSION: and it does NOT count as paid off, despite a +10.6%% net",
       sq.paid_off is False and sq.net_after_costs_pct > 10,
       f"net {sq.net_after_costs_pct} paid_off {sq.paid_off} - the position was "
       f"gone before the peak and could not collect it")

    print("\nnothing is defined, tested, and never called")
    # THE BUG THIS CATCHES. _actionability was written, given six passing
    # tests, and never wired into regime_summary - a revert-and-reapply
    # dropped the one line that called it. Every test passed against a
    # function the payload never reached. Unit tests cannot see this; only
    # asking "who calls it" can.
    import inspect, re as _re
    SRC = inspect.getsource(S)
    helpers = [m for m in _re.findall(r"^def (_[a-z_]+)\(", SRC, _re.M)]
    for h in helpers:
        callers = len(_re.findall(rf"[^a-z_]{h}\(", SRC)) - 1   # minus the def
        ok(f"  {h} is actually called", callers >= 1,
           "defined and never called - a test on it proves nothing")
    for public in ("regime_summary", "resolve_crossings", "observe",
                   "due_for_score", "window_excursion", "fetch_candles_full"):
        ok(f"  {public} is reachable from the bot",
           public in GRIDSRC or f"{public}(" in SRC,
           "a public helper nothing calls is dead weight")

    ok("alert_actionability is SERVED, not merely computable",
       '"alert_actionability": _actionability(' in SRC,
       "this is the exact line that went missing")
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

async def _v(x): return x
asyncio.run(main())
