"""The audit engine and the execution gate. Async, one driver, fail closed.

WHY A SERVICE AND NOT DATABASE TRIGGERS
---------------------------------------
This deployment has no file-based migration runner, so there is nowhere to
install a plpgsql function or a trigger where it would survive a redeploy.
Putting the safety logic in Python is the only version that actually runs.
It is also the only version that cannot block the event loop: the app is
async SQLAlchemy over asyncpg, and a synchronous psycopg2 call would stall
every worker sharing that loop.

THE RULE THAT DECIDES EVERYTHING HERE
-------------------------------------
If the audit infrastructure is unavailable at a safety-critical transition,
EXECUTION IS BLOCKED. Not logged-and-continue. An un-auditable trade is
exactly the trade nobody can reconstruct afterwards, and the whole point of
this layer is that every decision leaves a record.

So `record()` raises on failure rather than swallowing, and every gate below
treats a raised audit as a denial. That ordering is deliberate: the audit is
written BEFORE permission is returned, so a permission that was granted is
always a permission that was recorded.

WHY execution_enabled IS NOT CONSULTED AS AUTHORITY
---------------------------------------------------
BranchControlState.execution_enabled is the materialised answer from the
last time the gate ran. It is a cache. The gate recomputes from exchange
truth every call, because a cached True outlives the condition that produced
it, and a stale True is a trade nobody authorised.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

import audit_models as am

log = logging.getLogger("branch_audit")


class AuditUnavailable(RuntimeError):
    """The audit could not be written. Callers must treat this as a denial."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason_code: str
    detail: str
    gate: str

    def __bool__(self) -> bool:          # so `if decision:` reads correctly
        return self.allowed


@dataclass
class ExchangeTruth:
    """What the venue says, and whether that answer is usable at all."""
    readable: bool
    is_current: bool
    matched: bool
    detail: str = ""
    as_of: Optional[datetime] = None

    @property
    def is_pass(self) -> bool:
        return bool(self.readable and self.is_current and self.matched)


class BranchAuditService:
    def __init__(self, session, boot_id: Optional[str] = None):
        self.session = session
        self.boot_id = boot_id or str(uuid.uuid4())

    async def record(self, *, bot_name, event_type, reason_code,
                     reason_detail=None, severity="INFO", source_component,
                     branch_id=None, previous_state=None, new_state=None,
                     exchange_ref_id=None):
        if severity not in am.SEVERITIES:
            raise ValueError(f"severity {severity!r} not in {am.SEVERITIES}")
        row = am.BranchAuditEvent(
            bot_name=bot_name, branch_id=branch_id, event_type=event_type,
            reason_code=reason_code, reason_detail=reason_detail,
            severity=severity, source_component=source_component,
            previous_state=previous_state, new_state=new_state,
            exchange_ref_id=exchange_ref_id, boot_id=self.boot_id)
        try:
            self.session.add(row)
            await self.session.flush()
        except Exception as exc:
            # NOT swallowed. See the module docstring.
            raise AuditUnavailable(
                f"audit write failed ({type(exc).__name__}: {exc}) - the caller "
                f"must block rather than proceed unaudited") from exc
        return row.id

    async def record_truth_failure(self, *, bot_name, asset, classification,
                                   inventory_status, db_quantity,
                                   exchange_quantity, reason_code,
                                   previous_inventory_status=None,
                                   branch_id=None, position_id=None):
        if classification not in am.CLASSIFICATIONS:
            raise ValueError(f"classification {classification!r} unknown")
        # UNCLASSIFIED is the loudest on purpose: coin nobody can place is the
        # case that needs a human, not the case that needs a default.
        severity = {"KNOWN_EXTERNAL": "INFO",
                    "KNOWN_INTERNAL": "ERROR"}.get(classification, "CRITICAL")
        db_q = 0.0 if db_quantity is None else float(db_quantity)
        ex_q = 0.0 if exchange_quantity is None else float(exchange_quantity)
        row = am.ExchangeTruthFailure(
            bot_name=bot_name, branch_id=branch_id, position_id=position_id,
            asset=asset, classification=classification,
            previous_inventory_status=previous_inventory_status,
            inventory_status=inventory_status,
            db_quantity=db_quantity, exchange_quantity=exchange_quantity,
            difference=db_q - ex_q, reason_code=reason_code,
            severity=severity, boot_id=self.boot_id)
        self.session.add(row)
        await self.session.flush()
        await self.record(
            bot_name=bot_name, branch_id=branch_id,
            event_type="EXCHANGE_TRUTH_FAILURE", reason_code=reason_code,
            reason_detail=f"asset={asset} diff={db_q - ex_q} class={classification}",
            severity=severity, source_component="reconciliation_engine",
            previous_state={"inventory_status": previous_inventory_status,
                            "db_qty": db_quantity},
            new_state={"inventory_status": inventory_status,
                       "exchange_qty": exchange_quantity})
        return row.id

    async def record_authority_change(self, *, bot_name, previous_authority,
                                      new_authority, reason_code,
                                      reason_detail=None,
                                      reconciliation_status=None, branch_id=None):
        self.session.add(am.ExecutionAuthorityEvent(
            bot_name=bot_name, branch_id=branch_id,
            previous_authority=bool(previous_authority),
            new_authority=bool(new_authority), reason_code=reason_code,
            reason_detail=reason_detail,
            reconciliation_status=reconciliation_status, boot_id=self.boot_id))
        await self.session.flush()
        await self.record(
            bot_name=bot_name, branch_id=branch_id,
            event_type=("EXECUTION_AUTHORITY_RESTORED" if new_authority
                        else "EXECUTION_AUTHORITY_REVOKED"),
            reason_code=reason_code, reason_detail=reason_detail,
            severity="INFO" if new_authority else "WARN",
            source_component="execution_gate",
            previous_state={"execution_authorized": bool(previous_authority)},
            new_state={"execution_authorized": bool(new_authority)})

    async def record_denial(self, *, bot_name, reason_code, gate_failed,
                            reason_detail=None, candidate_id=None,
                            context=None, branch_id=None):
        self.session.add(am.AllocatorDenial(
            bot_name=bot_name, branch_id=branch_id, candidate_id=candidate_id,
            reason_code=reason_code, reason_detail=reason_detail,
            gate_failed=gate_failed, context=context, boot_id=self.boot_id))
        await self.session.flush()
        await self.record(
            bot_name=bot_name, branch_id=branch_id, event_type="ALLOCATOR_DENIED",
            reason_code=reason_code,
            reason_detail=f"gate={gate_failed} detail={reason_detail}",
            severity="WARN", source_component="allocator_gate",
            new_state=context)


