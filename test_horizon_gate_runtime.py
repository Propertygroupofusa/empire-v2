"""The horizon gate, exercised against a real database.

test_horizon_gate.py reads the source and proves the code SAYS the right
things. That is worth having - it is how the naming collision with
SHADOW_MODE was caught - but it cannot catch a column that does not exist, a
query that will not run, or an excursion bounded at the wrong epoch.

So this runs the whole path: score, record, back-date the row past the
six-hour window, resolve it against a synthetic candle series, and summarise.
The series is built so that the one assertion that matters has a wrong answer
available: it peaks at +4% INSIDE the window and +40% well outside it. A
resolver bounded at the wrong epoch returns 40 and every number downstream is
a fiction that looks like a triumph.

Run: python3 test_horizon_gate_runtime.py
"""
import asyncio, os, sys
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/t_hg_runtime.db"
for f in ("/tmp/t_hg_runtime.db",):
    if os.path.exists(f): os.remove(f)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta, timezone
from sqlalchemy import select
import opportunity_signals as S
from database import get_session_factory, init_db
from models import ShortTermSignal

# 6h of 5-minute bars, rising 0.1%/bar then flat: a real, directional move.
N = 200
closes = [100 * (1.001 ** i) for i in range(N)]
highs  = [c * 1.002 for c in closes]
lows   = [c * 0.998 for c in closes]
vols   = [1000.0] * N

async def main():
    await init_db()
    sc = S.score(closes=closes, highs=highs, lows=lows, volumes=vols,
                 spread_pct=0.02, bid_depth_usd=5000, ask_depth_usd=5000,
                 atr_pct=0.30, rsi=55,
                 economics={"net_edge_pct": -1.20, "slice_usd": 23.08},
                 gate_reason="net edge is negative")
    print("live  expected_move", sc["expected_move_pct"], "edge", sc["expected_net_edge_pct"],
          "would_trade", sc["would_trade"])
    print("gate  horizon move ", sc["horizon_gate_move_pct"],
          "range", sc["horizon_gate_move_range_pct"],
          "edge", sc["horizon_gate_edge_pct"], "would_trade", sc["horizon_gate_would_trade"])
    assert sc["horizon_gate_move_pct"] > sc["expected_move_pct"], "6h move must exceed 30m move"
    cost = sc["cost_assumed_pct"]
    assert abs((sc["horizon_gate_move_pct"] - cost) - sc["horizon_gate_edge_pct"]) < 1e-6, \
        "the gate must charge the SAME cost"
    print("cost held fixed at", cost, "for both")

    ok = await S.record("TEST-USD", "bot", closes[-1], sc)
    print("recorded:", ok)

    # Back-date it past the 6h window so the resolver sees it as due.
    t0 = datetime.utcnow() - timedelta(minutes=S.HORIZON_GATE_MIN + 30)
    async with get_session_factory()() as db:
        row = (await db.execute(select(ShortTermSignal))).scalars().first()
        row.scored_at = t0
        row.price_at_score = 100.0
        await db.commit()

    base = int(t0.replace(tzinfo=timezone.utc).timestamp())
    # A move that peaks at +4% INSIDE the window and +40% well outside it.
    times = [base + i * 300 for i in range(120)]
    hh = [104.0 if i == 10 else 101.0 for i in range(120)]
    ll = [96.0 if i == 5 else 99.5 for i in range(120)]
    cc = [100.0] * 120
    hh[-1] = 140.0                      # long past t0 + 6h; must NOT be counted
    async def candles_for(_pid):
        return (times, cc, hh, ll, [1.0] * 120)

    n = await S.resolve_horizon_gate(candles_for)
    print("resolved rows:", n)
    async with get_session_factory()() as db:
        r = (await db.execute(select(ShortTermSignal))).scalars().first()
        print("  mfe", r.horizon_gate_mfe_pct, "mae", r.horizon_gate_mae_pct,
              "net", r.horizon_gate_net_pct, "paid", r.horizon_gate_paid,
              "resolved_at", r.horizon_gate_resolved_at is not None)
        assert n == 1
        assert abs(r.horizon_gate_mfe_pct - 4.0) < 0.01, \
            f"MFE must be the 4% inside the window, not the 40% outside it: {r.horizon_gate_mfe_pct}"
        assert abs(r.horizon_gate_mae_pct - (-4.0)) < 0.01, r.horizon_gate_mae_pct
        assert r.horizon_gate_paid is True
        assert r.resolved_at is None, "the LIVE ledger must be untouched by this pass"

    again = await S.resolve_horizon_gate(candles_for)
    print("second pass re-resolves:", again, "(must be 0)")
    assert again == 0

    s = await S.summary()
    hg = s["horizon_gate"]
    print("summary:", {k: hg[k] for k in
                       ("horizon_minutes","scored","live_pass","horizon_pass",
                        "horizon_resolved","horizon_paid_pct")})
    print("verdict:", hg["verdict"])
    assert "not enough data" in hg["verdict"], "1 resolved must not produce a verdict"
    print("\nALL RUNTIME CHECKS PASSED")

try:
    asyncio.run(main())
except AssertionError as e:
    print(f"\nFAILED: {e}")
    sys.exit(1)
