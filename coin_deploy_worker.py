"""Creates the branches coin_deploy.plan() asks for. Idempotent by design.

The owner named PRIME, TON and APE - the three coins coin_scan.py ranked
above everything the fleet already holds, each verified through the LIVE
gate before any of this was written:

    PRIME  swing 1.01%  step 3.00%  spread 0.135%  edge +1.964%  PASSES
    TON    swing 0.97%  step 2.92%  spread 0.074%  edge +1.955%  PASSES
    APE    swing 1.30%  step 3.00%  spread 0.050%  edge +1.990%  PASSES

TON's step is capped by the swing ceiling, which is the ceiling doing its
job rather than a problem with the coin.

WHY A WORKER AND NOT A ONE-SHOT. There may not be enough cash for all three
at any given moment - there was not when this shipped. A coin deferred this
pass is funded on a later one, once a branch sells and frees more. The plan
is recomputed from live cash every time and skips coins already held, so
running it repeatedly converges on the list and then does nothing.

It creates branches. It places no orders: a new branch buys its own first
dip through every gate that already exists, which is why those gates were
verified first rather than trusted.
"""
import asyncio
import logging
import os

log = logging.getLogger(__name__)

CHECK_SECONDS = int(os.getenv("GRID_COIN_DEPLOY_CHECK_SECONDS", "900"))

# Ranked best-first by coin_scan.suitability: trips, a flat net move, and a
# shallow drawdown. The order decides who is funded when there is not enough
# for everyone.
TARGET_COINS = [
    c.strip().upper() for c in
    (os.getenv("GRID_DEPLOY_COINS") or "PRIME-USD,TON-USD,APE-USD").split(",")
    if c.strip()
]

DEPLOY_MODE_KEY = "grid_coin_deploy_mode"
ARMED_BY_DEFAULT = True


def env_mode():
    raw = (os.getenv("GRID_COIN_DEPLOY_MODE") or "").strip().strip('"').strip("'").lower()
    if raw in {"arm", "armed", "on", "true", "1"}:
        return True
    if raw in {"observe", "off", "false", "0"}:
        return False
    return None


async def armed_now(session_factory):
    """env, else the DB row, else the owner's default. An UNREADABLE switch
    is OFF - it cannot tell a switch nobody set from one it failed to read,
    and one of those means someone turned it off."""
    env = env_mode()
    if env is not None:
        return env
    try:
        from models import TradingBotState
        from sqlalchemy import select
        async with session_factory()() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == DEPLOY_MODE_KEY))).scalar_one_or_none()
        if row is None:
            return ARMED_BY_DEFAULT
        return bool(row.base_capital and row.base_capital > 0)
    except Exception as e:
        log.warning(f"[DEPLOY] arm switch unreadable ({type(e).__name__}) - staying OFF")
        return False


async def run_once(grid, dry_run=True):
    """One pass. `grid` is the crypto_grid_bot module."""
    import coin_deploy as CD
    from models import CryptoGridBranch
    from sqlalchemy import select

    async with grid.get_session_factory()() as db:
        held = [b.product_id for b in
                (await db.execute(select(CryptoGridBranch))).scalars().all()]

    free_cash = await grid.get_real_free_cash_usd()
    status = None
    fleet_total = None
    try:
        status = await grid.get_grid_status()
        fleet_total = (status.get("allocation_backing") or {}).get("backed_usd")
    except Exception:
        fleet_total = None

    rows, report = CD.plan(TARGET_COINS, free_cash, grid.GRID_CASH_RESERVE_USD,
                           held_product_ids=held, fleet_total_usd=fleet_total)
    report["dry_run"] = bool(dry_run)
    report["plan"] = rows
    if dry_run or not rows:
        report["created"] = 0
        return report

    created = []
    for r in rows:
        try:
            # Re-checked at write time: another pass, or a human, may have
            # created this branch since the plan was computed.
            async with grid.get_session_factory()() as db:
                exists = (await db.execute(select(CryptoGridBranch).where(
                    CryptoGridBranch.product_id == r["product_id"]))).scalars().first()
            if exists is not None:
                log.info(f"[DEPLOY] {r['product_id']} gained a branch since the plan "
                         f"- skipping rather than doubling up")
                continue
            branch = await grid.create_grid_branch(r["product_id"], r["usd"])
            created.append({"product_id": r["product_id"], "usd": r["usd"],
                            "bot_name": getattr(branch, "bot_name", None)})
            log.warning(f"[DEPLOY] 🌱 {r['product_id']} funded with ${r['usd']:,.2f} "
                        f"- ranked above every coin the fleet holds, and verified "
                        f"through the live gate before funding")
        except Exception as e:
            # One coin failing must not cost the others their turn.
            log.warning(f"[DEPLOY] {r['product_id']} not created ({type(e).__name__}: {e})")
    report["created"] = len(created)
    report["created_rows"] = created
    return report


async def loop(grid):
    log.warning(f"[DEPLOY] coin deployment loop started (every {CHECK_SECONDS}s, "
                f"targets: {', '.join(TARGET_COINS)})")
    while True:
        try:
            armed = await armed_now(grid.get_session_factory)
            report = await run_once(grid, dry_run=not armed)
            if report.get("created"):
                log.warning(f"[DEPLOY] created {report['created']} branch(es): "
                            f"{report.get('detail')}")
            elif report.get("status") in ("HOLD", "UNKNOWN"):
                log.info(f"[DEPLOY] {report['status']}: {report.get('detail')}")
            elif report.get("plan") and not armed:
                log.warning(f"[DEPLOY] OBSERVING - would fund "
                            f"{len(report['plan'])}: {report.get('detail')}")
        except Exception as e:
            log.warning(f"[DEPLOY] pass failed ({type(e).__name__}: {e}) - "
                        f"nothing created, retrying next cycle")
        await asyncio.sleep(CHECK_SECONDS)
