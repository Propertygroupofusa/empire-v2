"""Profit sweep engine: collect platform earnings, propose sweeps, track funding & application."""

import logging
from datetime import datetime
from sqlalchemy import select, and_
from database import AsyncSessionLocal
from models import Payment, SweepProposal, SweepAuditLog, TradingBotState

logger = logging.getLogger(__name__)


async def create_sweep_proposal(manual_approval_required: bool = True) -> dict | None:
    """
    Create a sweep proposal by summing platform_amount from eligible completed payments.

    Workflow:
    1. Query all payments with payout_status not yet swept (e.g., 'received' or 'pending')
    2. Sum their platform_amount (earnings)
    3. Create SweepProposal with status='proposed' (or auto-approve if manual_approval_required=False)
    4. Log audit entry with action='created'
    5. Return sweep proposal details

    Returns:
        dict with sweep proposal details, or None if no eligible payments
    """
    async with AsyncSessionLocal() as session:
        try:
            # Find eligible payments: those with platform earnings not yet swept
            # Status can be 'received', 'pending', or any non-swept state
            stmt = select(Payment).where(
                and_(
                    Payment.platform_amount > 0,
                    Payment.payout_status.in_(["received", "pending"])
                )
            )
            result = await session.execute(stmt)
            eligible_payments = result.scalars().all()

            if not eligible_payments:
                logger.info("No eligible payments for sweep")
                return None

            # Sum platform_amount
            total_platform_amount = sum(p.platform_amount for p in eligible_payments)
            payment_ids = [p.id for p in eligible_payments]

            # Create sweep proposal
            sweep = SweepProposal(
                status="proposed" if manual_approval_required else "approved",
                eligible_amount=total_platform_amount,
                manual_approval_required=manual_approval_required,
                notes=f"Auto-generated sweep of {len(payment_ids)} payments",
                payment_ids=payment_ids,
            )
            session.add(sweep)
            await session.flush()  # Get the ID before commit
            sweep_id = sweep.id

            # Create audit log entry
            audit = SweepAuditLog(
                sweep_proposal_id=sweep_id,
                action="created",
                amount=total_platform_amount,
                details={
                    "payment_count": len(payment_ids),
                    "manual_approval_required": manual_approval_required,
                },
            )
            session.add(audit)
            await session.commit()

            logger.info(
                f"Created sweep proposal #{sweep_id}: ${total_platform_amount:.2f} "
                f"from {len(payment_ids)} payments"
            )

            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error creating sweep proposal: {e}")
            await session.rollback()
            return None


async def mark_sweep_funded(sweep_id: int) -> dict | None:
    """
    Mark a sweep proposal as funded (user has ACH'd the capital into trading account).

    Updates:
    - SweepProposal.status -> "funded"
    - SweepProposal.funded_at -> now
    - Creates audit log entry with action='funded'

    Args:
        sweep_id: ID of the sweep proposal

    Returns:
        Updated sweep proposal dict, or None if not found or error
    """
    async with AsyncSessionLocal() as session:
        try:
            # Fetch sweep
            stmt = select(SweepProposal).where(SweepProposal.id == sweep_id)
            result = await session.execute(stmt)
            sweep = result.scalar_one_or_none()

            if not sweep:
                logger.warning(f"Sweep #{sweep_id} not found")
                return None

            # Update status
            sweep.status = "funded"
            sweep.funded_at = datetime.utcnow()

            # Log audit entry
            audit = SweepAuditLog(
                sweep_proposal_id=sweep_id,
                action="funded",
                amount=sweep.eligible_amount,
                details={"funded_at": sweep.funded_at.isoformat()},
            )
            session.add(audit)
            await session.commit()

            logger.info(f"Marked sweep #{sweep_id} as funded (${sweep.eligible_amount:.2f})")
            return sweep.to_dict()

        except Exception as e:
            logger.error(f"Error marking sweep funded: {e}")
            await session.rollback()
            return None


