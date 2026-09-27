"""The loop that writes down where the numbers were.

It spends nothing. There is no arm switch here on purpose: an observer
that has to be armed is an observer that will be off on the day someone
needs the history, and the history only exists if collection started
before it was wanted. The one setting is the interval.

What it must not do is compete with the loops that DO spend. A full
account census is ~50 signed Coinbase requests and the trimmer needs
that same allowance to place orders - on 2026-09-27 a dashboard poll
every 55s drove /auto-trim to "accounts HTTP 429" and starved the worker,
which then correctly refused to trim against an account it could not
read. So this runs on a long interval, takes the census on a slower
schedule than the rest of the reading, and stores NULL rather than
waiting when it cannot have one.

A reading with a missing field is still worth keeping. A reading that
blocked the trimmer to get one is not.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

import growth_ledger

log = logging.getLogger("growth_ledger")

HEARTBEAT = {
    "started_at": None,
    "last_pass_at": None,
    "passes": 0,
    "rows_written": 0,
    "last_result": None,
    "last_error": None,
}

# 15 minutes: four readings an hour is enough to see a day take shape
# without the series becoming its own rate-limit problem.
INTERVAL_SECONDS = int(os.getenv("GROWTH_SNAPSHOT_SECONDS", "900"))

# The census is the expensive part, so it is refreshed on its own slower
# clock. Between refreshes the account total is carried forward with the
# reading marked, never re-read and never invented.
CENSUS_EVERY_SECONDS = int(os.getenv("GROWTH_CENSUS_SECONDS", "3600"))

# Readings older than this are pruned. The chart looks back days, not
# months, and an unbounded table on a small instance is its own outage.
RETAIN_DAYS = int(os.getenv("GROWTH_SNAPSHOT_RETAIN_DAYS", "45"))

_last_census = {"at": None, "total_usd": None}


def interval_seconds() -> int:
    return max(INTERVAL_SECONDS, 60)


async def _read_ledger(session_factory):
    """The closed book, row by row. DB only - costs the venue nothing."""
    from models import CryptoGridTradeHistory
    from sqlalchemy import select
    async with session_factory()() as db:
        rows = (await db.execute(
            select(CryptoGridTradeHistory)
            .order_by(CryptoGridTradeHistory.closed_at.desc())
            .limit(2000))).scalars().all()
    return [{"pnl": r.pnl, "qty": r.qty, "entry_price": r.entry_price,
             "exit_price": r.exit_price, "opened_at": r.opened_at,
             "closed_at": r.closed_at, "product_id": r.product_id} for r in rows]


async def _account_total(now):
    """The census, on its own slower clock.

    Returns (total, note). A stale-but-real total is carried forward and
    SAID to be carried forward; a failed census returns None, never a
    number, because a zero here draws a crash that did not happen.
    """
    last_at = _last_census["at"]
    fresh = (last_at is not None
             and (now - last_at).total_seconds() < CENSUS_EVERY_SECONDS)
    if fresh:
        age_m = (now - last_at).total_seconds() / 60.0
        return _last_census["total_usd"], f"account total carried forward, {age_m:.0f}m old"

    try:
        import aiohttp
        import account_census
        async with aiohttp.ClientSession() as session:
            census = await account_census.census(session, tracked_usd=0.0)
        if census.get("available"):
            _last_census["at"] = now
            _last_census["total_usd"] = census.get("total_usd")
            return census.get("total_usd"), None
        err = str(census.get("error") or "unknown")
        # Keep the previous figure rather than blanking the series over a
        # rate limit, and mark the row so a flat stretch is explainable.
        return _last_census["total_usd"], f"census unavailable ({err[:60]})"
    except Exception as exc:
        return _last_census["total_usd"], f"census failed ({type(exc).__name__})"


async def check_once(session_factory):
    """One reading, written. Never raises out."""
    import capital_kpis
    from models import CapitalKpiSnapshot

    now = datetime.utcnow()
    notes = []

    trades = []
    try:
        trades = await _read_ledger(session_factory)
    except Exception as exc:
        notes.append(f"ledger unreadable ({type(exc).__name__})")

    allocated = free = claimed = None
    branch_count = open_slices = None
    try:
        import crypto_grid_bot as grid
        st = await grid.get_grid_status()
        backing = st.get("allocation_backing") or {}
        allocated = backing.get("deployed_coin_usd")
        free = backing.get("wallet_cash_usd")
        claimed = backing.get("claimed_usd")
        branch_count = st.get("branch_count")
        open_slices = sum(len(b.get("slices") or b.get("open_slices") or [])
                          for b in (st.get("branches") or []))
    except Exception as exc:
        notes.append(f"grid status unreadable ({type(exc).__name__})")

    total, census_note = await _account_total(now)
    if census_note:
        notes.append(census_note)

    k = capital_kpis.compute(trades, allocated_usd=allocated,
                             free_cash_usd=free or 0.0, account_total_usd=total)
    cause, _why = capital_kpis.bottleneck(k)

    row = growth_ledger.from_kpis(
        k, claimed_usd=claimed, branch_count=branch_count,
        open_slices=open_slices, bottleneck=cause,
        note="; ".join(notes) or None, captured_at=now)

    async with session_factory()() as db:
        db.add(CapitalKpiSnapshot(**{
            "captured_at": row["captured_at"],
            "bottleneck": row["bottleneck"],
            "note": row["note"],
            **{f: row[f] for f in growth_ledger.FIELDS},
        }))
        await db.commit()
    HEARTBEAT["rows_written"] += 1

    return {"written": True, "bottleneck": cause,
            "detail": (f"net ${k.get('net_usd')} over {k.get('trades')} trades, "
                       f"${allocated} deployed, cause {cause}"
                       + (f" [{'; '.join(notes)}]" if notes else ""))}


async def prune(session_factory):
    """Drop readings past the retention window."""
    from models import CapitalKpiSnapshot
    from sqlalchemy import delete
    cutoff = datetime.utcnow() - timedelta(days=max(RETAIN_DAYS, 1))
    async with session_factory()() as db:
        await db.execute(delete(CapitalKpiSnapshot)
                         .where(CapitalKpiSnapshot.captured_at < cutoff))
        await db.commit()


async def run_periodically(session_factory):
    """Never dies. A recorder that raises records nothing."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[growth] snapshot loop up, every {interval_seconds()}s")
    passes = 0
    while True:
        try:
            r = await check_once(session_factory)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[growth] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        passes += 1
        # Pruning is not worth a pass of its own and must never be the
        # thing that stops a reading being written.
        if passes % 96 == 0:
            try:
                await prune(session_factory)
            except Exception as e:
                log.warning(f"[growth] prune failed: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds())
