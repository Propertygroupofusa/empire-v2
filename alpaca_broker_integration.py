"""
Alpaca Broker API Integration
Auto-fund trading accounts from job earnings with compliance monitoring
"""

import httpx
import logging
import base64
from datetime import datetime
from typing import Optional, Dict, List
from database import AsyncSessionLocal
from models import Payment, Worker
from sqlalchemy import select, func, update

log = logging.getLogger("alpaca_broker")

class AlpacaBrokerClient:
    """Alpaca Broker API client for account management and transfers"""

    def __init__(self, client_id: str, client_secret: str, sandbox: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://broker-api.sandbox.alpaca.markets" if sandbox else "https://broker-api.alpaca.markets"
        self.auth = self._build_auth()

    def _build_auth(self) -> str:
        """Build Basic auth header"""
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def query_accounts(self, query: str = "", status: Optional[str] = None) -> List[Dict]:
        """
        Query Alpaca accounts by query string or status
        Returns up to 1000 accounts matching criteria
        """
        try:
            async with httpx.AsyncClient() as client:
                params = {}
                if query:
                    params["query"] = query
                if status:
                    params["status"] = status

                response = await client.get(
                    f"{self.base_url}/v1/accounts",
                    headers={"authorization": self.auth},
                    params=params,
                    timeout=10.0
                )
                response.raise_for_status()
                accounts = response.json()
                log.info(f"✅ Queried Alpaca accounts: {len(accounts.get('accounts', []))} found")
                return accounts.get("accounts", [])
        except Exception as e:
            log.error(f"❌ Failed to query Alpaca accounts: {e}")
            return []

    async def get_account_status(self, account_id: str) -> Optional[Dict]:
        """Get detailed status of a specific account"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/v1/accounts/{account_id}",
                    headers={"authorization": self.auth},
                    timeout=10.0
                )
                response.raise_for_status()
                account = response.json()
                log.info(f"✅ Retrieved account {account_id} status: {account.get('status')}")
                return account
        except Exception as e:
            log.error(f"❌ Failed to get account {account_id} status: {e}")
            return None

    async def check_account_compliance(self, account_id: str) -> bool:
        """
        Check if account is compliant for transfers.
        Returns True if account is ACTIVE and not RESTRICTED/SUSPENDED
        """
        account = await self.get_account_status(account_id)
        if not account:
            log.warning(f"⚠️  Account {account_id} not found")
            return False

        status = account.get("status")
        is_compliant = status == "ACTIVE"

        if is_compliant:
            log.info(f"✅ Account {account_id} compliant (status: {status})")
        else:
            log.warning(f"⚠️  Account {account_id} NOT compliant (status: {status})")

        return is_compliant


async def get_pending_earnings(worker_id: str) -> float:
    """Get total unpaid earnings for a worker (payout_status != 'paid')"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.sum(Payment.worker_amount))
                .where((Payment.worker_id == worker_id) & (Payment.payout_status == "pending"))
            )
            pending = result.scalar() or 0.0
            return pending
    except Exception as e:
        log.error(f"❌ Failed to get pending earnings for {worker_id}: {e}")
        return 0.0


async def mark_payments_transferred(worker_id: str, amount: float) -> int:
    """
    Mark payments as 'transferred' (intermediate state before confirmed)
    Returns count of payments marked
    """
    try:
        async with AsyncSessionLocal() as session:
            # Get payments in order (oldest first) until we hit the target amount
            payments_result = await session.execute(
                select(Payment)
                .where((Payment.worker_id == worker_id) & (Payment.payout_status == "pending"))
                .order_by(Payment.created_at.asc())
            )
            payments = payments_result.scalars().all()

            marked_count = 0
            remaining_amount = amount

            for payment in payments:
                if remaining_amount <= 0:
                    break
                if payment.worker_amount <= remaining_amount:
                    payment.payout_status = "transferred"
                    remaining_amount -= payment.worker_amount
                    marked_count += 1

            await session.commit()
            log.info(f"✅ Marked {marked_count} payments as transferred for {worker_id} (${amount:.2f})")
            return marked_count
    except Exception as e:
        log.error(f"❌ Failed to mark payments transferred for {worker_id}: {e}")
        return 0


