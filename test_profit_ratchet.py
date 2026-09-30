"""Profit ratchet, final design: only VERIFIED realized profit moves a tier;
1% tiers, half locked, locked never decreases, buys pause when realized
losses take trading capital below the floor, reconciliation fails closed,
the pre-arm seed needs a clean ledger, and nothing is ever sold."""
import asyncio, os, tempfile, inspect
_db = os.path.join(tempfile.mkdtemp(), "ratchet.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"

from datetime import datetime
import profit_ratchet as pr

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if not cond else ""))
    fails += 0 if cond else 1

# ---------- arithmetic, on the live numbers ----------
P = 8523.34 - 82.72
v, ev = pr.advance(P, 0, 0, 0, 82.72)
check("$82.72 is short of the first 1% tier", v["tier"] == 0 and v["locked_usd"] == 0, str(v))
check("tier size is 1% of trading capital", v["next_tier_size"] == round(P * 0.01, 2))
v, ev = pr.advance(P, 0, 0, 0, 90.0)
t1 = P * 0.01
check("crossing tier 1 locks half", v["tier"] == 1 and v["locked_usd"] == round(t1 / 2, 2))
check("unlocked half compounds into trading capital",
      abs(v["trading_capital"] - (P + 90.0 - t1 / 2)) < 0.01)
check("next tier is 1% of the NEW capital", v["next_tier_size"] == round((P + t1 / 2) * 0.01, 2))
check("floor = capital as the last tier locked", abs(v["floor"] - (P + t1 / 2)) < 0.01)
check("overflow above the floor -> buying", not v["buys_paused"])
v, ev = pr.advance(P, 0, 0, 0, 300.0)
check("large profit crosses several tiers", v["tier"] >= 3 and len(ev) == v["tier"])
L = v["locked_usd"]
v2, ev2 = pr.advance(P, v["tier"], v["consumed"], L, 150.0)
check("realized loss never unlocks", v2["locked_usd"] == L and not ev2)
check("realized loss below the floor -> buys paused", v2["buys_paused"])
v3, _ = pr.advance(P, v["tier"], v["consumed"], L, v["consumed"])
check("exactly at the floor -> still buying", not v3["buys_paused"])
v4, e4 = pr.advance(P, 0, 0, 0, 300.0, cross=False)
check("cross=False reports without crossing", v4["tier"] == 0 and not e4 and v4["locked_usd"] == 0)
v5, _ = pr.advance(0, 0, 0, 0, 50.0)
check("zero capital cannot loop or lock", v5["tier"] == 0 and v5["next_tier_size"] is None)
check("no price or unrealized input exists",
      not ({"fleet_equity", "price", "unrealized"} & set(inspect.signature(pr.advance).parameters)))

# reconciliation, through the real restart_recovery comparison
ok, f = pr.reconcile({"QNT-USD": 0.34}, {"QNT": 0.0}, {"QNT-USD": "0.001"})
check("phantom inventory (tracked, not held) fails closed", not ok and "QNT-USD SHORT" in f[0], str(f))
ok, _ = pr.reconcile({"BTC-USD": 0.01}, {"BTC": 0.05}, {"BTC-USD": "0.00000001"})
check("holding MORE than the grid tracks is fine", ok)
ok, _ = pr.reconcile({"BTC-USD": 0.01}, {"BTC": 0.01}, {"BTC-USD": "0.00000001"})
check("matched ledger passes", ok)
ok, _ = pr.reconcile({"BTC-USD": 0.01}, None, {})
check("unreadable balances fail closed", not ok)
ok, _ = pr.reconcile({"BTC-USD": 0.01}, {"BTC": 0.01}, {})
check("unknown increment fails closed", not ok)

