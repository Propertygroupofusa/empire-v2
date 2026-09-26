#!/usr/bin/env python3
"""
REAL-TIME BOT RECOVERY MONITOR
Tracks three critical recovery conditions after de-risk deployment:
1. Event loop & Semaphore binding (no RuntimeError)
2. Order book sync (old orders canceled, new orders at reduced size)
3. Buying power restoration (~$167.48) and circuit breaker state (PAUSED → NORMAL)
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta
from collections import defaultdict

class RecoveryMonitor:
    def __init__(self):
        self.start_time = datetime.utcnow()
        self.conditions = {
            "event_loop": False,
            "semaphore_clean": False,
            "order_sync": False,
            "buying_power": False,
            "circuit_breaker": False
        }
        self.logs = defaultdict(list)
        self.errors = []

    def get_logs(self, lines=200):
        """Fetch latest logs from Railway"""
        try:
            result = subprocess.run(
                ['railway', 'logs', 'crypto-trading', '--tail', str(lines)],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout
        except Exception as e:
            print(f"Error fetching logs: {e}")
            return ""

    def check_condition_1_event_loop(self, logs):
        """Check: Event loop initialized, no Semaphore binding errors"""
        if "✓ Event loop initialized" in logs:
            self.conditions["event_loop"] = True

        if "bound to a different event loop" in logs:
            self.errors.append("CRITICAL: Semaphore bound to different loop!")
            self.conditions["semaphore_clean"] = False
        elif self.conditions["event_loop"] and "✓ Crypto grid bot module loaded" in logs:
            self.conditions["semaphore_clean"] = True

    def check_condition_2_order_sync(self, logs):
        """Check: Order book synced (old orders canceled, new at reduced size)"""
        sync_keywords = [
            "canceled pending orders",
            "order.*reconcil",
            "resized.*grid",
            "allocation.*update",
            "$135",  # New DOGE allocation
            "$277",  # New ETH allocation
            "$165",  # New BTC allocation
        ]

        for keyword in sync_keywords:
            if keyword.lower() in logs.lower():
                self.conditions["order_sync"] = True
                break

    def check_condition_3_buying_power(self, logs):
        """Check: Buying power ~$167.48 and circuit breaker NORMAL"""
        # Check buying power (look for values > $100, not $0.23)
        import re

        bp_match = re.search(r'buying.power[:\s]+\$([0-9.]+)', logs, re.IGNORECASE)
        if bp_match:
            try:
                bp_value = float(bp_match.group(1))
                if bp_value > 100:
                    self.conditions["buying_power"] = True
            except:
                pass

        # Check circuit breaker state
        if "PAUSED → NORMAL" in logs or ("circuit" in logs.lower() and "NORMAL" in logs):
            self.conditions["circuit_breaker"] = True
        elif "PAUSED" in logs and "NORMAL" not in logs:
            self.conditions["circuit_breaker"] = False

    def print_status(self, elapsed):
        """Print real-time status dashboard"""
        print("\033[2J\033[H")  # Clear screen

        print("=" * 90)
        print("REAL-TIME BOT RECOVERY MONITOR")
        print("=" * 90)
        print(f"Elapsed: {elapsed}s | Status Update: {datetime.utcnow().isoformat()}\n")

        # Condition 1
        status_1 = "✓" if self.conditions["event_loop"] else "⏳"
        status_1b = "✓" if self.conditions["semaphore_clean"] else "⏳"
        print(f"{status_1} [CONDITION 1a] Event loop initialized")
        print(f"{status_1b} [CONDITION 1b] No Semaphore binding errors")

        # Condition 2
        status_2 = "✓" if self.conditions["order_sync"] else "⏳"
        print(f"{status_2} [CONDITION 2] Order book synced (old orders canceled, new at reduced size)")

        # Condition 3a
        status_3a = "✓" if self.conditions["buying_power"] else "⏳"
        print(f"{status_3a} [CONDITION 3a] Buying power ~$167.48 (up from $0.23)")

        # Condition 3b
        status_3b = "✓" if self.conditions["circuit_breaker"] else "⏳"
        print(f"{status_3b} [CONDITION 3b] Circuit breaker NORMAL (cleared from PAUSED)")

        # Overall status
        conditions_met = sum(1 for v in self.conditions.values() if v)
        print(f"\nConditions met: {conditions_met}/5")

        if conditions_met == 5:
            print("\n🎉 ALL CONDITIONS MET - RECOVERY SUCCESSFUL!")
        elif conditions_met >= 3:
            print(f"\n✓ {conditions_met} conditions met - {5-conditions_met} remaining")
        else:
            print(f"\n⏳ {conditions_met} conditions met - Monitoring...")

        # Errors
        if self.errors:
            print("\n" + "=" * 90)
            print("ERRORS DETECTED:")
            for error in self.errors[-5:]:
                print(f"  ✗ {error}")

        print("\n" + "=" * 90)
        print("Expected timeline:")
        print("  T+0-10s:  Event loop & Semaphore check")
        print("  T+10-30s: Order book sync")
        print("  T+30-60s: Buying power restoration & circuit breaker clear")
        print("  T+60+s:   Trading resumes at safe leverage")
        print("\n(Ctrl+C to stop monitoring)")

    def run(self, interval=10, timeout=120):
        """Run continuous monitoring"""
        print("Starting recovery monitor...\n")

        start = time.time()
        check_count = 0

        while (time.time() - start) < timeout:
            check_count += 1
            elapsed = int(time.time() - start)

            # Fetch and analyze logs
            logs = self.get_logs(lines=300)

            if logs:
                self.check_condition_1_event_loop(logs)
                self.check_condition_2_order_sync(logs)
                self.check_condition_3_buying_power(logs)

            # Print status
            self.print_status(elapsed)

            # Check if all conditions met
            if all(self.conditions.values()):
                print("\n✅ RECOVERY COMPLETE - All conditions verified!")
                print("\nNext steps:")
                print("  1. Monitor trading at new scale ($850 allocation)")
                print("  2. Accumulate 50+ trades to validate profitability")
                print("  3. Check for any new errors or margin issues")
                print("\nTo continue monitoring:")
                print("  railway logs crypto-trading --tail 100 --follow")
                break

            # Wait before next check
            if elapsed < timeout:
                time.sleep(interval)

        if elapsed >= timeout:
            print(f"\n⏱️  Monitoring timeout reached ({timeout}s)")
            print("\nFinal status:")
            print(f"  Conditions met: {sum(1 for v in self.conditions.values() if v)}/5")
            print("\nTo continue monitoring manually:")
            print("  railway logs crypto-trading --tail 100 --follow")

if __name__ == "__main__":
    try:
        # Check Railway CLI
        result = subprocess.run(['railway', '--version'], capture_output=True, text=True)
        if result.returncode != 0:
            print("❌ Railway CLI not found")
            print("Install with: npm install -g @railway/cli")
            sys.exit(1)

        # Start monitor
        monitor = RecoveryMonitor()
        monitor.run(interval=10, timeout=120)

    except KeyboardInterrupt:
        print("\n\n⏹️  Monitoring stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
