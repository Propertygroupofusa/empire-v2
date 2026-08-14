"""
OPTIONS TRADING MODULE FOR PROP BOT
====================================
Single-leg and multi-leg options strategies with Greeks-aware position sizing.
Integrates with Alpaca Trading API (Nov 2025+)

Strategies:
  - Long Call / Long Put (directional)
  - Covered Call (income)
  - Iron Condor (range-bound)
  - Straddle (volatility play)
  - Bull/Bear Spread (defined risk)
  - Butterfly (mean reversion)
"""

import os
import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from zoneinfo import ZoneInfo

log = logging.getLogger("options_bot")

ET = ZoneInfo("America/New_York")

# ============================================================================
# ENUMS & DATA STRUCTURES
# ============================================================================

class OptionType(Enum):
    """Option contract type"""
    CALL = "C"
    PUT = "P"

class OptionStrategy(Enum):
    """Multi-leg option strategies"""
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    COVERED_CALL = "covered_call"
    IRON_CONDOR = "iron_condor"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    BUTTERFLY = "butterfly"

class OptionSide(Enum):
    """Buy or sell leg"""
    BUY = "buy"
    SELL = "sell"

@dataclass
class OptionLeg:
    """Single leg of an options order"""
    symbol: str              # "SPY", "QQQ", etc
    expiration: str          # "2026-09-18" (YYYY-MM-DD)
    strike: float            # e.g., 500.0
    option_type: OptionType  # CALL or PUT
    side: OptionSide         # BUY or SELL
    quantity: int            # contracts

    def to_alpaca_format(self) -> Dict[str, Any]:
        """Convert to Alpaca Trading API format"""
        # Format: SPY20260918C00500000 (symbol + date + type + strike with leading zeros)
        exp_clean = self.expiration.replace("-", "")  # "20260918"
        strike_fmt = str(int(self.strike * 1000)).zfill(8)  # Alpaca uses cents
        contract_id = f"{self.symbol}{exp_clean}{self.option_type.value}{strike_fmt}"

        return {
            "contract_id": contract_id,
            "side": self.side.value,
            "qty": self.quantity,
        }

@dataclass
class OptionPosition:
    """Tracked options position"""
    strategy: OptionStrategy
    legs: List[OptionLeg]
    entry_time: datetime
    entry_premium: float         # Total premium paid/received
    max_profit: float            # Max profit at expiration
    max_loss: float              # Max loss at expiration
    breakeven: List[float]       # Breakeven prices
    current_value: Optional[float] = None
    greeks: Optional[Dict[str, float]] = None  # delta, gamma, theta, vega

@dataclass
class GreekEstimate:
    """Simplified Greeks estimation (before Alpaca returns live Greeks)"""
    delta: float     # Price sensitivity
    gamma: float     # Delta sensitivity
    theta: float     # Time decay per day
    vega: float      # IV sensitivity

# ============================================================================
# OPTIONS PRICING & GREEKS (SIMPLIFIED)
# ============================================================================

def estimate_option_greeks(
    spot: float,
    strike: float,
    expiration_days: float,
    option_type: OptionType,
    volatility: float = 0.25,  # 25% IV assumption
    risk_free_rate: float = 0.05
) -> GreekEstimate:
    """
    Simplified Black-Scholes Greeks estimation.
    Note: Alpaca will provide live Greeks via Trading API once enabled.

    Args:
        spot: Current stock price
        strike: Option strike price
        expiration_days: Days to expiration
        option_type: CALL or PUT
        volatility: Implied volatility (default 25%)
        risk_free_rate: Risk-free rate (default 5%)

    Returns:
        GreekEstimate with delta, gamma, theta, vega
    """
    import math

    if expiration_days <= 0:
        return GreekEstimate(delta=0, gamma=0, theta=0, vega=0)

    T = expiration_days / 365.0
    sigma = volatility

    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    # Normal distribution functions
    from scipy.stats import norm

    nd1 = norm.pdf(d1)
    N_d1 = norm.cdf(d1)
    N_d2 = norm.cdf(d2)
    N_minus_d1 = norm.cdf(-d1)
    N_minus_d2 = norm.cdf(-d2)

    # Delta (price sensitivity)
    if option_type == OptionType.CALL:
        delta = N_d1
    else:
        delta = N_d1 - 1

    # Gamma (delta sensitivity) — same for calls and puts
    gamma = nd1 / (spot * sigma * math.sqrt(T))

    # Theta (time decay, per day)
    if option_type == OptionType.CALL:
        theta = (-(spot * nd1 * sigma) / (2 * math.sqrt(T))
                 - risk_free_rate * strike * math.exp(-risk_free_rate * T) * N_d2) / 365
    else:
        theta = (-(spot * nd1 * sigma) / (2 * math.sqrt(T))
                 + risk_free_rate * strike * math.exp(-risk_free_rate * T) * N_minus_d2) / 365

    # Vega (IV sensitivity, per 1% move in volatility)
    vega = spot * nd1 * math.sqrt(T) / 100

    return GreekEstimate(delta=delta, gamma=gamma, theta=theta, vega=vega)

