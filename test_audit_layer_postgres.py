"""The audit layer against REAL PostgreSQL. Not SQLite.

SQLite would prove Python behaviour and nothing about JSON round-trips,
server defaults, index creation or create_all idempotency on Postgres, which
are the things that actually break on deploy. Set AUDIT_TEST_DSN to run.
"""
import asyncio
import os
import sys

FAILS = []
DSN = os.getenv("AUDIT_TEST_DSN")


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


if not DSN:
    print("SKIPPED - set AUDIT_TEST_DSN to a PostgreSQL DSN to run this suite.")
    print("A skip is not a pass. Nothing below was verified.")
    sys.exit(0)

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import Base
import audit_models as am
from branch_audit_service import (AuditUnavailable, BranchAuditService,
                                  Decision, ExchangeTruth, ExecutionGate)

engine = create_async_engine(DSN)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def run():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    print("\n[1] every field survives a PostgreSQL round trip")
    async with Session() as s:
        svc = BranchAuditService(s, boot_id="boot-aaa")
        eid = await svc.record(
            bot_name="crypto_grid_6", branch_id=17, event_type="TEST",
            reason_code="ROUND_TRIP", reason_detail="detail text",
            severity="WARN", source_component="test",
            previous_state={"status": "ACTIVE", "n": 1},
            new_state={"status": "QUARANTINED", "nested": {"a": [1, 2]}},
            exchange_ref_id="order-123")
        await s.commit()
        row = (await s.execute(select(am.BranchAuditEvent).where(
            am.BranchAuditEvent.id == eid))).scalar_one()
        ok("bot_name preserved", row.bot_name == "crypto_grid_6", row.bot_name)
        ok("branch_id preserved", row.branch_id == 17, row.branch_id)
        ok("boot_id preserved", row.boot_id == "boot-aaa", row.boot_id)
        ok("previous_state JSON preserved", row.previous_state == {"status": "ACTIVE", "n": 1})
        ok("NESTED new_state JSON preserved",
           row.new_state["nested"] == {"a": [1, 2]}, row.new_state)
        ok("reason_code preserved", row.reason_code == "ROUND_TRIP")
        ok("exchange_ref_id preserved", row.exchange_ref_id == "order-123")
        ok("created_at populated by default", row.created_at is not None)
        ok("id is a real uuid string", len(row.id) == 36, row.id)

    print("\n[2] severity is validated in Python, since there is no DB enum")
    async with Session() as s:
        svc = BranchAuditService(s)
        try:
            await svc.record(bot_name="b", event_type="T", reason_code="R",
                             severity="NOT_A_SEVERITY", source_component="test")
            ok("an invalid severity is refused", False, "accepted")
        except ValueError:
            ok("an invalid severity is refused", True)

    print("\n[3] classification routes severity, UNCLASSIFIED is loudest")
    async with Session() as s:
        svc = BranchAuditService(s)
        for cls, want in (("KNOWN_EXTERNAL", "INFO"), ("KNOWN_INTERNAL", "ERROR"),
                          ("UNCLASSIFIED", "CRITICAL")):
            fid = await svc.record_truth_failure(
                bot_name="crypto_grid_14", asset="QNT", classification=cls,
                inventory_status="DB_ONLY", db_quantity=0.675982,
                exchange_quantity=0.000973, reason_code="QTY_MISMATCH")
            await s.commit()
            r = (await s.execute(select(am.ExchangeTruthFailure).where(
                am.ExchangeTruthFailure.id == fid))).scalar_one()
            ok(f"{cls} -> {want}", r.severity == want, r.severity)
            if cls == "UNCLASSIFIED":
                ok("difference computed from the two quantities",
                   abs(r.difference - 0.675009) < 1e-6, r.difference)

    print("\n[4] THE CRITICAL PATH: DB 100 BTC vs Coinbase 50 BTC")
    async with Session() as s:
        svc = BranchAuditService(s, boot_id="boot-bbb")
        gate = ExecutionGate(svc)
        s.add(am.BranchControlState(bot_name="crypto_grid_btc", branch_id=5,
                                    lifecycle_status="ACTIVE",
                                    reconciliation_status="MATCHED",
                                    execution_enabled=True))
        await s.commit()
        await svc.record_truth_failure(
            bot_name="crypto_grid_btc", asset="BTC", classification="KNOWN_INTERNAL",
            previous_inventory_status="RECONCILED", inventory_status="DB_ONLY",
            db_quantity=100.0, exchange_quantity=50.0,
            reason_code="RECONCILIATION_MISMATCH", branch_id=5)
        mismatch = ExchangeTruth(readable=True, is_current=True, matched=False,
                                 detail="DB 100 BTC vs venue 50 BTC")
        d_alloc = await gate.check(bot_name="crypto_grid_btc", action="ALLOCATE",
                                   truth=mismatch, candidate_id="cand-1")
        d_entry = await gate.check(bot_name="crypto_grid_btc", action="ENTRY",
                                   truth=mismatch)
        await s.commit()
        ok("NEW ALLOCATION = 0", d_alloc.allowed is False, d_alloc)
        ok("NEW BUY = 0", d_entry.allowed is False, d_entry)
        ok("denied by the inventory truth gate",
           d_alloc.gate == "inventory_truth_gate", d_alloc.gate)
        ok("reason is RECONCILIATION_MISMATCH",
           d_alloc.reason_code == "RECONCILIATION_MISMATCH", d_alloc.reason_code)
        d_exit = await gate.check(bot_name="crypto_grid_btc", action="EXIT",
                                  truth=mismatch)
        await s.commit()
        ok("an EXIT is still permitted - a position must be able to leave",
           d_exit.allowed is True, d_exit)
        f = (await s.execute(select(am.ExchangeTruthFailure).where(
            am.ExchangeTruthFailure.asset == "BTC"))).scalars().all()
        ok("the 50 BTC discrepancy is REPORTED, not corrected",
           len(f) == 1 and f[0].difference == 50.0, [x.difference for x in f])
        ok("...and the db quantity is left exactly as it was",
           f[0].db_quantity == 100.0, f[0].db_quantity)
        dn = (await s.execute(select(am.AllocatorDenial))).scalars().all()
        ok("both denials written to the denial log", len(dn) >= 2, len(dn))

    print("\n[5] the full lifecycle leaves every transition in the timeline")
    async with Session() as s:
        svc = BranchAuditService(s, boot_id="boot-ccc")
        bot = "crypto_grid_life"
        s.add(am.BranchControlState(bot_name=bot, lifecycle_status="ACTIVE",
                                    reconciliation_status="MATCHED",
                                    execution_enabled=True))
        await s.flush()
        st = (await s.execute(select(am.BranchControlState).where(
            am.BranchControlState.bot_name == bot))).scalar_one()
        await svc.record_truth_failure(
            bot_name=bot, asset="ZEC", classification="KNOWN_INTERNAL",
            previous_inventory_status="RECONCILED", inventory_status="DB_ONLY",
            db_quantity=1.419, exchange_quantity=1.332,
            reason_code="RECONCILIATION_MISMATCH")
        st.lifecycle_status, st.reconciliation_status = "QUARANTINED", "MISMATCH"
        st.quarantine_reason = "book and venue disagree"
        await svc.record(bot_name=bot, event_type="AUTO_QUARANTINE",
                         reason_code="SAFETY_VIOLATION_QUARANTINE",
                         severity="ERROR", source_component="reconciliation_engine",
                         previous_state={"lifecycle_status": "ACTIVE"},
                         new_state={"lifecycle_status": "QUARANTINED"})
        await svc.record_authority_change(
            bot_name=bot, previous_authority=True, new_authority=False,
            reason_code="RECONCILIATION_MISMATCH", reconciliation_status="MISMATCH")
        st.reconciliation_status = "MATCHED"
        await svc.record(bot_name=bot, event_type="RECONCILIATION_REPAIRED",
                         reason_code="REPAIRED", severity="INFO",
                         source_component="reconciliation_engine",
                         previous_state={"reconciliation_status": "MISMATCH"},
                         new_state={"reconciliation_status": "MATCHED"})
        st.lifecycle_status, st.quarantine_reason = "ACTIVE", None
        await svc.record(bot_name=bot, event_type="BRANCH_REACTIVATED",
                         reason_code="QUARANTINE_RECOVERY_COMPLETED",
                         severity="INFO", source_component="branch_state_machine",
                         previous_state={"lifecycle_status": "QUARANTINED"},
                         new_state={"lifecycle_status": "ACTIVE"})
        await svc.record_authority_change(
            bot_name=bot, previous_authority=False, new_authority=True,
            reason_code="QUARANTINE_RECOVERY_COMPLETED",
            reconciliation_status="MATCHED")
        await s.commit()
        types = [r.event_type for r in (await s.execute(
            select(am.BranchAuditEvent).where(am.BranchAuditEvent.bot_name == bot)
            .order_by(am.BranchAuditEvent.created_at))).scalars().all()]
        for want in ("EXCHANGE_TRUTH_FAILURE", "AUTO_QUARANTINE",
                     "EXECUTION_AUTHORITY_REVOKED", "RECONCILIATION_REPAIRED",
                     "BRANCH_REACTIVATED", "EXECUTION_AUTHORITY_RESTORED"):
            ok(f"timeline contains {want}", want in types, types)
        gate = ExecutionGate(svc)
        d = await gate.check(bot_name=bot, action="ENTRY",
                             truth=ExchangeTruth(True, True, True))
        await s.commit()
        ok("after repair, ENTRY is permitted again", d.allowed is True, d)

    print("\n[6] FAIL CLOSED: an audit that cannot be written blocks the trade")
    async with Session() as s:
        svc = BranchAuditService(s)
        gate = ExecutionGate(svc)
        s.add(am.BranchControlState(bot_name="crypto_grid_fc",
                                    lifecycle_status="ACTIVE",
                                    reconciliation_status="MATCHED"))
        await s.commit()
        async def broken(*a, **k):
            raise AuditUnavailable("audit table unreachable")
        svc.record_denial = broken
        try:
            d = await gate.check(bot_name="crypto_grid_fc", action="ENTRY",
                                 truth=ExchangeTruth(readable=False, is_current=False,
                                                     matched=False, detail="429"))
            ok("a failed audit does NOT return a permission", False, d)
        except AuditUnavailable:
            ok("a failed audit raises rather than permitting", True)

    print("\n[7] unknown branch is UNKNOWN, not ACTIVE")
    async with Session() as s:
        gate = ExecutionGate(BranchAuditService(s))
        d = await gate.check(bot_name="never_seen", action="ENTRY",
                             truth=ExchangeTruth(True, True, True))
        await s.commit()
        ok("a branch with no control row is refused", d.allowed is False, d)
        ok("...named as BRANCH_INACTIVE", d.reason_code == "BRANCH_INACTIVE", d)

    print("\n[8] the dashboard denial query runs on this schema")
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT gate_failed, reason_code, COUNT(*) AS total,
                   ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) AS pct
            FROM allocator_denials
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY gate_failed, reason_code ORDER BY total DESC"""))).all()
        ok("the window-function query executes", True)
        for r in rows:
            print(f"        {r[0]:24s} {r[1]:26s} {r[2]:>3}  {r[3]}%")

    await engine.dispose()


asyncio.run(run())
print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
