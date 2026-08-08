"""Profit sweep engine: controlled, rules-based capital injection from platform earnings."""

import logging
from datetime import datetime, timedelta
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from database import AsyncSessionLocal
from models import Payment, SweepProposal, SweepAuditLog, TradingBotState

logger = logging.getLogger(__name__)

# ── Rules Configuration ───────────────────────────────────────────────────
SWEEP_RULES = {
    "min_transfer": 100.0,  # Don't bother with tiny moves
    "max_pct_of_eligible": 0.50,  # Never sweep all free cash
    "min_business_reserve": 500.0,  # Keep operating buffer
    "tax_reserve_pct": 0.25,  # Hold 25% for taxes before eligibility
    "daily_transfer_limit": 1000.0,  # Cap risk per day
    "manual_approval_required": True,  # Start safe
    "auto_propose": True,  # Engine still calculates and creates proposals
}


async def calculate_eligible_amount(session: AsyncSession) -> dict:
    """
    Pure calculator: returns eligible amount and breakdown without writing.

    Formula:
        gross_platform = Σ Payment.platform_amount (paid/processing jobs only)
        − tax_reserve = gross_platform × tax_reserve_pct
        − already_swept = Σ funded SweepProposals.amount (not yet applied)
        − min_business_reserve
        = free_cash

        eligible = min(
            free_cash × max_pct_of_eligible,
            daily_transfer_limit − today_funded,
        )
        if eligible < min_transfer → 0

    Returns:
        dict with: gross_platform, tax_reserve, already_swept, free_cash,
                   today_funded, daily_remaining, eligible_amount, reason
    """
    try:
        # 1. Sum platform_amount from all paid/processing payments
        stmt = select(func.coalesce(func.sum(Payment.platform_amount), 0.0)).where(
            Payment.payout_status.in_(["paid", "processing"])
        )
        result = await session.execute(stmt)
        gross_platform = float(result.scalar())

        # 2. Calculate tax reserve
        tax_reserve = gross_platform * SWEEP_RULES["tax_reserve_pct"]

        # 3. Sum already_swept (funded but not yet applied proposals)
        already_swept_stmt = select(
            func.coalesce(func.sum(SweepProposal.amount), 0.0)
        ).where(SweepProposal.status.in_(["approved", "funded"]))
        already_swept_result = await session.execute(already_swept_stmt)
        already_swept = float(already_swept_result.scalar())

        # 4. Calculate free_cash
        free_cash = (
            gross_platform
            - tax_reserve
            - already_swept
            - SWEEP_RULES["min_business_reserve"]
        )

        # 5. Today's daily limit tracking
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_funded_stmt = select(
            func.coalesce(func.sum(SweepProposal.amount), 0.0)
        ).where(
            and_(
                SweepProposal.status.in_(["approved", "funded", "applied"]),
                SweepProposal.proposed_at >= today_start,
            )
        )
        today_funded_result = await session.execute(today_funded_stmt)
        today_funded = float(today_funded_result.scalar())
        daily_remaining = SWEEP_RULES["daily_transfer_limit"] - today_funded

        # 6. Calculate eligible amount
        if free_cash <= 0:
            eligible = 0.0
            reason = "free_cash <= 0"
        else:
            max_by_pct = free_cash * SWEEP_RULES["max_pct_of_eligible"]
            max_by_daily = max(0, daily_remaining)
            eligible = min(max_by_pct, max_by_daily)

            if eligible < SWEEP_RULES["min_transfer"]:
                reason = f"eligible ${eligible:.2f} < min_transfer ${SWEEP_RULES['min_transfer']:.2f}"
                eligible = 0.0
            else:
                reason = "eligible"

        return {
            "gross_platform": gross_platform,
            "tax_reserve": tax_reserve,
            "business_reserve": SWEEP_RULES["min_business_reserve"],
            "already_swept": already_swept,
            "free_cash": free_cash,
            "today_funded": today_funded,
            "daily_remaining": daily_remaining,
            "eligible_amount": eligible,
            "reason": reason,
            "rules": SWEEP_RULES,
        }

    except Exception as e:
        logger.error(f"Error calculating eligible amount: {e}")
        raise