# ============================================================================
# STRATEGY BUILDERS
# ============================================================================

class OptionStrategyBuilder:
    """Constructs multi-leg options strategies"""

    @staticmethod
    def long_call(
        symbol: str,
        strike: float,
        expiration: str,
        contracts: int = 1
    ) -> OptionPosition:
        """Long call: bullish directional bet"""
        legs = [
            OptionLeg(
                symbol=symbol,
                expiration=expiration,
                strike=strike,
                option_type=OptionType.CALL,
                side=OptionSide.BUY,
                quantity=contracts
            )
        ]
        return OptionPosition(
            strategy=OptionStrategy.LONG_CALL,
            legs=legs,
            entry_time=datetime.now(ET),
            entry_premium=0,  # Will be set after order fill
            max_profit=float('inf'),
            max_loss=0,  # Limited to premium paid
            breakeven=[strike],
        )

    @staticmethod
    def long_put(
        symbol: str,
        strike: float,
        expiration: str,
        contracts: int = 1
    ) -> OptionPosition:
        """Long put: bearish directional bet"""
        legs = [
            OptionLeg(
                symbol=symbol,
                expiration=expiration,
                strike=strike,
                option_type=OptionType.PUT,
                side=OptionSide.BUY,
                quantity=contracts
            )
        ]
        return OptionPosition(
            strategy=OptionStrategy.LONG_PUT,
            legs=legs,
            entry_time=datetime.now(ET),
            entry_premium=0,
            max_profit=strike,
            max_loss=0,  # Limited to premium paid
            breakeven=[strike],
        )

    @staticmethod
    def iron_condor(
        symbol: str,
        short_call_strike: float,
        long_call_strike: float,
        short_put_strike: float,
        long_put_strike: float,
        expiration: str,
        contracts: int = 1
    ) -> OptionPosition:
        """
        Iron condor: sell call/put spreads, defined risk, range-bound

        Max profit: credit received
        Max loss: difference between strikes minus credit
        Breakeven: call spread strikes and put spread strikes
        """
        legs = [
            OptionLeg(symbol, expiration, short_call_strike, OptionType.CALL, OptionSide.SELL, contracts),
            OptionLeg(symbol, expiration, long_call_strike, OptionType.CALL, OptionSide.BUY, contracts),
            OptionLeg(symbol, expiration, short_put_strike, OptionType.PUT, OptionSide.SELL, contracts),
            OptionLeg(symbol, expiration, long_put_strike, OptionType.PUT, OptionSide.BUY, contracts),
        ]

        call_width = long_call_strike - short_call_strike
        put_width = short_put_strike - long_put_strike

        return OptionPosition(
            strategy=OptionStrategy.IRON_CONDOR,
            legs=legs,
            entry_time=datetime.now(ET),
            entry_premium=0,
            max_profit=0,  # Will be credit received
            max_loss=call_width * 100 * contracts,  # Contracts × width × $100 multiplier
            breakeven=[
                short_call_strike,  # Call spread breakeven
                short_put_strike,   # Put spread breakeven
            ],
        )

    @staticmethod
    def straddle(
        symbol: str,
        strike: float,
        expiration: str,
        contracts: int = 1,
        long_straddle: bool = True
    ) -> OptionPosition:
        """
        Straddle: long or short same strike call + put
        Long straddle: profit from big moves in either direction (volatility bet)
        Short straddle: profit from no move (theta decay bet)
        """
        call_side = OptionSide.BUY if long_straddle else OptionSide.SELL
        put_side = OptionSide.BUY if long_straddle else OptionSide.SELL

        legs = [
            OptionLeg(symbol, expiration, strike, OptionType.CALL, call_side, contracts),
            OptionLeg(symbol, expiration, strike, OptionType.PUT, put_side, contracts),
        ]

        return OptionPosition(
            strategy=OptionStrategy.STRADDLE,
            legs=legs,
            entry_time=datetime.now(ET),
            entry_premium=0,
            max_profit=float('inf') if long_straddle else 0,
            max_loss=0 if long_straddle else float('inf'),
            breakeven=[strike],
        )

    @staticmethod
    def bull_call_spread(
        symbol: str,
        long_call_strike: float,
        short_call_strike: float,
        expiration: str,
        contracts: int = 1
    ) -> OptionPosition:
        """
        Bull call spread: long lower strike call, short higher strike call
        Defined risk, lower cost than long call
        Max profit: difference between strikes minus debit
        """
        legs = [
            OptionLeg(symbol, expiration, long_call_strike, OptionType.CALL, OptionSide.BUY, contracts),
            OptionLeg(symbol, expiration, short_call_strike, OptionType.CALL, OptionSide.SELL, contracts),
        ]

        width = short_call_strike - long_call_strike

        return OptionPosition(
            strategy=OptionStrategy.BULL_CALL_SPREAD,
            legs=legs,
            entry_time=datetime.now(ET),
            entry_premium=0,
            max_profit=width * 100 * contracts,
            max_loss=0,  # Limited to debit paid
            breakeven=[long_call_strike],
        )

