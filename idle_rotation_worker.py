"""The loop that actually moves stale cash. The only file here that moves it.

idle_capital decides WHAT should move and WHERE. This executes that plan by
calling crypto_grid_bot.move_cash_between_grid_branches - the same function
the dashboard's "Move Cash Between Grid Branches" modal already calls, not a
second implementation beside it. Everything that function refuses, this
refuses too: a source that is not flat, an amount above its own
allocated_usd, a locked branch, the same branch on both ends, STOP_TRADING.

WHY THIS IS NOT GRID_AUTO_ROTATE SWITCHED ON

The fleet already has an auto-rotation, and it is off by the account owner's
own instruction. It stays off, and this does not turn it on, because on the
live fleet it would do two things that are wrong for this job:

  It keys on FLATNESS. Measured live, ONDO and TIA were flat having traded 4
  and 6 hours earlier, while BONK had been silent 18 days. A flatness rule
  moves all three and churns the two that were working.

  It RETIRES to unallocated USD when nothing clears the ROI floor - turning
  idle money into more idle money, which is the opposite of the ask.

This moves only what idle_capital calls STALE, only into a branch that has
demonstrably closed a round trip, and never to cash.

ARMED TWICE, LIKE EVERY OTHER MONEY PATH HERE

`is_armed()` before anything is fetched, and again immediately before each
move. The periodic loop does nothing at all while observing. The dashboard
endpoint is a separate door: it is dry-run by default and its execution is
an explicit, write-guarded request, so a human pressing it IS the arming.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import idle_capital

log = logging.getLogger("idle_rotation")

HEARTBEAT = {
    "started_at": None,
    "last_pass_at": None,
    "passes": 0,
    "moved": 0,
    "moved_usd": 0.0,
    "last_result": None,
    "last_error": None,
}

MODE_ENV = "GRID_IDLE_ROTATION_MODE"
# 15 minutes. The condition this detects moves on a 72-HOUR clock - a branch
# is stale after three days of silence - so a faster sweep cannot change
# whether a branch qualifies, only how soon it is noticed after the mark
# passes. What made hourly the right answer before was cost: each pass spent
# ~45 venue calls. Reading the branches from the database instead makes a
# pass two indexed queries, so the interval is now free to be chosen on
# responsiveness. 900s buys a worst-case delay of 15 minutes on a 4,320
# minute condition, and costs nothing that the trading loop needs.
CHECK_SECONDS = int(os.getenv("GRID_IDLE_ROTATION_CHECK_SECONDS", "900"))
# One move per pass by default. Rotation is the rarest action this fleet
# takes and a burst of them is the signature of a bad plan, not a busy one.
MAX_MOVES_PER_PASS = int(os.getenv("GRID_IDLE_ROTATION_MAX_PER_PASS", "1"))


# THE SWITCH LIVES IN THE DATABASE, WITH THE ENV AS AN ESCAPE HATCH.
#
# Not a preference. On this deployment CRYPTO_STRATEGY_MODE could not be
# corrected through the Railway UI at all - edited, deleted, re-added,
# across a confirmed restart, six attempts, still reading the old value.
# The fix both times was a second control with no deployment history to
# fight: the DB flag, checked FIRST. Maker-only carries the same pair for
# the same reason.
#
# So arming is a write-guarded dashboard action rather than a redeploy, and
# GRID_IDLE_ROTATION_MODE stays as the override for when the database is
# the thing that is wrong.
#
# FAILS CLOSED. An unreadable switch reads as observing. Every consumer is
# safe when this answers False and moves money when it answers True.
IDLE_ROTATION_MODE_KEY = "grid_idle_rotation_mode"


def env_mode() -> str:
    """What GRID_IDLE_ROTATION_MODE reads. Empty when unset."""
    return (os.getenv(MODE_ENV, "") or "").strip().lower()


async def is_armed_db() -> bool:
    """The stored flag alone. False on anything unreadable."""
    try:
        from sqlalchemy import select
        from models import TradingBotState
        from database import get_session_factory
        async with get_session_factory()() as db:
            row = (await db.execute(
                select(TradingBotState)
                .where(TradingBotState.bot_name == IDLE_ROTATION_MODE_KEY))).scalar_one_or_none()
            return bool(row is not None and row.base_capital and row.base_capital >= 1.0)
    except Exception as e:
        log.warning(f"[idle-rot] arm switch unreadable ({e}) - observing")
        return False


async def set_armed(enabled: bool):
    """Arm or disarm the sweep. The dashboard's door."""
    from sqlalchemy import select
    from models import TradingBotState
    from database import get_session_factory
    async with get_session_factory()() as db:
        row = (await db.execute(
            select(TradingBotState)
            .where(TradingBotState.bot_name == IDLE_ROTATION_MODE_KEY))).scalar_one_or_none()
        if row is None:
            db.add(TradingBotState(bot_name=IDLE_ROTATION_MODE_KEY,
                                   base_capital=1.0 if enabled else 0.0))
        else:
            row.base_capital = 1.0 if enabled else 0.0
        await db.commit()
    log.warning(f"[idle-rot] arm switch set to {enabled} - stale cash "
                f"{'WILL' if enabled else 'will NOT'} be moved automatically")
    return {"armed": enabled, "source": "database"}


