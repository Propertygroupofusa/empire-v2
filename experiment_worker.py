"""The loop that actually ends the experiment, whether or not anyone looks.

A budget nobody enforces is a wish. This runs every ten minutes, measures
what the trial has cost since the gates came off, and flips the profile
back to guarded the moment the budget or the deadline is reached.

It writes with DASHBOARD_WRITE_TOKEN unset for the same reason the alert
queue does: write_guard is HTTP middleware over requests, and a background
task is not a request. That matters here more than anywhere else - the
safety mechanism must not depend on the operator having a token in a
browser somewhere. It can only ever move the profile in the SAFE direction:
this file turns gates ON, never off.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from sqlalchemy import select

import experiment_guard

log = logging.getLogger("experiment_worker")

CHECK_SECONDS = int(os.getenv("EXPERIMENT_CHECK_SECONDS", "600"))   # 10 min


async def current_realized_pnl(session_factory):
    """Combined realized P&L from both ledgers. None if it cannot be read.

    None is load-bearing: it becomes a BLIND check rather than a spend of
    zero, and six of those in a row end the trial. Returning 0.0 on an
    error would read as "it has cost nothing", which is the most dangerous
    possible answer to a question the budget exists to ask.
    """
    from models import CryptoCoinTradeHistory, CryptoGridTradeHistory
    from sqlalchemy import func
    try:
        async with session_factory()() as db:
            tree = (await db.execute(select(func.sum(CryptoCoinTradeHistory.pnl)))).scalar()
            grid = (await db.execute(select(func.sum(CryptoGridTradeHistory.pnl)))).scalar()
        return float(tree or 0.0) + float(grid or 0.0)
    except Exception as e:
        log.warning(f"[experiment] realized P&L unreadable: {type(e).__name__}: {e}")
        return None


async def check_once(session_factory, set_profile) -> dict:
    """One pass. Ends the running experiment if it should end."""
    from models import TradingExperiment
    async with session_factory()() as db:
        exp = (await db.execute(
            select(TradingExperiment)
            .where(TradingExperiment.ended_at == None)      # noqa: E711
            .order_by(TradingExperiment.id.desc())
            .limit(1))).scalar_one_or_none()
        if exp is None:
            return {"running": False}

        pnl = await current_realized_pnl(session_factory)
        d = experiment_guard.decide(
            baseline_pnl=exp.baseline_realized_pnl, current_pnl=pnl,
            budget_usd=exp.budget_usd, deadline_at=exp.deadline_at,
            blind_checks=exp.blind_checks or 0)

        exp.last_checked_at = datetime.utcnow()
        exp.last_spend_usd = d.get("spend_usd")
        exp.blind_checks = d.get("blind_checks", 0)

        if d["end"]:
            exp.ended_at = datetime.utcnow()
            exp.ended_reason = d["reason"]
            exp.ended_spend_usd = d.get("spend_usd")
            await db.commit()
            # The profile flip happens AFTER the row is committed. If it
            # were the other way round and the commit failed, the gates
            # would be back on with an experiment still marked running,
            # and the next check would try to end it again forever.
            try:
                await set_profile("guarded")
                log.warning(f"[experiment] ENDED on {d['reason']}: {d['detail']} "
                            f"- gates are back ON")
            except Exception as e:
                log.error(f"[experiment] ended but could not restore the profile: "
                          f"{type(e).__name__}: {e}")
            return {"running": False, "ended": True, "reason": d["reason"],
                    "detail": d["detail"], "spend_usd": d.get("spend_usd")}

        await db.commit()
        return {"running": True, "spend_usd": d.get("spend_usd"),
                "detail": d["detail"], "blind_checks": exp.blind_checks}


async def run_periodically(session_factory, set_profile):
    """Never dies. A guard that raises is a guard that is not guarding."""
    while True:
        try:
            r = await check_once(session_factory, set_profile)
            if r.get("ended"):
                log.warning(f"[experiment] {r['reason']}: {r['detail']}")
        except Exception as e:
            log.warning(f"[experiment] check failed: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_SECONDS)
