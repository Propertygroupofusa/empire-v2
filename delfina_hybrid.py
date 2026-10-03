"""
DELFINA HYBRID PROTECTION SYSTEM
--------------------------------
Purpose:
1. Preserve existing grid positions.
2. Block all new BUY orders.
3. Cancel outstanding BUY orders when live protection is activated.
4. Preserve existing SELL orders.
5. Evaluate scalping opportunities without automatically trading.
6. Allow only explicitly authorized, profitable SELL executions.
7. Track realized net PnL after fees.

IMPORTANT:
- Dry-run is the default.
- No exchange API is implemented here.
- Connect and test the exchange adapter before live use.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

D = Decimal


# =====================================================
# 1. OPERATING CONFIGURATION
# =====================================================

@dataclass(frozen=True)
class Config:
    protection_mode: bool = True

    # Global buy protection
    allow_new_buys: bool = False
    cancel_resting_buys: bool = True

    # Scalper initially observes only
    scalper_enabled: bool = True
    scalper_live_execution: bool = False

    # Never liquidate a whole branch automatically
    force_exit: bool = False

    # Require estimated net profit before a new scalper sell
    min_net_profit_pct: Decimal = D("0.50")

    # Execution safety
    max_slippage_bps: int = 20
    max_sell_fraction: Decimal = D("0.10")

    # Reconciliation must pass before trading
    require_reconciliation: bool = True

    # Never use unrealized profit as spendable cash
    reinvest_realized_profit_only: bool = True


CFG = Config()


# =====================================================
# 2. ORDER TYPES
# =====================================================

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Mode(str, Enum):
    OBSERVE = "OBSERVE"
    PROTECTED = "PROTECTED"
    LIVE = "LIVE"


@dataclass
class Position:
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    current_price: Decimal


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Optional[Decimal]
    status: str


@dataclass
class SellSignal:
    symbol: str
    quantity: Decimal
    reference_price: Decimal
    estimated_net_profit_pct: Decimal
    reason: str


# =====================================================
# 3. EXCHANGE ADAPTER
# =====================================================

class ExchangeAdapter(Protocol):

    def get_open_orders(self) -> list[Order]:
        ...

    def get_positions(self) -> list[Position]:
        ...

    def get_available_balance(self) -> Decimal:
        ...

    def cancel_order(self, order_id: str) -> bool:
        ...

    def place_sell_limit(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal
    ) -> Optional[str]:
        ...

    def get_fee_rate(self, maker: bool) -> Decimal:
        ...


# =====================================================
# 4. ACCOUNTING VALIDATION
# =====================================================

@dataclass
class Reconciliation:
    coin_tracked_is_held: bool
    allocation_backed: bool
    orders_valid: bool

    @property
    def passed(self):
        return (
            self.coin_tracked_is_held
            and self.allocation_backed
            and self.orders_valid
        )


def check_reconciliation(exchange: ExchangeAdapter):
    """
    Replace with your existing reconciliation engine.
    Never assume reconciliation passes.
    """
    raise NotImplementedError(
        "Connect existing wallet reconciliation checks."
    )


# =====================================================
# 5. GLOBAL BUY PROTECTION
# =====================================================

class OrderController:

    def __init__(self, exchange, config):
        self.exchange = exchange
        self.config = config

    def allow_order(self, side: Side) -> bool:

        if side == Side.BUY:
            if not self.config.allow_new_buys:
                logging.warning("BUY BLOCKED: protection mode")
                return False

        return True

    def cancel_resting_buys(self):

        orders = self.exchange.get_open_orders()

        for order in orders:

            if order.side != Side.BUY:
                continue

            logging.warning(
                "Canceling resting BUY: %s %s",
                order.symbol,
                order.order_id
            )

            success = self.exchange.cancel_order(
                order.order_id
            )

            if not success:
                raise RuntimeError(
                    f"Could not cancel BUY {order.order_id}"
                )

        logging.info("Resting BUY cancellation pass complete.")

    def verify_no_open_buys(self):

        orders = self.exchange.get_open_orders()

        remaining = [
            order for order in orders
            if order.side == Side.BUY
        ]

        if remaining:
            raise RuntimeError(
                f"{len(remaining)} resting BUY orders remain."
            )

        return True


# =====================================================
# 6. DELFINA SCALPER
# =====================================================

class DelfinaScalper:

    def __init__(self, exchange, config):
        self.exchange = exchange
        self.config = config

    def evaluate_position(
        self,
        position: Position
    ) -> Optional[SellSignal]:

        if position.quantity <= 0:
            return None

        if position.average_cost <= 0:
            return None

        if position.current_price <= 0:
            return None

        # Estimate maker execution costs.
        fee_rate = self.exchange.get_fee_rate(
            maker=True
        )

        gross_value = (
            position.quantity * position.current_price
        )

        cost_basis = (
            position.quantity * position.average_cost
        )

        estimated_entry_fee = cost_basis * fee_rate
        estimated_exit_fee = gross_value * fee_rate

        estimated_net = (
            gross_value
            - cost_basis
            - estimated_entry_fee
            - estimated_exit_fee
        )

        total_cost = cost_basis + estimated_entry_fee

        if total_cost <= 0:
            return None

        net_profit_pct = (
            estimated_net / total_cost
        ) * D("100")

        # Do not sell at an estimated loss.
        if net_profit_pct < self.config.min_net_profit_pct:
            return None

        # Never automatically sell the whole position.
        quantity = (
            position.quantity
            * self.config.max_sell_fraction
        )

        if quantity <= 0:
            return None

        return SellSignal(
            symbol=position.symbol,
            quantity=quantity,
            reference_price=position.current_price,
            estimated_net_profit_pct=net_profit_pct,
            reason="Estimated net profitable exit"
        )

    def scan(self):

        positions = self.exchange.get_positions()

        signals = []

        for position in positions:

            signal = self.evaluate_position(position)

            if signal:
                signals.append(signal)

                logging.info(
                    "SCALPER SIGNAL: %s | Qty=%s | "
                    "Estimated net=%s%%",
                    signal.symbol,
                    signal.quantity,
                    signal.estimated_net_profit_pct
                )

        return signals


# =====================================================
# 7. PROFITABLE SELL EXECUTION
# =====================================================

class SellExecutor:

    def __init__(
        self,
        exchange,
        config,
        order_controller
    ):
        self.exchange = exchange
        self.config = config
        self.orders = order_controller

    def execute(self, signal: SellSignal):

        if not self.config.scalper_live_execution:
            logging.info(
                "OBSERVATION ONLY: %s",
                signal.symbol
            )
            return None

        if not self.orders.allow_order(Side.SELL):
            return None

        # Recheck live inventory before selling.
        positions = {
            p.symbol: p
            for p in self.exchange.get_positions()
        }

        position = positions.get(signal.symbol)

        if position is None:
            raise RuntimeError("Position no longer exists.")

        if signal.quantity > position.quantity:
            raise RuntimeError("Insufficient held inventory.")

        # Recheck the market and estimated net profit.
        fresh_scalper = DelfinaScalper(
            self.exchange,
            self.config
        )

        fresh_signal = fresh_scalper.evaluate_position(
            position
        )

        if fresh_signal is None:
            logging.warning(
                "SELL REJECTED: profitability condition no longer met."
            )
            return None

        # Conservative limit execution.
        # Never submit an aggressive market sell here.
        price = position.current_price * (
            D("1") - D(self.config.max_slippage_bps)
            / D("10000")
        )

        order_id = self.exchange.place_sell_limit(
            signal.symbol,
            min(signal.quantity, fresh_signal.quantity),
            price
        )

        if not order_id:
            raise RuntimeError("Exchange rejected sell order.")

        logging.info(
            "SELL LIMIT SUBMITTED: %s | %s",
            signal.symbol,
            order_id
        )

        return order_id


# =====================================================
# 8. MAIN PROTECTION ENGINE
# =====================================================

class DelfinaHybrid:

    def __init__(self, exchange):

        self.exchange = exchange

        self.orders = OrderController(
            exchange,
            CFG
        )

        self.scalper = DelfinaScalper(
            exchange,
            CFG
        )

        self.executor = SellExecutor(
            exchange,
            CFG,
            self.orders
        )

        self.mode = Mode.OBSERVE

    def activate_protection(self):

        logging.warning("ACTIVATING CAPITAL PROTECTION")

        if CFG.cancel_resting_buys:
            self.orders.cancel_resting_buys()

        self.orders.verify_no_open_buys()

        self.mode = Mode.PROTECTED

        logging.info(
            "PROTECTION ACTIVE: no new BUY orders."
        )

    def run_cycle(self):

        # Fail closed if accounting cannot be verified.
        if CFG.require_reconciliation:

            reconciliation = check_reconciliation(
                self.exchange
            )

            if not reconciliation.passed:
                raise RuntimeError(
                    "RECONCILIATION FAILED. "
                    "Trading halted."
                )

        # Confirm no new BUY orders have appeared.
        self.orders.verify_no_open_buys()

        # Existing positions remain intact.
        signals = self.scalper.scan()

        # Initially, this records opportunities only.
        for signal in signals:
            self.executor.execute(signal)

        logging.info(
            "Cycle complete. Mode=%s Signals=%s",
            self.mode.value,
            len(signals)
        )


# =====================================================
# 9. STARTUP
# =====================================================

def main(exchange):

    bot = DelfinaHybrid(exchange)

    # First protect existing capital.
    bot.activate_protection()

    # Then evaluate positions without automatic execution.
    while True:

        try:
            bot.run_cycle()

        except Exception:
            logging.exception(
                "PROTECTION ERROR: automatic execution halted."
            )
            break

        time.sleep(60)


if __name__ == "__main__":
    print(
        "Delfina Hybrid is a framework. "
        "Connect your exchange adapter before execution."
    )