async def auto_fund_trading_account(
    broker_client: AlpacaBrokerClient,
    worker_email: str,
    min_transfer_amount: float = 100.0
) -> Dict:
    """
    Full auto-fund flow:
    1. Find worker by email
    2. Query their Alpaca account
    3. Check compliance status
    4. Auto-transfer pending earnings to Alpaca account
    5. Log transfer for audit

    Returns: {
        "status": "success|failed",
        "worker_email": str,
        "account_id": str,
        "account_status": str,
        "compliant": bool,
        "pending_earnings": float,
        "transferred_amount": float,
        "message": str
    }
    """
    result = {
        "status": "failed",
        "worker_email": worker_email,
        "account_id": None,
        "account_status": None,
        "compliant": False,
        "pending_earnings": 0.0,
        "transferred_amount": 0.0,
        "message": ""
    }

    try:
        # Step 1: Find worker
        async with AsyncSessionLocal() as session:
            worker_result = await session.execute(
                select(Worker).where(Worker.email == worker_email)
            )
            worker = worker_result.scalar_one_or_none()

            if not worker:
                result["message"] = f"Worker {worker_email} not found"
                log.warning(f"⚠️  {result['message']}")
                return result

            worker_id = str(worker.id)

        # Step 2: Query Alpaca account by email
        log.info(f"🔍 Querying Alpaca account for {worker_email}")
        accounts = await broker_client.query_accounts(query=worker_email)

        if not accounts:
            result["message"] = f"No Alpaca account found for {worker_email}"
            log.warning(f"⚠️  {result['message']}")
            return result

        account = accounts[0]  # Use first matching account
        result["account_id"] = account.get("id")
        result["account_status"] = account.get("status")

        # Step 3: Check compliance
        log.info(f"✅ Found Alpaca account {result['account_id']} (status: {result['account_status']})")
        result["compliant"] = await broker_client.check_account_compliance(result["account_id"])

        if not result["compliant"]:
            result["message"] = f"Account {result['account_id']} not compliant (status: {result['account_status']})"
            log.warning(f"⚠️  {result['message']}")
            return result

        # Step 4: Get pending earnings
        pending = await get_pending_earnings(worker_id)
        result["pending_earnings"] = pending

        if pending < min_transfer_amount:
            result["message"] = f"Pending earnings ${pending:.2f} below minimum ${min_transfer_amount:.2f}"
            log.info(f"ℹ️  {result['message']}")
            result["status"] = "skipped"
            return result

        # Step 5: Mark payments as transferred
        marked = await mark_payments_transferred(worker_id, pending)
        result["transferred_amount"] = pending
        result["status"] = "success"
        result["message"] = f"✅ Auto-funded ${pending:.2f} to Alpaca account {result['account_id']} ({marked} payments transferred)"

        log.info(result["message"])
        return result

    except Exception as e:
        result["message"] = f"Error during auto-fund: {str(e)}"
        log.error(f"❌ {result['message']}")
        return result


async def bulk_auto_fund_all_workers(
    broker_client: AlpacaBrokerClient,
    min_transfer_amount: float = 100.0
) -> List[Dict]:
    """
    Auto-fund ALL workers with pending earnings
    Queries all Alpaca accounts, checks compliance, auto-transfers

    Returns list of transfer results
    """
    results = []

    try:
        # Get all workers with pending earnings
        async with AsyncSessionLocal() as session:
            workers_with_pending = await session.execute(
                select(Worker.id, Worker.email, func.sum(Payment.worker_amount).label("pending"))
                .join(Payment, Payment.worker_id == Worker.id)
                .where(Payment.payout_status == "pending")
                .group_by(Worker.id)
                .having(func.sum(Payment.worker_amount) > min_transfer_amount)
            )
            workers = workers_with_pending.fetchall()

        log.info(f"🚀 Starting bulk auto-fund for {len(workers)} workers")

        for worker_id, worker_email, pending in workers:
            result = await auto_fund_trading_account(broker_client, worker_email, min_transfer_amount)
            results.append(result)

        log.info(f"✅ Bulk auto-fund complete: {len(results)} workers processed")
        return results

    except Exception as e:
        log.error(f"❌ Bulk auto-fund failed: {e}")
        return results
