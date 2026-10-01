"""Turn the backing MEASUREMENT into the audit layer's truth record.

WHY THIS FILE EXISTS

audit_models.py declares `exchange_truth_failures` - a table for "the book
disagrees with the venue" - and `branch_control_state.reconciliation_status`,
which the execution gate reads to decide whether a branch may trade.

Nothing wrote to either. Measured on the live account 2026-10-01:

    exchange_truth_failures   0 rows, and READABLE
    branch_control_state      2 rows, both reconciliation_status=UNKNOWN
    backing (same moment)     8 unbacked branches, $1,168.59 of claimed
                              coin that is not in the wallet

So the table built for exactly this condition was empty while the fleet was
in exactly this condition, and the gate denied on UNKNOWN - meaning "never
checked" - when the check had in fact been run, every cycle, by
slice_backing. The answer existed and never reached the place that decides.

This module is the wire between them. It computes nothing new: slice_backing
already produces claimed vs held per branch, and this translates that into
the audit layer's vocabulary.

FOUR RULES, AND THE FIRST ONE IS THE WHOLE POINT

1.  AN UNREADABLE MEASUREMENT CHANGES NOTHING. If the balance read failed,
    this writes no row and moves no status - not even to STALE. The venue
    not answering is not evidence about the books. Downgrading a branch on
    a rate limit is how the concentration ceiling failed OPEN during the
    429 storm, and it is the same mistake pointed the other way.

2.  AN ASSET THE READING DID NOT MENTION IS STALE, NOT A MISMATCH.
    slice_backing already separates those into `unknown`, with the reason.
    A gap is not a zero; an absent asset is not a confirmed shortfall.

3.  ONLY A CHANGE IS RECORDED. The fleet cycles every 30s. Writing eight
    failure rows per cycle would be ~23,000 rows a day that all say the
    same thing, and the signal - WHEN did this branch break - would be
    buried in it. A failure row is written when the status CHANGES, and
    `previous_inventory_status` carries what it changed from.

4.  IT NEVER SETS execution_enabled. That column is the gate's own cached
    answer and the owner's switch. This file reports what is true about
    inventory; what to DO about it is the gate's decision, in its own mode.
"""
from __future__ import annotations

import logging

import audit_models as am

log = logging.getLogger("exchange_truth")

MATCHED, MISMATCH, STALE, UNKNOWN = "MATCHED", "MISMATCH", "STALE", "UNKNOWN"

# KNOWN_INTERNAL: the books and the venue disagree and the cause is ours -
# a partial fill retired as whole, a sale the rotation never settled. That
# is every case this fleet has actually produced. KNOWN_EXTERNAL would be a
# venue-side change (a delisting, a forced conversion); nothing here can
# tell those apart yet, so this does not pretend to.
CLASSIFICATION = "KNOWN_INTERNAL"


def classify(measurement):
    """Measurement -> {bot_name: verdict}. Pure; no database, no network.

    Returns {} for an unreadable measurement - rule 1. The caller must not
    read that as "every branch is fine"; it means nothing was measured, and
    an empty result writes nothing, which is the same thing.
    """
    out = {}
    if not measurement or not measurement.get("readable"):
        return out

    by_product = {}
    for row in (measurement.get("rows") or ()):
        pid = row.get("product_id")
        if not pid:
            continue
        by_product[pid] = {
            "status": MATCHED if row.get("backed") else MISMATCH,
            "asset": row.get("asset") or str(pid).split("-")[0].upper(),
            "db_quantity": row.get("claimed_units"),
            "exchange_quantity": row.get("held_units"),
            "difference": row.get("short_units"),
            "reason_code": ("INVENTORY_MATCHED" if row.get("backed")
                            else "INVENTORY_SHORT_AT_VENUE"),
            "detail": row.get("why"),
            "short_usd": row.get("short_usd"),
        }

    # STALE, not MISMATCH. slice_backing puts an asset here when the
    # balance could not be read or the reading never mentioned it, and it
    # says which - neither is a shortfall the venue confirmed.
    for row in (measurement.get("unknown") or ()):
        pid = row.get("product_id")
        if not pid or pid in by_product:
            continue
        by_product[pid] = {
            "status": STALE,
            "asset": row.get("asset") or str(pid).split("-")[0].upper(),
            "db_quantity": row.get("claimed_units"),
            "exchange_quantity": None,
            "difference": None,
            "reason_code": "INVENTORY_NOT_MEASURED",
            "detail": row.get("reason"),
            "short_usd": None,
        }
    return by_product


