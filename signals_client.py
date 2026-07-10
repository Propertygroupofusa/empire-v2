"""
TRADING SIGNALS CLIENT SDK
===========================
Easy integration for trading signal subscribers.

Usage:
    from signals_client import SignalsClient

    client = SignalsClient(api_key="your-api-key", base_url="https://signals.propertygroupusa.com")
    signals = client.get_signals()
    print(f"Profitable days: {signals['consecutive_profitable']}/7")
"""

import requests
from typing import Dict, Any
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals_client")

class SignalsClient:
    """Client for consuming trading signals from the subscription API"""

    def __init__(self, api_key: str, base_url: str = "http://localhost:8001"):
        """
        Initialize signals client

        Args:
            api_key: Your subscription API key
            base_url: Trading signals API endpoint
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}

    def get_signals(self) -> Dict[str, Any]:
        """Get current trading signals"""
        try:
            response = requests.get(
                f"{self.base_url}/signals",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"Failed to fetch signals: {e}")
            raise

    def get_info(self) -> Dict[str, Any]:
        """Get subscription info"""
        try:
            response = requests.get(
                f"{self.base_url}/subscriber/info",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"Failed to fetch subscriber info: {e}")
            raise

    def is_ready_to_trade(self) -> bool:
        """Check if prop_bot has 7+ consecutive profitable days"""
        signals = self.get_signals()
        return signals.get("signals", {}).get("consecutive_profitable", 0) >= 7

    def get_daily_pnl(self) -> float:
        """Get today's P&L from prop_bot"""
        signals = self.get_signals()
        return signals.get("signals", {}).get("daily_pnl", 0.0)

    def monitor_signals(self, callback, interval=60):
        """
        Monitor signals continuously and call callback on changes

        Args:
            callback: Function called with signals dict on each check
            interval: Check interval in seconds
        """
        import time
        last_signals = None

        log.info(f"Monitoring signals every {interval}s...")
        while True:
            try:
                signals = self.get_signals()
                if signals != last_signals:
                    log.info(f"Signal update: {signals}")
                    callback(signals)
                    last_signals = signals
            except Exception as e:
                log.error(f"Monitor error: {e}")

            time.sleep(interval)


# Example usage
if __name__ == "__main__":
    # Example: Check if ready to trade
    client = SignalsClient(api_key="your-api-key-here")

    try:
        info = client.get_info()
        print(f"Connected as: {info['email']}")
        print(f"Subscription status: {info['status']}")

        signals = client.get_signals()
        print(f"\nTrading Status:")
        print(f"  Profitable days: {signals['signals']['consecutive_profitable']}/7")
        print(f"  Daily P&L: ${signals['signals']['daily_pnl']:.2f}")
        print(f"  Status: {signals['signals']['status']}")

        if client.is_ready_to_trade():
            print("\n✅ READY TO TRADE LIVE!")
        else:
            print("\n⏳ Still in evaluation phase...")

    except Exception as e:
        print(f"Error: {e}")