# ============================================================================
# ALPACA API INTEGRATION
# ============================================================================

def get_headers():
    """Dynamically read Alpaca API credentials"""
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        "Content-Type": "application/json"
    }

def get_base_url():
    """Dynamically read Alpaca base URL"""
    return os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

async def place_options_order(
    session: aiohttp.ClientSession,
    position: OptionPosition,
    time_in_force: str = "day"
) -> Dict[str, Any]:
    """
    Place a multi-leg options order (strategies with 1-4 legs)

    Args:
        session: aiohttp session
        position: OptionPosition with strategy and legs
        time_in_force: "day", "gtc", etc

    Returns:
        Alpaca API response with order ID and status
    """
    headers = get_headers()
    url = f"{get_base_url()}/v2/orders"

    legs_data = [leg.to_alpaca_format() for leg in position.legs]

    payload = {
        "legs": legs_data,
        "type": "market",  # or "limit"
        "time_in_force": time_in_force,
        "client_order_id": f"opt_{position.strategy.value}_{datetime.now(ET).timestamp()}",
    }

    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status not in [200, 201]:
                error = await resp.text()
                log.error(f"❌ Options order failed ({resp.status}): {error}")
                return {"error": error, "status": resp.status}

            data = await resp.json()
            log.info(f"✅ Options order placed: {position.strategy.value} | ID: {data.get('id')}")
            return data
    except Exception as e:
        log.error(f"❌ Options order exception: {e}")
        return {"error": str(e)}

async def get_option_chain(
    session: aiohttp.ClientSession,
    symbol: str,
    expiration_date: str,  # "2026-09-18"
) -> Dict[str, Any]:
    """
    Fetch option chain data from Alpaca (when available)
    Returns strikes, bid/ask prices, Greeks, IV
    """
    headers = get_headers()
    url = f"{get_base_url()}/v2/option_chains/{symbol}"

    params = {
        "expiration_date": expiration_date,
    }

    try:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                log.warning(f"⚠️  Option chain unavailable ({resp.status})")
                return {}

            data = await resp.json()
            return data
    except Exception as e:
        log.warning(f"⚠️  Option chain fetch failed: {e}")
        return {}

async def close_options_position(
    session: aiohttp.ClientSession,
    order_id: str,
) -> Dict[str, Any]:
    """Close (liquidate) an open options position"""
    headers = get_headers()
    url = f"{get_base_url()}/v2/orders/{order_id}"

    try:
        async with session.delete(url, headers=headers) as resp:
            if resp.status not in [200, 204]:
                error = await resp.text()
                log.error(f"❌ Close position failed ({resp.status}): {error}")
                return {"error": error}

            log.info(f"✅ Options position closed: {order_id}")
            return {"status": "closed", "order_id": order_id}
    except Exception as e:
        log.error(f"❌ Close position exception: {e}")
        return {"error": str(e)}

# ============================================================================
# RISK MANAGEMENT
# ============================================================================

def calculate_position_sizing(
    account_balance: float,
    max_loss_per_trade: float = 0.02,  # 2% of account
    position: Optional[OptionPosition] = None
) -> int:
    """
    Calculate how many contracts to trade based on account size and risk

    For spreads with defined max loss, size based on max_loss
    For unlimited risk (long calls), size based on premium
    """
    risk_amount = account_balance * max_loss_per_trade

    if position and position.max_loss > 0:
        # Defined risk strategy (spreads, etc)
        contracts = max(1, int(risk_amount / position.max_loss))
    else:
        # Undefined risk (long calls/puts) — use fixed premium per contract
        assumed_premium_per_contract = 100  # $100 per contract assumption
        contracts = max(1, int(risk_amount / assumed_premium_per_contract))

    return contracts

