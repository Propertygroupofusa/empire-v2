"""Bot earnings and payout dashboard"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from models import Payment, Worker, Job
from datetime import datetime, timedelta
# bcrypt's own API, not passlib's CryptContext. passlib is not in
# requirements.txt, so this module-level import raised
# ModuleNotFoundError and the whole Bot Dashboard router failed to load.
# Adding passlib would not fix it either: its bcrypt backend reads
# bcrypt.__about__.__version__, removed in bcrypt 4.x, and the repo pins
# bcrypt==5.0.0. See worker_auth.py, which documented this already.
from worker_auth import hash_password

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.post("/initialize-bots")
async def initialize_bots(db: AsyncSession = Depends(get_db)):
    """Initialize bot workers if they don't exist"""
    try:
        # Check if bots already exist
        result = await db.execute(
            select(Worker).where(Worker.email.like("%bot%pgusa.local"))
        )
        existing_bots = result.scalars().all()

        if existing_bots:
            return {"status": "ok", "message": f"{len(existing_bots)} bots already exist"}

        # Create initial bot fleet (2 bots to start)
        created = []
        for i in range(1, 3):
            bot_email = f"bot{i if i > 1 else ''}@pgusa.local"
            bot = Worker(
                email=bot_email,
                name=f"Job Bot {i}",
                status="active",
                password_hash=hash_password("auto_bot_password_123"),
            )
            db.add(bot)
            created.append(bot_email)

        await db.commit()
        return {
            "status": "ok",
            "message": f"Created {len(created)} bots",
            "bots": created
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"Failed to initialize bots: {e}")