def current_mode() -> str:
    """Synchronous view: the ENV only.

    Kept because the boot log and the tests read it, and because the env
    remains the override. It is NOT the whole answer - use armed_now().
    """
    return (os.getenv(MODE_ENV, "observe") or "observe").strip().lower()


def is_armed() -> bool:
    """Env-only, synchronous. The loop calls armed_now() instead."""
    return current_mode() == "arm"


async def armed_now() -> bool:
    """The real answer: env override first, then the stored flag.

    An env value of exactly `arm` or `observe` decides it outright - that is
    what makes it an escape hatch. Anything else (unset, blank, a typo) hands
    the decision to the database rather than silently arming or disarming on
    a misspelling.
    """
    e = env_mode()
    if e == "arm":
        return True
    if e == "observe":
        return False
    return await is_armed_db()


async def _branches_from_db():
    """The five fields the idle check needs, straight from the database.

    WHY NOT get_grid_status().
    
    The sweep reads allocated_usd, bot_name, product_id, open_slices and
    created_at - every one a stored column. get_grid_status fetches a LIVE
    PRICE per distinct product, a wallet balance, and an adaptive stop per
    product: roughly forty-five venue calls to answer a question no live
    price takes part in.

    That is the whole reason the sweep interval was ever a tradeoff. This
    account is already hitting the venue's rate limit - "HTTP 429 fetching
    USD" and "[dashboard] Coinbase USD balance fetch failed: HTTP 429"
    appear repeatedly in the production log - and the calls this would
    crowd out belong to the TRADING loop, which shares the same outbound
    IP and the same limit. A janitor that starves the engine to sweep more
    often is a bad trade at any interval.

    Read from the DB it costs two indexed queries, so the interval can be
    chosen on how fast the answer should arrive rather than on what it
    costs to ask.
    """
    from sqlalchemy import func, select
    from models import CryptoGridBranch, CryptoGridSlice
    from database import get_session_factory

    async with get_session_factory()() as db:
        branches = (await db.execute(select(CryptoGridBranch))).scalars().all()
        counts = dict((await db.execute(
            select(CryptoGridSlice.bot_name, func.count(CryptoGridSlice.id))
            .group_by(CryptoGridSlice.bot_name))).all())
    return [{
        "bot_name": b.bot_name,
        "product_id": b.product_id,
        "allocated_usd": float(b.allocated_usd or 0.0),
        # Absent must never read as "holding something" - that would hide a
        # stale branch behind a missing count.
        "open_slices": int(counts.get(b.bot_name, 0)),
        "created_at": b.created_at,
        "active": bool(b.active),
        "locked": bool(getattr(b, "locked", False)),
    } for b in branches]


