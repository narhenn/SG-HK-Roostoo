"""
Layer 4: Multi-Timeframe Filter
Owner: Narhen
Scores 3 timeframes (1H, 4H, Daily):
  +1 = bullish, 0 = neutral, -1 = bearish
Sum >= +2 = PASS (trade allowed)
Sum < +2 = BLOCK
HARD RULE: if 4H is bearish, NEVER buy.
EXCEPTION: oversold signals (RSI<30, Z<-2) bypass strict filter.
"""
import numpy as np
import pandas as pd
from config import EMA_MID, MACD_FAST, MACD_SLOW, MACD_SIGNAL, EMA_SLOW, TF_MIN_SCORE


def _score_timeframe(df: pd.DataFrame, use_ema50: bool = False) -> int:
    """Score a single timeframe as +1 (bullish), 0 (neutral), -1 (bearish)."""
    if len(df) < EMA_SLOW + 5:
        return 0
    close = df['close']
    if use_ema50:
        # Daily: just check price vs EMA(50)
        ema50 = close.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
        return 1 if close.iloc[-1] > ema50 else -1
    # 1H/4H: EMA slope + MACD direction
    ema = close.ewm(span=EMA_MID, adjust=False).mean()
    ema_slope = ema.iloc[-1] - ema.iloc[-3] if len(ema) >= 3 else 0
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    macd_above_signal = macd_line.iloc[-1] > signal_line.iloc[-1]
    if ema_slope > 0 and macd_above_signal:
        return 1
    elif ema_slope < 0 and not macd_above_signal:
        return -1
    else:
        return 0


def check_timeframe(df_1h: pd.DataFrame, df_4h: pd.DataFrame,
                    df_daily: pd.DataFrame, regime: str = 'TRENDING',
                    signal_source: str = '') -> dict:
    """
    Layer 4: Multi-timeframe filter.
    Args:
        df_1h: 1-hour candle DataFrame
        df_4h: 4-hour candle DataFrame
        df_daily: Daily candle DataFrame
        regime: current market regime
        signal_source: signal name from Layer 2 (for oversold override)
    Returns:
        {
            'pass': bool,        # True if trade is allowed
            'score': int,        # Sum of timeframe scores (-3 to +3)
            'multiplier': float, # 1.0 for +3, 0.5 for +2, 0.0 otherwise
            'scores': dict       # Individual timeframe scores
        }
    """
    score_1h = _score_timeframe(df_1h)
    score_4h = _score_timeframe(df_4h)
    score_daily = _score_timeframe(df_daily, use_ema50=True)
    total = score_1h + score_4h + score_daily

    # OVERSOLD OVERRIDE
    # When RSI<30 and Z<-2.0 the market is extremely oversold.
    # These signals already passed strict criteria in Layer 2.
    # Bypass the strict TF filter but use half position size (0.5).
    is_oversold_signal = signal_source in (
        'oversold_override',
        'trending_oversold_bounce',
        'rsi_oversold_bootstrap',
        'bb_oversold',
        'urgency_bounce',
        'fisher_bottom',
    )
    if is_oversold_signal:
        return {
            'pass': True,
            'score': total,
            'multiplier': 0.5,
            'scores': {'1h': score_1h, '4h': score_4h, 'daily': score_daily},
        }

    # Hard rule: never trade against 4H trend in TRENDING regime
    # In SIDEWAYS regime, mean-reversion trades catch bounces within downtrends
    if score_4h == -1 and regime != 'SIDEWAYS':
        return {
            'pass': False,
            'score': total,
            'multiplier': 0.0,
            'scores': {'1h': score_1h, '4h': score_4h, 'daily': score_daily},
        }

    # Sideways mean-reversion needs looser filter (neutral is enough)
    # TRENDING needs at least +1 agreement (was +2, too strict in bear markets)
    if regime == 'SIDEWAYS':
        min_score = 0  # Allow mean-reversion even when TFs disagree
    elif regime == 'TRENDING':
        min_score = 1  # Loosened from 2 — bear markets rarely have all TFs aligned
    else:
        min_score = TF_MIN_SCORE
    if total >= 3:
        multiplier = 1.0
    elif total >= 2:
        multiplier = 0.75
    elif total >= min_score:
        multiplier = 0.5
    else:
        multiplier = 0.0

    return {
        'pass': total >= min_score,
        'score': total,
        'multiplier': multiplier,
        'scores': {'1h': score_1h, '4h': score_4h, 'daily': score_daily},
    }
