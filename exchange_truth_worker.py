"""The loop that keeps the audit layer's inventory truth current.

It spends no money and places no orders. It reads the venue's balances and
the grid's own books, compares them with slice_backing (which the dashboard
has been running every cycle all along), and writes the verdict into the
tables the execution gate reads.

WHY A SEPARATE LOOP RATHER THAN THE DASHBOARD ENDPOINT

The measurement already happens inside GET /grid-status. Writing audit rows
from a GET is the wrong shape twice over: the truth would only be recorded
while somebody happened to be looking at the page, and a read endpoint that
writes surprises the next person to touch it. The gate must know whether a
branch is reconciled whether or not a browser is open.

SLOW ON PURPOSE. Every 5 minutes, not every 30s. Inventory drifts when a
trade settles, which is minutes apart at best, and the balance read is the
expensive call that caused the 429 storm this fleet is still paying for.
Ten reads an hour cannot recreate that.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime

import aiohttp

import exchange_truth_recorder as recorder

log = logging.getLogger("exchange_truth")

CHECK_SECONDS = int(os.getenv("EXCHANGE_TRUTH_CHECK_SECONDS", "300"))

# Proof of life, same shape as the trimmer's. A loop whose failures are
# invisible is indistinguishable from one that is not running - which is
# exactly the eleven-day blindness this whole audit layer exists to end.
HEARTBEAT = {"started_at": None, "last_pass_at": None, "passes": 0,
             "last_result": None, "last_error": None}

BOOT_ID = str(uuid.uuid4())


async def check_once(session_factory):
    import account_census
    import slice_backing
    import crypto_grid_bot

    status = await crypto_grid_bot.get_grid_status()
    branches = status.get("branches") or []
    if not branches:
        return {"measured": 0, "detail": "no branches reported"}

    async with aiohttp.ClientSession() as http:
        bal = await account_census.fetch_balances(http)

    if not bal or not bal.get("available"):
        # RULE 1. An unreadable balance is not evidence about the books, so
        # nothing is written and no status moves. Reported, not swallowed.
        log.info("[truth] balances unreadable this pass - nothing recorded "
                 "(UNKNOWN is not a clean bill of health)")
        return {"measured": 0, "skipped_unreadable": True,
                "detail": "balances unreadable - nothing recorded"}

    avail = dict(bal.get("available_units") or {})
    # A CONFIRMED ZERO IS NOT A MISSING READING. Same separation the
    # dashboard makes: a currency the venue listed at 0.0 is the largest
    # shortfall there is; one the reading never mentioned is UNKNOWN.
    for cur, total in (bal.get("held_including_zero") or {}).items():
        avail.setdefault(cur, 0.0 if not total else avail.get(cur, 0.0))

    measurement = slice_backing.assess(branches, avail)
    p2b = {b.get("product_id"): b.get("bot_name") for b in branches
           if b.get("product_id") and b.get("bot_name")}
    # branch_id is deliberately NOT passed. get_grid_status() does not expose
    # the row id (checked against the live payload, not assumed), and
    # audit_models' IDENTITY note already makes branch_id nullable precisely
    # so history survives a branch being deleted and recreated. bot_name is
    # the durable identity; inventing an id from nothing would be worse than
    # leaving the nullable column null.
    async with session_factory() as db:
        summary = await recorder.record(db, measurement, product_to_bot=p2b,
                                        boot_id=BOOT_ID)
        await db.commit()

    summary["detail"] = (
        f"{summary['measured']} branch(es) measured: {summary['matched']} matched, "
        f"{summary['mismatch']} mismatched, {summary['stale']} not measured; "
        f"{summary['changed']} changed, {summary['failures_written']} failure row(s)")
    return summary


async def run_periodically(session_factory):
    """Never dies. A recorder that raises records nothing."""
    HEARTBEAT["started_at"] = datetime.utcnow().isoformat() + "Z"
    log.info(f"[truth] exchange-truth recorder up, every {CHECK_SECONDS}s")
    while True:
        try:
            r = await check_once(session_factory)
            HEARTBEAT["last_result"] = r.get("detail")
            HEARTBEAT["last_error"] = None
            if r.get("failures_written"):
                log.warning(f"[truth] {r['failures_written']} branch(es) newly "
                            f"disagree with the venue: {r['detail']}")
        except Exception as e:
            HEARTBEAT["last_error"] = f"{type(e).__name__}: {e}"
            HEARTBEAT["last_result"] = None
            log.warning(f"[truth] pass failed: {type(e).__name__}: {e}")
        HEARTBEAT["last_pass_at"] = datetime.utcnow().isoformat() + "Z"
        HEARTBEAT["passes"] += 1
        await asyncio.sleep(CHECK_SECONDS)