async def propose_sweep(target_bot_name: str = "prop_bot") -> dict | None:
    """
    Create a sweep proposal if eligible ≥ min_transfer.

    1. Calculate eligible amount
    2. If eligible > 0, create SweepProposal(status="proposed")
    3. If auto_propose=False, return without status="approved"
    4. Log audit event
    5. Return proposal details

    Args:
        target_bot_name: Bot to receive the capital (e.g. "prop_bot")

    Returns:
        Proposal dict with id, amount, status, snapshots, or None if not eligible
    """
    async with AsyncSessionLocal() as session:
        try:
            # Calculate
            calc = await calculate_eligible_amount(session)
            eligible = calc["eligible_amount"]

            # Log calculation event
            calc_audit = SweepAuditLog(
                event="calculated",
                proposal_id=None,
                detail={
                    "gross_platform": calc["gross_platform"],
                    "tax_reserve": calc["tax_reserve"],
                    "business_reserve": calc["business_reserve"],
                    "already_swept": calc["already_swept"],
                    "free_cash": calc["free_cash"],
                    "today_funded": calc["today_funded"],
                    "daily_remaining": calc["daily_remaining"],
                    "eligible_amount": eligible,
                    "reason": calc["reason"],
                },
            )
            session.add(calc_audit)
            await session.flush()

            if eligible <= 0:
                await session.commit()
                logger.info(f"Sweep not eligible: {calc['reason']}")
                return None

            # Create proposal
            initial_status = "approved" if not SWEEP_RULES["manual_approval_required"] else "proposed"
            sweep = SweepProposal(
                amount=eligible,
                status=initial_status,
                gross_platform=calc["gross_platform"],
                tax_reserve=calc["tax_reserve"],
                business_reserve=SWEEP_RULES["min_business_reserve"],
                already_swept=calc["already_swept"],
                free_cash=calc["free_cash"],
                rules_snapshot=SWEEP_RULES,
                target_bot_name=target_bot_name,
                notes=f"Auto-proposed: ${eligible:.2f} eligible from ${calc['gross_platform']:.2f} gross",
            )
            session.add(sweep)
            await session.flush()
            sweep_id = sweep.id

            # Log proposal event
            proposal_audit = SweepAuditLog(
                event="proposed",
                proposal_id=sweep_id,
                detail={
                    "amount": eligible,
                    "status": initial_status,
                    "target_bot": target_bot_name,
                    "manual_approval_required": SWEEP_RULES["manual_approval_required"],
                },
            )
            session.add(proposal_audit)
            await session.commit()

            logger.info(
                f"Sweep proposal #{sweep_id} created: ${eligible:.2f} "
                f"(status={initial_status}, target={target_bot_name})"
            )
            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error proposing sweep: {e}")
            await session.rollback()
            return None


async def approve_sweep(proposal_id: int) -> dict | None:
    """Mark a proposed sweep as approved (human approval)."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepProposal).where(SweepProposal.id == proposal_id)
            result = await session.execute(stmt)
            sweep = result.scalar_one_or_none()

            if not sweep:
                logger.warning(f"Sweep #{proposal_id} not found")
                return None

            if sweep.status != "proposed":
                logger.warning(f"Sweep #{proposal_id} not in proposed state (status={sweep.status})")
                return None

            sweep.status = "approved"
            sweep.approved_at = datetime.utcnow()

            audit = SweepAuditLog(
                event="approved",
                proposal_id=proposal_id,
                detail={"amount": sweep.amount},
            )
            session.add(audit)
            await session.commit()

            logger.info(f"Sweep #{proposal_id} approved (${sweep.amount:.2f})")
            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error approving sweep: {e}")
            await session.rollback()
            return None


async def mark_sweep_funded(proposal_id: int) -> dict | None:
    """Mark a sweep as funded (ACH landed in Alpaca account)."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepProposal).where(SweepProposal.id == proposal_id)
            result = await session.execute(stmt)
            sweep = result.scalar_one_or_none()

            if not sweep:
                logger.warning(f"Sweep #{proposal_id} not found")
                return None

            if sweep.status not in ("approved", "proposed"):
                logger.warning(f"Sweep #{proposal_id} cannot be marked funded from status={sweep.status}")
                return None

            sweep.status = "funded"
            sweep.funded_at = datetime.utcnow()

            audit = SweepAuditLog(
                event="funded",
                proposal_id=proposal_id,
                detail={
                    "amount": sweep.amount,
                    "target_bot": sweep.target_bot_name,
                },
            )
            session.add(audit)
            await session.commit()

            logger.info(f"Sweep #{proposal_id} marked funded (${sweep.amount:.2f})")
            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error marking sweep funded: {e}")
            await session.rollback()
            return None