class ExecutionGate:
    """The single entry point. Nothing places an order without passing here.

    FOUR ACTIONS, NOT ONE BOOLEAN. An inventory mismatch must stop a BUY, but
    stopping a reconciliation SELL could entrench the mismatch instead of
    clearing it - so EXIT is judged on different evidence from ENTRY.
    """
    ACTIONS = ("ALLOCATE", "ENTRY", "EXIT", "RELEASE")

    def __init__(self, service: BranchAuditService):
        self.svc = service

    async def _state(self, bot_name):
        res = await self.svc.session.execute(
            select(am.BranchControlState).where(
                am.BranchControlState.bot_name == bot_name))
        return res.scalar_one_or_none()

    async def check(self, *, bot_name, action, truth: ExchangeTruth,
                    candidate_id=None, context=None) -> Decision:
        if action not in self.ACTIONS:
            raise ValueError(f"action {action!r} not in {self.ACTIONS}")

        async def deny(reason_code, gate, detail):
            # The audit is written BEFORE the denial is returned. If it cannot
            # be written the caller gets AuditUnavailable, which is still a
            # refusal - never a silent pass.
            await self.svc.record_denial(
                bot_name=bot_name, reason_code=reason_code, gate_failed=gate,
                reason_detail=detail, candidate_id=candidate_id, context=context)
            return Decision(False, reason_code, detail, gate)

        if not truth.readable:
            return await deny("EXCHANGE_UNAVAILABLE", "exchange_truth_gate",
                              truth.detail or "venue balances unreadable")
        if not truth.is_current:
            return await deny("EXCHANGE_STALE", "exchange_truth_gate",
                              truth.detail or "exchange truth is not current")

        state = await self._state(bot_name)
        if state is None:
            # No control row is UNKNOWN, not ACTIVE. A branch nobody has
            # reconciled has not earned permission by never being looked at.
            return await deny("BRANCH_INACTIVE", "branch_permission_gate",
                              "no branch_control_state row - unknown is not active")
        if state.lifecycle_status == "QUARANTINED":
            # An EXIT is still allowed out of quarantine: the position has to
            # be able to leave, and refusing that would freeze the mismatch in.
            if action != "EXIT":
                return await deny("BRANCH_QUARANTINED", "branch_permission_gate",
                                  state.quarantine_reason or "branch is quarantined")
        elif state.lifecycle_status != "ACTIVE":
            return await deny("BRANCH_INACTIVE", "branch_permission_gate",
                              f"lifecycle_status={state.lifecycle_status}")

        if action in ("ALLOCATE", "ENTRY") and not truth.matched:
            return await deny("RECONCILIATION_MISMATCH", "inventory_truth_gate",
                              truth.detail or "book and venue disagree")
        if action in ("ALLOCATE", "ENTRY") and state.reconciliation_status != "MATCHED":
            return await deny("INVENTORY_UNRECONCILED", "inventory_truth_gate",
                              f"reconciliation_status={state.reconciliation_status}")

        return Decision(True, "OK", f"{action} permitted", "all_gates")


