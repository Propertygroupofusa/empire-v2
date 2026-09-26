"""The two loops: produce alerts from the watch, and drain the queue.

SEPARATE ON PURPOSE. The producer writes rows; the sender reads them. A
dead webhook cannot stop the producer noticing a breach, and a broken watch
cannot stop the sender flushing a backlog. Neither loop imports the other.

IT WRITES WITH THE WRITE TOKEN UNSET, and that is deliberate rather than a
loophole. write_guard is Starlette middleware over HTTP requests: it stops
anyone POSTing an order through the dashboard without the token. A
background task is not a request, so the alarm works while trading is
frozen - which is exactly when an unmonitored $11,200 most needs watching.
Nothing here places an order or moves money; the only thing it can do is
tell someone something.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import select

import alert_queue
import alert_sender

log = logging.getLogger("alert_worker")

PRODUCE_INTERVAL = int(os.getenv("ALERT_PRODUCE_SECONDS", "900"))    # 15 min
DRAIN_INTERVAL = int(os.getenv("ALERT_DRAIN_SECONDS", "30"))
COVERAGE_KEY = "newsroom_last_coverage_pct"

_last_coverage = None


async def produce_once(watch: dict, session_factory) -> int:
    """Compare this watch against stored state, enqueue the transitions."""
    global _last_coverage
    from models import AssetAlertState, NewsroomAlert
    if not watch or not watch.get("rows"):
        return 0

    async with session_factory()() as db:
        rows = (await db.execute(select(AssetAlertState))).scalars().all()
        last_state = {r.asset: r.status for r in rows}
        by_asset = {r.asset: r for r in rows}

        planned = alert_queue.plan(watch, last_state, _last_coverage)

        written = 0
        for a in planned:
            # The unique index on dedupe_key is the real guard; this check
            # only avoids a pointless round trip and a noisy rollback.
            existing = (await db.execute(
                select(NewsroomAlert).where(
                    NewsroomAlert.dedupe_key == a["dedupe_key"]))).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(NewsroomAlert(
                kind=a["kind"], asset=a["asset"], severity=a["severity"],
                message=a["message"], detail=a.get("detail"),
                dedupe_key=a["dedupe_key"], status="pending",
                attempts=0, next_attempt_at=datetime.utcnow()))
            written += 1

        now = datetime.utcnow()
        for asset, status in alert_queue.next_state(watch).items():
            row = by_asset.get(asset)
            if row is None:
                db.add(AssetAlertState(asset=asset, status=status,
                                       last_seen_at=now, last_changed_at=now))
            else:
                if row.status != status:
                    row.last_changed_at = now
                row.status = status
                row.last_seen_at = now
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            log.warning(f"[alerts] produce rolled back: {type(e).__name__}: {e}")
            return 0

    _last_coverage = watch.get("covered_share_pct")
    return written


async def drain_once(session_factory, http_session) -> dict:
    """Send what is due. One row at a time, so a crash loses at most one."""
    from models import NewsroomAlert
    if not alert_sender.channel_configured():
        # Nothing is attempted and nothing is marked sent. A queue that
        # reports success with no channel configured is worse than an empty
        # one, because it looks like coverage.
        return {"sent": 0, "failed": 0, "skipped": "no channel configured"}

    now = datetime.utcnow()
    async with session_factory()() as db:
        row = (await db.execute(
            select(NewsroomAlert)
            .where(NewsroomAlert.status == "pending")
            .where((NewsroomAlert.next_attempt_at == None) |     # noqa: E711
                   (NewsroomAlert.next_attempt_at <= now))
            .order_by(NewsroomAlert.id)
            .limit(1))).scalar_one_or_none()
        if row is None:
            return {"sent": 0, "failed": 0}

        alert = {"kind": row.kind, "asset": row.asset, "severity": row.severity,
                 "message": row.message, "detail": row.detail}
        ok, err = await alert_sender.deliver(http_session, alert)

        if ok:
            row.status = "sent"
            row.sent_at = datetime.utcnow()
            row.last_error = None
        else:
            row.attempts = (row.attempts or 0) + 1
            row.last_error = err
            if row.attempts >= alert_queue.MAX_ATTEMPTS:
                # Never deleted, never retried forever. A dead alert you
                # can see beats a live one you cannot.
                row.status = "failed"
            else:
                row.next_attempt_at = datetime.utcnow() + timedelta(
                    seconds=alert_queue.backoff_seconds(row.attempts))
        await db.commit()
        return {"sent": 1 if ok else 0, "failed": 0 if ok else 1,
                "error": err, "alert_id": row.id}


async def run_producer_periodically(get_watch, session_factory):
    """Never dies. A producer that raises stops the alarm permanently."""
    while True:
        try:
            n = await produce_once(await get_watch(), session_factory)
            if n:
                log.info(f"[alerts] queued {n} new alert(s)")
        except Exception as e:
            log.warning(f"[alerts] producer error: {type(e).__name__}: {e}")
        await asyncio.sleep(PRODUCE_INTERVAL)


async def run_sender_periodically(session_factory):
    while True:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as http:
                # Drain in a burst, then idle - a backlog should not take
                # one row per DRAIN_INTERVAL to clear.
                for _ in range(25):
                    r = await drain_once(session_factory, http)
                    if not r.get("sent") and not r.get("failed"):
                        break
        except Exception as e:
            log.warning(f"[alerts] sender error: {type(e).__name__}: {e}")
        await asyncio.sleep(DRAIN_INTERVAL)
