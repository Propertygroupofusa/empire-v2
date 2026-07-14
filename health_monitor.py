"""
Automated Health Monitor & Self-Healing System
Continuously checks for errors and automatically fixes them
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Any
import os

log = logging.getLogger("health_monitor")

class HealthMonitor:
    def __init__(self):
        self.is_running = False
        self.last_check = None
        self.error_history: List[Dict] = []
        self.fixed_issues: List[Dict] = []
        self.check_interval = 30  # Check every 30 seconds

    async def start(self):
        """Start continuous monitoring"""
        self.is_running = True
        log.info("Health Monitor starting - will check every 30 seconds forever")
        asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self):
        """Main monitoring loop that runs forever"""
        while self.is_running:
            try:
                await self.run_health_check()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                log.error(f"Monitor loop error: {e}")
                await asyncio.sleep(5)

    async def run_health_check(self):
        """Run comprehensive health checks"""
        self.last_check = datetime.now()
        errors_found = []

        try:
            # Check 1: Database Connection
            db_status = await self._check_database()
            if not db_status["ok"]:
                errors_found.append({
                    "type": "database",
                    "error": db_status.get("error", "Unknown error"),
                    "severity": "critical",
                    "timestamp": datetime.now().isoformat()
                })
                await self._fix_database()

            # Check 2: Required Files
            files_status = await self._check_required_files()
            for file_path, status in files_status.items():
                if not status["ok"]:
                    errors_found.append({
                        "type": "file",
                        "file": file_path,
                        "error": status.get("error", "File missing"),
                        "severity": "medium",
                        "timestamp": datetime.now().isoformat()
                    })

            # Check 3: Routers
            routers_status = await self._check_routers()
            for router, status in routers_status.items():
                if not status["ok"]:
                    errors_found.append({
                        "type": "router",
                        "router": router,
                        "error": status.get("error", "Router failed"),
                        "severity": "high",
                        "timestamp": datetime.now().isoformat()
                    })

        except Exception as e:
            errors_found.append({
                "type": "monitor",
                "error": str(e),
                "severity": "critical",
                "timestamp": datetime.now().isoformat()
            })

        if errors_found:
            self.error_history.extend(errors_found)
            for error in errors_found:
                log.warning(f"⚠ {error['type'].upper()}: {error['error']}")
        else:
            log.info("✓ Health check passed - all systems OK")

        return errors_found

    async def _check_database(self) -> Dict[str, Any]:
        """Check if database is accessible"""
        try:
            from database import engine
            async with engine.begin() as conn:
                await conn.execute("SELECT 1")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _fix_database(self):
        """Auto-fix database issues"""
        try:
            log.info("→ Attempting database reconnection...")
            from database import init_db
            await init_db()
            self.fixed_issues.append({
                "issue": "database",
                "fixed_at": datetime.now().isoformat(),
                "status": "success"
            })
            log.info("✓ Database reconnected successfully")
        except Exception as e:
            log.error(f"✗ Failed to fix database: {e}")

    async def _check_required_files(self) -> Dict[str, Dict[str, Any]]:
        """Check if required files exist"""
        required_files = [
            "main.py",
            "database.py",
            "social_media_dashboard.html",
            "routers/social_dashboard.py",
            "routers/__init__.py",
            "requirements.txt",
        ]

        results = {}
        app_dir = os.path.dirname(__file__)
        for file_path in required_files:
            full_path = os.path.join(app_dir, file_path)
            exists = os.path.exists(full_path)
            results[file_path] = {
                "ok": exists,
                "error": "File not found" if not exists else None
            }

        return results

    async def _check_routers(self) -> Dict[str, Dict[str, Any]]:
        """Check if all routers can be imported"""
        routers = [
            'workers', 'clients', 'jobs', 'bookings', 'payments',
            'admin', 'whitelabel', 'auth', 'partners', 'labeling',
            'revenue_automation', 'social_dashboard'
        ]

        results = {}
        for router_name in routers:
            try:
                __import__(f'routers.{router_name}', fromlist=[router_name])
                results[router_name] = {"ok": True}
            except Exception as e:
                results[router_name] = {
                    "ok": False,
                    "error": str(e)
                }

        return results

    def get_status(self) -> Dict[str, Any]:
        """Get current monitor status"""
        return {
            "monitoring": self.is_running,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "uptime": self._get_uptime(),
            "errors_detected": len(self.error_history),
            "issues_auto_fixed": len(self.fixed_issues),
            "recent_errors": self.error_history[-5:] if self.error_history else [],
            "recent_fixes": self.fixed_issues[-5:] if self.fixed_issues else [],
        }

    def get_error_history(self, limit: int = 50) -> List[Dict]:
        """Get error history"""
        return self.error_history[-limit:]

    def get_fixed_issues(self, limit: int = 50) -> List[Dict]:
        """Get list of automatically fixed issues"""
        return self.fixed_issues[-limit:]

    def _get_uptime(self) -> str:
        """Get uptime since monitor started"""
        if not self.last_check:
            return "Not started"
        return f"{(datetime.now() - self.last_check).seconds}s ago"


monitor = HealthMonitor()

async def start_health_monitor():
    """Initialize and start the health monitor"""
    await monitor.start()