# ---------------------------------------------------------------------------
# ARMING, AND THE TRAP THAT MAKES IT NECESSARY
# ---------------------------------------------------------------------------
#
# ExecutionGate.check() refuses a branch with no BranchControlState row, on
# purpose: a branch nobody has reconciled has not earned permission by never
# having been looked at. That rule is correct and it is also a live hazard.
# There are 23 branches and zero control rows. Wiring the gate in as binding
# would deny every one of them on the next deploy and halt the whole fleet.
#
# So the gate ships the way every other money-touching worker in this repo
# ships: OBSERVE by default. It is CALLED from the order path - so the wiring
# exists and cannot be forgotten - and it records what it would have blocked,
# but it does not block until EXECUTION_GATE_MODE is exactly "enforce".
#
# Observe mode is honestly a staging step, not the finished control. While it
# is observing, a worker CAN still proceed past a denial. What it buys is the
# denial log: a day of evidence showing exactly which branches would have been
# refused and why, before that refusal becomes real money not being deployed.
MODE_ENV = "EXECUTION_GATE_MODE"
MODE_OBSERVE, MODE_ENFORCE = "observe", "enforce"


def gate_mode() -> str:
    v = (os.getenv(MODE_ENV) or "").strip().lower()
    return MODE_ENFORCE if v == MODE_ENFORCE else MODE_OBSERVE


def is_enforcing() -> bool:
    return gate_mode() == MODE_ENFORCE


async def ensure_control_state(session, *, bot_name, branch_id=None,
                               lifecycle_status="ACTIVE",
                               reconciliation_status="UNKNOWN"):
    """Create a control row for a branch that has none. Never overwrites.

    Seeded UNKNOWN, not MATCHED. A branch that has never been reconciled is
    not a reconciled branch, and seeding it MATCHED would hand out the exact
    permission this table exists to withhold - the row would say "checked and
    fine" about a check that never happened.
    """
    row = (await session.execute(
        select(am.BranchControlState).where(
            am.BranchControlState.bot_name == bot_name))).scalar_one_or_none()
    if row is not None:
        return row
    row = am.BranchControlState(
        bot_name=bot_name, branch_id=branch_id,
        lifecycle_status=lifecycle_status,
        reconciliation_status=reconciliation_status,
        execution_enabled=False)
    session.add(row)
    await session.flush()
    return row


async def check_or_observe(gate: "ExecutionGate", *, bot_name, action, truth,
                           candidate_id=None, context=None) -> Decision:
    """Evaluate the gate, and in observe mode report rather than block.

    The audit is written either way - that is the whole point of observing.
    Only the returned `allowed` differs, and the reason code is prefixed so
    nobody reading the denial log can mistake an observation for a refusal
    that actually stopped something.
    """
    decision = await gate.check(bot_name=bot_name, action=action, truth=truth,
                                candidate_id=candidate_id, context=context)
    if decision.allowed or is_enforcing():
        return decision
    log.info(f"[gate] OBSERVE-ONLY: would have blocked {action} on {bot_name} "
             f"- {decision.reason_code} ({decision.gate}). "
             f"Set {MODE_ENV}=enforce to make this binding.")
    return Decision(True, f"OBSERVED_{decision.reason_code}",
                    f"observe mode: would have blocked - {decision.detail}",
                    decision.gate)
