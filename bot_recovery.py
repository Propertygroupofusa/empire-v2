"""
BOT AUTO-RECOVERY & CIRCUIT BREAKER SYSTEM
============================================================
Detects and auto-fixes common bot failure modes:
- Loop detection: catches infinite cycles (e.g. $1000.15 profit-taking)
- Circuit breakers: enforces position-level and daily max loss
- Self-healing: exponential backoff retry on API failures
- Silent failure alerts: catches balance read failures, API unreachability
"""

import logging
import time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Tuple

log = logging.getLogger("bot_recovery")


class LoopDetector:
    """Detects when a bot repeatedly takes the same action (sign of a stuck loop)."""

    def __init__(self, max_consecutive: int = 3, window_seconds: int = 120):
        """
        Args:
            max_consecutive: flag as loop after N identical actions
            window_seconds: time window to check for repetition
        """
        self.max_consecutive = max_consecutive
        self.window_seconds = window_seconds
        self.action_history = defaultdict(list)  # {bot_id: [(action, timestamp), ...]}
        self.loop_detected = defaultdict(bool)   # {bot_id: True/False}
        self.loop_count = defaultdict(int)       # {bot_id: count}

    def record_action(self, bot_id: str, action: str) -> Tuple[bool, str]:
        """
        Record an action and detect if it's looping.

        Returns:
            (is_loop: bool, status: str)
        """
        now = time.time()

        # Prune old entries outside window
        self.action_history[bot_id] = [
            (act, ts) for act, ts in self.action_history[bot_id]
            if (now - ts) < self.window_seconds
        ]

        # Append new action
        self.action_history[bot_id].append((action, now))

        # Check if last N actions are identical
        if len(self.action_history[bot_id]) >= self.max_consecutive:
            recent = [act for act, ts in self.action_history[bot_id][-self.max_consecutive:]]
            if len(set(recent)) == 1:  # All same action
                if not self.loop_detected[bot_id]:
                    self.loop_detected[bot_id] = True
                    self.loop_count[bot_id] = 1
                    log.warning(f"🔄 LOOP DETECTED ({bot_id}): {action} repeated {self.max_consecutive}x in {self.window_seconds}s")
                    return True, f"Loop detected: {action}"
                else:
                    self.loop_count[bot_id] += 1
                    if self.loop_count[bot_id] % 5 == 0:
                        log.warning(f"🔄 LOOP PERSISTS ({bot_id}): {self.loop_count[bot_id]} cycles of {action}")
                    return True, f"Loop persists: {action} (cycle #{self.loop_count[bot_id]})"
            else:
                # Actions changed, loop broken
                if self.loop_detected[bot_id]:
                    log.info(f"✅ LOOP BROKEN ({bot_id}): action changed from {recent[-2]} to {action}")
                self.loop_detected[bot_id] = False
                self.loop_count[bot_id] = 0
                return False, f"No loop: {action}"

        return False, f"Recording: {action}"

    def is_looping(self, bot_id: str) -> bool:
        """Quick check if a bot is currently in loop state."""
        return self.loop_detected.get(bot_id, False)

    def reset(self, bot_id: str):
        """Manual reset if recovery is applied."""
        self.action_history[bot_id] = []
        self.loop_detected[bot_id] = False
        self.loop_count[bot_id] = 0
        log.info(f"🔁 Loop detector reset for {bot_id}")


class CircuitBreaker:
    """Enforces position-level and daily max loss to prevent catastrophic bleeding."""

    def __init__(self):
        self.daily_loss_dollars = defaultdict(float)  # {bot_id: loss}
        self.daily_reset_time = defaultdict(lambda: datetime.utcnow())  # {bot_id: timestamp}
        self.position_loss_limit_dollars = 50.0  # Max $50 loss per single position
        self.daily_loss_limit_dollars = 100.0  # Max $100 loss per day

    def set_daily_limit(self, bot_id: str, limit_dollars: float):
        """Configure daily loss limit per bot."""
        self.daily_loss_limit_dollars = limit_dollars
        log.info(f"Circuit breaker: {bot_id} daily limit set to ${limit_dollars}")

    def set_position_limit(self, bot_id: str, limit_dollars: float):
        """Configure per-position loss limit."""
        self.position_loss_limit_dollars = limit_dollars
        log.info(f"Circuit breaker: {bot_id} position limit set to ${limit_dollars}")

    def should_close_position(self, bot_id: str, symbol: str, loss_dollars: float) -> Tuple[bool, str]:
        """
        Check if a single position exceeded loss limit.

        Returns:
            (should_close: bool, reason: str)
        """
        if loss_dollars < 0 and abs(loss_dollars) >= self.position_loss_limit_dollars:
            reason = f"Position {symbol} loss ${loss_dollars:.2f} >= limit ${self.position_loss_limit_dollars}"
            log.warning(f"⚠️  CIRCUIT BREAKER ({bot_id}): {reason}")
            return True, reason
        return False, f"Position {symbol} OK (loss ${loss_dollars:.2f})"

    def check_daily_limit(self, bot_id: str, current_loss: float) -> Tuple[bool, str]:
        """
        Check if daily loss exceeded. Resets every 24h UTC.

        Returns:
            (should_pause: bool, reason: str)
        """
        now = datetime.utcnow()
        last_reset = self.daily_reset_time[bot_id]

        # Reset daily loss if 24h passed
        if (now - last_reset).total_seconds() > 86400:
            self.daily_loss_dollars[bot_id] = 0
            self.daily_reset_time[bot_id] = now
            log.info(f"🔄 Daily loss reset for {bot_id}")

        # Track current session loss
        self.daily_loss_dollars[bot_id] = current_loss

        # Check limit
        if current_loss < 0 and abs(current_loss) >= self.daily_loss_limit_dollars:
            reason = f"Daily loss ${abs(current_loss):.2f} >= limit ${self.daily_loss_limit_dollars}"
            log.error(f"🛑 DAILY CIRCUIT BREAKER ({bot_id}): {reason}")
            return True, reason

        return False, f"Daily loss ${abs(current_loss):.2f} OK (limit ${self.daily_loss_limit_dollars})"


