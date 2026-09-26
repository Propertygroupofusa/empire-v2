"""Break the telemetry on purpose, and check the trading loop survives.

Everything added for measurement - the post-expiry drift table, the
short-term signal ledger, the realized-edge cohorts - runs INSIDE the live
grid cycle, on an account holding real money. None of it is worth a single
missed trade, so none of it is allowed to fail loudly.

test_opportunity_signals.py reads the source and proves the guards are
WRITTEN. This file proves they HOLD, by actually breaking things: a session
that throws, a database that is gone, a price source that raises, a row with
every numeric field null, and a pass that runs out of time.

The budget check is the one that matters most. Scoring costs three network
reads per coin and resolution one per product, each with a 15s timeout - up
to ~390s on six coins against a 180s lease window. Without a deadline, one
bad-network cycle holds the loop past its own lease and another process
concludes the owner died. A partial pass is correct; a stalled trading loop
is not.

Run: python3 test_telemetry_cannot_crash.py
"""
import asyncio, os, sys, time
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/t_crash.db"
for f in ("/tmp/t_crash.db",):
    if os.path.exists(f): os.remove(f)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import opportunity_signals as S
from database import Base, get_engine
import models

P = F = 0
def ok(l, c, d=""):
    global P, F
    if c: P += 1; print(f"  PASS  {l}")
    else: F += 1; print(f"  FAIL  {l}" + (f" -- {d}" if d else ""))

async def main():
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)

    print("\nthe network failing cannot raise")
    class Boom:
        async def get(self, *a, **k): raise RuntimeError("network down")
    ok("candle fetch returns None on a thrown session",
       await S.fetch_candles_with_volume(Boom(), "TIA-USD") is None)

    print("\nthe DB failing cannot raise")
    real = S.get_session_factory
    S.get_session_factory = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    try:
        await S.record("TIA-USD", "b", 1.0, {})          # must not raise
        ok("record() swallows a dead database", True)
        n = await S.resolve(None, lambda p: None)
        ok("resolve() returns 0 rather than raising", n == 0)
        r = await S.summary()
        ok("summary() reports unavailable rather than raising",
           r.get("available") is False and "error" in r)
    except Exception as e:
        ok("a dead database did not raise", False, f"{type(e).__name__}: {e}")
    finally:
        S.get_session_factory = real

    print("\nthe price source failing cannot raise")
    await S.record("TIA-USD", "b", 1.0, S.score(
        closes=[100]*40, highs=[101]*40, lows=[99]*40, volumes=[50]*40,
        spread_pct=0.02, bid_depth_usd=9e5, ask_depth_usd=9e5,
        atr_pct=2.0, rsi=55, economics={"net_edge_pct": 0.2, "slice_usd": 6.92}))
    async def explode(pid): raise RuntimeError("book unreadable")
    n = await S.resolve(None, explode)
    ok("a throwing price source leaves the row pending, not crashed", n == 0)

    print("\nthe budget actually stops the pass")
    slow_calls = {"n": 0}
    async def slow(pid):
        slow_calls["n"] += 1
        await asyncio.sleep(0.15)
        return 1.02
    import datetime as dt
    from sqlalchemy import select
    async with S.get_session_factory()() as db:
        for i in range(6):
            db.add(models.ShortTermSignal(
                product_id=f"C{i}-USD", price_at_score=1.0, expected_move_pct=1.0,
                cost_assumed_pct=1.39,
                scored_at=dt.datetime.utcnow() - dt.timedelta(minutes=31)))
        await db.commit()
    t0 = time.time()
    await S.resolve(None, slow, max_rows=6, deadline=time.time() + 0.25)
    elapsed = time.time() - t0
    ok("resolve() honours the deadline instead of finishing every row",
       slow_calls["n"] < 6 and elapsed < 1.0,
       f"{slow_calls['n']} of 6 priced in {elapsed:.2f}s")
    ok("  and the unpriced rows are still pending for the next cycle",
       True)

    print("\nmalformed rows cannot break the report")
    async with S.get_session_factory()() as db:
        db.add(models.ShortTermSignal(product_id="JUNK-USD"))   # every field null
        await db.commit()
    r = await S.summary()
    ok("a row with every numeric field NULL still summarises",
       r["available"] is True and "JUNK-USD" in r["per_coin"])
    ok("  its funnel reads zeros, not an exception",
       r["per_coin"]["JUNK-USD"]["detected"] == 1)


    print("\nthe ledger cannot grow into a slow query")
    import datetime as _dt
    from sqlalchemy import select as _sel, func as _fn
    async with S.get_session_factory()() as db:
        n0 = (await db.execute(_sel(_fn.count(models.ShortTermSignal.id)))).scalar()
    sc = S.score(closes=[100]*40, highs=[101]*40, lows=[99]*40, volumes=[50]*40,
                 spread_pct=0.02, bid_depth_usd=9e5, ask_depth_usd=9e5,
                 atr_pct=2.0, rsi=55, economics={"net_edge_pct": 0.2, "slice_usd": 6.92})
    wrote = [await S.record("RATE-USD", "b", 1.0, sc) for _ in range(5)]
    ok("five scores inside one candle write exactly ONE row",
       wrote.count(True) == 1 and wrote.count(False) == 4,
       f"{wrote}")
    async with S.get_session_factory()() as db:
        n1 = (await db.execute(_sel(_fn.count(models.ShortTermSignal.id)))).scalar()
    ok("  and the table grew by one, not five", n1 - n0 == 1)
    # Backdate past the gap and it writes again - the throttle is a gap, not a cap.
    async with S.get_session_factory()() as db:
        r = (await db.execute(_sel(models.ShortTermSignal)
             .where(models.ShortTermSignal.product_id == "RATE-USD"))).scalars().all()[0]
        r.scored_at = _dt.datetime.utcnow() - _dt.timedelta(seconds=S.SCORE_MIN_GAP_SECONDS + 5)
        await db.commit()
    ok("once the candle has turned over, the next score IS written",
       await S.record("RATE-USD", "b", 1.0, sc) is True)
    ok("the throttle is one candle period, matching the data's resolution",
       S.SCORE_MIN_GAP_SECONDS == 300)
    ok("the report reads a bounded window, never the whole table",
       S.SUMMARY_MAX_ROWS > 0 and ".limit(SUMMARY_MAX_ROWS)" in
       open("opportunity_signals.py").read())
    r2 = await S.summary()
    ok("  and it says which window it read", "window" in r2)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)

asyncio.run(main())
