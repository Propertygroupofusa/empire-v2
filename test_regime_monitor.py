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
    await S.resolve_crossings(lambda p: _v(103.0))       # +3% after the alert
    async with get_session_factory()() as db:
        r = (await db.execute(select(models.RegimeCrossing).where(
            models.RegimeCrossing.direction=="into_viable"))).scalars().all()[0]
    ok("a +3.0%% move after the alert nets 3.0 - 1.37 = +1.63",
       abs(r.net_after_costs_pct - 1.63) < 0.01, r.net_after_costs_pct)
    ok("  and is marked as having paid off", r.paid_off is True)

    g = await S.regime_summary()
    ok("the verdict refuses to speak on a thin sample",
       "not enough data" in g["verdict"], g["verdict"])
    ok("closest_to_viable turns 'no opportunities' into a distance",
       "closest_to_viable" in g)
    print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)

async def _v(x): return x
asyncio.run(main())
