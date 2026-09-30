"""Lock-in tiers on BANKED profit: 1% of trading capital per tier, half locked,
locked never goes down, buys pause at the floor, nothing is ever sold.
Runs against a throwaway SQLite DB."""
import asyncio, os, tempfile
_db = os.path.join(tempfile.mkdtemp(), "ratchet.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"

from datetime import datetime
import profit_ratchet as pr

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if not cond else ""))
    fails += 0 if cond else 1

# ---------- pure arithmetic, on the live numbers ----------
P = 8523.34 - 82.72          # principal: allocated minus profit it already carries
v, ev = pr.advance(P, 0, 0, 0, 82.72)
check("$82.72 banked is short of a $84.41 first tier", v["tier"] == 0 and v["locked_usd"] == 0,
      str(v))
check("next tier is 1% of trading capital", v["next_tier_size"] == round(P * 0.01, 2))
v, ev = pr.advance(P, 0, 0, 0, 90.0)
check("crossing tier 1 locks half of it", v["tier"] == 1 and v["locked_usd"] == round(P * 0.005, 2))
check("counter keeps the overflow", abs(v["progress_usd"] - (90.0 - P * 0.01)) < 0.01)
t1 = P * 0.01
check("tier 2 is 1% of the capital after the lock", v["next_tier_size"] == round((P + t1 - t1 / 2) * 0.01, 2))
v, ev = pr.advance(P, 0, 0, 0, 300.0)
check("large profit crosses several tiers at once", v["tier"] >= 3 and len(ev) == v["tier"])
locked_before = v["locked_usd"]
v2, ev2 = pr.advance(P, v["tier"], v["consumed"], v["locked_usd"], 150.0)
check("a realized LOSS never unlocks anything", v2["locked_usd"] == locked_before and not ev2)
check("...it moves the counter back instead", v2["progress_usd"] < 0)
v3, _ = pr.advance(P, 0, 0, 0, -40.0)
check("net losses lock nothing", v3["locked_usd"] == 0)
check("floor = anchor + locked SINCE arming", pr.floor_value(8000, 50, 20) == 8030.0)
check("pre-arm locks do not raise the floor twice", pr.floor_value(8000, 20, 20) == 8000.0)
check("no anchor -> no floor", pr.floor_value(0, 50, 0) is None)
check("exactly at the floor -> still buying", not pr.buys_paused(8030, 8030.0))
check("below the floor -> paused", pr.buys_paused(8029.99, 8030.0))
check("above the floor -> buying", not pr.buys_paused(8031, 8030.0))
check("unknown fleet value never pauses", not pr.buys_paused(None, 8030.0))

# ---------- persisted state, as the loop and dashboard use it ----------
async def main():
    from database import init_db, get_session_factory
    await init_db()
    import crypto_grid_bot as g
    from models import CryptoGridTradeHistory, CryptoGridBranch

    async def bank(pnl):
        async with get_session_factory()() as db:
            db.add(CryptoGridTradeHistory(bot_name="b", product_id="X-USD", entry_price=1,
                                          exit_price=2, qty=1, pnl=pnl, closed_at=datetime.utcnow()))
            await db.commit()

    async with get_session_factory()() as db:
        db.add(CryptoGridBranch(bot_name="crypto_grid_t", product_id="X-USD", allocated_usd=1100.0,
                                grid_pct=0.02, num_levels=3, reference_price=1.0, active=True))
        await db.commit()
    await bank(100.0)            # banked BEFORE arming

    s = await g.get_profit_ratchet_status()
    check("off by default", s["armed"] is False and s["locked_usd"] == 0.0)
    check("off -> update does nothing", await g.update_profit_ratchet(900.0) is None
          and not g._ratchet_buys_paused)

    await g.set_profit_ratchet_armed(True)
    s = await g.get_profit_ratchet_status()
    # principal = 1100 - 100 = 1000; tier = $10. $100 banked crosses tiers now.
    check("pre-arm profit is credited at once", s["tier"] >= 1 and s["locked_usd"] > 0, str(s))
    locked0 = s["locked_usd"]
    check("no floor until a full pass is measured", s["floor"] is None)

    await g.update_profit_ratchet(None)
    check("never anchors on an unknown fleet value",
          (await g.get_profit_ratchet_status())["anchor_equity"] is None)

    await g.update_profit_ratchet(1050.0)   # open losses already in here
    s = await g.get_profit_ratchet_status()
    check("anchors at first complete pass, open losses included", s["anchor_equity"] == 1050.0)
    check("pre-arm locks do not pause buying at arm", s["floor"] == 1050.0 and not g._ratchet_buys_paused)

    await bank(40.0)
    await g.update_profit_ratchet(1070.0)
    s = await g.get_profit_ratchet_status()
    check("new banked profit locks more", s["locked_usd"] > locked0)
    check("floor rises by exactly what was locked since arming",
          abs(s["floor"] - (1050.0 + s["locked_usd"] - locked0)) < 0.01)
    check("lock reaches the in-process buy mirror", g._ratchet_locked_usd == s["locked_usd"])

    peak_locked = s["locked_usd"]
    await bank(-80.0)
    await g.update_profit_ratchet(1000.0)
    s = await g.get_profit_ratchet_status()
    check("realized loss: locked unchanged", s["locked_usd"] == peak_locked)
    check("fleet below the floor -> buys paused", g._ratchet_buys_paused)
    await g.update_profit_ratchet(None)
    check("an unreadable pass keeps the last pause", g._ratchet_buys_paused)

    spend, _ = g.spendable_for_slice(100.0, 200.0, deployment_reserve=g._ratchet_locked_usd)
    base, _ = g.spendable_for_slice(100.0, 200.0, deployment_reserve=0.0)
    check("locked cash is held back from a buy", spend < base, f"{spend} vs {base}")
    import inspect
    src = inspect.getsource(g.run_grid_branch_cycle)
    check("buy gate obeys the pause", "_ratchet_buys_paused" in src)
    check("buy sizing reserves the lock", "_dep_reserve + _ratchet_locked_usd" in src)
    for fn in (g.update_profit_ratchet, g.set_profit_ratchet_armed, g.get_profit_ratchet_status):
        check(f"{fn.__name__} never sells", "grid_sell" not in inspect.getsource(fn)
              and "place_market" not in inspect.getsource(fn))
    check("tiers never read price or unrealized P&L",
          "fleet_equity" not in inspect.getsource(pr.advance))

    await g.set_profit_ratchet_armed(False)
    s = await g.get_profit_ratchet_status()
    check("disarm releases lock and pause", s["armed"] is False and not g._ratchet_buys_paused
          and g._ratchet_locked_usd == 0.0)

asyncio.run(main())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
