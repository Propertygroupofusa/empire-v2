"""
PROP BOT OPTIONS EXTENSION
===========================
Integrates options trading with existing prop_bot RSI/mean-reversion logic.
Runs alongside futures/stock trading with separate capital allocation.

Once Alpaca API egress enabled on Railway, uncomment imports and activate.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp
from typing import Optional, List, Dict, Any

# Uncomment after options_trading.py is deployed:
# from options_trading import (
#     OptionPosition, OptionStrategyBuilder, GreekEstimate,
#     estimate_option_greeks, place_options_order, get_option_chain,
#     run_options_scanner, should_close_position, calculate_position_sizing,
#     OptionType, OptionStrategy
# )

ET = ZoneInfo("America/New_York")
log = logging.getLogger("prop_bot_options")

# ============================================================================
# CONFIGURATION
# ============================================================================

# Capital allocation: 20% of account to options, 80% to futures
OPTIONS_CAPITAL_ALLOCATION = float(os.getenv("OPTIONS_CAPITAL_PCT", "0.20"))

# Risk per options trade: max 2% account loss
OPTIONS_MAX_LOSS_PER_TRADE = float(os.getenv("OPTIONS_MAX_LOSS_PCT", "0.02"))

# Profit targets for early exit
PROFIT_TARGET_PCT = float(os.getenv("OPTIONS_PROFIT_TARGET", "0.50"))  # 50% profit
MAX_LOSS_PCT = float(os.getenv("OPTIONS_MAX_LOSS_LIMIT", "0.20"))     # 20% loss

# Symbols to run options scanner on
OPTIONS_SYMBOLS = ["SPY", "QQQ", "IWM", "GLD", "TLT"]  # Extended to bonds, ETFs

# RSI thresholds for options entry
OPTIONS_RSI_BUY_BELOW = float(os.getenv("OPTIONS_RSI_BUY", "30"))
OPTIONS_RSI_SELL_ABOVE = float(os.getenv("OPTIONS_RSI_SELL", "70"))
OPTIONS_IV_HIGH = float(os.getenv("OPTIONS_IV_HIGH", "0.80"))  # IV percentile
OPTIONS_IV_LOW = float(os.getenv("OPTIONS_IV_LOW", "0.20"))

# Days to expiration for options positions
OPTIONS_DTE = int(os.getenv("OPTIONS_DTE", "30"))  # 30 days to expiration

# ============================================================================
# POSITION TRACKING
# ============================================================================

class OptionsPositionTracker:
    """Track open options positions and P&L"""

    def __init__(self):
        self.open_positions: Dict[str, OptionPosition] = {}  # order_id -> position
        self.closed_positions: List[Dict[str, Any]] = []
        self.daily_pnl: float = 0.0

    def add_position(self, order_id: str, position: OptionPosition):
        """Track newly opened position"""
        self.open_positions[order_id] = position
        log.info(f"📍 Options position opened: {position.strategy.value} | ID: {order_id}")

    def close_position(self, order_id: str, exit_price: float, reason: str):
        """Close and track position P&L"""
        if order_id not in self.open_positions:
            log.warning(f"⚠️  Position {order_id} not found")
            return

        position = self.open_positions[order_id]
        pnl = exit_price - position.entry_premium
        pnl_pct = (pnl / position.entry_premium * 100) if position.entry_premium > 0 else 0

        self.closed_positions.append({
            "strategy": position.strategy.value,
            "entry_time": position.entry_time,
            "exit_time": datetime.now(ET),
            "entry_premium": position.entry_premium,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
        })

        self.daily_pnl += pnl
        del self.open_positions[order_id]
        log.info(f"✅ Position closed: {reason} | P&L: ${pnl:.2f} ({pnl_pct:.1f}%)")

    def get_summary(self) -> Dict[str, Any]:
        """Summary of today's options trading"""
        return {
            "open_positions": len(self.open_positions),
            "closed_positions": len(self.closed_positions),
            "daily_pnl": self.daily_pnl,
            "avg_pnl_pct": (
                sum(p["pnl_pct"] for p in self.closed_positions) / len(self.closed_positions)
                if self.closed_positions else 0
            ),
        }

# Global tracker
tracker = OptionsPositionTracker()

# ============================================================================
# OPTIONS ENTRY LOGIC
# ============================================================================

async def check_options_entry(
    session: aiohttp.ClientSession,
    symbol: str,
    current_price: float,
    rsi: float,
    iv_percentile: float,
    account_balance: float,
) -> Optional[str]:
    """
    Check if options entry should be triggered for a symbol

    Returns:
        order_id if successful, None if rejected or failed
    """

    # Uncomment when Alpaca options API is accessible:
    # position = await run_options_scanner(session, symbol, current_price, rsi, iv_percentile)
    #
    # if not position:
    #     return None
    #
    # # Size position based on account balance and risk
    # options_balance = account_balance * OPTIONS_CAPITAL_ALLOCATION
    # contracts = calculate_position_sizing(options_balance, OPTIONS_MAX_LOSS_PER_TRADE, position)
    # position.legs[0].quantity = contracts  # Update quantity
    #
    # # Place order
    # response = await place_options_order(session, position)
    #
    # if "error" in response:
    #     log.error(f"❌ Entry failed for {symbol}: {response['error']}")
    #     return None
    #
    # # Track position
    # order_id = response.get("id")
    # tracker.add_position(order_id, position)
    # return order_id

    log.debug(f"⏳ Options entry check: {symbol} (RSI {rsi:.1f}, IV {iv_percentile:.0f})")
    return None

