"""
Alpaca Broker API Funding Router
Endpoints for auto-funding trading accounts from job earnings
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
import os
import logging
from alpaca_broker_integration import (
    AlpacaBrokerClient,
    auto_fund_trading_account,
    bulk_auto_fund_all_workers,
    get_pending_earnings
)

log = logging.getLogger("alpaca_funding")
router = APIRouter()

# Initialize Alpaca Broker client
ALPACA_CLIENT_ID = os.getenv("ALPACA_BROKER_CLIENT_ID", "")
ALPACA_CLIENT_SECRET = os.getenv("ALPACA_BROKER_CLIENT_SECRET", "")
ALPACA_SANDBOX = os.getenv("ALPACA_SANDBOX", "true").lower() == "true"

if ALPACA_CLIENT_ID and ALPACA_CLIENT_SECRET:
    broker_client = AlpacaBrokerClient(ALPACA_CLIENT_ID, ALPACA_CLIENT_SECRET, sandbox=ALPACA_SANDBOX)
    ALPACA_AVAILABLE = True
    log.info(f"✅ Alpaca Broker API configured (sandbox: {ALPACA_SANDBOX})")
else:
    broker_client = None
    ALPACA_AVAILABLE = False
    log.warning("⚠️  Alpaca Broker API credentials not configured")


@router.post("/auto-fund/{worker_email}", summary="Auto-fund trading account from earnings")
async def auto_fund_worker(
    worker_email: str,
    min_transfer_amount: float = 100.0,
    db: AsyncSession = Depends(get_db)
):
    """
    Auto-fund a worker's Alpaca trading account from pending job earnings.

    Flow:
    1. Query Alpaca for account matching worker_email
    2. Check account compliance status (must be ACTIVE)
    3. Get pending earnings from job payments
    4. If >= min_transfer_amount, mark payments as transferred
    5. Log transfer for audit

    Returns transfer status and amount
    """
    if not ALPACA_AVAILABLE:
        raise HTTPException(status_code=503, detail="Alpaca Broker API not configured")

    try:
        result = await auto_fund_trading_account(
            broker_client,
            worker_email,
            min_transfer_amount
        )

        if result["status"] == "failed":
            raise HTTPException(status_code=400, detail=result["message"])

        return result

    except Exception as e:
        log.error(f"❌ Auto-fund failed for {worker_email}: {e}")
        raise HTTPException(status_code=500, detail=f"Auto-fund error: {str(e)}")


@router.post("/bulk-auto-fund", summary="Auto-fund all workers with pending earnings")
async def bulk_auto_fund(
    min_transfer_amount: float = 100.0,
    db: AsyncSession = Depends(get_db)
):
    """
    Auto-fund ALL workers who have pending earnings >= min_transfer_amount.

    Processes:
    - Queries all workers with pending payments
    - For each worker: queries Alpaca account, checks compliance, transfers earnings
    - Returns list of transfer results (success/failed/skipped)

    Use this as a daily batch job to auto-fund all active workers
    """
    if not ALPACA_AVAILABLE:
        raise HTTPException(status_code=503, detail="Alpaca Broker API not configured")

    try:
        results = await bulk_auto_fund_all_workers(broker_client, min_transfer_amount)

        successful = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        skipped = sum(1 for r in results if r["status"] == "skipped")

        return {
            "summary": {
                "total_processed": len(results),
                "successful": successful,
                "failed": failed,
                "skipped": skipped,
            },
            "results": results
        }

    except Exception as e:
        log.error(f"❌ Bulk auto-fund failed: {e}")
        raise HTTPException(status_code=500, detail=f"Bulk auto-fund error: {str(e)}")


@router.get("/compliance-check/{worker_email}", summary="Check account compliance")
async def check_compliance(worker_email: str, db: AsyncSession = Depends(get_db)):
    """
    Check if a worker's Alpaca account is compliant for auto-funding.

    Compliance means:
    - Account exists in Alpaca
    - Account status is ACTIVE (not RESTRICTED, SUSPENDED, etc.)
    - Ready to receive transfers

    Returns account details and compliance status
    """
    if not ALPACA_AVAILABLE:
        raise HTTPException(status_code=503, detail="Alpaca Broker API not configured")

    try:
        # Query Alpaca for account
        accounts = await broker_client.query_accounts(query=worker_email)

        if not accounts:
            return {
                "worker_email": worker_email,
                "found": False,
                "compliant": False,
                "message": f"No Alpaca account found for {worker_email}"
            }

        account = accounts[0]
        account_id = account.get("id")

        # Check compliance
        compliant = await broker_client.check_account_compliance(account_id)

        # Get pending earnings
        from models import Payment, Worker
        from sqlalchemy import select
        result = await db.execute(
            select(Worker).where(Worker.email == worker_email)
        )
        worker = result.scalar_one_or_none()
        pending = 0.0
        if worker:
            pending = await get_pending_earnings(str(worker.id))

        return {
            "worker_email": worker_email,
            "account_id": account_id,
            "account_status": account.get("status"),
            "found": True,
            "compliant": compliant,
            "pending_earnings": pending,
            "message": "Ready for auto-funding" if compliant else f"Account not compliant (status: {account.get('status')})"
        }

    except Exception as e:
        log.error(f"❌ Compliance check failed for {worker_email}: {e}")
        raise HTTPException(status_code=500, detail=f"Compliance check error: {str(e)}")


@router.get("/pending-earnings/{worker_email}", summary="Get pending earnings for worker")
async def get_worker_pending_earnings(worker_email: str, db: AsyncSession = Depends(get_db)):
    """Get total unpaid/pending earnings for a worker"""
    try:
        from models import Worker
        from sqlalchemy import select

        result = await db.execute(
            select(Worker).where(Worker.email == worker_email)
        )
        worker = result.scalar_one_or_none()

        if not worker:
            raise HTTPException(status_code=404, detail=f"Worker {worker_email} not found")

        pending = await get_pending_earnings(str(worker.id))

        return {
            "worker_email": worker_email,
            "pending_earnings": round(pending, 2),
            "ready_for_transfer": pending >= 100.0
        }

    except Exception as e:
        log.error(f"❌ Failed to get pending earnings for {worker_email}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