async def apply_sweep_to_bot(sweep_id: int, bot_name: str = "prop_bot") -> dict | None:
    """
    Apply a funded sweep to a trading bot by injecting capital into TradingBotState.base_capital.

    Updates:
    - SweepProposal.status -> "applied"
    - SweepProposal.applied_at -> now
    - TradingBotState.base_capital += sweep.eligible_amount
    - Creates audit log entries for both sweep and bot state change

    Args:
        sweep_id: ID of the sweep proposal (must be status="funded")
        bot_name: Name of the trading bot to inject capital into

    Returns:
        dict with sweep and bot state details, or None if error
    """
    async with AsyncSessionLocal() as session:
        try:
            # Fetch sweep
            sweep_stmt = select(SweepProposal).where(SweepProposal.id == sweep_id)
            sweep_result = await session.execute(sweep_stmt)
            sweep = sweep_result.scalar_one_or_none()

            if not sweep:
                logger.warning(f"Sweep #{sweep_id} not found")
                return None

            if sweep.status != "funded":
                logger.warning(f"Sweep #{sweep_id} not funded (status={sweep.status})")
                return None

            # Fetch bot state
            bot_stmt = select(TradingBotState).where(TradingBotState.bot_name == bot_name)
            bot_result = await session.execute(bot_stmt)
            bot_state = bot_result.scalar_one_or_none()

            if not bot_state:
                logger.warning(f"Bot '{bot_name}' not found in trading bot state")
                return None

            # Inject capital
            previous_capital = bot_state.base_capital
            bot_state.base_capital += sweep.eligible_amount

            # Update sweep
            sweep.status = "applied"
            sweep.applied_at = datetime.utcnow()

            # Log audit entries
            sweep_audit = SweepAuditLog(
                sweep_proposal_id=sweep_id,
                action="applied",
                amount=sweep.eligible_amount,
                details={
                    "bot_name": bot_name,
                    "applied_at": sweep.applied_at.isoformat(),
                },
            )
            session.add(sweep_audit)

            # Also log the capital injection to bot state
            bot_audit = SweepAuditLog(
                sweep_proposal_id=sweep_id,
                action="capital_injected",
                amount=sweep.eligible_amount,
                details={
                    "bot_name": bot_name,
                    "previous_capital": previous_capital,
                    "new_capital": bot_state.base_capital,
                },
            )
            session.add(bot_audit)

            await session.commit()

            logger.info(
                f"Applied sweep #{sweep_id} to bot '{bot_name}': "
                f"injected ${sweep.eligible_amount:.2f} "
                f"(${previous_capital:.2f} -> ${bot_state.base_capital:.2f})"
            )

            return {
                "sweep": sweep.to_dict(),
                "bot_state": {
                    "bot_name": bot_state.bot_name,
                    "base_capital": bot_state.base_capital,
                    "starting_capital": bot_state.starting_capital,
                },
            }

        except Exception as e:
            logger.error(f"Error applying sweep to bot: {e}")
            await session.rollback()
            return None


async def get_sweep_status(sweep_id: int) -> dict | None:
    """Fetch current sweep proposal status and audit trail."""
    async with AsyncSessionLocal() as session:
        try:
            # Fetch sweep
            sweep_stmt = select(SweepProposal).where(SweepProposal.id == sweep_id)
            sweep_result = await session.execute(sweep_stmt)
            sweep = sweep_result.scalar_one_or_none()

            if not sweep:
                return None

            # Fetch audit trail
            audit_stmt = select(SweepAuditLog).where(
                SweepAuditLog.sweep_proposal_id == sweep_id
            ).order_by(SweepAuditLog.created_at)
            audit_result = await session.execute(audit_stmt)
            audit_logs = audit_result.scalars().all()

            return {
                "sweep": sweep.to_dict(),
                "audit_trail": [a.to_dict() for a in audit_logs],
            }

        except Exception as e:
            logger.error(f"Error fetching sweep status: {e}")
            return None


async def get_pending_sweeps() -> list[dict]:
    """Fetch all pending (proposed or approved but not yet funded/applied) sweeps."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = select(SweepProposal).where(
                SweepProposal.status.in_(["proposed", "approved"])
            ).order_by(SweepProposal.created_at.desc())
            result = await session.execute(stmt)
            sweeps = result.scalars().all()
            return [s.to_dict() for s in sweeps]

        except Exception as e:
            logger.error(f"Error fetching pending sweeps: {e}")
            return []