# ---------- persisted state ----------
async def main():
    from database import init_db, get_session_factory
    await init_db()
    import crypto_grid_bot as g
    from models import CryptoGridTradeHistory, CryptoGridBranch

    recon = {"ok": True, "calls": 0}
    async def fake_reconcile(session=None):
        recon["calls"] += 1
        return recon["ok"], ([] if recon["ok"] else ["QNT-USD SHORT"])
    g.reconcile_grid_with_exchange = fake_reconcile

    async def bank(pnl):
        async with get_session_factory()() as db:
            db.add(CryptoGridTradeHistory(bot_name="b", product_id="X-USD", entry_price=1,
                                          exit_price=2, qty=1, pnl=pnl, closed_at=datetime.utcnow()))
            await db.commit()

    async with get_session_factory()() as db:
        db.add(CryptoGridBranch(bot_name="crypto_grid_t", product_id="X-USD", allocated_usd=1100.0,
                                grid_pct=0.02, num_levels=3, reference_price=1.0, active=True))
        await db.commit()
    await bank(100.0)          # principal 1000 -> $10 tiers

    s = await g.get_profit_ratchet_status()
    check("off by default", s["armed"] is False)
    check("off -> update does nothing", await g.update_profit_ratchet() is None)

    # seed refused while the ledger is not clean
    recon["ok"] = False
    a = await g.set_profit_ratchet_armed(True)
    s = await g.get_profit_ratchet_status()
    check("dirty ledger at arming -> seed refused", a["seed_credited"] is False and s["tier"] == 0
          and s["locked_usd"] == 0.0 and not s["seed_credited"])
    await bank(5.0)
    await g.update_profit_ratchet()
    check("refused seed never counts later", (await g.get_profit_ratchet_status())["tier"] == 0)

    # clean ledger: seed credited
    recon["ok"] = True
    a = await g.set_profit_ratchet_armed(True)
    s = await g.get_profit_ratchet_status()
    check("clean ledger at arming -> past profit credited", a["seed_credited"] and s["tier"] >= 1
          and s["locked_usd"] > 0)
    locked0, tier0 = s["locked_usd"], s["tier"]

    calls = recon["calls"]
    await g.update_profit_ratchet()
    check("no tier due -> no venue call", recon["calls"] == calls)

    await bank(30.0)
    recon["ok"] = False
    await g.update_profit_ratchet()
    s = await g.get_profit_ratchet_status()
    check("tier due + dirty ledger -> nothing locked (fail closed)",
          s["tier"] == tier0 and s["locked_usd"] == locked0)
    check("...and it reports the tier as pending", s["tiers_pending_reconciliation"] >= 1)
    recon["ok"] = True
    await g.update_profit_ratchet()
    s = await g.get_profit_ratchet_status()
    check("ledger clean again -> the tier locks", s["tier"] > tier0 and s["locked_usd"] > locked0)
    check("lock reaches the buy path", g._ratchet_locked_usd == s["locked_usd"])
    check("in profit -> buying", not g._ratchet_buys_paused)

    peak = s["locked_usd"]
    await bank(-60.0)
    await g.update_profit_ratchet()
    s = await g.get_profit_ratchet_status()
    check("realized loss: locked unchanged", s["locked_usd"] == peak)
    check("realized loss below the floor -> buys paused", g._ratchet_buys_paused and s["buys_paused"])

    spend, _ = g.spendable_for_slice(100.0, 200.0, deployment_reserve=g._ratchet_locked_usd)
    base, _ = g.spendable_for_slice(100.0, 200.0, deployment_reserve=0.0)
    check("locked cash is held back from a buy", spend < base, f"{spend} vs {base}")
    src = inspect.getsource(g.run_grid_branch_cycle)
    check("buy gate obeys the pause", "_ratchet_buys_paused" in src)
    check("buy sizing reserves the lock", "_dep_reserve + _ratchet_locked_usd" in src)
    for fn in (g.update_profit_ratchet, g.set_profit_ratchet_armed, g.get_profit_ratchet_status,
               g.reconcile_grid_with_exchange.__wrapped__ if hasattr(g.reconcile_grid_with_exchange, "__wrapped__") else fake_reconcile):
        body = inspect.getsource(fn)
        check(f"{fn.__name__} never sells", "grid_sell" not in body and "place_market" not in body)
    check("ratchet never reads fleet value", "fleet" not in inspect.getsource(g.update_profit_ratchet))

    await g.set_profit_ratchet_armed(False)
    s = await g.get_profit_ratchet_status()
    check("disarm releases lock and pause", s["armed"] is False and not g._ratchet_buys_paused
          and g._ratchet_locked_usd == 0.0)

async def real_reconcile():
    """The real reconcile_grid_with_exchange, with only venue calls faked."""
    import importlib, crypto_grid_bot as g
    importlib.reload(g)
    import account_census
    from database import get_session_factory
    from models import CryptoGridSlice
    async with get_session_factory()() as db:
        for q in (0.2, 0.14):   # two open QNT slices = 0.34 tracked
            db.add(CryptoGridSlice(bot_name="crypto_grid_q", product_id="QNT-USD", qty=q,
                                   entry_price=100.0))
        await db.commit()
    async def rules(session, pid):
        return {"base_increment": "0.001"}
    g.engine.get_product_rules = rules
    async def bal_phantom(session):
        return {"available": True, "held_including_zero": {"QNT": 0.0}}
    async def bal_real(session):
        return {"available": True, "held_including_zero": {"QNT": 0.34}}
    async def bal_down(session):
        return {"available": False, "error": "HTTP 429"}
    class S:
        async def close(self): pass
    account_census.fetch_balances = bal_phantom
    ok, f = await g.reconcile_grid_with_exchange(S())
    check("real reconcile: grid slices summed, phantom QNT fails closed", not ok and "QNT-USD" in f[0], str(f))
    account_census.fetch_balances = bal_real
    ok, _ = await g.reconcile_grid_with_exchange(S())
    check("real reconcile: coin actually held passes", ok)
    account_census.fetch_balances = bal_down
    ok, f = await g.reconcile_grid_with_exchange(S())
    check("real reconcile: rate-limited balance read fails closed", not ok)
    check("real reconcile: verdict published for the dashboard", g._ratchet_last_reconcile[0] is False)

asyncio.run(main())
asyncio.run(real_reconcile())
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
raise SystemExit(1 if fails else 0)
