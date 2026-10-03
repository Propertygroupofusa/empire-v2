"""Take the profit off redeployed money, instead of letting it ride.

THE OWNER'S INSTRUCTION, 2026-10-03: "Any money that you redeploy, make
sure you hurry up and take the profit off of them from now on."

Skim is off fleet-wide, so every winning sell compounds straight back into
its own branch's allocated_usd. That is the right default for a branch
building itself up, and it is the wrong one for money that was deliberately
moved somewhere to see whether it works: the gains never leave, so a good
week is indistinguishable from a flat one once the price turns.

This harvests. When a branch is FLAT it withdraws the profit it earned
since its baseline, leaving the capital to keep working.

WHY "SINCE ITS BASELINE" AND NOT "ABOVE SOME NUMBER". CryptoGridBranch has
exactly one money column, allocated_usd, and no record of what the branch
was originally funded with - five weeks of compounding and rotations are
already baked into it, and no amount of arithmetic recovers the split now.
So this never tries. It records, per branch, the cumulative realised P&L at
the moment it starts watching, and only ever harvests what is earned AFTER
that. Which is exactly what "from now on" means, and it makes the harvest
impossible to confuse with capital: every dollar it takes is a dollar that
appears in the trade ledger as a closed, realised, fee-adjusted win.

WHY FLAT ONLY. Not a policy - withdraw_from_grid_branch refuses any branch
holding an open slice, because that cash is already bought coin. So the
harvest happens at the moments a branch empties out, which is the only time
the money is cash at all.

WHERE IT GOES. Out of allocated_usd, which leaves it as unallocated cash in
the wallet. It is not re-deployed anywhere. Taking profit off the table is
the point; putting it straight back on would be the thing being replaced.

NOTHING HERE PLACES AN ORDER OR SELLS ANY COIN.
"""
import logging
import os

log = logging.getLogger(__name__)

BASELINE_PREFIX = "harvest_base:"

# Never leave a branch under this. withdraw_from_grid_branch deletes a row
# it drains below a cent, and a deleted branch takes its coin out of the
# fleet entirely - the same floor the idle rotation keeps.
KEEP_BRANCH_ALIVE_USD = 15.0

# Below this, harvesting costs more attention than it returns and churns
# allocated_usd for pennies.
MIN_HARVEST_USD = 10.0


async def realised_by_branch(session_factory):
    """{bot_name: cumulative realised P&L} straight from the trade ledger.

    The ledger is the only honest source: allocated_usd mixes compounding,
    funding and rotations together and cannot say which dollars were won.
    """
    from sqlalchemy import select

    from models import CryptoGridTradeHistory
    out = {}
    async with session_factory()() as db:
        rows = (await db.execute(select(CryptoGridTradeHistory))).scalars().all()
        for r in rows:
            if not r.bot_name:
                continue
            out[r.bot_name] = out.get(r.bot_name, 0.0) + float(r.pnl or 0.0)
    return out


async def _baseline(session_factory, bot_name, realised_now, create=True):
    """This branch's realised-P&L mark. Creates it at the current total.

    A branch seen for the first time is marked where it stands and harvests
    nothing on that pass. Marking it at zero would treat five weeks of
    history as freshly earned profit and withdraw it on sight.
    """
    from sqlalchemy import select

    from models import TradingBotState
    key = BASELINE_PREFIX + bot_name
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is None:
            if not create:
                return None
            db.add(TradingBotState(bot_name=key, base_capital=realised_now))
            await db.commit()
            return realised_now
        return float(row.base_capital or 0.0)


async def _advance_baseline(session_factory, bot_name, to_value):
    from sqlalchemy import select

    from models import TradingBotState
    key = BASELINE_PREFIX + bot_name
    async with session_factory()() as db:
        row = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name == key))).scalars().first()
        if row is not None:
            row.base_capital = to_value
            await db.commit()


