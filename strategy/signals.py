"""
Layer 2: Signal Generation
Owner: Narhen

Generates BUY/SELL/HOLD signals based on current regime.
- TRENDING → Donchian breakout, EMA alignment, MACD
- SIDEWAYS → RSI oversold/overbought, Bollinger Band touch, z-score
- VOLATILE → almost nothing passes
"""

import logging
import numpy as np
import pandas as pd
from config import (
    DONCHIAN_UPPER_PERIOD, DONCHIAN_LOWER_PERIOD,
    EMA_FAST, EMA_MID, EMA_SLOW,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
)

log = logging.getLogger(__name__)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.inf)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series):
    ema_fast = _ema(close, MACD_FAST)
    ema_slow = _ema(close, MACD_SLOW)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, MACD_SIGNAL)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _trending_signals(df: pd.DataFrame) -> dict:
    """Momentum signals for trending regime."""
    close = df['close']
    current_price = close.iloc[-1]

    # Donchian breakout — require previous bar INSIDE channel, current bar OUTSIDE
    # Prevents phantom signals from bootstrap→live price gap
    donchian_high = close.rolling(window=DONCHIAN_UPPER_PERIOD).max()
    donchian_low = close.rolling(window=DONCHIAN_LOWER_PERIOD).min()

    broke_upper = (close.iloc[-2] < donchian_high.iloc[-2]) and \
                  (current_price >= donchian_high.iloc[-1])
    broke_lower = (close.iloc[-2] > donchian_low.iloc[-2]) and \
                  (current_price <= donchian_low.iloc[-1])

    # EMA alignment
    ema_fast = _ema(close, EMA_FAST).iloc[-1]
    ema_mid = _ema(close, EMA_MID).iloc[-1]
    ema_slow = _ema(close, EMA_SLOW).iloc[-1]
    bullish_alignment = ema_fast > ema_mid > ema_slow
    bearish_alignment = ema_fast < ema_mid < ema_slow

    # MACD
    _, _, histogram = _macd(close)
    macd_bullish = histogram.iloc[-1] > 0 and histogram.iloc[-1] > histogram.iloc[-2]
    macd_bearish = histogram.iloc[-1] < 0 and histogram.iloc[-1] < histogram.iloc[-2]

    # Extreme oversold override — in a bear TRENDING regime, RSI < 30 is
    # capitulation. Generate BUY for bounce trade even against the trend.
    # Without this, TRENDING + bear market = only SELL signals = stuck forever.
    rsi_series = _rsi(close, RSI_PERIOD)
    rsi_val = rsi_series.iloc[-1]
    if rsi_val < 30:
        return {'direction': 'BUY', 'source': 'trending_oversold_bounce',
                'macd_confirms': False}

    # Decision
    if broke_upper and bullish_alignment:
        return {'direction': 'BUY', 'source': 'donchian_breakout',
                'macd_confirms': macd_bullish}
    elif broke_upper and macd_bullish:
        return {'direction': 'BUY', 'source': 'donchian_macd',
                'macd_confirms': True}
    elif broke_lower:
        return {'direction': 'SELL', 'source': 'donchian_exit',
                'macd_confirms': macd_bearish}
    elif bearish_alignment and macd_bearish:
        return {'direction': 'SELL', 'source': 'ema_macd_bearish',
                'macd_confirms': True}
    else:
        return {'direction': 'HOLD', 'source': 'no_signal',
                'macd_confirms': False}


def _sideways_signals(df: pd.DataFrame) -> dict:
    """Mean-reversion signals for sideways regime."""
    close = df['close']
    current_price = close.iloc[-1]

    # RSI
    rsi_series = _rsi(close, RSI_PERIOD)
    rsi_val = rsi_series.iloc[-1]

    # Bollinger Bands
    ma = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    lower_band = (ma - 2 * std).iloc[-1]
    upper_band = (ma + 2 * std).iloc[-1]

    # Z-score
    mean_price = close.rolling(window=20).mean().iloc[-1]
    std_price = close.rolling(window=20).std().iloc[-1]
    z_score = (current_price - mean_price) / std_price if std_price > 0 else 0

    # Suppress BB signals while bootstrap data dominates (stale Binance prices)
    from data.candle_builder import BOOTSTRAP_DOMINANT
    if BOOTSTRAP_DOMINANT:
        # BB/z-score unreliable — only use RSI (which is relative, not price-anchored)
        if rsi_val < RSI_OVERSOLD:
            return {'direction': 'BUY', 'source': 'rsi_oversold_bootstrap'}
        elif rsi_val > RSI_OVERBOUGHT:
            return {'direction': 'SELL', 'source': 'rsi_overbought_bootstrap'}
        return {'direction': 'HOLD', 'source': 'bootstrap_stale'}

    # Decision — BB touch alone is sufficient in SIDEWAYS
    # Price below lower BB is the oversold signal; RSI confirms but doesn't gate
    if current_price <= lower_band:
        return {'direction': 'BUY', 'source': 'bb_oversold'}
    elif rsi_val < RSI_OVERSOLD or z_score < -1.5:
        return {'direction': 'BUY', 'source': 'mean_reversion_buy'}
    elif current_price >= upper_band:
        return {'direction': 'SELL', 'source': 'bb_overbought'}
    elif rsi_val > RSI_OVERBOUGHT or z_score > 1.5:
        return {'direction': 'SELL', 'source': 'mean_reversion_sell'}
    else:
        return {'direction': 'HOLD', 'source': 'no_signal'}


def generate_signal(df: pd.DataFrame, regime: str) -> dict:
    """
    Layer 2: Generate trading signal based on current regime.

    Args:
        df: DataFrame with OHLCV columns
        regime: 'TRENDING' | 'SIDEWAYS' | 'VOLATILE' (from Layer 1)

    Returns:
        {'direction': 'BUY'/'SELL'/'HOLD', 'source': str}
    """
    if len(df) < EMA_SLOW + 5:
        return {'direction': 'HOLD', 'source': 'insufficient_data'}

    if regime == 'TRENDING':
        return _trending_signals(df)
    elif regime == 'SIDEWAYS':
        return _sideways_signals(df)
    else:  # VOLATILE
        return {'direction': 'HOLD', 'source': 'volatile_regime_skip'}
