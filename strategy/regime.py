"""
Layer 1: Regime Detection
Owner: Narhen

Detects market regime: TRENDING / SIDEWAYS / VOLATILE
Uses: ATR, ADX, BB width, market breadth, Fear & Greed, funding rate
"""

import numpy as np
import pandas as pd
from config import (
    ATR_PERIOD, ADX_PERIOD, BB_PERIOD, BB_STD,
    ADX_TREND_THRESHOLD, ADX_NOTREND_THRESHOLD,
    BREADTH_BULLISH, BREADTH_BEARISH,
)


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Average True Range — measures volatility per candle."""
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low - close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> float:
    """
    Average Directional Index — measures trend strength (not direction).
    ADX > 25 = trending, ADX < 20 = sideways.
    """
    high = df['high']
    low = df['low']
    close = df['close']

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    adx = dx.rolling(window=period).mean()

    return adx.iloc[-1] if not adx.empty and not np.isnan(adx.iloc[-1]) else 0.0


def calculate_bb_width(df: pd.DataFrame, period: int = BB_PERIOD, num_std: float = BB_STD) -> float:
    """Bollinger Band width — narrow = quiet, wide = volatile."""
    close = df['close']
    ma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    width = (upper - lower) / ma
    return width.iloc[-1] if not width.empty and not np.isnan(width.iloc[-1]) else 0.0


def detect_regime(df: pd.DataFrame, fear_greed: int = 50,
                  funding_rate: float = 0.0, breadth: float = 0.5) -> str:
    """
    Layer 1: Detect current market regime.

    Args:
        df: DataFrame with OHLCV columns (at least 'high', 'low', 'close')
        fear_greed: Fear & Greed index 0-100 (from Alankritha's fetcher)
        funding_rate: BTC funding rate (from Alankritha's fetcher)
        breadth: Market breadth 0.0-1.0 (from Alankritha's fetcher)

    Returns:
        'TRENDING' | 'SIDEWAYS' | 'VOLATILE'
    """
    if len(df) < max(ATR_PERIOD, ADX_PERIOD, BB_PERIOD) + 5:
        return 'SIDEWAYS'  # Not enough data, default to cautious

    # Calculate indicators
    adx = calculate_adx(df)
    bb_width = calculate_bb_width(df)

    # ATR percentile (is current volatility high/low vs recent history?)
    atr_series = calculate_atr(df)
    current_atr = atr_series.iloc[-1]
    if len(atr_series.dropna()) > 50:
        atr_pct = (atr_series.dropna() < current_atr).mean()
    else:
        atr_pct = 0.5

    # Decision logic
    # VOLATILE only on extreme ATR — breadth no longer blocks trading entirely
    # In bear markets breadth stays <40% for weeks, but BTC still has tradeable ranges
    is_volatile = (
        atr_pct > 0.92              # ATR in top 8% only
    )

    is_trending = (
        adx > ADX_TREND_THRESHOLD and   # Strong directional movement
        atr_pct >= 0.20 and              # Not dead quiet
        atr_pct <= 0.92                  # Not extreme volatility
    )

    is_sideways = (
        adx < ADX_NOTREND_THRESHOLD     # No trend
    )

    # Priority: VOLATILE > TRENDING > SIDEWAYS
    if is_volatile:
        return 'VOLATILE'
    elif is_trending:
        return 'TRENDING'
    else:
        return 'SIDEWAYS'