async def plan_now(*, cheap: bool = True):
    """The current plan, read-only. Shared by the loop and the endpoint.

    `cheap` reads branches from the database. The endpoint passes False so a
    human looking at the preview sees the same branch rows the rest of the
    dashboard is showing them, priced live; the loop leaves it True.
    """
    import crypto_grid_bot as grid
    if cheap:
        branches = await _branches_from_db()
    else:
        branches = (await grid.get_grid_status()).get("branches") or []
    history = await grid.get_grid_trade_history()
    report = idle_capital.report(
        branches,
        history.get("recent_trades") or [],
        total_trade_count=history.get("total_trade_count"),
    )
    return report, idle_capital.rotation_plan(report)


async def rotate_once(*, dry_run: bool = True, require_arm: bool = True,
                      max_moves: int = None):
    """One pass. Moves nothing unless dry_run is False.

    `require_arm` is True for the periodic loop and False for the dashboard
    endpoint, where the write-guarded request is itself the authorisation.
    """
    import crypto_grid_bot as grid

    max_moves = MAX_MOVES_PER_PASS if max_moves is None else max_moves
    # Read ONCE per pass and reused, so every figure this returns describes
    # the same moment. Re-read immediately before each write below.
    armed = await armed_now()
    if require_arm and not armed and not dry_run:
        return {"armed": False, "moved": 0,
                "detail": (f"idle rotation is observing - env {MODE_ENV}="
                           f"{env_mode() or '(unset)'}, database flag "
                           f"{'on' if await is_armed_db() else 'off'}. Nothing moved.")}

    report, plan = await plan_now(cheap=require_arm)
    if not plan["ok"]:
        return {"armed": armed, "moved": 0, "plan": plan, "report": report,
                "detail": f"nothing to rotate: {plan['detail'][:160]}"}
    if dry_run:
        return {"armed": armed, "moved": 0, "dry_run": True,
                "plan": plan, "report": report,
                "detail": f"PREVIEW only - would move ${plan['total_usd']:,.2f}"}

    moved, failed = [], []
    for m in plan["moves"][:max_moves]:
        # Re-checked immediately before the write, against the same constant.
        if require_arm and not await armed_now():
            log.warning("[idle-rot] disarmed mid-pass - stopping before the move")
            break
        try:
            result = await grid.move_cash_between_grid_branches(
                m["from_bot_name"], m["usd"], to_bot_name=m["to_bot_name"])
            moved.append({**m, "result": result})
            HEARTBEAT["moved"] += 1
            HEARTBEAT["moved_usd"] = round(HEARTBEAT["moved_usd"] + m["usd"], 2)
            log.warning(
                f"[idle-rot] 🔁 ${m['usd']:,.2f} moved from {m['from_product_id']} "
                f"(silent {m['idle_days']} days) into {m['to_product_id']} "
                f"(closed a trip {m['dest_last_trade_hours']}h ago). Nothing retired to cash.")
        except Exception as exc:
            failed.append({**m, "error": f"{type(exc).__name__}: {exc}"})
            log.warning(f"[idle-rot] {m['from_product_id']} -> {m['to_product_id']} "
                        f"failed: {type(exc).__name__}: {exc}")

    return {"armed": armed, "moved": len(moved), "moves": moved,
            "failed": failed, "plan": plan,
            "detail": (f"{len(moved)} move(s), "
                       f"${sum(x['usd'] for x in moved):,.2f} put back to work"
                       if moved else "nothing moved")}


async def run_periodically(session_factory=None):
    """Never dies. A loop that raises moves nothing and says nothing."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[idle-rot] loop up, mode={current_mode()}, every {CHECK_SECONDS}s")
    while True:
        try:
            r = await rotate_once(dry_run=not await armed_now(), require_arm=True)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
            if r.get("moved"):
                log.warning(f"[idle-rot] {r['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[idle-rot] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(max(CHECK_SECONDS, 60))
