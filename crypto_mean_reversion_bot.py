"""
CRYPTO MEAN REVERSION BOT - Real directional trading on oversold coins

Strategy: Buy coins that are oversold (RSI < 30), sell on mean reversion (RSI > 60)
Risk: Fixed stop loss 2.5% below entry + position size limited to max exposure
Edge: Historical data shows this works better than momentum chasing

Completely separate from crypto_grid_bot.py:
- Grid bot: market-neutral, multiple concurrent slices, FIFO exits
- Mean reversion: directional, single position per coin, RSI-based exits

Key difference from family tree bot (which lost -$485):
- Family tree: Chases uptrends (bullish > 0 + high vol) → catches extended moves → losses
- Mean reversion: Buys dips (RSI < 30) → reversal trades → proven edge

Position sizing: max 2% of account per trade, locked to avoid overleveraging
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Dict, Optional, Tuple

from sqlalchemy import select
import crypto_btc_compound_bot as engine
from database import AsyncSessionLocal
from models import CryptoGridBranch, CryptoActivityEvent, TradingBotState

log = logging.getLogger("crypto_mean_reversion_bot")

# Configuration
MEAN_REVERSION_MODE_KEY = "crypto_mean_reversion_active"
RSI_OVERSOLD_THRESHOLD = 30      # Buy when RSI drops below this
RSI_OVERBOUGHT_THRESHOLD = 60    # Sell when RSI rises above this
STOP_LOSS_PCT = 0.025            # 2.5% hard stop
TARGET_PROFIT_PCT = 0.04          # 4% target (favorable risk/reward: 2.5% risk:4% reward = 1.6x)
MAX_POSITION_SIZE_PCT = 0.02     # Max 2% of account per trade
MIN_TRADE_USD = 10.0             # Minimum trade size
POSITION_HOLD_HOURS = 6          # Hold max 6 hours regardless (avoid overnight gap risk)

# Coins to trade - need real historical data + decent volume
MEAN_REVERSION_COINS = [
    "BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD", "ADA-USD",
    "DOGE-USD", "LINK-USD", "AVAX-USD", "UNI-USD", "MATIC-USD"
]


class MeanReversionPosition:
    """Single open position for mean reversion trade"""
    def __init__(self, product_id: str, entry_price: float, quantity: float,
                 entry_time: datetime, rsi_at_entry: float):
        self.product_id = product_id
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_time = entry_time
        self.rsi_at_entry = rsi_at_entry
        self.stop_loss_price = entry_price * (1 - STOP_LOSS_PCT)
        self.target_price = entry_price * (1 + TARGET_PROFIT_PCT)
        self.status = "open"  # open, closed_profit, closed_loss, closed_timeout


class MeanReversionEngine:
    """Manages mean reversion positions and trades"""

    def __init__(self):
        self.positions: Dict[str, MeanReversionPosition] = {}  # product_id -> position

    async def is_enabled(self, db) -> bool:
        """Check if mean reversion mode is enabled"""
        stmt = select(TradingBotState).where(
            TradingBotState.bot_name == MEAN_REVERSION_MODE_KEY
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        return bool(row and row.base_capital and row.base_capital >= 1.0)

    async def get_rsi(self, product_id: str, period: int = 14) -> Optional[float]:
        """
        Fetch RSI for a coin over the last period.
        Uses the same engine.get_average_hourly_swing_pct-style approach.
        """
        try:
            # Fetch last 2*period candles to compute RSI
            candles = await engine._fetch_candles(product_id, limit=2*period, granularity=3600)
            if not candles or len(candles) < period:
                return None

            closes = [float(c[4]) for c in candles]
            rsi = compute_rsi(closes, period)
            return rsi
        except Exception as e:
            log.error(f"[MR] RSI fetch error for {product_id}: {e}")
            return None

    async def check_entry_signal(self, product_id: str, current_price: float,
                                 rsi: float) -> bool:
        """
        Should we BUY this coin right now?
        - RSI < 30 (oversold)
        - Not already in a position
        - Have capital available
        """
        # Already in a position on this coin
        if product_id in self.positions:
            return False

        # RSI not ready
        if rsi is None or rsi >= RSI_OVERSOLD_THRESHOLD:
            return False

        # Price sanity check (not at 0)
        if current_price <= 0:
            return False

        return True

    async def check_exit_signal(self, position: MeanReversionPosition,
                               current_price: float, current_rsi: float) -> Tuple[bool, str]:
        """
        Should we SELL this position?
        Returns: (should_exit, reason)
        """
        # Hard stop loss
        if current_price <= position.stop_loss_price:
            return True, "stop_loss_hit"

        # Profit target hit
        if current_price >= position.target_price:
            return True, "target_hit"

        # Timeout - don't hold overnight
        hours_held = (datetime.now() - position.entry_time).total_seconds() / 3600
        if hours_held > POSITION_HOLD_HOURS:
            return True, "timeout_exceeded"

        # Mean reversion complete - RSI back above overbought
        if current_rsi is not None and current_rsi >= RSI_OVERBOUGHT_THRESHOLD:
            return True, "rsi_overbought_reversal"

        return False, "hold"

    async def enter_position(self, db, product_id: str, current_price: float,
                            rsi: float, available_capital: float) -> bool:
        """
        Execute entry: BUY on mean reversion signal
        Returns: True if entry placed, False otherwise
        """
        # Position size: max 2% of capital, but also check minimum trade
        position_size_usd = min(
            available_capital * MAX_POSITION_SIZE_PCT,
            available_capital  # can't spend more than we have
        )

        if position_size_usd < MIN_TRADE_USD:
            log.warning(f"[MR] {product_id}: insufficient capital ({position_size_usd:.2f} < {MIN_TRADE_USD})")
            return False

        quantity = position_size_usd / current_price

        try:
            # Place real market buy order
            order_result = await engine.place_market_buy(
                product_id=product_id,
                quantity=quantity,
                reason=f"[MR] RSI oversold entry @ RSI {rsi:.1f}"
            )

            if order_result.get("status") not in ["filled", "pending"]:
                log.error(f"[MR] Buy order failed: {order_result}")
                return False

            # Create position record
            self.positions[product_id] = MeanReversionPosition(
                product_id=product_id,
                entry_price=current_price,
                quantity=quantity,
                entry_time=datetime.now(),
                rsi_at_entry=rsi
            )

            log.info(f"[MR] ✅ Entered {product_id}: {quantity:.8f} @ ${current_price:.2f} (size: ${position_size_usd:.2f}, RSI: {rsi:.1f})")

            # Log to activity feed
            await _log_activity(db, product_id, "mean_reversion_entry", {
                "quantity": float(quantity),
                "price": current_price,
                "rsi": rsi,
                "position_size_usd": position_size_usd,
                "stop_loss": self.positions[product_id].stop_loss_price,
                "target": self.positions[product_id].target_price
            })

            return True

        except Exception as e:
            log.error(f"[MR] Entry error for {product_id}: {e}")
            return False

    async def exit_position(self, db, product_id: str, current_price: float,
                           exit_reason: str) -> bool:
        """
        Execute exit: SELL position
        Returns: True if exit placed, False otherwise
        """
        if product_id not in self.positions:
            return False

        position = self.positions[product_id]
        quantity = position.quantity

        try:
            # Place real market sell order
            order_result = await engine.place_market_sell(
                product_id=product_id,
                quantity=quantity,
                reason=f"[MR] {exit_reason} @ ${current_price:.2f}"
            )

            if order_result.get("status") not in ["filled", "pending"]:
                log.error(f"[MR] Sell order failed: {order_result}")
                return False

            # Calculate P&L
            entry_value = position.entry_price * quantity
            exit_value = current_price * quantity
            gross_pnl = exit_value - entry_value
            net_fees = exit_value * 0.005  # 0.5% round-trip fee estimate
            net_pnl = gross_pnl - net_fees

            position.status = "closed_profit" if net_pnl > 0 else "closed_loss"

            log.info(f"[MR] 🎯 Exit {product_id}: {exit_reason}")
            log.info(f"[MR]   Entry: ${entry_value:.2f}, Exit: ${exit_value:.2f}")
            log.info(f"[MR]   P&L: ${net_pnl:+.2f} ({net_pnl/entry_value*100:+.2f}%) after ${net_fees:.2f} fees")

            # Log to activity feed
            await _log_activity(db, product_id, "mean_reversion_exit", {
                "quantity": float(quantity),
                "entry_price": position.entry_price,
                "exit_price": current_price,
                "gross_pnl": float(gross_pnl),
                "net_pnl": float(net_pnl),
                "net_pnl_pct": float(net_pnl / entry_value * 100),
                "exit_reason": exit_reason,
                "rsi_at_entry": position.rsi_at_entry,
                "hold_minutes": int((datetime.now() - position.entry_time).total_seconds() / 60)
            })

            del self.positions[product_id]
            return True

        except Exception as e:
            log.error(f"[MR] Exit error for {product_id}: {e}")
            return False

    async def run_cycle(self, db):
        """Run one trading cycle: check all coins for entry/exit signals"""
        if not await self.is_enabled(db):
            return

        try:
            # Check existing positions for exit signals
            products_to_exit = []
            for product_id, position in self.positions.items():
                try:
                    ticker = await engine._fetch_current_price(product_id)
                    current_price = float(ticker["price"])
                    rsi = await self.get_rsi(product_id)

                    should_exit, exit_reason = await self.check_exit_signal(position, current_price, rsi)
                    if should_exit:
                        products_to_exit.append((product_id, current_price, exit_reason))

                except Exception as e:
                    log.error(f"[MR] Position check error for {product_id}: {e}")

            # Execute exits
            for product_id, current_price, exit_reason in products_to_exit:
                await self.exit_position(db, product_id, current_price, exit_reason)

            # Check new coins for entry signals (only if we have capital)
            available_capital = await _get_mean_reversion_capital(db)

            for product_id in MEAN_REVERSION_COINS:
                if product_id in self.positions:
                    continue  # Already in position

                if available_capital < MIN_TRADE_USD:
                    break  # Out of capital

                try:
                    ticker = await engine._fetch_current_price(product_id)
                    current_price = float(ticker["price"])
                    rsi = await self.get_rsi(product_id)

                    if await self.check_entry_signal(product_id, current_price, rsi):
                        entered = await self.enter_position(db, product_id, current_price, rsi, available_capital)
                        if entered:
                            available_capital -= MIN_TRADE_USD  # Rough capital tracking

                except Exception as e:
                    log.error(f"[MR] Entry check error for {product_id}: {e}")

        except Exception as e:
            log.error(f"[MR] Cycle error: {e}")


def compute_rsi(closes: list, period: int = 14) -> float:
    """
    Compute Relative Strength Index for a price series.
    RSI = 100 - (100 / (1 + RS))
    RS = avg_gain / avg_loss
    """
    if len(closes) < period + 1:
        return 50.0  # Neutral if insufficient data

    # Calculate price changes
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]

    # Separate gains and losses
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    # Average gains/losses over period (first use SMA, then EMA)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    # Compute RS and RSI
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


async def _get_mean_reversion_capital(db) -> float:
    """Get available capital for mean reversion trades"""
    # Get total account capital
    total_capital = await engine.get_free_cash_usd(db)

    # Reserve 80% for grid bot, 20% for mean reversion
    mr_capital = total_capital * 0.20

    return mr_capital


async def _log_activity(db, product_id: str, action: str, details: dict):
    """Log mean reversion activity to shared feed"""
    try:
        activity = CryptoActivityEvent(
            product_id=product_id,
            action=action,
            details_json=details,
            created_at=datetime.now()
        )
        db.add(activity)
        await db.commit()
    except Exception as e:
        log.error(f"[MR] Activity log error: {e}")


# Singleton instance
_mean_reversion_engine = None

def get_mean_reversion_engine() -> MeanReversionEngine:
    """Get or create singleton engine instance"""
    global _mean_reversion_engine
    if _mean_reversion_engine is None:
        _mean_reversion_engine = MeanReversionEngine()
    return _mean_reversion_engine