def harvestable(branch, realised_now, baseline):
    """Dollars this branch could give back right now, and why not if zero.

    Three independent caps, all of which must hold:
      the branch is FLAT          - withdraw refuses anything else
      profit earned since baseline - never capital, only logged wins
      what is left stays >= $15    - a drained branch row is deleted
    """
    slices = branch.get("slices") or []
    if slices:
        return 0.0, f"holds {len(slices)} open slice(s); withdraw needs a flat branch"
    earned = round(realised_now - baseline, 2)
    if earned < MIN_HARVEST_USD:
        return 0.0, (f"${earned:,.2f} earned since baseline, under the "
                     f"${MIN_HARVEST_USD:,.2f} minimum")
    alloc = float(branch.get("allocated_usd") or 0.0)
    room = round(alloc - KEEP_BRANCH_ALIVE_USD, 2)
    if room < MIN_HARVEST_USD:
        return 0.0, (f"only ${room:,.2f} above the ${KEEP_BRANCH_ALIVE_USD:,.2f} "
                     f"keep-alive floor")
    return min(earned, room), None


async def plan(grid):
    """What a harvest would take, per branch. Reads only; writes nothing."""
    status = await grid.get_grid_status()
    branches = status.get("branches") or []
    realised = await realised_by_branch(grid.get_session_factory)
    rows, total = [], 0.0
    for b in branches:
        bot = b.get("bot_name")
        if not bot:
            continue
        now = round(realised.get(bot, 0.0), 2)
        base = await _baseline(grid.get_session_factory, bot, now)
        take, why_not = harvestable(b, now, base)
        row = {"bot_name": bot, "product_id": b.get("product_id"),
               "allocated_usd": round(float(b.get("allocated_usd") or 0.0), 2),
               "realised_total": now, "baseline": round(base, 2),
               "earned_since_baseline": round(now - base, 2),
               "harvest_usd": round(take, 2), "why_not": why_not}
        rows.append(row)
        total += take
    rows.sort(key=lambda r: -r["harvest_usd"])
    return {"branches": rows, "total_harvest_usd": round(total, 2),
            "ready": round(total, 2) >= MIN_HARVEST_USD}


async def run(grid, dry_run=True):
    """Harvest. dry_run=True (the default) computes and writes nothing."""
    if (os.getenv("STOP_TRADING", "false") or "").strip().lower() == "true":
        return {"ran": False, "reason": "STOP_TRADING"}

    p = await plan(grid)
    if dry_run:
        return dict(p, ran=False, dry_run=True,
                    detail=("PREVIEW ONLY - nothing was withdrawn. "
                            f"${p['total_harvest_usd']:,.2f} of realised profit "
                            f"is sitting in flat branches."))

    taken, failed = [], []
    for r in p["branches"]:
        if r["harvest_usd"] < MIN_HARVEST_USD:
            continue
        try:
            await grid.withdraw_from_grid_branch(r["bot_name"], r["harvest_usd"])
        except Exception as e:
            failed.append({**r, "error": f"{type(e).__name__}: {e}"})
            log.warning(f"[harvest] {r['product_id']} refused: {e}")
            continue
        # Advance the mark ONLY after the withdrawal succeeded. Advancing
        # first would forget profit that is still sitting in the branch.
        await _advance_baseline(grid.get_session_factory, r["bot_name"],
                                r["realised_total"])
        taken.append(r)
        log.warning(f"[harvest] took ${r['harvest_usd']:,.2f} of profit out of "
                    f"{r['product_id']}, leaving "
                    f"${r['allocated_usd'] - r['harvest_usd']:,.2f} working")
        try:
            await grid._log_activity_safe(
                None, r["product_id"], "HARVEST",
                f"Took ${r['harvest_usd']:,.2f} of realised profit off the table. "
                f"No order was placed and no coin was sold.")
        except Exception:
            pass
    total = round(sum(r["harvest_usd"] for r in taken), 2)
    return {"ran": bool(taken), "harvested_usd": total,
            "branches": taken, "failed": failed or None,
            "rows_written": len(taken),
            "detail": (f"${total:,.2f} of profit taken out of {len(taken)} "
                       f"branch(es)." if taken else
                       "Nothing was harvested - no flat branch had "
                       f"${MIN_HARVEST_USD:,.2f} of profit since its baseline.")}