def should_close_position(
    entry_premium: float,
    current_value: float,
    max_loss_pct: float = 0.20,  # Close at 20% loss
    profit_target_pct: float = 0.50,  # Close at 50% profit
) -> tuple[bool, str]:
    """
    Determine if a position should be closed early

    Returns:
        (should_close, reason)
    """
    if entry_premium == 0:
        return False, "No entry premium"

    pnl_pct = (current_value - entry_premium) / entry_premium

    if pnl_pct <= -max_loss_pct:
        return True, f"Max loss hit ({pnl_pct:.1%})"

    if pnl_pct >= profit_target_pct:
        return True, f"Profit target hit ({pnl_pct:.1%})"

    return False, ""

# ============================================================================
# INTEGRATION WITH PROP BOT
# ============================================================================

async def run_options_scanner(
    session: aiohttp.ClientSession,
    symbol: str,
    current_price: float,
    rsi: float,
    iv_percentile: float,  # 0-100
) -> Optional[OptionPosition]:
    """
    Scan for options entry opportunities based on price action + volatility

    Entry logic:
    - RSI < 35: Long call or call spread (oversold reversal)
    - RSI > 65: Long put or put spread (overbought reversal)
    - IV high (>70): Sell volatility (spreads, short straddles)
    - IV low (<30): Buy volatility (long straddles, long calls/puts)
    """

    if not (10 <= iv_percentile <= 90):
        return None  # Only trade within normal IV range

    # Find next expiration (typically 21-45 DTE recommended)
    today = datetime.now(ET).date()
    default_expiration = (datetime.now(ET) + timedelta(days=30)).strftime("%Y-%m-%d")

    # 1. RSI-based directional entries
    if rsi < 30:
        # Oversold: long call bullish bet
        strike = int(current_price * 1.02)  # Strike ~2% OTM
        log.info(f"📊 Options signal: {symbol} oversold (RSI {rsi:.1f}) → Long Call ${strike}")
        return OptionStrategyBuilder.long_call(symbol, strike, default_expiration, contracts=1)

    elif rsi > 70:
        # Overbought: long put bearish bet
        strike = int(current_price * 0.98)  # Strike ~2% OTM
        log.info(f"📊 Options signal: {symbol} overbought (RSI {rsi:.1f}) → Long Put ${strike}")
        return OptionStrategyBuilder.long_put(symbol, strike, default_expiration, contracts=1)

    # 2. IV-based volatility entries
    if iv_percentile > 80:
        # High IV: sell volatility with iron condor
        atm = int(current_price)
        log.info(f"📊 Options signal: {symbol} high IV ({iv_percentile:.0f}) → Iron Condor")
        return OptionStrategyBuilder.iron_condor(
            symbol,
            short_call_strike=atm + 5,
            long_call_strike=atm + 10,
            short_put_strike=atm - 5,
            long_put_strike=atm - 10,
            expiration=default_expiration,
            contracts=1
        )

    elif iv_percentile < 20:
        # Low IV: buy volatility with long straddle
        atm = int(current_price)
        log.info(f"📊 Options signal: {symbol} low IV ({iv_percentile:.0f}) → Long Straddle")
        return OptionStrategyBuilder.straddle(symbol, atm, default_expiration, contracts=1)

    return None

# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Create an iron condor position
    position = OptionStrategyBuilder.iron_condor(
        symbol="SPY",
        short_call_strike=500,
        long_call_strike=505,
        short_put_strike=495,
        long_put_strike=490,
        expiration="2026-09-18",
        contracts=1
    )

    print(f"Strategy: {position.strategy.value}")
    print(f"Legs: {len(position.legs)}")
    print(f"Max Loss: ${position.max_loss:.0f}")
    print(f"Max Profit: ${position.max_profit:.0f}")

    # Greeks estimation
    greeks = estimate_option_greeks(
        spot=500,
        strike=500,
        expiration_days=30,
        option_type=OptionType.CALL,
    )
    print(f"\nGreeks (ATM Call, 30 DTE):")
    print(f"  Delta: {greeks.delta:.2f}")
    print(f"  Gamma: {greeks.gamma:.4f}")
    print(f"  Theta: ${greeks.theta:.2f}/day")
    print(f"  Vega: ${greeks.vega:.2f} per 1% IV")
