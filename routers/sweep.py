"""Profit sweep API: managed capital injection from platform earnings with rules and audit."""

import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from profit_sweep_engine import (
    calculate_eligible_amount,
    propose_sweep,
    approve_sweep,
    mark_sweep_funded,
    apply_sweep_to_bot,
    reject_sweep,
    get_sweep_history,
    get_proposal_status,
    list_pending_proposals,
)

router = APIRouter(tags=["sweep"])
logger = logging.getLogger(__name__)


@router.get("/eligible")
async def get_eligible(db: AsyncSession = Depends(get_db)):
    """
    Run the sweep calculator without writing to database.

    Returns breakdown of:
    - gross_platform: Sum of all platform earnings
    - tax_reserve: Amount held for taxes
    - business_reserve: Operating buffer
    - already_swept: Previously funded proposals
    - free_cash: Available for sweep
    - daily_remaining: Budget left today
    - eligible_amount: Amount that can be swept right now
    - reason: Why eligible_amount is what it is

    No side effects; read-only.
    """
    try:
        calc = await calculate_eligible_amount(db)
        return {
            "status": "success",
            "calculator": calc,
        }

    except Exception as e:
        logger.error(f"Error calculating eligible amount: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/propose")
async def propose(target_bot_name: str = "prop_bot", db: AsyncSession = Depends(get_db)):
    """
    Create a sweep proposal if eligible ≥ min_transfer.

    If manual_approval_required (default), status="proposed" (awaiting approval).
    If auto_propose, still creates the proposal.

    Triggers SweepAuditLog events: "calculated" + "proposed".

    Returns:
        Sweep proposal details {id, amount, status, snapshots...}
        or error if not eligible.
    """
    try:
        proposal = await propose_sweep(target_bot_name=target_bot_name)
        if not proposal:
            raise HTTPException(
                status_code=400,
                detail="No eligible amount for sweep (see /sweep/eligible for breakdown)",
            )
        return {"status": "success", "proposal": proposal}

    except Exception as e:
        logger.error(f"Error proposing sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{proposal_id}/approve")
async def approve(proposal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Human approve a proposed sweep (status: proposed → approved).

    Approval is a checkpoint; it does NOT move money.
    Next: user funds via ACH in Alpaca, then call /mark-funded.
    """
    try:
        proposal = await approve_sweep(proposal_id)
        if not proposal:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{proposal_id} not found or not in proposed state",
            )
        return {"status": "success", "proposal": proposal}

    except Exception as e:
        logger.error(f"Error approving sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{proposal_id}/mark-funded")
async def mark_funded(proposal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Mark a sweep as funded (user has ACH'd capital into Alpaca account).

    Updates status: approved/proposed → funded.
    Audit logs the funding event.

    Next: call /apply-to-bot to inject into trading capital.
    """
    try:
        proposal = await mark_sweep_funded(proposal_id)
        if not proposal:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{proposal_id} not found or cannot be marked funded",
            )
        return {"status": "success", "proposal": proposal}

    except Exception as e:
        logger.error(f"Error marking sweep funded: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{proposal_id}/apply-to-bot")
async def apply_to_bot(proposal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Apply a funded sweep: inject capital into TradingBotState.base_capital.

    Preconditions:
    - Sweep must be status="funded"
    - Target bot must exist in TradingBotState

    Updates:
    - SweepProposal.status → "applied"
    - TradingBotState.base_capital += sweep.amount
    - Audit logs the capital injection

    After this, the bot will see the higher capital balance on next /v2/account check.
    """
    try:
        result = await apply_sweep_to_bot(proposal_id)
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{proposal_id} not found, not funded, or bot not found",
            )
        return {"status": "success", "data": result}

    except Exception as e:
        logger.error(f"Error applying sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{proposal_id}/reject")
async def reject(proposal_id: int, reason: str = "", db: AsyncSession = Depends(get_db)):
    """
    Reject/cancel a sweep proposal.

    Updates status → "cancelled" and logs the cancellation event.
    """
    try:
        proposal = await reject_sweep(proposal_id, reason=reason)
        if not proposal:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{proposal_id} not found",
            )
        return {"status": "success", "proposal": proposal}

    except Exception as e:
        logger.error(f"Error rejecting sweep: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{proposal_id}/status")
async def get_status(proposal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Fetch a specific proposal with its full audit trail.

    Returns:
        {
            "proposal": {...full proposal object...},
            "audit_trail": [{event, proposal_id, detail, created_at}, ...]
        }
    """
    try:
        result = await get_proposal_status(proposal_id)
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Sweep #{proposal_id} not found",
            )
        return result

    except Exception as e:
        logger.error(f"Error fetching proposal status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pending/list")
async def list_pending(db: AsyncSession = Depends(get_db)):
    """
    List all pending proposals (proposed/approved, not yet funded/applied).

    Returns proposals waiting for action:
    - proposed: awaiting human approval
    - approved: approved but not yet funded
    """
    try:
        proposals = await list_pending_proposals()
        return {"proposals": proposals}

    except Exception as e:
        logger.error(f"Error listing pending proposals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """
    Fetch the append-only audit trail of all sweep events.

    Events include: calculated, proposed, approved, funded, applied, cancelled.
    Sorted newest first.

    Args:
        limit: Max events to return (default 50)

    Returns:
        {audit_trail: [{event, proposal_id, detail, created_at}, ...]}
    """
    try:
        logs = await get_sweep_history(limit=limit)
        return {"audit_trail": logs}

    except Exception as e:
        logger.error(f"Error fetching sweep history: {e}")
        raise HTTPException(status_code=500, detail=str(e))
