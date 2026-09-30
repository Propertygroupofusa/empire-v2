"""Lock-in tiers: +5% steps, half of each step locked, buys pause at the floor.
Never sells. Runs against a throwaway SQLite DB."""
import asyncio, os, tempfile
_db = os.path.join(tempfile.mkdtemp(), "ratchet.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"

from datetime import datetime, timedelta
import profit_ratchet as pr

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1

# ---------- pure arithmetic ----------
v, ev = pr.advance(1000.0, 0, 0.0, 1040.0, 100.0)
check("below +5%: no tier, no floor, no pause", v["tier"] == 0 and v["floor"] is None and not v["buys_paused"])
v, ev = pr.advance(1000.0, 0, 0.0, 1050.0, 100.0)
check("at +5%: tier 1, locks half of $50 = $25", v["tier"] == 1 and v["locked_target"] == 25.0 and v["locked_usd"] == 25.0)
check("tier 1 floor is the starting value", v["floor"] == 1000.0)
v, ev = pr.advance(1000.0, 0, 0.0, 1110.0, 1000.0)
check("jump crosses two tiers at once", v["tier"] == 2 and len(ev) == 2, str(v))
check("tier levels compound (1102.50)", abs(pr.tier_level(1000, 2) - 1102.5) < 1e-9)
check("tier 2 locks 25 + 26.25", v["locked_target"] == 51.25)
check("next tier compounds from 1102.50", v["next_tier"] == round(1000 * 1.05 ** 3, 2))
v, _ = pr.advance(1000.0, 2, 51.25, 1060.0, 1000.0)
check("tier never goes down", v["tier"] == 2)
check("falls to 1050 floor -> no pause above it", not v["buys_paused"] and v["floor"] == 1050.0)
v, _ = pr.advance(1000.0, 2, 51.25, 1050.0, 1000.0)
check("at the floor -> buys paused", v["buys_paused"])
v, _ = pr.advance(1000.0, 1, 25.0, 1080.0, 10.0)
check("lock capped at realized cash", v["locked_usd"] == 10.0 and v["locked_unfunded"] == 15.0)
v, _ = pr.advance(1000.0, 1, 25.0, 1080.0, -50.0)
check("realized loss locks nothing", v["locked_usd"] == 0.0)
v, _ = pr.advance(1000.0, 2, 51.25, None, 1000.0)
check("unknown fleet value never pauses", not v["buys_paused"] and v["tier"] == 2)

# ---------- persisted state, as the loop uses it ----------
async def main():
    from database import init_db, get_session_factory
    await init_db()
    import crypto_grid_bot as g
    from models import CryptoGridTradeHistory

    s = await g.get_profit_ratchet_status()
    check("off by default", s["armed"] is False and s["locked_usd"] == 0.0)
    check("off -> update does nothing", await g.update_profit_ratchet(9000.0) is None and not g._ratchet_buys_paused)

    await g.set_profit_ratchet_armed(True)
    await g.update_profit_ratchet(None)
    s = await g.get_profit_ratchet_status()
    check("never anchors on an unknown fleet value", s["base"] is None)

    await g.update_profit_ratchet(8000.0)
    s = await g.get_profit_ratchet_status()
    check("anchors at first complete pass", s["base"] == 8000.0 and s["tier"] == 0)

    await g.update_profit_ratchet(8400.0)       # +5% on paper, nothing banked yet
    s = await g.get_profit_ratchet_status()
    check("tier 1 crossed and persisted", s["tier"] == 1)
    check("nothing locked until profit is banked", s["locked_usd"] == 0.0 and s["locked_unfunded"] == 200.0)

    async with get_session_factory()() as db:
        db.add(CryptoGridTradeHistory(bot_name="b", product_id="X-USD", entry_price=1, exit_price=2,
                                      qty=1, pnl=150.0, closed_at=datetime.utcnow()))
        db.add(CryptoGridTradeHistory(bot_name="b", product_id="X-USD", entry_price=1, exit_price=2,
                                      qty=1, pnl=999.0, closed_at=datetime.utcnow() - timedelta(days=3)))
        await db.commit()
    await g.update_profit_ratchet(8300.0)
    s = await g.get_profit_ratchet_status()
    check("only profit banked SINCE arming counts", s["realized_since_arm"] == 150.0)
    check("lock funds itself as sells bank", g._ratchet_locked_usd == 150.0 and s["locked_usd"] == 150.0)
    check("above the 8000 floor -> buying continues", not g._ratchet_buys_paused)

    await g.update_profit_ratchet(7990.0)
    check("fleet back at the locked tier -> buys paused", g._ratchet_buys_paused)
    await g.update_profit_ratchet(None)
    check("an unreadable pass keeps the last pause", g._ratchet_buys_paused)

    # the pause and the lock both reach the real buy path
    spend, why = g.spendable_for_slice(100.0, 200.0, deployment_reserve=g._ratchet_locked_usd)
    base_spend, _ = g.spendable_for_slice(100.0, 200.0, deployment_reserve=0.0)
    check("locked cash is held back from a buy", spend < base_spend, f"{spend} vs {base_spend}")
    import inspect
    src = inspect.getsource(g.run_grid_branch_cycle)
    check("buy gate obeys the pause", "_ratchet_buys_paused" in src)
    check("buy sizing reserves the lock", "_dep_reserve + _ratchet_locked_usd" in src)
    check("ratchet code never sells", "grid_sell" not in inspect.getsource(g.update_profit_ratchet))

    await g.set_profit_ratchet_armed(False)
    s = await g.get_profit_ratchet_status()
    check("disarm releases lock and pause", s["armed"] is False and not g._ratchet_buys_paused
          and g._ratchet_locked_usd == 0.0)

asyncio.run(main())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
