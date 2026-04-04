"""
Technical Indicators
Simple math functions that analyze price data and tell your bot when to buy/sell.

HOW THIS WORKS (for beginners):
- We collect recent prices (e.g., last 20 prices)
- We calculate signals from this data
- These signals tell us: is the price going UP, DOWN, or SIDEWAYS?
"""


def moving_average(prices: list, period: int) -> float:
    """
    Moving Average (MA): Average of the last N prices.

    If current price > MA → price is trending UP (bullish)
    If current price < MA → price is trending DOWN (bearish)

    Example: prices = [100, 102, 101, 103, 105], period = 3
    MA = (101 + 103 + 105) / 3 = 103
    """
    if len(prices) < period:
        return 0.0
    return sum(prices[-period:]) / period


def exponential_moving_average(prices: list, period: int) -> float:
    """
    Exponential Moving Average (EMA): Like MA but gives MORE weight to recent prices.
    Reacts faster to price changes than simple MA.
    """
    if len(prices) < period:
        return 0.0

    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start with simple MA

    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def rsi(prices: list, period: int = 14) -> float:
    """
    Relative Strength Index (RSI): Measures if something is overbought or oversold.

    RSI > 70 → OVERBOUGHT (price went up too much, might come down) → SELL signal
    RSI < 30 → OVERSOLD (price went down too much, might go up) → BUY signal
    RSI 30-70 → NEUTRAL

    Scale: 0 to 100
    """
    if len(prices) < period + 1:
        return 50.0  # Neutral if not enough data

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    # Use last 'period' values
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0  # All gains, no losses

    rs = avg_gain / avg_loss
    rsi_value = 100 - (100 / (1 + rs))

    return rsi_value


def bollinger_bands(prices: list, period: int = 20, num_std: float = 2.0):
    """
    Bollinger Bands: Shows if price is unusually high or low.

    Returns: (upper_band, middle_band, lower_band)

    Price hits UPPER band → might be too high → SELL signal
    Price hits LOWER band → might be too low → BUY signal
    Price near MIDDLE → normal

    Think of it as: the price usually stays within these bands.
    If it goes outside, something unusual is happening.
    """
    if len(prices) < period:
        avg = sum(prices) / len(prices) if prices else 0
        return avg, avg, avg

    recent = prices[-period:]
    middle = sum(recent) / period

    # Standard deviation (how spread out the prices are)
    variance = sum((p - middle) ** 2 for p in recent) / period
    std_dev = variance ** 0.5

    upper = middle + (num_std * std_dev)
    lower = middle - (num_std * std_dev)

    return upper, middle, lower


def macd(prices: list):
    """
    MACD (Moving Average Convergence Divergence):
    Shows momentum and trend direction.

    Returns: (macd_line, signal_line, histogram)

    MACD crosses ABOVE signal → BUY (momentum shifting up)
    MACD crosses BELOW signal → SELL (momentum shifting down)
    Histogram > 0 → bullish momentum
    Histogram < 0 → bearish momentum
    """
    if len(prices) < 26:
        return 0.0, 0.0, 0.0

    if len(prices) < 26 + 9:
        # Not enough data for signal line
        ema_12 = exponential_moving_average(prices, 12)
        ema_26 = exponential_moving_average(prices, 26)
        macd_line = ema_12 - ema_26
        return macd_line, 0.0, macd_line

    # Calculate MACD line for each bar to build signal line from real EMA
    macd_values = []
    for i in range(26, len(prices) + 1):
        ema_12 = exponential_moving_average(prices[:i], 12)
        ema_26 = exponential_moving_average(prices[:i], 26)
        macd_values.append(ema_12 - ema_26)

    macd_line = macd_values[-1]

    # Signal line: 9-period EMA of MACD values
    signal_line = macd_values[0]
    multiplier = 2 / (9 + 1)
    for val in macd_values[1:]:
        signal_line = (val - signal_line) * multiplier + signal_line

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def price_change_pct(prices: list, lookback: int = 1) -> float:
    """
    Simple percentage change over last N periods.

    Example: price was 100, now 105 → returns 0.05 (5%)
    """
    if len(prices) < lookback + 1:
        return 0.0

    old_price = prices[-(lookback + 1)]
    new_price = prices[-1]

    if old_price == 0:
        return 0.0

    return (new_price - old_price) / old_price


def volatility(prices: list, period: int = 20) -> float:
    """
    Volatility: How much the price is jumping around.

    High volatility → risky, big moves
    Low volatility → stable, small moves

    Returns standard deviation of returns.
    """
    if len(prices) < period + 1:
        return 0.0

    returns = []
    recent = prices[-period - 1:]
    for i in range(1, len(recent)):
        if recent[i - 1] != 0:
            ret = (recent[i] - recent[i - 1]) / recent[i - 1]
            returns.append(ret)

    if not returns:
        return 0.0

    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)

    return variance ** 0.5
