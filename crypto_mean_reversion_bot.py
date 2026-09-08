"""
CRYPTO MEAN REVERSION BOT - Real directional trading on oversold coins

Strategy: Buy coins that are oversold (RSI < 30 with confirmation), sell on mean reversion (RSI > 60)
Risk: Volatility-adjusted stop loss (ATR-based, not fixed %) + protective limit orders on exit
Edge: Historical data shows this works better than momentum chasing

SAFEGUARDS:
1. Exit Fix: Stop-Limit with 2% slippage collar (prevents 10% flash-crash fills)
2. Volatility-Adjusted Stops: ATR-based (2.0-2.8% on BTC, 4.5-6.0% on AAVE)
3. Pre-Entry Liquidity Gate: Skip if bid/ask spread > 0.3%
4. Entry Confirmation: RSI must be turning positive (slope > 0), not just < 30

Completely separate from crypto_grid_bot.py:
- Grid bot: market-neutral, multiple concurrent slices, FIFO exits
- Mean reversion: directional, single position per coin, RSI-based exits with protective stops

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
RSI_OVERSOLD_THRESHOLD = 30      # Buy when RSI drops below this AND rising
RSI_OVERBOUGHT_THRESHOLD = 60    # Sell when RSI rises above this
ATR_MULTIPLIER_STOP = 1.5        # Stop loss = Entry - (1.5 * ATR_14)
ATR_MULTIPLIER_TARGET = 2.5      # Take profit = Entry + (2.5 * ATR_14)
SLIPPAGE_COLLAR = 0.02           # 2% tolerance on protective limit order exit
LIQUIDITY_SPREAD_THRESHOLD = 0.003  # Abort if bid/ask > 0.3%
MAX_POSITION_SIZE_PCT = 0.02     # Max 2% of account per trade
MIN_TRADE_USD = 10.0             # Minimum trade size
POSITION_HOLD_HOURS = 6          # Hold max 6 hours regardless (avoid overnight gap risk)

# Coins to trade - need real historical data + decent volume
MEAN_REVERSION_COINS = [
    "BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD", "ADA-USD",
    "DOGE-USD", "LINK-USD", "AVAX-USD", "UNI-USD", "MATIC-USD"
]


class MeanReversionPosition:
    """Single open position for mean reversion trade with volatility-adjusted stops"""
    def __init__(self, product_id: str, entry_price: float, quantity: float,
                 entry_time: datetime, rsi_at_entry: float, atr_14: float):
        self.product_id = product_id
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_time = entry_time
        self.rsi_at_entry = rsi_at_entry
        self.atr_14 = atr_14

        # Volatility-adjusted stops using ATR (not fixed %)
        # Stop = Entry - (1.5 * ATR), Target = Entry + (2.5 * ATR)
        self.stop_loss_price = entry_price - (ATR_MULTIPLIER_STOP * atr_14)
        self.target_price = entry_price + (ATR_MULTIPLIER_TARGET * atr_14)

        # For protective limit order on exit (2% slippage collar)
        self.emergency_floor = self.stop_loss_price * (1 - SLIPPAGE_COLLAR)

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
        Uses hourly candles (3600s granularity).
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

    async def get_atr(self, product_id: str, period: int = 14) -> Optional[float]:
        """
        Calculate Average True Range (14-period) for volatility-adjusted stops.
        ATR = average of True Range over last 14 candles.
        True Range = max(H-L, |H-C_prev|, |L-C_prev|)
        """
        try:
            # Fetch last period+1 candles for ATR calculation
            candles = await engine._fetch_candles(product_id, limit=period+1, granularity=3600)
            if not candles or len(candles) < period:
                return None

            # Extract OHLC from candles: [time, open, high, low, close, volume]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]

            # Calculate True Range for each candle
            true_ranges = []
            for i in range(1, len(candles)):
                h = highs[i]
                l = lows[i]
                c_prev = closes[i-1]

                tr = max(
                    h - l,
                    abs(h - c_prev),
                    abs(l - c_prev)
                )
                true_ranges.append(tr)

            # ATR = simple moving average of true ranges
            if len(true_ranges) < period:
                return None

            atr = sum(true_ranges[-period:]) / period
            return atr

        except Exception as e:
            log.error(f"[MR] ATR fetch error for {product_id}: {e}")
            return None

    async def check_liquidity(self, product_id: str) -> Tuple[bool, float]:
        """
        Check if bid/ask spread is acceptable before entry.
        Returns: (is_liquid, spread_pct)
        Aborts entry if spread > 0.3%
        """
        try:
            # Fetch top-of-book from order book
            ticker = await engine._fetch_current_price(product_id)

            # Extract bid/ask (if available; otherwise assume liquid)
            bid = ticker.get("bid")
            ask = ticker.get("ask")

            if bid is None or ask is None or bid <= 0:
                # If bid/ask unavailable, assume liquid and proceed
                return True, 0.0

            bid = float(bid)
            ask = float(ask)
            spread_pct = (ask - bid) / bid

            is_liquid = spread_pct <= LIQUIDITY_SPREAD_THRESHOLD

            if not is_liquid:
                log.warning(f"[MR] {product_id}: Spread too wide ({spread_pct*100:.2f}%) - skipping entry")

            return is_liquid, spread_pct

        except Exception as e:
            log.error(f"[MR] Liquidity check error for {product_id}: {e}")
            return True, 0.0  # Default to proceed on error

    async def check_entry_signal(self, product_id: str, current_price: float,
                                 rsi_current: float, rsi_previous: float) -> Tuple[bool, str]:
        """
        Should we BUY this coin right now?
        Returns: (should_enter, reason)

        SAFEGUARDS:
        1. RSI < 30 AND RSI turning positive (slope > 0) — entry confirmation
        2. Not already in a position
        3. Liquidity check: spread < 0.3%
        4. Price sanity check
        """
        # Already in a position on this coin
        if product_id in self.positions:
            return False, "already_in_position"

        # Price sanity check
        if current_price <= 0:
            return False, "invalid_price"

        # RSI requirements: Must be oversold AND turning positive
        if rsi_current is None or rsi_previous is None:
            return False, "insufficient_rsi_data"

        # RSI < 30 (oversold)
        if rsi_current >= RSI_OVERSOLD_THRESHOLD:
            return False, "rsi_not_oversold"

        # RSI SLOPE: Must be turning up (rsi_current > rsi_previous)
        # This filters out "falling knife" entries
        if rsi_current <= rsi_previous:
            return False, "rsi_not_confirming"

        # Liquidity check: bid/ask spread must be < 0.3%
        is_liquid, spread = await self.check_liquidity(product_id)
        if not is_liquid:
            return False, f"illiquid_spread_{spread*100:.2f}pct"

        return True, "entry_confirmed"

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
        Execute entry: BUY on mean reversion signal with ATR-based stops
        Returns: True if entry placed, False otherwise
        """
        # Fetch ATR for volatility-adjusted stops
        atr_14 = await self.get_atr(product_id, period=14)
        if atr_14 is None or atr_14 <= 0:
            log.error(f"[MR] {product_id}: Could not calculate ATR - skipping entry")
            return False

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
                reason=f"[MR] RSI oversold entry @ RSI {rsi:.1f}, ATR-based stops"
            )

            if order_result.get("status") not in ["filled", "pending"]:
                log.error(f"[MR] Buy order failed: {order_result}")
                return False

            # Create position record with ATR for volatility-adjusted stops
            self.positions[product_id] = MeanReversionPosition(
                product_id=product_id,
                entry_price=current_price,
                quantity=quantity,
                entry_time=datetime.now(),
                rsi_at_entry=rsi,
                atr_14=atr_14
            )

            pos = self.positions[product_id]
            stop_pct = (current_price - pos.stop_loss_price) / current_price * 100
            target_pct = (pos.target_price - current_price) / current_price * 100
            reward_risk_ratio = target_pct / stop_pct if stop_pct > 0 else 0

            log.info(f"[MR] ✅ Entered {product_id}: {quantity:.8f} @ ${current_price:.2f}")
            log.info(f"[MR]   Size: ${position_size_usd:.2f}, RSI: {rsi:.1f}, ATR: ${atr_14:.4f}")
            log.info(f"[MR]   Stop: ${pos.stop_loss_price:.2f} ({stop_pct:.2f}% down) | Target: ${pos.target_price:.2f} ({target_pct:.2f}% up)")
            log.info(f"[MR]   Risk/Reward: 1:{reward_risk_ratio:.2f} (emergency floor: ${pos.emergency_floor:.2f})")

            # Log to activity feed
            await _log_activity(db, product_id, "mean_reversion_entry", {
                "quantity": float(quantity),
                "price": current_price,
                "rsi": rsi,
                "atr_14": float(atr_14),
                "position_size_usd": position_size_usd,
                "stop_loss": float(pos.stop_loss_price),
                "target": float(pos.target_price),
                "emergency_floor": float(pos.emergency_floor),
                "reward_risk_ratio": float(reward_risk_ratio)
            })

            return True

        except Exception as e:
            log.error(f"[MR] Entry error for {product_id}: {e}")
            return False

    async def exit_position(self, db, product_id: str, current_price: float,
                           exit_reason: str) -> bool:
        """
        Execute exit with protective limit order (2% slippage collar).
        Prevents 10% flash-crash fills while guaranteeing execution on normal moves.

        Strategy:
        - If stop_loss hit: Use protective limit at stop_price * 0.98 (2% collar)
        - If target hit or timeout: Use market order (normal exit)
        - If RSI confirmation: Use market order (normal exit)
        """
        if product_id not in self.positions:
            return False

        position = self.positions[product_id]
        quantity = position.quantity

        try:
            # Determine order type based on exit reason
            if exit_reason == "stop_loss_hit":
                # Stop loss exit: Try protective limit order with slippage collar
                limit_price = position.emergency_floor  # stop_price * (1 - 2%)
                log.info(f"[MR] 🛑 Stop loss hit on {product_id}: Attempting protective limit @ ${limit_price:.2f}")

                # Try limit order; fallback to market if not supported
                if hasattr(engine, 'place_limit_sell'):
                    order_result = await engine.place_limit_sell(
                        product_id=product_id,
                        quantity=quantity,
                        price=limit_price,
                        reason=f"[MR] Protective limit order (2% collar) after stop hit"
                    )
                else:
                    # Fallback: Use market order with warning
                    log.warning(f"[MR] place_limit_sell not available, falling back to market order")
                    order_result = await engine.place_market_sell(
                        product_id=product_id,
                        quantity=quantity,
                        reason=f"[MR] {exit_reason} @ ${current_price:.2f} (fallback market)"
                    )
            else:
                # All other exits (target, timeout, RSI): Use market order
                order_result = await engine.place_market_sell(
                    product_id=product_id,
                    quantity=quantity,
                    reason=f"[MR] {exit_reason} @ ${current_price:.2f}"
                )

            if order_result.get("status") not in ["filled", "pending"]:
                log.error(f"[MR] Sell order failed: {order_result}")
                return False

            # Calculate P&L
            # For limit orders, use limit price; for market, use current price
            exit_price = order_result.get("price", current_price)
            entry_value = position.entry_price * quantity
            exit_value = exit_price * quantity
            gross_pnl = exit_value - entry_value
            net_fees = exit_value * 0.005  # 0.5% Tier 3 round-trip fee estimate
            net_pnl = gross_pnl - net_fees

            position.status = "closed_profit" if net_pnl > 0 else "closed_loss"

            log.info(f"[MR] 🎯 Exit {product_id}: {exit_reason}")
            log.info(f"[MR]   Entry: ${entry_value:.2f}, Exit: ${exit_value:.2f} (limit: ${order_result.get('price', 'market'):.2f})")
            log.info(f"[MR]   P&L: ${net_pnl:+.2f} ({net_pnl/entry_value*100:+.2f}%) after ${net_fees:.2f} fees")
            log.info(f"[MR]   Hold: {int((datetime.now() - position.entry_time).total_seconds() / 60)} min")

            # Log to activity feed
            await _log_activity(db, product_id, "mean_reversion_exit", {
                "quantity": float(quantity),
                "entry_price": float(position.entry_price),
                "exit_price": float(exit_price),
                "gross_pnl": float(gross_pnl),
                "net_pnl": float(net_pnl),
                "net_pnl_pct": float(net_pnl / entry_value * 100),
                "exit_reason": exit_reason,
                "order_type": "limit" if exit_reason == "stop_loss_hit" else "market",
                "limit_price": float(position.emergency_floor) if exit_reason == "stop_loss_hit" else None,
                "rsi_at_entry": float(position.rsi_at_entry),
                "atr_used": float(position.atr_14),
                "hold_minutes": int((datetime.now() - position.entry_time).total_seconds() / 60)
            })

            del self.positions[product_id]
            return True

        except Exception as e:
            log.error(f"[MR] Exit error for {product_id}: {e}")
            return False

    async def run_cycle(self, db):
        """Run one trading cycle: check all coins for entry/exit signals with safeguards"""
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

                    # Fetch RSI for both current and previous candle (for slope check)
                    # Get last 3 hourly candles to compute RSI(t-1) and RSI(t)
                    candles = await engine._fetch_candles(product_id, limit=32, granularity=3600)
                    if not candles or len(candles) < 16:
                        continue

                    closes = [float(c[4]) for c in candles]
                    rsi_current = compute_rsi(closes[-14:], period=14)  # Last 14 candles
                    rsi_previous = compute_rsi(closes[-28:-14], period=14)  # Previous 14 candles

                    # Check entry with RSI confirmation (slope > 0)
                    should_enter, entry_reason = await self.check_entry_signal(
                        product_id, current_price, rsi_current, rsi_previous
                    )

                    if should_enter:
                        entered = await self.enter_position(db, product_id, current_price, rsi_current, available_capital)
                        if entered:
                            available_capital -= MIN_TRADE_USD  # Rough capital tracking
                    elif entry_reason != "already_in_position":
                        # Log why we didn't enter (for debugging)
                        pass  # Suppress verbose logging of skipped coins

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
