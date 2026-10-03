"""Append-only audit and control-state tables. ADDITIVE ONLY.

WHY THESE LIVE HERE AND NOT IN A .sql FILE
------------------------------------------
This repo has no file-based migration runner. The whole mechanism is two
steps, both already proven in production:

    database.py   create_all()      creates tables that do not exist
    main.py:413   run_migrations()  adds columns the ORM declares and the
                                    real table lacks, for EVERY table on
                                    Base.metadata

A V0xx__*.sql file would have nothing to execute it. Declaring these as ORM
models is the only route that actually runs, and it inherits the existing
drift repair for free.

For the same reason there are NO plpgsql functions and NO triggers here:
there is nowhere in this deployment to install them where they would survive
a redeploy. The audit engine is an async Python service instead
(branch_audit_service.py), which also keeps the event loop unblocked - the
app runs async SQLAlchemy over asyncpg and a synchronous psycopg2 path would
stall every worker sharing the loop.

DELIBERATE TYPE CHOICES
-----------------------
String primary keys with a PYTHON-side uuid4 default, not gen_random_uuid().
That function needs PostgreSQL 13+ or pgcrypto, and nothing here can verify
the deployed server's version. A Python default removes the dependency
entirely and behaves identically.

JSON, not JSONB. models.py already imports and uses JSON 21 times; JSONB
would be a second convention for no benefit until something actually needs
to index inside a document.

String, not a native PostgreSQL ENUM, for severity and classification. An
ENUM is a trap under a model-driven migration runner: adding a value later
needs ALTER TYPE, and run_migrations() can only add COLUMNS. The allowed
values are enforced in Python, where they can change without a migration.

IDENTITY
--------
bot_name is the durable identity and survives a branch being deleted and
recreated. branch_id is the CURRENT row id and is nullable, because the row
it points at may be gone while the history must not be. An audit trail keyed
only on an integer PK loses continuity exactly when it is most needed.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, Index

from models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# Allowed values, enforced in Python rather than by a database ENUM.
SEVERITIES = ("INFO", "WARN", "ERROR", "CRITICAL")
CLASSIFICATIONS = ("KNOWN_INTERNAL", "KNOWN_EXTERNAL", "UNCLASSIFIED")
LIFECYCLE_STATUSES = ("ACTIVE", "PAUSED", "QUARANTINED", "CLOSED")
RECONCILIATION_STATUSES = ("MATCHED", "MISMATCH", "STALE", "UNKNOWN")


class BranchAuditEvent(Base):
    """The one timeline. Every other table in this module also routes here."""
    __tablename__ = "branch_audit_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    bot_name = Column(String, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True)
    event_type = Column(String, nullable=False)
    reason_code = Column(String, nullable=False, index=True)
    reason_detail = Column(String, nullable=True)
    severity = Column(String, nullable=False, default="INFO")
    source_component = Column(String, nullable=False)
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=True)
    exchange_ref_id = Column(String, nullable=True)
    boot_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ExchangeTruthFailure(Base):
    """A disagreement between the book and the venue. NOT a loss.

    db_quantity and exchange_quantity are Float to match every other
    quantity column in this schema. NUMERIC(38,18) would be more precise
    and would also be the only exact-decimal column in the repo, disagreeing
    with the floats it is compared against - a second precision convention
    is its own class of bug.
    """
    __tablename__ = "exchange_truth_failures"

    id = Column(String(36), primary_key=True, default=_uuid)
    bot_name = Column(String, nullable=True, index=True)
    branch_id = Column(Integer, nullable=True)
    position_id = Column(String, nullable=True)
    asset = Column(String, nullable=False, index=True)
    classification = Column(String, nullable=False, default="UNCLASSIFIED")
    previous_inventory_status = Column(String, nullable=True)
    inventory_status = Column(String, nullable=False)
    db_quantity = Column(Float, nullable=True)
    exchange_quantity = Column(Float, nullable=True)
    difference = Column(Float, nullable=True)
    reason_code = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False, default="ERROR")
    boot_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ExecutionAuthorityEvent(Base):
    """Every time a branch gained or lost the right to trade, and why."""
    __tablename__ = "execution_authority_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    bot_name = Column(String, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True)
    previous_authority = Column(Boolean, nullable=False)
    new_authority = Column(Boolean, nullable=False)
    reason_code = Column(String, nullable=False)
    reason_detail = Column(String, nullable=True)
    reconciliation_status = Column(String, nullable=True)
    boot_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class AllocatorDenial(Base):
    """Why capital was NOT allocated. The question the dashboard cannot
    currently answer: 'the allocator did nothing - was that correct?'"""
    __tablename__ = "allocator_denials"

    id = Column(String(36), primary_key=True, default=_uuid)
    bot_name = Column(String, nullable=False, index=True)
    branch_id = Column(Integer, nullable=True)
    candidate_id = Column(String, nullable=True)
    reason_code = Column(String, nullable=False, index=True)
    reason_detail = Column(String, nullable=True)
    gate_failed = Column(String, nullable=False, index=True)
    context = Column(JSON, nullable=True)
    boot_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class BranchControlState(Base):
    """The safety state, kept OUT of crypto_grid_branches on purpose.

    crypto_grid_branches.active is operational configuration: should the
    cycle driver visit this branch. It is not a permission. Putting a
    lifecycle status on that table would have made one column answer two
    questions, and the existing one already has a meaning.

    So: active = True does NOT mean allowed_to_trade = True. That
    separation is the entire point of this table.

    execution_enabled is the MATERIALISED permission - the last answer the
    gate computed. It is not the authority. Nothing should read this column
    and place an order; the order path calls the gate, which recomputes from
    exchange truth. A persisted boolean is a cache, and a cache consulted
    as a permission is how a stale True becomes a trade.
    """
    __tablename__ = "branch_control_state"

    bot_name = Column(String, primary_key=True)
    branch_id = Column(Integer, nullable=True)
    lifecycle_status = Column(String, nullable=False, default="ACTIVE")
    reconciliation_status = Column(String, nullable=False, default="UNKNOWN")
    execution_enabled = Column(Boolean, nullable=False, default=False)
    quarantine_reason = Column(String, nullable=True)
    boot_id = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


# Composite indexes the dashboard query in the brief actually needs.
Index("idx_bae_bot_created", BranchAuditEvent.bot_name, BranchAuditEvent.created_at)
Index("idx_ad_reason_gate", AllocatorDenial.reason_code, AllocatorDenial.gate_failed)
Index("idx_etf_bot_created", ExchangeTruthFailure.bot_name, ExchangeTruthFailure.created_at)
Index("idx_eae_bot_created", ExecutionAuthorityEvent.bot_name, ExecutionAuthorityEvent.created_at)

AUDIT_TABLES = ("branch_audit_events", "exchange_truth_failures",
                "execution_authority_events", "allocator_denials",
                "branch_control_state")