@router.get("/bot-earnings")
async def bot_earnings_data(db: AsyncSession = Depends(get_db)):
    """Get comprehensive bot earnings data for dashboard"""
    try:
        # Get all bot workers
        result = await db.execute(
            select(Worker).where(Worker.email.like("%bot%pgusa.local"))
        )
        bot_workers = result.scalars().all()

        # Aggregate stats
        all_payments = []
        bot_stats = []

        for bot in bot_workers:
            payments_result = await db.execute(
                select(Payment).where(Payment.worker_id == str(bot.id))
            )
            payments = payments_result.scalars().all()
            all_payments.extend(payments)

            earnings = sum(p.worker_amount for p in payments)
            pending = sum(
                p.worker_amount for p in payments if p.payout_status == "pending"
            )
            paid = sum(
                p.worker_amount for p in payments if p.payout_status == "paid"
            )

            bot_stats.append(
                {
                    "worker": bot.email,
                    "total_earned": earnings,
                    "pending_payout": pending,
                    "paid_out": paid,
                    "jobs_completed": len(payments),
                    "status": bot.status,
                }
            )

        # Overall stats
        total_earned = sum(p.worker_amount for p in all_payments)
        total_paid = sum(
            p.worker_amount for p in all_payments if p.payout_status == "paid"
        )
        total_pending = sum(
            p.worker_amount for p in all_payments if p.payout_status == "pending"
        )

        # Recent payments (last 10)
        recent_payments = sorted(all_payments, key=lambda p: p.created_at, reverse=True)[
            :10
        ]

        # Jobs completed today
        today = datetime.utcnow().date()
        today_payments = [
            p
            for p in all_payments
            if p.created_at and p.created_at.date() == today
        ]

        return {
            "summary": {
                "total_earned": total_earned,
                "paid_out": total_paid,
                "pending_payout": total_pending,
                "total_jobs": len(all_payments),
                "jobs_today": len(today_payments),
                "active_bots": len(bot_workers),
            },
            "bot_stats": bot_stats,
            "recent_payments": [
                {
                    "id": p.id,
                    "worker": next(
                        (b.email for b in bot_workers if str(b.id) == p.worker_id),
                        "Unknown",
                    ),
                    "amount": p.worker_amount,
                    "status": p.payout_status,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in recent_payments
            ],
        }

    except Exception as e:
        return {"error": str(e), "summary": {}}


@router.get("/html")
async def dashboard_html(db: AsyncSession = Depends(get_db)):
    """Serve interactive HTML dashboard"""
    from fastapi.responses import HTMLResponse

    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Earnings Dashboard</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                color: #333;
                padding: 20px;
                min-height: 100vh;
            }
            .container {
                max-width: 1400px;
                margin: 0 auto;
            }
            .header {
                text-align: center;
                color: white;
                margin-bottom: 40px;
            }
            .header h1 {
                font-size: 2.5em;
                margin-bottom: 10px;
            }
            .stats-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 40px;
            }
            .stat-card {
                background: white;
                border-radius: 10px;
                padding: 25px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                text-align: center;
            }
            .stat-card h3 {
                color: #666;
                font-size: 0.9em;
                text-transform: uppercase;
                margin-bottom: 10px;
                letter-spacing: 1px;
            }
            .stat-card .value {
                font-size: 2.5em;
                font-weight: bold;
                color: #2a5298;
                margin: 10px 0;
            }
            .stat-card .subtext {
                color: #999;
                font-size: 0.85em;
            }
            .bot-workers {
                background: white;
                border-radius: 10px;
                padding: 30px;
                margin-bottom: 40px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }
            .bot-workers h2 {
                margin-bottom: 20px;
                color: #2a5298;
            }
            .bot-item {
                padding: 15px;
                border: 2px solid #e0e0e0;
                border-radius: 8px;
                margin-bottom: 15px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .bot-item.active {
                border-color: #4CAF50;
                background: #f0f8f4;
            }
            .bot-info h4 {
                margin-bottom: 5px;
                color: #333;
            }
            .bot-info p {
                font-size: 0.85em;
                color: #666;
            }
            .bot-stats {
                text-align: right;
            }
            .bot-stats .earning {
                font-size: 1.5em;
                font-weight: bold;
                color: #4CAF50;
                margin-bottom: 5px;
            }
            .badge {
                display: inline-block;
                padding: 4px 12px;
                border-radius: 20px;
                font-size: 0.75em;
                font-weight: bold;
            }
            .badge.pending {
                background: #FFF3CD;
                color: #856404;
            }
            .badge.paid {
                background: #D4EDDA;
                color: #155724;
            }
            .recent-payments {
                background: white;
                border-radius: 10px;
                padding: 30px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }
            .recent-payments h2 {
                margin-bottom: 20px;
                color: #2a5298;
            }
            .payment-row {
                display: grid;
                grid-template-columns: 30% 20% 20% 15% 15%;
                padding: 12px 0;
                border-bottom: 1px solid #e0e0e0;
                align-items: center;
            }
            .payment-row:last-child {
                border-bottom: none;
            }
            .payment-row.header {
                font-weight: bold;
                background: #f5f5f5;
                border-radius: 5px;
                padding: 15px 0;
            }
            .payment-amount {
                font-weight: bold;
                color: #2a5298;
                font-size: 1.1em;
            }
            .refresh-btn {
                background: #2a5298;
                color: white;
                border: none;
                padding: 12px 30px;
                border-radius: 5px;
                cursor: pointer;
                font-size: 1em;
                margin-bottom: 20px;
            }
            .refresh-btn:hover {
                background: #1e3c72;
            }
            .loading {
                text-align: center;
                padding: 40px;
                color: white;
            }
            .updated-at {
                text-align: center;
                color: white;
                font-size: 0.9em;
                margin-top: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🤖 Bot Earnings Dashboard</h1>
                <p>Real-time bot worker earnings and payout tracking</p>
            </div>

            <button class="refresh-btn" onclick="loadDashboard()">🔄 Refresh Data</button>

            <div class="stats-grid" id="stats">
                <div class="loading">Loading dashboard data...</div>
            </div>

            <div class="bot-workers" id="bots">
                <h2>🤖 Bot Workers</h2>
            </div>

            <div class="recent-payments" id="recent">
                <h2>💰 Recent Payments</h2>
            </div>

            <div class="updated-at" id="updated"></div>
        </div>

        <script>
            async function loadDashboard() {
                try {
                    const response = await fetch('/dashboard/bot-earnings');
                    const data = await response.json();

                    // Update summary stats
                    const stats = data.summary;
                    document.getElementById('stats').innerHTML = `
                        <div class="stat-card">
                            <h3>💵 Total Earned</h3>
                            <div class="value">$${stats.total_earned.toFixed(2)}</div>
                            <div class="subtext">All time earnings</div>
                        </div>
                        <div class="stat-card">
                            <h3>✅ Paid Out</h3>
                            <div class="value">$${stats.paid_out.toFixed(2)}</div>
                            <div class="subtext">Completed payouts</div>
                        </div>
                        <div class="stat-card">
                            <h3>⏳ Pending</h3>
                            <div class="value">$${stats.pending_payout.toFixed(2)}</div>
                            <div class="subtext">Awaiting payout</div>
                        </div>
                        <div class="stat-card">
                            <h3>📊 Jobs Completed</h3>
                            <div class="value">${stats.total_jobs}</div>
                            <div class="subtext">${stats.jobs_today} today</div>
                        </div>
                        <div class="stat-card">
                            <h3>🤖 Active Bots</h3>
                            <div class="value">${stats.active_bots}</div>
                            <div class="subtext">Workers online</div>
                        </div>
                    `;

                    // Update bot workers
                    const botsHtml = data.bot_stats.map(bot => `
                        <div class="bot-item ${bot.status === 'active' ? 'active' : ''}">
                            <div class="bot-info">
                                <h4>🤖 ${bot.worker}</h4>
                                <p>${bot.jobs_completed} jobs completed</p>
                            </div>
                            <div class="bot-stats">
                                <div class="earning">$${bot.total_earned.toFixed(2)}</div>
                                ${bot.pending_payout > 0 ? `<span class="badge pending">Pending: $${bot.pending_payout.toFixed(2)}</span>` : ''}
                                ${bot.paid_out > 0 ? `<span class="badge paid">Paid: $${bot.paid_out.toFixed(2)}</span>` : ''}
                            </div>
                        </div>
                    `).join('');
                    document.getElementById('bots').innerHTML = '<h2>🤖 Bot Workers</h2>' + botsHtml;

                    // Update recent payments
                    const paymentsHtml = `
                        <div class="payment-row header">
                            <div>Worker</div>
                            <div>Amount</div>
                            <div>Status</div>
                            <div>Date</div>
                            <div></div>
                        </div>
                        ${data.recent_payments.map(p => `
                            <div class="payment-row">
                                <div>${p.worker}</div>
                                <div class="payment-amount">$${p.amount.toFixed(2)}</div>
                                <div><span class="badge ${p.status === 'paid' ? 'paid' : 'pending'}">${p.status}</span></div>
                                <div>${new Date(p.created_at).toLocaleDateString()}</div>
                                <div></div>
                            </div>
                        `).join('')}
                    `;
                    document.getElementById('recent').innerHTML = '<h2>💰 Recent Payments</h2>' + paymentsHtml;

                    document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
                } catch (error) {
                    console.error('Error loading dashboard:', error);
                    document.getElementById('stats').innerHTML = '<div class="loading">Error loading data</div>';
                }
            }

            // Load on page load and refresh every 10 seconds
            loadDashboard();
            setInterval(loadDashboard, 10000);
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)
