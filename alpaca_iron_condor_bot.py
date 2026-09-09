#!/usr/bin/env python3
"""
Automated Iron Condor Bot for Alpaca Paper Trading
Implements passive 30-45 DTE spreads with defined risk and profit targets
Entry: 10:00 AM ET | Exit check: 2:00 PM ET
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest,
    QueryOrderStatus
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =====================================================================
# IRON CONDOR RULES (LOCKED IN)
# =====================================================================

ENTRY_RULES = {
    "underlyings": ["SPY", "QQQ", "IWM"],  # Preferred order
    "dte_min": 30,
    "dte_max": 45,
    "short_delta_min": 0.20,
    "short_delta_max": 0.30,
    "spread_width": 5,  # $5 between short and long
    "target_credit_min": 25,
    "target_credit_max": 50,
    "limit_order_discount": 0.05,  # Place limit $0.05 below target
    "position_size": 1,  # 1 contract (1 spread = 4 legs)
}

EXIT_RULES = {
    "profit_target_pct": 0.50,  # Close at 50% of max profit
    "stop_loss_pct": 1.00,  # Hard stop at 100% loss
    "days_to_expiry_exit": 5,  # Close on last 5 days
}

MARKET_HOURS = {
    "entry_window_start": "10:00",  # 10:00 AM ET
    "entry_window_end": "12:00",
    "exit_check_time": "14:00",  # 2:00 PM ET
}


class SpreadType(Enum):
    CALL = "call"
    PUT = "put"
    IRON_CONDOR = "iron_condor"


class TradeStatus(Enum):
    PENDING_ENTRY = "pending_entry"
    ENTRY_PLACED = "entry_placed"
    ACTIVE = "active"
    PROFITABLE = "profitable"
    STOPPED_OUT = "stopped_out"
    CLOSED = "closed"


class IronCondorBot:
    """Automated iron condor spread bot for Alpaca paper trading"""

    def __init__(self):
        self.client = self._init_alpaca_client()
        self.account = self.client.get_account()
        self.trades_log = []
        self.tz = pytz.timezone("America/New_York")

        log.info(f"[BOT] Connected to Alpaca paper trading")
        log.info(f"[ACCOUNT] Equity: ${self.account.equity:.2f} | "
                 f"Buying Power: ${self.account.buying_power:.2f}")

    def _init_alpaca_client(self) -> TradingClient:
        """Initialize Alpaca trading client from environment variables"""
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

        if not api_key or not secret_key:
            raise ValueError(
                "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment"
            )

        return TradingClient(api_key=api_key, secret_key=secret_key, base_url=base_url)

    def get_market_time(self) -> datetime:
        """Get current time in NYSE timezone"""
        return datetime.now(self.tz)

    def is_market_hours(self) -> bool:
        """Check if market is currently open (9:30 AM - 4:00 PM ET)"""
        now = self.get_market_time()
        if now.weekday() >= 5:  # Weekend
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    def is_entry_window(self) -> bool:
        """Check if we're in the entry window (10:00 AM - 12:00 PM ET)"""
        if not self.is_market_hours():
            return False
        now = self.get_market_time()
        entry_start = now.replace(hour=10, minute=0, second=0, microsecond=0)
        entry_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        return entry_start <= now <= entry_end

    def is_exit_check_time(self) -> bool:
        """Check if it's time for exit check (2:00 PM ET)"""
        if not self.is_market_hours():
            return False
        now = self.get_market_time()
        exit_time_start = now.replace(hour=14, minute=0, second=0, microsecond=0)
        exit_time_end = now.replace(hour=14, minute=30, second=0, microsecond=0)
        return exit_time_start <= now <= exit_time_end

    def get_options_chain(self, symbol: str, dte_min: int = 30, dte_max: int = 45) -> Optional[Dict]:
        """
        Fetch options chain for symbol with specified DTE range.
        Note: Alpaca's options data is limited. This is a stub for real implementation.
        """
        try:
            # In production, connect to real options data source
            # (IB, TD Ameritrade, Polygon.io, etc.)
            log.info(f"[OPTIONS] Fetching chain for {symbol} ({dte_min}-{dte_max} DTE)")

            # PLACEHOLDER: Real implementation would fetch from data provider
            # For now, return None to indicate no live options data available in paper account
            log.warning(f"[OPTIONS] Live options data not available (requires external data source)")
            return None

        except Exception as e:
            log.error(f"[OPTIONS] Error fetching chain: {e}")
            return None

    def build_iron_condor(
        self,
        symbol: str,
        expiration: str,
        short_call_strike: float,
        short_put_strike: float,
        credit: float
    ) -> Tuple[List[Dict], float]:
        """
        Build iron condor spread with 4 legs:
        - SELL call at short_call_strike
        - BUY call at short_call_strike + $5
        - SELL put at short_put_strike
        - BUY put at short_put_strike - $5

        Returns: (legs list, max_risk in dollars)
        """
        spread_width = ENTRY_RULES["spread_width"]
        max_risk = spread_width * 100  # $500 for $5 width
        max_profit = credit * 100

        legs = [
            {
                "symbol": symbol,
                "side": OrderSide.SELL,
                "type": "call",
                "strike": short_call_strike,
                "expiration": expiration,
            },
            {
                "symbol": symbol,
                "side": OrderSide.BUY,
                "type": "call",
                "strike": short_call_strike + spread_width,
                "expiration": expiration,
            },
            {
                "symbol": symbol,
                "side": OrderSide.SELL,
                "type": "put",
                "strike": short_put_strike,
                "expiration": expiration,
            },
            {
                "symbol": symbol,
                "side": OrderSide.BUY,
                "type": "put",
                "strike": short_put_strike - spread_width,
                "expiration": expiration,
            },
        ]

        log.info(f"[SPREAD] {symbol} {expiration} Iron Condor")
        log.info(f"  Short Call: ${short_call_strike} | Long Call: ${short_call_strike + spread_width}")
        log.info(f"  Short Put:  ${short_put_strike} | Long Put: ${short_put_strike - spread_width}")
        log.info(f"  Credit: ${credit} | Max Risk: ${max_risk} | Max Profit: ${max_profit}")

        return legs, max_risk

    def place_entry_order(
        self,
        symbol: str,
        expiration: str,
        short_call_strike: float,
        short_put_strike: float,
        target_credit: float
    ) -> Optional[str]:
        """
        Place entry order for iron condor spread.
        Uses limit order at (target_credit - $0.05)
        """
        try:
            if not self.is_entry_window():
                log.warning("[ENTRY] Not in entry window")
                return None

            # Build spread
            legs, max_risk = self.build_iron_condor(
                symbol, expiration, short_call_strike, short_put_strike, target_credit
            )

            # Validate position sizing
            account_equity = float(self.account.equity)
            allocation_pct = max_risk / account_equity

            if allocation_pct > 0.02:  # Max 2% risk per trade
                log.warning(f"[ENTRY] Position size exceeds 2% risk limit ({allocation_pct:.2%})")
                return None

            # Place limit order
            limit_price = target_credit - ENTRY_RULES["limit_order_discount"]

            # NOTE: Alpaca's options order API requires special handling
            # This is a simplified placeholder - real implementation needs proper options routing
            log.info(f"[ENTRY] Placing limit order for {symbol} at ${limit_price:.2f} credit")
            log.info(f"[ENTRY] Order would execute through Alpaca options trading")

            # In production, use Alpaca's options order API
            # For now, log intent and return mock order ID
            order_id = f"MOCK_{symbol}_{datetime.utcnow().isoformat()}"

            self.trades_log.append({
                "timestamp": datetime.utcnow().isoformat(),
                "type": "entry_placed",
                "symbol": symbol,
                "expiration": expiration,
                "short_call": short_call_strike,
                "short_put": short_put_strike,
                "target_credit": target_credit,
                "limit_price": limit_price,
                "max_risk": max_risk,
                "status": "pending_fill"
            })

            return order_id

        except Exception as e:
            log.error(f"[ENTRY] Error placing order: {e}")
            return None

    def check_exit_conditions(self) -> None:
        """
        Check all open positions for:
        1. 50% profit exits (close immediately)
        2. 100% stop loss (close immediately)
        3. Last 5 days to expiration (close at max profit)
        """
        if not self.is_exit_check_time():
            log.debug("[EXIT] Not at scheduled exit check time")
            return

        try:
            positions = self.client.get_all_positions()

            if not positions:
                log.info("[EXIT] No open positions")
                return

            log.info(f"[EXIT] Checking {len(positions)} position(s) for exits")

            for position in positions:
                self._evaluate_position_for_exit(position)

        except Exception as e:
            log.error(f"[EXIT] Error checking positions: {e}")

    def _evaluate_position_for_exit(self, position) -> None:
        """Evaluate single position against exit rules"""
        try:
            symbol = position.symbol
            qty = float(position.qty)
            unrealized_pl = float(position.unrealized_pl)
            unrealized_plpc = float(position.unrealized_plpc)

            log.info(f"[EXIT] {symbol}: P&L ${unrealized_pl:.2f} ({unrealized_plpc*100:.2f}%)")

            # For iron condor spreads, evaluate against profit targets
            # NOTE: Real implementation tracks spread-level P&L, not individual legs

            # Exit rule 1: 50% profit
            if unrealized_plpc >= 0.50:
                log.info(f"[EXIT] {symbol} hit 50% profit target - closing")
                self._close_position(symbol, "profit_target")

            # Exit rule 2: Stop loss at 100% loss
            elif unrealized_plpc <= -1.00:
                log.warning(f"[EXIT] {symbol} hit stop loss - closing")
                self._close_position(symbol, "stop_loss")

            # Exit rule 3: Last 5 days to expiration
            else:
                # Check expiration date if tracked
                log.debug(f"[EXIT] {symbol} holding ({unrealized_plpc*100:.2f}% P&L)")

        except Exception as e:
            log.error(f"[EXIT] Error evaluating position: {e}")

    def _close_position(self, symbol: str, reason: str) -> bool:
        """Close a position by symbol"""
        try:
            log.info(f"[CLOSE] Closing {symbol} ({reason})")

            # Get current position
            position = self.client.get_position(symbol)
            if not position:
                log.warning(f"[CLOSE] Position not found: {symbol}")
                return False

            # Market order to close (immediate exit)
            qty = int(position.qty)
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY

            order = self.client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=abs(qty),
                    side=side,
                    time_in_force=TimeInForce.DAY
                )
            )

            log.info(f"[CLOSE] {symbol} closed: Order {order.id}")

            self.trades_log.append({
                "timestamp": datetime.utcnow().isoformat(),
                "type": "exit",
                "symbol": symbol,
                "reason": reason,
                "order_id": order.id
            })

            return True

        except Exception as e:
            log.error(f"[CLOSE] Error closing {symbol}: {e}")
            return False

    def run_morning_check(self) -> None:
        """Morning pre-market check and entry candidate evaluation"""
        now = self.get_market_time()
        log.info(f"\n{'='*80}")
        log.info(f"[MORNING] Alpaca Iron Condor Bot - {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        log.info(f"{'='*80}")

        account = self.client.get_account()
        log.info(f"[ACCOUNT] Equity: ${account.equity:.2f} | "
                 f"Buying Power: ${account.buying_power:.2f}")

        if not self.is_market_hours():
            log.info("[MARKET] Market not open yet")
            return

        log.info("[MARKET] Market is open")

        # Check for new entry opportunities
        for underlying in ENTRY_RULES["underlyings"]:
            log.info(f"\n[CANDIDATE] {underlying}")
            options_chain = self.get_options_chain(
                underlying,
                ENTRY_RULES["dte_min"],
                ENTRY_RULES["dte_max"]
            )

            if not options_chain:
                log.info(f"[CANDIDATE] No options data available for {underlying}")
                continue

            # In production: analyze chain, select strikes, place order
            log.info(f"[CANDIDATE] {underlying} ready for entry evaluation")

    def run_afternoon_check(self) -> None:
        """Afternoon exit check (2:00 PM ET)"""
        now = self.get_market_time()
        log.info(f"\n{'='*80}")
        log.info(f"[AFTERNOON] Exit Check - {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        log.info(f"{'='*80}")

        self.check_exit_conditions()

    def run_daily_cycle(self) -> None:
        """Run full daily cycle: morning check + exit check"""
        self.run_morning_check()
        self.run_afternoon_check()

    def save_trade_log(self, filepath: str = "trades_log.json") -> None:
        """Save trade log to file"""
        try:
            with open(filepath, 'w') as f:
                json.dump(self.trades_log, f, indent=2)
            log.info(f"[LOG] Saved {len(self.trades_log)} trades to {filepath}")
        except Exception as e:
            log.error(f"[LOG] Error saving trades: {e}")

    def generate_daily_report(self) -> Dict:
        """Generate daily performance report"""
        try:
            account = self.client.get_account()
            positions = self.client.get_all_positions()

            total_pl = 0.0
            winning_positions = 0
            losing_positions = 0

            for position in positions:
                pl = float(position.unrealized_pl)
                total_pl += pl
                if pl > 0:
                    winning_positions += 1
                else:
                    losing_positions += 1

            report = {
                "timestamp": datetime.utcnow().isoformat(),
                "account_equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "total_unrealized_pl": total_pl,
                "open_positions": len(positions),
                "winning_positions": winning_positions,
                "losing_positions": losing_positions,
                "trades_logged": len(self.trades_log),
            }

            log.info(f"\n[DAILY REPORT]")
            log.info(f"  Equity: ${report['account_equity']:.2f}")
            log.info(f"  Unrealized P&L: ${report['total_unrealized_pl']:.2f}")
            log.info(f"  Open Positions: {report['open_positions']}")
            log.info(f"  Winning: {winning_positions} | Losing: {losing_positions}")

            return report

        except Exception as e:
            log.error(f"[REPORT] Error generating report: {e}")
            return {}


# =====================================================================
# MAIN EXECUTION
# =====================================================================

if __name__ == "__main__":
    log.info("\n" + "="*80)
    log.info("ALPACA IRON CONDOR BOT - AUTOMATED PAPER TRADING")
    log.info("="*80)

    try:
        bot = IronCondorBot()

        # Run daily cycle for testing
        log.info("\n[BOT] Starting daily cycle...")
        bot.run_daily_cycle()

        # Generate report
        bot.generate_daily_report()

        # Save trade log
        bot.save_trade_log()

        log.info("\n[BOT] Daily cycle complete")

    except Exception as e:
        log.error(f"[BOT] Fatal error: {e}", exc_info=True)
        exit(1)
