"""Network access configuration and fallback mechanisms for broker APIs.

Handles cases where Railway network egress is restricted and direct API calls
fail with 403 errors. Provides retry logic, caching, and alternative paths.
"""

import os
import time
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Network Configuration
NETWORK_CONFIG = {
    "coinbase": {
        "primary_endpoint": "https://api.coinbase.com",
        "fallback_endpoints": [
            "https://api.coinbase.com",
        ],
        "timeout": 10,
        "retry_attempts": 3,
        "retry_backoff": [1, 2, 4],  # exponential: 1s, 2s, 4s
    },
    "alpaca": {
        "primary_endpoint": "https://api.alpaca.markets",
        "fallback_endpoints": [
            "https://api.alpaca.markets",
            "https://api2.alpaca.markets",
        ],
        "timeout": 10,
        "retry_attempts": 3,
        "retry_backoff": [1, 2, 4],
    },
}

# Cache for API responses (to reduce repeated calls during network issues)
API_RESPONSE_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 300  # 5-minute cache


class NetworkAccessError(Exception):
    """Raised when network access to broker APIs fails."""
    pass


class CachedAPIResponse:
    """Wrapper for cached API responses with TTL."""

    def __init__(self, data: Any, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.data = data
        self.created_at = datetime.now(timezone.utc)
        self.ttl_seconds = ttl_seconds

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        age = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return age > self.ttl_seconds

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        return not self.is_expired()


def get_cached_response(key: str) -> Optional[Any]:
    """Retrieve cached API response if still valid."""
    if key in API_RESPONSE_CACHE:
        cached = API_RESPONSE_CACHE[key]
        if cached.is_valid():
            log.info(f"📦 Using cached response for {key} (expires in {cached.ttl_seconds - (datetime.now(timezone.utc) - cached.created_at).total_seconds():.0f}s)")
            return cached.data
        else:
            log.debug(f"⏰ Cache expired for {key}")
            del API_RESPONSE_CACHE[key]
    return None


def cache_response(key: str, data: Any, ttl_seconds: int = CACHE_TTL_SECONDS):
    """Cache API response with TTL."""
    API_RESPONSE_CACHE[key] = CachedAPIResponse(data, ttl_seconds)
    log.debug(f"💾 Cached response for {key} (TTL: {ttl_seconds}s)")


def make_request_with_retry(
    func,
    broker: str = "coinbase",
    cache_key: Optional[str] = None,
    use_cache: bool = True,
) -> Optional[Any]:
    """
    Attempt to call a broker API function with retry logic and caching.

    Args:
        func: Async function to call (must accept no args, return data or raise)
        broker: "coinbase" or "alpaca" (determines retry config)
        cache_key: If provided, check/store cache
        use_cache: Whether to use cached responses

    Returns:
        API response data, or None if all retries fail

    Behavior:
        1. Check cache if enabled and cache_key provided
        2. Try primary endpoint with retries
        3. If all retries fail, return None (bot continues with fallback logic)
        4. Cache successful responses
    """

    # Check cache first
    if use_cache and cache_key:
        cached = get_cached_response(cache_key)
        if cached is not None:
            return cached

    config = NETWORK_CONFIG.get(broker, NETWORK_CONFIG["coinbase"])
    max_retries = config["retry_attempts"]
    backoffs = config["retry_backoff"]

    last_error = None

    for attempt in range(max_retries):
        try:
            log.info(f"🔗 [{broker}] Attempt {attempt + 1}/{max_retries}")
            data = func()  # Call the actual API function

            # Cache successful response
            if cache_key:
                cache_response(cache_key, data, CACHE_TTL_SECONDS)

            log.info(f"✓ [{broker}] API call succeeded")
            return data

        except Exception as e:
            last_error = e
            error_msg = str(e)

            # Check if this is a network egress error
            if "403" in error_msg or "not in allowlist" in error_msg:
                log.warning(f"⚠️  [{broker}] Network access blocked (403): {error_msg}")
                log.warning(f"   Fix: Add '{config['primary_endpoint']}' to Railway egress allowlist")
                log.warning(f"   Or: Check CLAUDE.md for workaround configuration")

                # On 403, don't retry - it won't change mid-session
                return None

            elif "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                log.warning(f"⚠️  [{broker}] Connection error (attempt {attempt + 1}): {error_msg}")

                # Use exponential backoff
                if attempt < max_retries - 1:
                    backoff = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                    log.info(f"   Waiting {backoff}s before retry...")
                    time.sleep(backoff)

            else:
                log.error(f"❌ [{broker}] API error: {error_msg}")
                return None

    # All retries exhausted
    log.error(f"❌ [{broker}] All {max_retries} attempts failed. Last error: {last_error}")
    return None


def is_network_error(error: Exception) -> bool:
    """Determine if error is network-related vs. authentication/logic error."""
    error_str = str(error).lower()
    return any(term in error_str for term in [
        "403", "connection", "timeout", "not in allowlist",
        "network", "unreachable", "refused"
    ])


def get_fallback_data(broker: str, data_type: str) -> Optional[Dict[str, Any]]:
    """
    Get fallback/placeholder data when API is unavailable.

    Args:
        broker: "coinbase" or "alpaca"
        data_type: "balance", "price", "candles", "positions", etc.

    Returns:
        Fallback data dict, or None if no fallback available
    """

    fallbacks = {
        "coinbase": {
            "balance": {
                "currency": "USD",
                "amount": "0.00",
                "source": "FALLBACK (API unavailable)",
            },
            "price": {
                "price": "0.00",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "FALLBACK (API unavailable)",
            },
            "candles": [],  # Empty means "skip analysis"
        },
        "alpaca": {
            "balance": {
                "cash": 0.0,
                "equity": 0.0,
                "source": "FALLBACK (API unavailable)",
            },
            "positions": [],
        },
    }

    broker_fallbacks = fallbacks.get(broker, {})
    return broker_fallbacks.get(data_type)


# Environment-based network configuration
def load_network_env_config():
    """Load network configuration from environment variables."""

    # Example env vars:
    # ALLOWED_EGRESS_HOSTS=api.coinbase.com,api.alpaca.markets
    # NETWORK_RETRY_ATTEMPTS=5
    # NETWORK_CACHE_TTL=300

    allowed_hosts = os.getenv("ALLOWED_EGRESS_HOSTS", "")
    if allowed_hosts:
        log.info(f"✓ Network config from env: ALLOWED_EGRESS_HOSTS={allowed_hosts}")

    retry_attempts = int(os.getenv("NETWORK_RETRY_ATTEMPTS", "3"))
    cache_ttl = int(os.getenv("NETWORK_CACHE_TTL", "300"))

    log.info(f"✓ Network config: retry_attempts={retry_attempts}, cache_ttl={cache_ttl}s")

    return {
        "allowed_hosts": allowed_hosts.split(",") if allowed_hosts else [],
        "retry_attempts": retry_attempts,
        "cache_ttl": cache_ttl,
    }


# Initialize network config on import
NETWORK_ENV_CONFIG = load_network_env_config()