def _severity(status):
    # A mismatch is an ERROR because it means a branch's books claim coin
    # it cannot sell. STALE is a WARN: nothing is known to be wrong, but
    # nothing is confirmed right either.
    return {MISMATCH: "ERROR", STALE: "WARN"}.get(status, "INFO")


async def record(session, measurement, *, branch_ids=None, boot_id=None,
                 product_to_bot=None):
    """Write the measurement into the audit layer. Returns a summary dict.

    `product_to_bot` maps product_id -> bot_name, because bot_name is the
    durable identity in this schema (audit_models' own IDENTITY note) and
    the measurement is keyed by product.
    """
    from sqlalchemy import select

    verdicts = classify(measurement)
    summary = {"measured": len(verdicts), "changed": 0, "failures_written": 0,
               "matched": 0, "mismatch": 0, "stale": 0, "skipped_unreadable": False}
    if not verdicts:
        summary["skipped_unreadable"] = True
        return summary

    product_to_bot = product_to_bot or {}
    branch_ids = branch_ids or {}

    for pid, v in verdicts.items():
        bot_name = product_to_bot.get(pid)
        if not bot_name:
            # No durable identity, no row. Keying an audit trail on
            # something that can be renamed is worse than not writing it.
            continue
        summary[{MATCHED: "matched", MISMATCH: "mismatch",
                 STALE: "stale"}[v["status"]]] += 1

        row = (await session.execute(
            select(am.BranchControlState).where(
                am.BranchControlState.bot_name == bot_name))).scalar_one_or_none()
        previous = row.reconciliation_status if row is not None else UNKNOWN

        if row is None:
            row = am.BranchControlState(
                bot_name=bot_name, branch_id=branch_ids.get(pid),
                lifecycle_status="ACTIVE", reconciliation_status=v["status"],
                # NOT enabled. Rule 4: this file reports inventory truth and
                # never hands out permission to trade.
                execution_enabled=False, boot_id=boot_id)
            session.add(row)
        else:
            row.reconciliation_status = v["status"]
            if branch_ids.get(pid) is not None:
                row.branch_id = branch_ids[pid]

        if previous == v["status"]:
            continue                      # rule 3 - only a change is news
        summary["changed"] += 1

        # The timeline gets every transition, in both directions: a branch
        # coming BACK to MATCHED is as much of an event as one breaking.
        session.add(am.BranchAuditEvent(
            bot_name=bot_name, branch_id=branch_ids.get(pid),
            event_type="RECONCILIATION", reason_code=v["reason_code"],
            reason_detail=v["detail"], severity=_severity(v["status"]),
            source_component="exchange_truth_recorder",
            previous_state={"reconciliation_status": previous},
            new_state={"reconciliation_status": v["status"],
                       "db_quantity": v["db_quantity"],
                       "exchange_quantity": v["exchange_quantity"]},
            boot_id=boot_id))

        # The failure table is for disagreements only. A branch that is fine,
        # or one that was not measured, does not belong in it.
        if v["status"] != MISMATCH:
            continue
        session.add(am.ExchangeTruthFailure(
            bot_name=bot_name, branch_id=branch_ids.get(pid),
            asset=v["asset"], classification=CLASSIFICATION,
            previous_inventory_status=previous, inventory_status=v["status"],
            db_quantity=v["db_quantity"], exchange_quantity=v["exchange_quantity"],
            difference=v["difference"], reason_code=v["reason_code"],
            severity="ERROR", boot_id=boot_id))
        summary["failures_written"] += 1

    return summary