class APIRetryStrategy:
    """Exponential backoff retry on API failures."""

    def __init__(self, max_retries: int = 4, base_delay: float = 1.0):
        """
        Args:
            max_retries: max attempts (1s → 2s → 4s → 8s)
            base_delay: starting delay in seconds
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.failure_count = defaultdict(int)  # {endpoint: count}
        self.last_failure = defaultdict(float)  # {endpoint: timestamp}

    def should_retry(self, endpoint: str, error_code: int = None) -> Tuple[bool, float]:
        """
        Decide if we should retry, and how long to wait.

        Returns:
            (should_retry: bool, delay_seconds: float)
        """
        count = self.failure_count[endpoint]

        if count >= self.max_retries:
            log.error(f"❌ {endpoint}: max retries ({self.max_retries}) exceeded")
            return False, 0

        delay = self.base_delay * (2 ** count)  # Exponential backoff
        self.failure_count[endpoint] += 1
        self.last_failure[endpoint] = time.time()

        log.warning(f"🔄 {endpoint}: retry attempt {count + 1}/{self.max_retries} after {delay}s (error: {error_code})")
        return True, delay

    def reset(self, endpoint: str):
        """Clear failure count after successful call."""
        if self.failure_count[endpoint] > 0:
            log.info(f"✅ {endpoint}: connection recovered after {self.failure_count[endpoint]} attempt(s)")
        self.failure_count[endpoint] = 0
        self.last_failure[endpoint] = 0


class SilentFailureAlert:
    """Catches and logs silent failures (balance read = 0, API unavailable)."""

    def __init__(self):
        self.alert_history = defaultdict(list)  # {alert_type: [timestamp, ...]}
        self.alert_threshold = 3  # Alert after 3 consecutive failures

    def record_failure(self, alert_type: str, details: str) -> bool:
        """
        Record a failure. Return True if threshold reached (time to pause bot).

        Args:
            alert_type: "balance_zero", "api_unreachable", "price_stale", etc.
            details: error details for logging
        """
        now = time.time()

        # Prune old alerts (>5 min old)
        self.alert_history[alert_type] = [
            ts for ts in self.alert_history[alert_type]
            if (now - ts) < 300
        ]

        self.alert_history[alert_type].append(now)
        count = len(self.alert_history[alert_type])

        log.warning(f"⚠️  {alert_type}: {details} (occurrence #{count})")

        if count >= self.alert_threshold:
            log.error(f"🛑 {alert_type}: threshold reached ({count} failures in 5min) — PAUSING BOT")
            return True  # Signal to pause

        return False

    def is_healthy(self, alert_type: str) -> bool:
        """Check if an alert type is healthy (no recent failures)."""
        now = time.time()
        recent = [ts for ts in self.alert_history[alert_type] if (now - ts) < 60]
        return len(recent) < 2


# Global instances
loop_detector = LoopDetector(max_consecutive=3, window_seconds=120)
circuit_breaker = CircuitBreaker()
api_retry = APIRetryStrategy(max_retries=4, base_delay=1.0)
failure_alert = SilentFailureAlert()


def get_recovery_status(bot_id: str) -> Dict:
    """Get complete recovery status for a bot."""
    return {
        "is_looping": loop_detector.is_looping(bot_id),
        "loop_count": loop_detector.loop_count.get(bot_id, 0),
        "daily_loss": circuit_breaker.daily_loss_dollars.get(bot_id, 0),
        "api_failures": dict(api_retry.failure_count),
        "silent_failures": {k: len(v) for k, v in failure_alert.alert_history.items()},
    }
