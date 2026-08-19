"""
Real-time P&L Trading Dashboard
Live profit/loss tracking for Coinbase and Alpaca bots
"""
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import CryptoTradeLog, BotPosition
from datetime import datetime

router = APIRouter(prefix="/trading", tags=["trading"])

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    """Real-time P&L dashboard with live updates"""

    # Get today's trades
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    result = await db.execute(
        select(CryptoTradeLog)
        .where(CryptoTradeLog.executed_at >= today)
        .order_by(CryptoTradeLog.executed_at.desc())
    )
    trades = result.scalars().all()

    # Calculate P&L
    total_realized = sum(float(t.realized_pnl or 0) for t in trades if t.realized_pnl)
    total_unrealized = sum(float(t.unrealized_pnl or 0) for t in trades if t.unrealized_pnl)
    total_pnl = total_realized + total_unrealized

    # Get open positions
    result = await db.execute(
        select(BotPosition)
        .where(BotPosition.closed_at == None)
    )
    open_positions = result.scalars().all()

    # Format trades for dashboard
    trades_html = ""
    for trade in trades[:20]:  # Last 20 trades
        side_color = "🟢" if trade.entry_type == "ENTRY" else "🔴"
        pnl = float(trade.realized_pnl or 0)
        pnl_color = "#2ecc71" if pnl >= 0 else "#e74c3c"

        trades_html += f"""
        <tr>
            <td>{side_color} {trade.symbol}</td>
            <td>{trade.entry_type or 'N/A'}</td>
            <td>${trade.entry_price:.2f}</td>
            <td>{trade.size}</td>
            <td style="color: {pnl_color};">${pnl:.2f}</td>
            <td>{trade.executed_at.strftime('%H:%M:%S') if trade.executed_at else 'N/A'}</td>
        </tr>
        """

    # Format positions for dashboard
    positions_html = ""
    for pos in open_positions[:10]:
        unrealized = float(pos.unrealized_pnl or 0)
        pos_color = "#2ecc71" if unrealized >= 0 else "#e74c3c"

        positions_html += f"""
        <tr>
            <td>{pos.symbol}</td>
            <td>{pos.side}</td>
            <td>${pos.entry_price:.2f}</td>
            <td>{pos.size}</td>
            <td style="color: {pos_color};">${unrealized:.2f}</td>
        </tr>
        """

    pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
    emoji = "📈" if total_pnl >= 0 else "📉"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Trading Dashboard | Empire v2</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .container {{
                max-width: 1400px;
                margin: 0 auto;
            }}
            .header {{
                background: rgba(255, 255, 255, 0.95);
                border-radius: 12px;
                padding: 30px;
                margin-bottom: 30px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            }}
            .header h1 {{
                font-size: 32px;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .pnl-display {{
                font-size: 48px;
                font-weight: bold;
                color: {pnl_color};
                margin: 20px 0;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-top: 20px;
            }}
            .stat-card {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                border-radius: 12px;
                text-align: center;
            }}
            .stat-value {{
                font-size: 28px;
                font-weight: bold;
                margin: 10px 0;
            }}
            .stat-label {{
                font-size: 14px;
                opacity: 0.9;
            }}
            .section {{
                background: rgba(255, 255, 255, 0.95);
                border-radius: 12px;
                padding: 25px;
                margin-bottom: 20px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            }}
            .section h2 {{
                font-size: 24px;
                margin-bottom: 20px;
                color: #333;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 14px;
            }}
            th {{
                background: #f5f5f5;
                padding: 12px;
                text-align: left;
                font-weight: 600;
                color: #333;
                border-bottom: 2px solid #e0e0e0;
            }}
            td {{
                padding: 12px;
                border-bottom: 1px solid #e0e0e0;
            }}
            tr:hover {{
                background: #f9f9f9;
            }}
            .refresh {{
                text-align: center;
                color: #666;
                font-size: 12px;
                margin-top: 20px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>💹 Trading Dashboard</h1>
                <div class="pnl-display">{emoji} ${total_pnl:.2f}</div>

                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-label">Realized P&L</div>
                        <div class="stat-value" style="color: {('#2ecc71' if total_realized >= 0 else '#e74c3c')}">
                            ${total_realized:.2f}
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Unrealized P&L</div>
                        <div class="stat-value" style="color: {('#2ecc71' if total_unrealized >= 0 else '#e74c3c')}">
                            ${total_unrealized:.2f}
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Open Positions</div>
                        <div class="stat-value">{len(open_positions)}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Today's Trades</div>
                        <div class="stat-value">{len(trades)}</div>
                    </div>
                </div>
            </div>

            <div class="section">
                <h2>📊 Open Positions</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Side</th>
                            <th>Entry Price</th>
                            <th>Size</th>
                            <th>Unrealized P&L</th>
                        </tr>
                    </thead>
                    <tbody>
                        {positions_html if positions_html else '<tr><td colspan="5" style="text-align: center; color: #999;">No open positions</td></tr>'}
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h2>📈 Recent Trades (Last 20)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Type</th>
                            <th>Entry Price</th>
                            <th>Size</th>
                            <th>P&L</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody>
                        {trades_html if trades_html else '<tr><td colspan="6" style="text-align: center; color: #999;">No trades today</td></tr>'}
                    </tbody>
                </table>
            </div>

            <div class="refresh">
                🔄 Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Auto-refresh every 30 seconds
            </div>
        </div>

        <script>
            // Auto-refresh every 30 seconds
            setTimeout(() => window.location.reload(), 30000);
        </script>
    </body>
    </html>
    """

    return html

@router.get("/api/pnl-summary")
async def get_pnl_summary(db: AsyncSession = Depends(get_db)):
    """JSON API for P&L data"""

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    result = await db.execute(
        select(CryptoTradeLog)
        .where(CryptoTradeLog.executed_at >= today)
    )
    trades = result.scalars().all()

    total_realized = sum(float(t.realized_pnl or 0) for t in trades if t.realized_pnl)
    total_unrealized = sum(float(t.unrealized_pnl or 0) for t in trades if t.unrealized_pnl)

    result = await db.execute(select(BotPosition).where(BotPosition.closed_at == None))
    open_positions = result.scalars().all()

    return {
        "timestamp": datetime.now().isoformat(),
        "realized_pnl": total_realized,
        "unrealized_pnl": total_unrealized,
        "total_pnl": total_realized + total_unrealized,
        "trade_count": len(trades),
        "open_positions": len(open_positions),
    }
