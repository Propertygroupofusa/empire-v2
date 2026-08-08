"""Profit sweep endpoint: manage sweep proposals, funding, and capital injection."""

import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from profit_sweep_engine import (
    create_sweep_proposal,
    mark_sweep_funded,
    apply_sweep_to_bot,
    get_sweep_status,
    get_pending_sweeps,
)

router = APIRouter(tags=["sweep"])
logger = logging.getLogger(__name__)


@router.post("/create")
async def create_sweep(
    manual_approval_required: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new sweep proposal from eligible earnings.

    Triggers profit_sweep_engine to:
    1. Sum platform_amount from eligible payments
    2. Create SweepProposal (status=proposed)
    3. Log audit trail
    4. Return proposal details

    Args:
        manual_approval_required: If True, sweep waits for manual approval before marking funded

    Returns:
        Sweep proposal details {id, status, eligible_amount, payment_ids, ...}
    """
    try:
        sweep = await create_sweep_proposal(manual_approval_required=manual_approval_required)
        if not sweep:
            raise HTTPException(
                status_code=400,
                detail="No eligible payments for sweep",
            )
        return {"status": "success", "sweep": sweep}

    except Exception as e:
        logger.error(f"Error creating sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{sweep_id}/mark-funded")
async def mark_funded(
    sweep_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark a sweep proposal as funded (user has ACH'd capital into account).

    Updates SweepProposal.status -> "funded" and creates audit log entry.
    Next step: call /sweep/{sweep_id}/apply-to-bot to inject capital.

    Args:
        sweep_id: ID of the sweep proposal

    Returns:
        Updated sweep proposal details
    """
    try:
        sweep = await mark_sweep_funded(sweep_id)
        if not sweep:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{sweep_id} not found",
            )
        return {"status": "success", "sweep": sweep}

    except Exception as e:
        logger.error(f"Error marking sweep funded: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{sweep_id}/apply-to-bot")
async def apply_to_bot(
    sweep_id: int,
    bot_name: str = "prop_bot",
    db: AsyncSession = Depends(get_db),
):
    """
    Apply a funded sweep to a trading bot (inject capital into base_capital).

    Updates:
    - SweepProposal.status -> "applied"
    - TradingBotState.base_capital += sweep.eligible_amount

    Args:
        sweep_id: ID of the sweep (must be status="funded")
        bot_name: Name of bot to inject capital into

    Returns:
        Sweep details and updated bot state
    """
    try:
        result = await apply_sweep_to_bot(sweep_id, bot_name=bot_name)
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{sweep_id} not found or not funded",
            )
        return {"status": "success", "data": result}

    except Exception as e:
        logger.error(f"Error applying sweep to bot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{sweep_id}/status")
async def get_status(
    sweep_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Get current status and audit trail of a sweep proposal.

    Returns sweep proposal details plus all audit log entries (creation, funding, application).

    Args:
        sweep_id: ID of the sweep

    Returns:
        {sweep: {...}, audit_trail: [{action, amount, timestamp, ...}]}
    """
    try:
        result = await get_sweep_status(sweep_id)
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{sweep_id} not found",
            )
        return result

    except Exception as e:
        logger.error(f"Error fetching sweep status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pending/list")
async def list_pending(db: AsyncSession = Depends(get_db)):
    """
    List all pending sweeps (proposed or approved, not yet funded or applied).

    Returns list of sweep proposals waiting for action.

    Returns:
        {sweeps: [{id, status, eligible_amount, created_at, ...}]}
    """
    try:
        sweeps = await get_pending_sweeps()
        return {"sweeps": sweeps}

    except Exception as e:
        logger.error(f"Error fetching pending sweeps: {e}")
        raise HTTPException(status_code=500, detail=str(e))
