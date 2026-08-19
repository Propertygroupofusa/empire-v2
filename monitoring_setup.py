"""
Live Trade Monitoring - Tracks deployment status and first trades
"""
import asyncio
import aiohttp
import json
from datetime import datetime
from typing import Optional

DASHBOARD_URL = "https://empire-v2-production.up.railway.app/trading/dashboard"
API_URL = "https://empire-v2-production.up.railway.app/trading/api/pnl-summary"
HEALTH_URL = "https://empire-v2-production.up.railway.app/health"

class TradingMonitor:
    def __init__(self):
        self.last_pnl = 0
        self.last_trade_count = 0
        self.deployment_ready = False

    async def check_health(self) -> bool:
        """Check if deployment is healthy"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HEALTH_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self.deployment_ready = True
                        return True
        except Exception as e:
            print(f"❌ Health check failed: {e}")
        return False

    async def check_pnl(self) -> Optional[dict]:
        """Get current P&L data"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data
        except Exception as e:
            print(f"⚠️  API call failed: {e}")
        return None

    async def monitor_loop(self):
        """Continuous monitoring loop"""
        print("\n" + "="*60)
        print("🚀 LIVE TRADING MONITOR STARTED")
        print("="*60)
        print("\nChecking deployment status...\n")

        check_count = 0
        max_checks = 120  # 2 hours of checks (every minute)

        while check_count < max_checks:
            check_count += 1
            timestamp = datetime.now().strftime("%H:%M:%S")

            # Check health
            if not self.deployment_ready:
                is_healthy = await self.check_health()
                if is_healthy:
                    print(f"✅ [{timestamp}] DEPLOYMENT ACTIVE - Bots are running!")
                else:
                    print(f"⏳ [{timestamp}] Waiting for deployment... ({check_count}/120)")
                    await asyncio.sleep(60)
                    continue

            # Get P&L data
            pnl_data = await self.check_pnl()

            if pnl_data:
                total_pnl = pnl_data.get("total_pnl", 0)
                realized = pnl_data.get("realized_pnl", 0)
                unrealized = pnl_data.get("unrealized_pnl", 0)
                trades = pnl_data.get("trade_count", 0)
                positions = pnl_data.get("open_positions", 0)

                # Check for new trades
                if trades > self.last_trade_count:
                    new_trades = trades - self.last_trade_count
                    print(f"\n🎯 [{timestamp}] NEW TRADES DETECTED!")
                    print(f"   Trades executed: {new_trades}")
                    print(f"   Total trades today: {trades}")
                    self.last_trade_count = trades

                # Display P&L
                emoji = "📈" if total_pnl >= 0 else "📉"
                print(f"\n{emoji} [{timestamp}] LIVE P&L")
                print(f"   Total P&L: ${total_pnl:.2f}")
                print(f"   Realized:   ${realized:.2f}")
                print(f"   Unrealized: ${unrealized:.2f}")
                print(f"   Positions:  {positions} open")
                print(f"   Trades:     {trades} total")

                # Update baseline
                self.last_pnl = total_pnl

            else:
                print(f"⏳ [{timestamp}] Waiting for first trades... ({check_count}/120)")

            print("\n" + "-"*60)
            await asyncio.sleep(60)  # Check every minute

        print("\n✅ Monitoring complete. Trading system is live!")
        print(f"📈 Final P&L: ${self.last_pnl:.2f}")
        print(f"📊 Total Trades: {self.last_trade_count}")
        print("\nAccess your dashboard anytime at:")
        print(f"  🔗 {DASHBOARD_URL}")

async def start_monitoring():
    """Start live monitoring"""
    monitor = TradingMonitor()
    await monitor.monitor_loop()

if __name__ == "__main__":
    print("\n📊 TRADING SYSTEM MONITOR")
    print("This will check your deployment and track first trades")
    print("Run this after adding credentials to Railway and clicking Redeploy\n")

    try:
        asyncio.run(start_monitoring())
    except KeyboardInterrupt:
        print("\n\n⏹️  Monitoring stopped by user")