async def check_options_exits(
    session: aiohttp.ClientSession,
) -> List[str]:
    """
    Check all open options positions for exit conditions

    Returns:
        List of order_ids that were closed
    """

    closed_orders = []

    # Uncomment when Alpaca options API is accessible:
    # for order_id, position in list(tracker.open_positions.items()):
    #     # Get current position value from Alpaca
    #     # (This would require fetching position details from API)
    #
    #     # For now, use simplified exit logic
    #     should_close, reason = should_close_position(
    #         position.entry_premium,
    #         position.current_value or position.entry_premium,
    #         max_loss_pct=MAX_LOSS_PCT,
    #         profit_target_pct=PROFIT_TARGET_PCT,
    #     )
    #
    #     if should_close:
    #         await close_options_position(session, order_id)
    #         tracker.close_position(order_id, position.current_value or 0, reason)
    #         closed_orders.append(order_id)

    return closed_orders

# ============================================================================
# RISK MANAGEMENT
# ============================================================================

def should_skip_options_trading() -> bool:
    """Check if options trading should be paused (circuit breaker)"""

    # Don't trade options if:
    # 1. Daily P&L already at limit
    # 2. Too many open positions
    # 3. Market conditions unfavorable

    summary = tracker.get_summary()

    if tracker.daily_pnl < -OPTIONS_MAX_LOSS_PER_TRADE * 0.05:  # 5% of account daily max
        log.warning(f"⚠️  Daily options loss limit reached: ${tracker.daily_pnl:.2f}")
        return True

    if summary["open_positions"] > 3:
        log.info(f"ℹ️  Max open positions ({3}) reached")
        return True

    return False

def get_options_account_status() -> Dict[str, Any]:
    """Get current options account metrics"""
    return {
        "open_positions": len(tracker.open_positions),
        "closed_today": len(tracker.closed_positions),
        "daily_pnl": tracker.daily_pnl,
        "avg_win_pct": tracker.get_summary()["avg_pnl_pct"],
    }

# ============================================================================
# MAIN LOOP INTEGRATION
# ============================================================================

async def run_options_cycle(
    session: aiohttp.ClientSession,
    price_data: Dict[str, Any],  # {symbol: {price, rsi, iv_percentile}}
    account_balance: float,
) -> Dict[str, Any]:
    """
    Run one options trading cycle: check entries, check exits, report

    Called periodically by main prop_bot loop
    """

    log.info("🎯 Starting options trading cycle...")

    # Check exits first
    exits = await check_options_exits(session)

    # Check entries
    entries = []
    if not should_skip_options_trading():
        for symbol in OPTIONS_SYMBOLS:
            if symbol not in price_data:
                continue

            data = price_data[symbol]
            order_id = await check_options_entry(
                session,
                symbol,
                data.get("price", 0),
                data.get("rsi", 50),
                data.get("iv_percentile", 50),
                account_balance,
            )
            if order_id:
                entries.append(order_id)

    summary = tracker.get_summary()
    log.info(
        f"📊 Options cycle complete: {len(entries)} entries, {len(exits)} exits | "
        f"Daily P&L: ${summary['daily_pnl']:.2f}"
    )

    return {
        "entries": entries,
        "exits": exits,
        "daily_pnl": summary["daily_pnl"],
        "open_positions": summary["open_positions"],
    }

# ============================================================================
# EXAMPLE: HOW TO CALL FROM PROP_BOT MAIN
# ============================================================================

"""
# In prop_bot.py main loop, add:

async def main_with_options():
    async with aiohttp.ClientSession() as session:
        while True:
            # Existing futures trading cycle
            account = await get_account(session)
            positions = await get_positions(session)

            # NEW: Scan symbols for options opportunities
            price_data = {}
            for symbol in prop_bot_options.OPTIONS_SYMBOLS:
                price = await get_price_data(session, symbol)
                rsi = calculate_rsi(...)
                iv_pct = fetch_iv_percentile(symbol)  # Requires data feed
                price_data[symbol] = {
                    "price": price,
                    "rsi": rsi,
                    "iv_percentile": iv_pct,
                }

            # Run options trading cycle
            options_result = await prop_bot_options.run_options_cycle(
                session,
                price_data,
                account.equity,
            )

            log.info(f"Options: {options_result}")

            # Wait before next cycle
            await asyncio.sleep(300)  # 5 minutes
"""

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info("Options extension ready. Awaiting Alpaca API egress approval.")
