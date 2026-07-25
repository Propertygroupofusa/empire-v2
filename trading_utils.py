"""Shared utilities for trading bots - RSI, metrics, helpers."""
import logging

log = logging.getLogger("trading_utils")


def compute_rsi(closes: list, period: int = 14) -> float:
    """Calculate RSI from close prices.

    Args:
        closes: List of close prices
        period: RSI period (default 14)

    Returns:
        RSI value 0-100, or None if insufficient data
    """
    if len(closes) < period + 1:
        return None

    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return round(100 - (100 / (1 + rs)), 1)


def sma(prices: list, n: int) -> float:
    """Simple moving average."""
    if len(prices) < n:
        return None
    return sum(prices[-n:]) / n


def ema(prices: list, n: int) -> float:
    """Exponential moving average."""
    if len(prices) < n:
        return None
    multiplier = 2 / (n + 1)
    ema_val = sum(prices[:n]) / n
    for price in prices[n:]:
        ema_val = price * multiplier + ema_val * (1 - multiplier)
    return ema_val


def calculate_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range for volatility."""
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return None

    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    return sum(tr_list[-period:]) / period


def validate_ohlcv_bars(bars: list) -> bool:
    """Validate OHLCV bar data structure."""
    if not isinstance(bars, list) or len(bars) == 0:
        return False
    for bar in bars:
        if not isinstance(bar, dict):
            return False
        required = {"o", "h", "l", "c", "v"}
        if not required.issubset(bar.keys()):
            return False
        try:
            for key in required:
                float(bar[key])
        except (ValueError, TypeError):
            return False
    return True


def format_trade_log(symbol: str, side: str, qty: float, price: float, reason: str = "") -> str:
    """Format trade log entry."""
    emoji = "🔴" if side.upper() == "SELL" else "🟢"
    msg = f"{emoji} {side.upper()} {qty} {symbol} @ ${price:.2f}"
    if reason:
        msg += f" ({reason})"
    return msg
