"""Applies reconcile.fleet_corrections to the live book, on a schedule.

Turned on at the account owner's instruction, 2026-09-28. What it does is
narrow on purpose: when the fleet's UNSPENT claims exceed the cash that
really exists, it reduces those claims until they fit. It never places an
order, never touches a slice, never moves a coin. It corrects a number that
had drifted away from the money behind it.

WHAT IT CANNOT DO, and every one of these is enforced in reconcile.py
rather than here, so the rules hold for any caller:

  * It never RAISES a claim. Writing an allocation up on the strength of an
    arithmetic identity would let a mis-measurement mint budget, and this
    codebase produced a wrong-but-plausible measurement twice in one evening.
  * It never reduces a branch below the coin that branch actually owns.
  * It refuses outright when a pass would remove more than half the unspent
    claims - that means the cash reading collapsed, which is a measurement
    to check, not an instruction to obey.
  * An unreadable wallet moves nothing. A gap is not a zero.

Measured before arming: unspent claims $495.02 against $515.31 of real cash,
so the first pass is a no-op. Knowing that BEFORE turning it on is the point
of having run it first.
"""
import asyncio
import logging
import os

log = logging.getLogger(__name__)

CHECK_SECONDS = int(os.getenv("GRID_RECONCILE_CHECK_SECONDS", "900"))

# DB-backed so it can be turned off without a deploy; env overrides outright.
# Matches the idle-rotation switch, for the same reason: a control that needs
# a deploy to flip is not a control you can use when something is going wrong.
RECONCILE_MODE_KEY = "grid_reconcile_mode"


def env_mode():
    raw = (os.getenv("GRID_RECONCILE_MODE") or "").strip().strip('"').strip("'").lower()
    if raw in {"arm", "armed", "on", "true", "1"}:
        return True
    if raw in {"observe", "off", "false", "0"}:
        return False
    return None


# The owner armed this on 2026-09-28, in those words. A default carries
# that instruction so it does not depend on a Railway variable nobody can
# set from here - but it is a DEFAULT, not a hardcoding: the env var and the
# DB row both override it, so it can be switched off without a deploy.
ARMED_BY_DEFAULT = True


async def armed_now(session_factory):
    """env decides outright, else the DB row, else the owner's default.

    THE THREE CASES ARE NOT THE SAME and collapsing them is the trap:

      env set          -> obey it, no question
      DB row present   -> obey it, it was set deliberately
      DB readable,
        no row         -> ARMED_BY_DEFAULT, the owner's standing instruction
      DB UNREADABLE    -> OFF, always

    The last one is why this cannot simply `return ARMED_BY_DEFAULT` on any
    exception. A worker that writes to allocations must not arm itself
    because a database read hiccupped - it cannot tell a missing switch from
    a switch it failed to read, and one of those means someone turned it off.
    """
    env = env_mode()
    if env is not None:
        return env
    try:
        from models import TradingBotState
        from sqlalchemy import select
        async with session_factory()() as db:
            row = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == RECONCILE_MODE_KEY))).scalar_one_or_none()
        if row is None:
            return ARMED_BY_DEFAULT
        return bool(row.base_capital and row.base_capital > 0)
    except Exception as e:
        log.warning(f"[RECONCILE] arm switch unreadable ({type(e).__name__}) - staying OFF")
        return False


async def _branch_rows(session_factory):
    """Each branch with its own coin basis. Computed from the slices, never
    from allocated_usd - the allocation is the claim being tested, so using
    it here would make the check agree with itself."""
    from models import CryptoGridBranch, CryptoGridSlice
    from sqlalchemy import select
    async with session_factory()() as db:
        branches = (await db.execute(select(CryptoGridBranch).where(
            CryptoGridBranch.active.is_(True)))).scalars().all()
        slices = (await db.execute(select(CryptoGridSlice))).scalars().all()
    basis = {}
    for s in slices:
        if s.qty is None or s.entry_price is None:
            continue
        basis[s.bot_name] = basis.get(s.bot_name, 0.0) + float(s.qty) * float(s.entry_price)
    return [{"bot_name": b.bot_name, "product_id": b.product_id,
             "allocated_usd": b.allocated_usd,
             "coin_basis_usd": round(basis.get(b.bot_name, 0.0), 2)}
            for b in branches]


async def run_once(session_factory, real_cash_usd, dry_run=True):
    """One pass. Returns the report plus what was applied."""
    import reconcile as rec
    rows = await _branch_rows(session_factory)
    corrections, report = rec.fleet_corrections(rows, real_cash_usd)
    report["dry_run"] = bool(dry_run)
    report["corrections"] = corrections
    if dry_run or not corrections:
        report["applied"] = 0
        return report

    from models import CryptoGridBranch
    from sqlalchemy import select
    applied = 0
    async with session_factory()() as db:
        for c in corrections:
            row = (await db.execute(select(CryptoGridBranch).where(
                CryptoGridBranch.bot_name == c["bot_name"]))).scalar_one_or_none()
            if row is None:
                continue
            # RE-CHECKED against the live row, not the planned one. A branch
            # whose allocation moved between the plan and now must not be
            # written from a stale figure, and one that is already at or
            # below the target is left alone - the write only ever lowers.
            if row.allocated_usd is None or float(row.allocated_usd) <= c["new_allocated_usd"]:
                continue
            before = float(row.allocated_usd)
            row.allocated_usd = c["new_allocated_usd"]
            applied += 1
            log.warning(f"[RECONCILE] {c['bot_name']} ({c['product_id']}): claim "
                        f"${before:,.2f} -> ${c['new_allocated_usd']:,.2f} "
                        f"(-${before - c['new_allocated_usd']:,.2f}) - it was budgeting "
                        f"cash that is not there. Coin owned: "
                        f"${c['coin_basis_usd']:,.2f}, untouched.")
        await db.commit()
    report["applied"] = applied
    return report


async def loop(session_factory, cash_reader):
    """Forever. cash_reader() -> real spendable+held USD, or None."""
    log.warning(f"[RECONCILE] claim-reconciliation loop started "
                f"(every {CHECK_SECONDS}s, writes only when armed)")
    while True:
        try:
            armed = await armed_now(session_factory)
            cash = await cash_reader()
            report = await run_once(session_factory, cash, dry_run=not armed)
            if report.get("status") in ("REFUSED", "UNKNOWN"):
                log.warning(f"[RECONCILE] {report['status']}: {report.get('detail')}")
            elif report.get("applied"):
                log.warning(f"[RECONCILE] applied {report['applied']} correction(s): "
                            f"{report.get('detail')}")
            elif report.get("corrections") and not armed:
                log.warning(f"[RECONCILE] OBSERVING - would correct "
                            f"{len(report['corrections'])} claim(s): {report.get('detail')}")
        except Exception as e:
            log.warning(f"[RECONCILE] pass failed ({type(e).__name__}: {e}) - "
                        f"nothing written, retrying next cycle")
        await asyncio.sleep(CHECK_SECONDS)