async def apply_sweep_to_bot(proposal_id: int) -> dict | None:
    """
    Apply a funded sweep: inject capital into TradingBotState.base_capital.

    Preconditions:
    - Sweep must be status="funded"
    - Target bot must exist in TradingBotState

    Updates:
    - SweepProposal.status → "applied"
    - SweepProposal.applied_at → now
    - TradingBotState.base_capital += sweep.amount
    - Log audit events
    """
    async with AsyncSessionLocal() as session:
        try:
            # Fetch sweep
            sweep_stmt = select(SweepProposal).where(SweepProposal.id == proposal_id)
            sweep_result = await session.execute(sweep_stmt)
            sweep = sweep_result.scalar_one_or_none()

            if not sweep:
                logger.warning(f"Sweep #{proposal_id} not found")
                return None

            if sweep.status != "funded":
                logger.warning(f"Sweep #{proposal_id} not funded (status={sweep.status})")
                return None

            target_bot = sweep.target_bot_name or "prop_bot"

            # Fetch bot state
            bot_stmt = select(TradingBotState).where(TradingBotState.bot_name == target_bot)
            bot_result = await session.execute(bot_stmt)
            bot_state = bot_result.scalar_one_or_none()

            if not bot_state:
                logger.warning(f"Bot '{target_bot}' not found")
                return None

            # Inject capital
            previous_capital = bot_state.base_capital
            bot_state.base_capital += sweep.amount

            # Update sweep
            sweep.status = "applied"
            sweep.applied_at = datetime.utcnow()

            # Audit logs
            sweep_audit = SweepAuditLog(
                event="applied",
                proposal_id=proposal_id,
                detail={
                    "amount": sweep.amount,
                    "bot_name": target_bot,
                    "previous_capital": previous_capital,
                    "new_capital": bot_state.base_capital,
                },
            )
            session.add(sweep_audit)
            await session.commit()

            logger.info(
                f"Sweep #{proposal_id} applied to '{target_bot}': "
                f"injected ${sweep.amount:.2f} "
                f"(${previous_capital:.2f} → ${bot_state.base_capital:.2f})"
            )

            return {
                "sweep": sweep.to_dict(),
                "bot_state": {
                    "bot_name": target_bot,
                    "base_capital": bot_state.base_capital,
                    "starting_capital": bot_state.starting_capital,
                },
            }

        except Exception as e:
            logger.error(f"Error applying sweep: {e}")
            await session.rollback()
            return None


async def reject_sweep(proposal_id: int, reason: str = "") -> dict | None:
    """Reject/cancel a sweep proposal."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepProposal).where(SweepProposal.id == proposal_id)
            result = await session.execute(stmt)
            sweep = result.scalar_one_or_none()

            if not sweep:
                return None

            sweep.status = "cancelled"
            sweep.cancelled_at = datetime.utcnow()

            audit = SweepAuditLog(
                event="cancelled",
                proposal_id=proposal_id,
                detail={"amount": sweep.amount, "reason": reason},
            )
            session.add(audit)
            await session.commit()

            logger.info(f"Sweep #{proposal_id} cancelled: {reason}")
            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error rejecting sweep: {e}")
            await session.rollback()
            return None


async def get_sweep_history(limit: int = 50) -> list[dict]:
    """Fetch audit trail of all sweep events."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepAuditLog).order_by(SweepAuditLog.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            logs = result.scalars().all()
            return [log.to_dict() for log in logs]

        except Exception as e:
            logger.error(f"Error fetching sweep history: {e}")
            return []


async def get_proposal_status(proposal_id: int) -> dict | None:
    """Fetch proposal with its audit trail."""
    async with AsyncSessionLocal() as session:
        try:
            prop_stmt = select(SweepProposal).where(SweepProposal.id == proposal_id)
            prop_result = await session.execute(prop_stmt)
            proposal = prop_result.scalar_one_or_none()

            if not proposal:
                return None

            audit_stmt = select(SweepAuditLog).where(
                SweepAuditLog.proposal_id == proposal_id
            ).order_by(SweepAuditLog.created_at)
            audit_result = await session.execute(audit_stmt)
            audit_logs = audit_result.scalars().all()

            return {
                "proposal": proposal.to_dict(),
                "audit_trail": [log.to_dict() for log in audit_logs],
            }

        except Exception as e:
            logger.error(f"Error fetching proposal status: {e}")
            return None


async def list_pending_proposals() -> list[dict]:
    """Fetch all proposed/approved (not yet funded/applied) proposals."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepProposal).where(
                SweepProposal.status.in_(["proposed", "approved"])
            ).order_by(SweepProposal.proposed_at.desc())
            result = await session.execute(stmt)
            proposals = result.scalars().all()
            return [p.to_dict() for p in proposals]

        except Exception as e:
            logger.error(f"Error listing pending proposals: {e}")
            return []
