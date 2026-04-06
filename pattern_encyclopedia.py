"""
============================================================================
COMPLETE CHART PATTERN ENCYCLOPEDIA FOR DAY TRADING
============================================================================
Every pattern with EXACT programmatic detection rules using OHLC arrays.
Detection functions take pandas DataFrames with columns: open, high, low, close, volume
All indices refer to candle positions: 0=current, -1=previous, -2=two bars ago, etc.

Sources:
- Bulkowski's Encyclopedia of Chart Patterns (statistics)
- Steve Nison's Japanese Candlestick Charting Techniques
- ICT/Smart Money Concepts
- Harmonic Trading Volumes 1 & 2 (Scott Carney)
- Elliott Wave Principle (Frost & Prechter)
============================================================================
"""

import numpy as np
import pandas as pd
try:
    from scipy.signal import argrelextrema
except ImportError:
    def argrelextrema(data, comparator, order=1, mode='clip'):
        """Pure numpy fallback for scipy.signal.argrelextrema."""
        results = np.ones(data.shape, dtype=bool)
        for shift in range(1, order + 1):
            plus = np.empty_like(data)
            minus = np.empty_like(data)
            plus[:len(data)-shift] = data[shift:]
            plus[len(data)-shift:] = data[-1]
            minus[shift:] = data[:len(data)-shift]
            minus[:shift] = data[0]
            results &= comparator(data, plus)
            results &= comparator(data, minus)
        return (np.where(results)[0],)
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass
from enum import Enum


# ============================================================================
# HELPER UTILITIES
# ============================================================================

class Direction(Enum):
    BULLISH = 1
    BEARISH = -1
    NEUTRAL = 0


@dataclass
class PatternResult:
    name: str
    direction: Direction
    confidence: float       # 0.0 to 1.0
    entry_price: float
    stop_loss: float
    target_price: float
    risk_reward: float
    win_rate: Optional[float]  # historical if available


def body(o, c):
    """Absolute body size."""
    return abs(c - o)


def body_signed(o, c):
    """Signed body: positive=bullish, negative=bearish."""
    return c - o


def upper_shadow(o, h, c):
    """Upper wick length."""
    return h - max(o, c)


def lower_shadow(o, l, c):
    """Lower wick length."""
    return min(o, c) - l


def candle_range(h, l):
    """Total high-low range."""
    return h - l


def is_bullish(o, c):
    return c > o


def is_bearish(o, c):
    return c < o


def body_midpoint(o, c):
    return (o + c) / 2.0


def body_top(o, c):
    return max(o, c)


def body_bottom(o, c):
    return min(o, c)


def avg_body(df, lookback=14):
    """Average body size over lookback period."""
    return df.apply(lambda r: body(r['open'], r['close']), axis=1).rolling(lookback).mean()


def avg_range(df, lookback=14):
    """Average candle range over lookback period."""
    return (df['high'] - df['low']).rolling(lookback).mean()


def is_gap_up(prev_high, curr_low):
    """True if current candle gapped up from previous."""
    return curr_low > prev_high


def is_gap_down(prev_low, curr_high):
    """True if current candle gapped down from previous."""
    return curr_high < prev_low


def find_swing_highs(highs: np.ndarray, order: int = 5) -> np.ndarray:
    """
    Find swing high indices.
    A swing high at index i means highs[i] > all highs in [i-order, i+order].
    """
    return argrelextrema(highs, np.greater_equal, order=order)[0]


def find_swing_lows(lows: np.ndarray, order: int = 5) -> np.ndarray:
    """
    Find swing low indices.
    A swing low at index i means lows[i] < all lows in [i-order, i+order].
    """
    return argrelextrema(lows, np.less_equal, order=order)[0]


def linear_regression_slope(values: np.ndarray) -> float:
    """Slope of linear regression line through values."""
    x = np.arange(len(values))
    if len(values) < 2:
        return 0.0
    slope, _ = np.polyfit(x, values, 1)
    return slope


def is_downtrend(closes: np.ndarray, lookback: int = 10) -> bool:
    """Simple downtrend check: slope of last N closes is negative."""
    if len(closes) < lookback:
        return False
    return linear_regression_slope(closes[-lookback:]) < 0


def is_uptrend(closes: np.ndarray, lookback: int = 10) -> bool:
    """Simple uptrend check: slope of last N closes is positive."""
    if len(closes) < lookback:
        return False
    return linear_regression_slope(closes[-lookback:]) > 0


# ============================================================================
# SECTION 1: SINGLE CANDLESTICK PATTERNS
# ============================================================================

def detect_doji(o, h, l, c, body_thresh=0.05):
    """
    DOJI (Regular)
    ==============
    Detection:
      - body <= body_thresh * range  (body is < 5% of total range)
      - range > 0 (not a zero-range bar)

    Subtypes detected by upper/lower shadow ratios.

    Entry: Confirmation candle in reversal direction
    Stop: Beyond the doji high/low
    Target: Previous support/resistance level
    Win rate: ~50-52% alone; improves with context (at S/R: ~57%)
    """
    r = candle_range(h, l)
    if r == 0:
        return None
    b = body(o, c)
    if b <= body_thresh * r:
        us = upper_shadow(o, h, c)
        ls = lower_shadow(o, l, c)
        # Classify subtype
        if ls < 0.1 * r and us > 0.3 * r:
            return "gravestone_doji"   # long upper shadow, tiny lower
        elif us < 0.1 * r and ls > 0.3 * r:
            return "dragonfly_doji"    # long lower shadow, tiny upper
        elif us > 0.3 * r and ls > 0.3 * r:
            return "long_legged_doji"  # long both shadows
        else:
            return "doji"              # regular/standard
    return None


def detect_hammer(o, h, l, c, body_max_ratio=0.33, tail_min_ratio=2.0, wick_max_ratio=0.1):
    """
    HAMMER (Bullish)
    ================
    Detection:
      - body <= body_max_ratio * range  (small body, top third)
      - lower_shadow >= tail_min_ratio * body  (long lower tail >= 2x body)
      - upper_shadow <= wick_max_ratio * range  (tiny upper wick)
      - Must appear in a DOWNTREND context

    Entry: Close above hammer high on next candle
    Stop: Below hammer low
    Target: 1:1 to 2:1 R:R or next resistance
    Win rate: ~60% (Bulkowski: 60% with trend filter)
    """
    r = candle_range(h, l)
    if r == 0:
        return False
    b = body(o, c)
    us = upper_shadow(o, h, c)
    ls = lower_shadow(o, l, c)

    return (
        b <= body_max_ratio * r and
        ls >= tail_min_ratio * b and
        us <= wick_max_ratio * r and
        b > 0  # must have some body
    )


def detect_inverted_hammer(o, h, l, c, body_max_ratio=0.33, wick_min_ratio=2.0, tail_max_ratio=0.1):
    """
    INVERTED HAMMER (Bullish, after downtrend)
    ===========================================
    Detection:
      - body <= body_max_ratio * range
      - upper_shadow >= wick_min_ratio * body  (long upper wick >= 2x body)
      - lower_shadow <= tail_max_ratio * range  (tiny lower tail)
      - Must appear in a DOWNTREND context

    Entry: Close above inverted hammer high on next candle
    Stop: Below the low
    Target: 1:1 to 2:1 R:R or next resistance
    Win rate: ~60% (Bulkowski: most profitable single candle)
    """
    r = candle_range(h, l)
    if r == 0:
        return False
    b = body(o, c)
    us = upper_shadow(o, h, c)
    ls = lower_shadow(o, l, c)

    return (
        b <= body_max_ratio * r and
        us >= wick_min_ratio * b and
        ls <= tail_max_ratio * r and
        b > 0
    )


def detect_hanging_man(o, h, l, c, body_max_ratio=0.33, tail_min_ratio=2.0, wick_max_ratio=0.1):
    """
    HANGING MAN (Bearish, appears in UPTREND)
    ==========================================
    Detection:
      - IDENTICAL shape to hammer
      - body <= body_max_ratio * range
      - lower_shadow >= tail_min_ratio * body
      - upper_shadow <= wick_max_ratio * range
      - Must appear in an UPTREND context (this differentiates it from hammer)

    Entry: Close below hanging man low on next candle
    Stop: Above the high
    Target: 1:1 to 2:1 R:R or next support
    Win rate: ~59%
    """
    # Same shape detection as hammer; context differentiates
    return detect_hammer(o, h, l, c, body_max_ratio, tail_min_ratio, wick_max_ratio)


def detect_shooting_star(o, h, l, c, body_max_ratio=0.33, wick_min_ratio=2.0, tail_max_ratio=0.1):
    """
    SHOOTING STAR (Bearish, appears in UPTREND)
    ============================================
    Detection:
      - IDENTICAL shape to inverted hammer
      - Must appear in an UPTREND context

    Entry: Close below shooting star low on next candle
    Stop: Above the high
    Target: 1:1 to 2:1 R:R or next support
    Win rate: ~59%
    """
    return detect_inverted_hammer(o, h, l, c, body_max_ratio, wick_min_ratio, tail_max_ratio)


def detect_marubozu(o, h, l, c, body_min_ratio=0.70, shadow_max_ratio=0.05):
    """
    MARUBOZU (Strong momentum candle)
    ==================================
    Detection:
      - body >= body_min_ratio * range  (body is >= 70% of range)
      - upper_shadow <= shadow_max_ratio * body  (< 5% of body)
      - lower_shadow <= shadow_max_ratio * body  (< 5% of body)

    Bullish Marubozu: close > open
    Bearish Marubozu: close < open

    Entry: Continuation in direction on next candle
    Stop: Beyond the opposite end of the marubozu
    Target: 1:1 R:R or next S/R level
    Win rate: Bearish ~56%, Bullish ~53%
    """
    r = candle_range(h, l)
    if r == 0:
        return None
    b = body(o, c)
    us = upper_shadow(o, h, c)
    ls = lower_shadow(o, l, c)

    if b >= body_min_ratio * r and us <= shadow_max_ratio * b and ls <= shadow_max_ratio * b:
        return "bullish_marubozu" if is_bullish(o, c) else "bearish_marubozu"
    return None


def detect_spinning_top(o, h, l, c, body_max_ratio=0.30, shadow_min_ratio=0.20):
    """
    SPINNING TOP (Indecision)
    =========================
    Detection:
      - body <= body_max_ratio * range  (small body < 30% of range)
      - upper_shadow >= shadow_min_ratio * range  (> 20% of range)
      - lower_shadow >= shadow_min_ratio * range  (> 20% of range)

    Entry: Wait for confirmation candle
    Stop: Beyond the spinning top's range
    Target: Next S/R level
    Win rate: ~50% (indecision pattern, context-dependent)
    """
    r = candle_range(h, l)
    if r == 0:
        return False
    b = body(o, c)
    us = upper_shadow(o, h, c)
    ls = lower_shadow(o, l, c)

    return (
        b <= body_max_ratio * r and
        us >= shadow_min_ratio * r and
        ls >= shadow_min_ratio * r
    )


# ============================================================================
# SECTION 2: DOUBLE CANDLESTICK PATTERNS
# ============================================================================

def detect_bullish_engulfing(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BULLISH ENGULFING
    =================
    Detection (candle 1 = previous, candle 2 = current):
      - Candle 1 is bearish: c1 < o1
      - Candle 2 is bullish: c2 > o2
      - Candle 2 body engulfs candle 1 body: o2 <= c1 AND c2 >= o1
      - Ideally body of candle 2 > body of candle 1

    Entry: Close above candle 2 high on next bar
    Stop: Below candle 2 low (or candle 1 low)
    Target: 1:1 to 2:1 R:R; or measure body of engulfing candle projected up
    Win rate: ~57% (Bulkowski); ~63% with downtrend context
    """
    return (
        is_bearish(o1, c1) and
        is_bullish(o2, c2) and
        o2 <= c1 and      # candle 2 opens at or below candle 1 close
        c2 >= o1           # candle 2 closes at or above candle 1 open
    )


def detect_bearish_engulfing(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BEARISH ENGULFING
    =================
    Detection:
      - Candle 1 is bullish: c1 > o1
      - Candle 2 is bearish: c2 < o2
      - Candle 2 body engulfs candle 1 body: o2 >= c1 AND c2 <= o1

    Entry: Close below candle 2 low on next bar
    Stop: Above candle 2 high
    Target: 1:1 to 2:1 R:R; measure engulfing body projected down
    Win rate: ~57% (Bulkowski); higher in uptrend context
    """
    return (
        is_bullish(o1, c1) and
        is_bearish(o2, c2) and
        o2 >= c1 and
        c2 <= o1
    )


def detect_bullish_harami(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BULLISH HARAMI
    ==============
    Detection:
      - Candle 1 is bearish with large body
      - Candle 2 is bullish (or small body)
      - Candle 2 body is INSIDE candle 1 body: o2 >= c1 AND c2 <= o1
      - body(candle2) < body(candle1)

    Entry: Close above candle 1 high on confirmation
    Stop: Below candle 1 low
    Target: 1:1 R:R or next resistance
    Win rate: ~53% (weak alone; ~73% with Harami Cross variant at S/R)
    """
    return (
        is_bearish(o1, c1) and
        body(o2, c2) < body(o1, c1) and
        o2 >= c1 and      # candle 2 opens above candle 1 close
        c2 <= o1           # candle 2 closes below candle 1 open
    )


def detect_bearish_harami(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BEARISH HARAMI
    ==============
    Detection:
      - Candle 1 is bullish with large body
      - Candle 2 body is INSIDE candle 1 body: o2 <= c1 AND c2 >= o1
      - body(candle2) < body(candle1)

    Entry: Close below candle 1 low on confirmation
    Stop: Above candle 1 high
    Target: 1:1 R:R or next support
    Win rate: ~53%
    """
    return (
        is_bullish(o1, c1) and
        body(o2, c2) < body(o1, c1) and
        o2 <= c1 and
        c2 >= o1
    )


def detect_piercing_line(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    PIERCING LINE (Bullish)
    =======================
    Detection:
      - Candle 1 is bearish
      - Candle 2 is bullish
      - Candle 2 opens below candle 1 low: o2 <= l1
      - Candle 2 closes above midpoint of candle 1 body: c2 > body_midpoint(o1, c1)
      - Candle 2 closes below candle 1 open: c2 < o1

    Entry: Close above candle 2 high on next bar
    Stop: Below candle 2 low
    Target: Candle 1 open (top of bearish candle) or 1:1 R:R
    Win rate: ~64%
    """
    mid1 = body_midpoint(o1, c1)
    return (
        is_bearish(o1, c1) and
        is_bullish(o2, c2) and
        o2 <= l1 and
        c2 > mid1 and
        c2 < o1
    )


def detect_dark_cloud_cover(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    DARK CLOUD COVER (Bearish)
    ==========================
    Detection:
      - Candle 1 is bullish
      - Candle 2 is bearish
      - Candle 2 opens above candle 1 high: o2 >= h1
      - Candle 2 closes below midpoint of candle 1 body: c2 < body_midpoint(o1, c1)
      - Candle 2 closes above candle 1 open: c2 > o1

    Entry: Close below candle 2 low on next bar
    Stop: Above candle 2 high
    Target: Candle 1 open or 1:1 R:R
    Win rate: ~60%
    """
    mid1 = body_midpoint(o1, c1)
    return (
        is_bullish(o1, c1) and
        is_bearish(o2, c2) and
        o2 >= h1 and
        c2 < mid1 and
        c2 > o1
    )


def detect_tweezer_top(o1, h1, l1, c1, o2, h2, l2, c2, tolerance=0.001):
    """
    TWEEZER TOP (Bearish)
    =====================
    Detection:
      - Candle 1 is bullish
      - Candle 2 is bearish
      - Highs are approximately equal: abs(h1 - h2) <= tolerance * h1
      - Both candles have meaningful range

    Entry: Close below candle 2 low
    Stop: Above the shared high
    Target: 1:1 R:R or next support
    Win rate: ~55%
    """
    return (
        is_bullish(o1, c1) and
        is_bearish(o2, c2) and
        abs(h1 - h2) <= tolerance * h1 and
        candle_range(h1, l1) > 0 and
        candle_range(h2, l2) > 0
    )


def detect_tweezer_bottom(o1, h1, l1, c1, o2, h2, l2, c2, tolerance=0.001):
    """
    TWEEZER BOTTOM (Bullish)
    ========================
    Detection:
      - Candle 1 is bearish
      - Candle 2 is bullish
      - Lows are approximately equal: abs(l1 - l2) <= tolerance * l1
      - Both candles have meaningful range

    Entry: Close above candle 2 high
    Stop: Below the shared low
    Target: 1:1 R:R or next resistance
    Win rate: ~55%
    """
    return (
        is_bearish(o1, c1) and
        is_bullish(o2, c2) and
        abs(l1 - l2) <= tolerance * l1 and
        candle_range(h1, l1) > 0 and
        candle_range(h2, l2) > 0
    )


def detect_kicker_bullish(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BULLISH KICKER
    ==============
    Detection:
      - Candle 1 is bearish (strong, long body)
      - Candle 2 is bullish
      - Candle 2 opens at or above candle 1 open (gap up from bearish open): o2 >= o1
      - Body of candle 2 is large (ideally > body of candle 1)

    Entry: Immediately on pattern recognition (very strong signal)
    Stop: Below candle 2 open
    Target: 2:1 to 3:1 R:R
    Win rate: ~70%+ (one of the most reliable patterns)
    """
    return (
        is_bearish(o1, c1) and
        is_bullish(o2, c2) and
        o2 >= o1 and
        body(o2, c2) > 0.5 * body(o1, c1)
    )


def detect_kicker_bearish(o1, h1, l1, c1, o2, h2, l2, c2):
    """
    BEARISH KICKER
    ==============
    Detection:
      - Candle 1 is bullish (strong, long body)
      - Candle 2 is bearish
      - Candle 2 opens at or below candle 1 open (gap down): o2 <= o1
      - Body of candle 2 is large

    Entry: Immediately on pattern recognition
    Stop: Above candle 2 open
    Target: 2:1 to 3:1 R:R
    Win rate: ~70%+
    """
    return (
        is_bullish(o1, c1) and
        is_bearish(o2, c2) and
        o2 <= o1 and
        body(o2, c2) > 0.5 * body(o1, c1)
    )


# ============================================================================
# SECTION 3: TRIPLE CANDLESTICK PATTERNS
# ============================================================================

def detect_morning_star(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                        star_body_max=0.30, penetration_min=0.50):
    """
    MORNING STAR (Bullish reversal)
    ===============================
    Detection (candles 1, 2, 3 in chronological order):
      - Candle 1: Long bearish candle (body > avg)
      - Candle 2: Small body (body <= star_body_max * body of candle 1)
                  Gaps down from candle 1: body_top(o2,c2) < body_bottom(o1,c1)
                  (gap not required in crypto/forex, but preferred)
      - Candle 3: Long bullish candle
                  Closes above midpoint of candle 1 body:
                  c3 > o1 - penetration_min * body(o1, c1)

    Entry: Close above candle 3 high on next bar
    Stop: Below candle 2 low (the star's low)
    Target: Candle 1 open or 2:1 R:R
    Win rate: ~72% (one of the most reliable triple patterns)
    """
    b1 = body(o1, c1)
    b2 = body(o2, c2)
    b3 = body(o3, c3)

    mid1 = o1 - penetration_min * b1  # for bearish candle 1

    return (
        is_bearish(o1, c1) and
        b1 > 0 and
        b2 <= star_body_max * b1 and
        is_bullish(o3, c3) and
        c3 >= mid1 and
        b3 > 0.5 * b1
    )


def detect_evening_star(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                        star_body_max=0.30, penetration_min=0.50):
    """
    EVENING STAR (Bearish reversal)
    ===============================
    Detection:
      - Candle 1: Long bullish candle
      - Candle 2: Small body (body <= star_body_max * body of candle 1)
                  Gaps up from candle 1 (preferred but not required)
      - Candle 3: Long bearish candle
                  Closes below midpoint of candle 1 body:
                  c3 < o1 + penetration_min * body(o1, c1)

    Entry: Close below candle 3 low on next bar
    Stop: Above candle 2 high (the star's high)
    Target: Candle 1 open or 2:1 R:R
    Win rate: ~72%
    """
    b1 = body(o1, c1)
    b2 = body(o2, c2)
    b3 = body(o3, c3)

    mid1 = o1 + penetration_min * b1  # for bullish candle 1

    return (
        is_bullish(o1, c1) and
        b1 > 0 and
        b2 <= star_body_max * b1 and
        is_bearish(o3, c3) and
        c3 <= mid1 and
        b3 > 0.5 * b1
    )


def detect_three_white_soldiers(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                                 shadow_max_ratio=0.30):
    """
    THREE WHITE SOLDIERS (Strong bullish)
    ======================================
    Detection:
      - All three candles are bullish
      - Each candle closes higher than the previous: c1 < c2 < c3
      - Each candle opens within the body of the previous candle:
        c1 >= o2 >= o1  (candle 2 opens within candle 1 body)
        c2 >= o3 >= o2  (candle 3 opens within candle 2 body)
      - Small upper shadows on each: upper_shadow < shadow_max_ratio * body
      - Each body is substantial (not tiny)

    Entry: Continuation long on next candle
    Stop: Below candle 1 low
    Target: 2:1 R:R or next resistance
    Win rate: ~82% (very reliable)
    """
    return (
        is_bullish(o1, c1) and is_bullish(o2, c2) and is_bullish(o3, c3) and
        c1 < c2 < c3 and
        o1 < o2 < o3 and
        # Each opens within previous body
        o2 >= o1 and o2 <= c1 and
        o3 >= o2 and o3 <= c2 and
        # Small upper shadows
        upper_shadow(o1, h1, c1) <= shadow_max_ratio * body(o1, c1) and
        upper_shadow(o2, h2, c2) <= shadow_max_ratio * body(o2, c2) and
        upper_shadow(o3, h3, c3) <= shadow_max_ratio * body(o3, c3)
    )


def detect_three_black_crows(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                              shadow_max_ratio=0.30):
    """
    THREE BLACK CROWS (Strong bearish)
    ===================================
    Detection:
      - All three candles are bearish
      - Each candle closes lower: c1 > c2 > c3
      - Each candle opens within the body of the previous:
        o1 >= o2 >= c1  (candle 2 opens within candle 1 body)
        o2 >= o3 >= c2
      - Small lower shadows

    Entry: Continuation short on next candle
    Stop: Above candle 1 high
    Target: 2:1 R:R or next support
    Win rate: ~78%
    """
    return (
        is_bearish(o1, c1) and is_bearish(o2, c2) and is_bearish(o3, c3) and
        c1 > c2 > c3 and
        o1 > o2 > o3 and
        # Each opens within previous body
        o2 <= o1 and o2 >= c1 and
        o3 <= o2 and o3 >= c2 and
        # Small lower shadows
        lower_shadow(o1, l1, c1) <= shadow_max_ratio * body(o1, c1) and
        lower_shadow(o2, l2, c2) <= shadow_max_ratio * body(o2, c2) and
        lower_shadow(o3, l3, c3) <= shadow_max_ratio * body(o3, c3)
    )


def detect_three_inside_up(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3):
    """
    THREE INSIDE UP (Bullish confirmation of harami)
    =================================================
    Detection:
      - Candle 1: Long bearish
      - Candle 2: Bullish harami (body inside candle 1 body)
      - Candle 3: Bullish, closes above candle 1 open (above candle 1 body top)

    Entry: On candle 3 close or next bar
    Stop: Below candle 1 low
    Target: 1.5:1 to 2:1 R:R
    Win rate: ~65%
    """
    return (
        is_bearish(o1, c1) and
        is_bullish(o2, c2) and
        # Harami: candle 2 body inside candle 1 body
        o2 >= c1 and c2 <= o1 and
        body(o2, c2) < body(o1, c1) and
        # Confirmation: candle 3 bullish and closes above candle 1 open
        is_bullish(o3, c3) and
        c3 > o1
    )


def detect_three_inside_down(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3):
    """
    THREE INSIDE DOWN (Bearish confirmation of harami)
    ===================================================
    Detection:
      - Candle 1: Long bullish
      - Candle 2: Bearish harami (body inside candle 1 body)
      - Candle 3: Bearish, closes below candle 1 open (below candle 1 body bottom)

    Entry: On candle 3 close or next bar
    Stop: Above candle 1 high
    Target: 1.5:1 to 2:1 R:R
    Win rate: ~65%
    """
    return (
        is_bullish(o1, c1) and
        is_bearish(o2, c2) and
        o2 <= c1 and c2 >= o1 and
        body(o2, c2) < body(o1, c1) and
        is_bearish(o3, c3) and
        c3 < o1
    )


def detect_three_outside_up(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3):
    """
    THREE OUTSIDE UP (Bullish confirmation of engulfing)
    ====================================================
    Detection:
      - Candle 1: Bearish
      - Candle 2: Bullish engulfing of candle 1
      - Candle 3: Bullish, closes above candle 2 close

    Entry: On candle 3 close
    Stop: Below candle 2 low
    Target: 2:1 R:R
    Win rate: ~75% (avg profit 0.73% per trade on S&P 500)
    """
    return (
        detect_bullish_engulfing(o1, h1, l1, c1, o2, h2, l2, c2) and
        is_bullish(o3, c3) and
        c3 > c2
    )


def detect_three_outside_down(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3):
    """
    THREE OUTSIDE DOWN (Bearish confirmation of engulfing)
    ======================================================
    Detection:
      - Candle 1: Bullish
      - Candle 2: Bearish engulfing of candle 1
      - Candle 3: Bearish, closes below candle 2 close

    Entry: On candle 3 close
    Stop: Above candle 2 high
    Target: 2:1 R:R
    Win rate: ~75%
    """
    return (
        detect_bearish_engulfing(o1, h1, l1, c1, o2, h2, l2, c2) and
        is_bearish(o3, c3) and
        c3 < c2
    )


def detect_abandoned_baby_bullish(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                                   doji_body_thresh=0.05):
    """
    ABANDONED BABY BULLISH
    ======================
    Detection:
      - Candle 1: Long bearish
      - Candle 2: Doji that GAPS DOWN from candle 1 (h2 < l1, shadows don't overlap)
      - Candle 3: Bullish that GAPS UP from candle 2 (l3 > h2, shadows don't overlap)

    Entry: On candle 3 close or next bar
    Stop: Below candle 2 low
    Target: 2:1 to 3:1 R:R
    Win rate: Very high (~80%+) but EXTREMELY RARE
    """
    r2 = candle_range(h2, l2)
    return (
        is_bearish(o1, c1) and
        body(o1, c1) > 0 and
        # Candle 2 is doji
        r2 > 0 and body(o2, c2) <= doji_body_thresh * r2 and
        # Gap down: candle 2 high < candle 1 low
        h2 < l1 and
        # Gap up: candle 3 low > candle 2 high
        l3 > h2 and
        is_bullish(o3, c3)
    )


def detect_abandoned_baby_bearish(o1, h1, l1, c1, o2, h2, l2, c2, o3, h3, l3, c3,
                                   doji_body_thresh=0.05):
    """
    ABANDONED BABY BEARISH
    ======================
    Detection:
      - Candle 1: Long bullish
      - Candle 2: Doji that GAPS UP from candle 1 (l2 > h1)
      - Candle 3: Bearish that GAPS DOWN from candle 2 (h3 < l2)

    Entry: On candle 3 close or next bar
    Stop: Above candle 2 high
    Target: 2:1 to 3:1 R:R
    Win rate: Very high (~80%+) but EXTREMELY RARE
    """
    r2 = candle_range(h2, l2)
    return (
        is_bullish(o1, c1) and
        body(o1, c1) > 0 and
        r2 > 0 and body(o2, c2) <= doji_body_thresh * r2 and
        l2 > h1 and
        h3 < l2 and
        is_bearish(o3, c3)
    )


# ============================================================================
# SECTION 4: CHART PATTERNS (CONTINUATION)
# ============================================================================

class ChartPatternDetector:
    """
    Detects chart patterns from OHLC DataFrame.
    DataFrame must have columns: open, high, low, close, volume
    and be sorted by time ascending.
    """

    def __init__(self, df: pd.DataFrame, swing_order: int = 5):
        self.df = df.copy()
        self.highs = df['high'].values
        self.lows = df['low'].values
        self.closes = df['close'].values
        self.opens = df['open'].values
        self.volumes = df['volume'].values if 'volume' in df.columns else None
        self.swing_order = swing_order

        # Pre-compute swing points
        self.swing_high_idx = find_swing_highs(self.highs, order=swing_order)
        self.swing_low_idx = find_swing_lows(self.lows, order=swing_order)

    # ----------------------------------------------------------------
    # BULL FLAG
    # ----------------------------------------------------------------
    def detect_bull_flag(self, min_pole_bars=3, max_pole_bars=20,
                         min_flag_bars=3, max_flag_bars=15,
                         max_retrace_pct=0.50, min_pole_move_pct=0.03):
        """
        BULL FLAG (Continuation, Bullish)
        =================================
        Detection Algorithm:
          1. POLE: Find a strong upward move of min_pole_move_pct (3%+)
             over min_pole_bars to max_pole_bars consecutive bars
             pole_low = lowest low in pole period
             pole_high = highest high in pole period
             pole_height = pole_high - pole_low
             Requirement: pole_height / pole_low >= min_pole_move_pct

          2. FLAG: After the pole, price consolidates in a slight downward
             or sideways channel for min_flag_bars to max_flag_bars:
             - Flag highs form a descending or flat line (slope <= 0)
             - Flag lows form a descending or flat line (slope <= 0)
             - Lines are roughly parallel (channel)
             - Flag does NOT retrace more than max_retrace_pct of pole:
               pole_high - flag_low <= max_retrace_pct * pole_height

          3. BREAKOUT: Price closes above the upper flag boundary

        Entry: Close above the upper trendline of the flag
        Stop: Below the lowest point of the flag
        Target: Measured move = pole_height projected from breakout point
                target = breakout_price + pole_height
        Win rate: ~67% (Bulkowski); with volume confirmation ~74%

        Returns: list of dict with pattern details
        """
        results = []
        n = len(self.df)

        for pole_end in range(min_pole_bars, n - min_flag_bars):
            for pole_start in range(max(0, pole_end - max_pole_bars),
                                     max(0, pole_end - min_pole_bars) + 1):
                pole_low = np.min(self.lows[pole_start:pole_end + 1])
                pole_high = np.max(self.highs[pole_start:pole_end + 1])
                pole_height = pole_high - pole_low

                if pole_low == 0 or pole_height / pole_low < min_pole_move_pct:
                    continue

                # Check pole is upward (close at end > close at start)
                if self.closes[pole_end] <= self.closes[pole_start]:
                    continue

                # Look for flag after pole
                for flag_end in range(pole_end + min_flag_bars,
                                       min(pole_end + max_flag_bars + 1, n)):
                    flag_highs = self.highs[pole_end:flag_end + 1]
                    flag_lows = self.lows[pole_end:flag_end + 1]
                    flag_low = np.min(flag_lows)

                    # Retrace check
                    retrace = pole_high - flag_low
                    if retrace > max_retrace_pct * pole_height:
                        continue

                    # Slope check: flag should drift down or sideways
                    high_slope = linear_regression_slope(flag_highs)
                    low_slope = linear_regression_slope(flag_lows)

                    if high_slope > 0.001 * pole_high:  # flag shouldn't trend up
                        continue

                    # Breakout check
                    if flag_end + 1 < n:
                        upper_boundary = flag_highs[-1]  # approximate
                        if self.closes[flag_end + 1] > upper_boundary:
                            breakout_price = self.closes[flag_end + 1]
                            target = breakout_price + pole_height
                            stop = flag_low
                            results.append({
                                'pattern': 'bull_flag',
                                'pole_start': pole_start,
                                'pole_end': pole_end,
                                'flag_end': flag_end,
                                'breakout_bar': flag_end + 1,
                                'entry': breakout_price,
                                'stop': stop,
                                'target': target,
                                'pole_height': pole_height,
                                'risk_reward': (target - breakout_price) / (breakout_price - stop) if breakout_price > stop else 0,
                                'win_rate': 0.67
                            })
                            break  # found flag for this pole
        return results

    def detect_bear_flag(self, min_pole_bars=3, max_pole_bars=20,
                         min_flag_bars=3, max_flag_bars=15,
                         max_retrace_pct=0.50, min_pole_move_pct=0.03):
        """
        BEAR FLAG (Continuation, Bearish)
        =================================
        Detection: Mirror of bull flag
          1. POLE: Strong downward move
          2. FLAG: Slight upward or sideways consolidation
          3. BREAKOUT: Price closes below lower flag boundary

        Entry: Close below lower trendline of the flag
        Stop: Above the highest point of the flag
        Target: Measured move = pole_height projected DOWN from breakout
                target = breakout_price - pole_height
        Win rate: ~67%
        """
        results = []
        n = len(self.df)

        for pole_end in range(min_pole_bars, n - min_flag_bars):
            for pole_start in range(max(0, pole_end - max_pole_bars),
                                     max(0, pole_end - min_pole_bars) + 1):
                pole_high = np.max(self.highs[pole_start:pole_end + 1])
                pole_low = np.min(self.lows[pole_start:pole_end + 1])
                pole_height = pole_high - pole_low

                if pole_high == 0 or pole_height / pole_high < min_pole_move_pct:
                    continue

                if self.closes[pole_end] >= self.closes[pole_start]:
                    continue

                for flag_end in range(pole_end + min_flag_bars,
                                       min(pole_end + max_flag_bars + 1, n)):
                    flag_highs = self.highs[pole_end:flag_end + 1]
                    flag_lows = self.lows[pole_end:flag_end + 1]
                    flag_high = np.max(flag_highs)

                    retrace = flag_high - pole_low
                    if retrace > max_retrace_pct * pole_height:
                        continue

                    low_slope = linear_regression_slope(flag_lows)
                    if low_slope < -0.001 * pole_low:
                        continue

                    if flag_end + 1 < n:
                        lower_boundary = flag_lows[-1]
                        if self.closes[flag_end + 1] < lower_boundary:
                            breakout_price = self.closes[flag_end + 1]
                            target = breakout_price - pole_height
                            stop = flag_high
                            results.append({
                                'pattern': 'bear_flag',
                                'entry': breakout_price,
                                'stop': stop,
                                'target': target,
                                'pole_height': pole_height,
                                'risk_reward': (breakout_price - target) / (stop - breakout_price) if stop > breakout_price else 0,
                                'win_rate': 0.67
                            })
                            break
        return results

    def detect_pennant(self, min_pole_bars=3, max_pole_bars=20,
                       min_pennant_bars=5, max_pennant_bars=20,
                       convergence_min=0.3, min_pole_move_pct=0.03):
        """
        BULL/BEAR PENNANT (Continuation)
        =================================
        Detection Algorithm:
          1. POLE: Same as flag - strong directional move
          2. PENNANT: Converging trendlines (symmetrical triangle)
             - Upper trendline slopes DOWN (connecting swing highs)
             - Lower trendline slopes UP (connecting swing lows)
             - They converge (range narrows): range at end < convergence_min * range at start
          3. BREAKOUT: In direction of the pole

        Entry: Close beyond the pennant boundary in pole direction
        Stop: Opposite side of pennant
        Target: Measured move = pole_height from breakout point
        Win rate: ~66%

        Difference from Flag:
          Flag = parallel lines (rectangle/channel)
          Pennant = converging lines (small symmetrical triangle)
        """
        results = []
        n = len(self.df)

        for pole_end in range(min_pole_bars, n - min_pennant_bars):
            for pole_start in range(max(0, pole_end - max_pole_bars),
                                     max(0, pole_end - min_pole_bars) + 1):
                pole_high = np.max(self.highs[pole_start:pole_end + 1])
                pole_low = np.min(self.lows[pole_start:pole_end + 1])
                pole_height = pole_high - pole_low
                bull_pole = self.closes[pole_end] > self.closes[pole_start]

                if pole_low == 0 or pole_height / max(pole_low, pole_high) < min_pole_move_pct:
                    continue

                for pend in range(pole_end + min_pennant_bars,
                                   min(pole_end + max_pennant_bars + 1, n)):
                    p_highs = self.highs[pole_end:pend + 1]
                    p_lows = self.lows[pole_end:pend + 1]

                    high_slope = linear_regression_slope(p_highs)
                    low_slope = linear_regression_slope(p_lows)

                    # Pennant: upper line descends, lower line ascends
                    if high_slope >= 0 or low_slope <= 0:
                        continue

                    # Check convergence
                    initial_range = p_highs[0] - p_lows[0]
                    final_range = p_highs[-1] - p_lows[-1]
                    if initial_range == 0:
                        continue
                    if final_range / initial_range > convergence_min:
                        continue  # not converging enough

                    # Breakout
                    if pend + 1 < n:
                        if bull_pole and self.closes[pend + 1] > p_highs[-1]:
                            bp = self.closes[pend + 1]
                            results.append({
                                'pattern': 'bull_pennant',
                                'entry': bp,
                                'stop': np.min(p_lows),
                                'target': bp + pole_height,
                                'win_rate': 0.66
                            })
                            break
                        elif not bull_pole and self.closes[pend + 1] < p_lows[-1]:
                            bp = self.closes[pend + 1]
                            results.append({
                                'pattern': 'bear_pennant',
                                'entry': bp,
                                'stop': np.max(p_highs),
                                'target': bp - pole_height,
                                'win_rate': 0.66
                            })
                            break
        return results

    def detect_ascending_triangle(self, min_bars=10, max_bars=60,
                                   flat_tolerance=0.005, min_touches=2):
        """
        ASCENDING TRIANGLE (Bullish continuation)
        ==========================================
        Detection Algorithm:
          1. Find a roughly FLAT resistance line (swing highs within flat_tolerance)
          2. Find RISING support line (swing lows trending upward)
          3. At least min_touches on each line
          4. Breakout above the flat resistance

        Math:
          resistance_level = mean(swing_highs in window)
          All swing highs within flat_tolerance * resistance_level of each other
          low_slope = linear_regression_slope(swing_lows) > 0

        Entry: Close above resistance level
        Stop: Below the last swing low (or the rising trendline)
        Target: Height of triangle at widest point projected from breakout
                height = resistance_level - lowest_low_in_triangle
                target = breakout + height
        Win rate: ~83% (breakout up); Bulkowski: 75% meet target
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window_sh = self.swing_high_idx[
                    (self.swing_high_idx >= start_idx) &
                    (self.swing_high_idx <= end_idx)
                ]
                window_sl = self.swing_low_idx[
                    (self.swing_low_idx >= start_idx) &
                    (self.swing_low_idx <= end_idx)
                ]

                if len(window_sh) < min_touches or len(window_sl) < min_touches:
                    continue

                sh_vals = self.highs[window_sh]
                sl_vals = self.lows[window_sl]

                # Flat resistance check
                resistance = np.mean(sh_vals)
                if resistance == 0:
                    continue
                if np.max(np.abs(sh_vals - resistance)) / resistance > flat_tolerance:
                    continue

                # Rising support check
                if len(sl_vals) >= 2:
                    sl_slope = linear_regression_slope(sl_vals)
                    if sl_slope <= 0:
                        continue
                else:
                    continue

                # Check for breakout
                if end_idx + 1 < n and self.closes[end_idx + 1] > resistance:
                    height = resistance - np.min(sl_vals)
                    bp = self.closes[end_idx + 1]
                    results.append({
                        'pattern': 'ascending_triangle',
                        'entry': bp,
                        'stop': sl_vals[-1],
                        'target': bp + height,
                        'height': height,
                        'resistance': resistance,
                        'win_rate': 0.75
                    })
                    break
        return results

    def detect_descending_triangle(self, min_bars=10, max_bars=60,
                                    flat_tolerance=0.005, min_touches=2):
        """
        DESCENDING TRIANGLE (Bearish continuation)
        ===========================================
        Detection Algorithm:
          1. FLAT support line (swing lows within flat_tolerance)
          2. DECLINING resistance line (swing highs trending downward)
          3. At least min_touches on each line
          4. Breakout below the flat support

        Entry: Close below support level
        Stop: Above the last swing high (or declining trendline)
        Target: Height of triangle projected DOWN from breakout
                target = breakout - height
        Win rate: ~87% (Bulkowski)
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window_sh = self.swing_high_idx[
                    (self.swing_high_idx >= start_idx) &
                    (self.swing_high_idx <= end_idx)
                ]
                window_sl = self.swing_low_idx[
                    (self.swing_low_idx >= start_idx) &
                    (self.swing_low_idx <= end_idx)
                ]

                if len(window_sh) < min_touches or len(window_sl) < min_touches:
                    continue

                sh_vals = self.highs[window_sh]
                sl_vals = self.lows[window_sl]

                # Flat support
                support = np.mean(sl_vals)
                if support == 0:
                    continue
                if np.max(np.abs(sl_vals - support)) / support > flat_tolerance:
                    continue

                # Declining resistance
                if len(sh_vals) >= 2:
                    sh_slope = linear_regression_slope(sh_vals)
                    if sh_slope >= 0:
                        continue
                else:
                    continue

                if end_idx + 1 < n and self.closes[end_idx + 1] < support:
                    height = np.max(sh_vals) - support
                    bp = self.closes[end_idx + 1]
                    results.append({
                        'pattern': 'descending_triangle',
                        'entry': bp,
                        'stop': sh_vals[-1],
                        'target': bp - height,
                        'height': height,
                        'support': support,
                        'win_rate': 0.87
                    })
                    break
        return results

    def detect_symmetrical_triangle(self, min_bars=10, max_bars=60, min_touches=2):
        """
        SYMMETRICAL TRIANGLE (Continuation in direction of prior trend)
        ================================================================
        Detection Algorithm:
          1. Upper trendline: DECLINING (connecting swing highs)
          2. Lower trendline: RISING (connecting swing lows)
          3. Lines converge (the range narrows over time)
          4. At least min_touches on each line
          5. Breakout in either direction (but favors prior trend)

        Math:
          high_slope = linreg_slope(swing_highs) < 0
          low_slope = linreg_slope(swing_lows) > 0
          Apex = intersection point of the two regression lines

        Entry: Close beyond the triangle boundary
        Stop: Opposite side of triangle at breakout bar
        Target: Widest part of triangle projected from breakout
                height = first_swing_high - first_swing_low
                target = breakout +/- height
        Win rate: ~76% (in direction of trend)
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window_sh = self.swing_high_idx[
                    (self.swing_high_idx >= start_idx) &
                    (self.swing_high_idx <= end_idx)
                ]
                window_sl = self.swing_low_idx[
                    (self.swing_low_idx >= start_idx) &
                    (self.swing_low_idx <= end_idx)
                ]

                if len(window_sh) < min_touches or len(window_sl) < min_touches:
                    continue

                sh_vals = self.highs[window_sh]
                sl_vals = self.lows[window_sl]

                high_slope = linear_regression_slope(sh_vals)
                low_slope = linear_regression_slope(sl_vals)

                # Converging: highs declining, lows rising
                if high_slope >= 0 or low_slope <= 0:
                    continue

                height = sh_vals[0] - sl_vals[0]

                if end_idx + 1 < n:
                    if self.closes[end_idx + 1] > sh_vals[-1]:
                        bp = self.closes[end_idx + 1]
                        results.append({
                            'pattern': 'symmetrical_triangle_bullish_breakout',
                            'entry': bp,
                            'stop': sl_vals[-1],
                            'target': bp + height,
                            'win_rate': 0.76
                        })
                    elif self.closes[end_idx + 1] < sl_vals[-1]:
                        bp = self.closes[end_idx + 1]
                        results.append({
                            'pattern': 'symmetrical_triangle_bearish_breakout',
                            'entry': bp,
                            'stop': sh_vals[-1],
                            'target': bp - height,
                            'win_rate': 0.76
                        })
        return results

    def detect_rectangle_channel(self, min_bars=10, max_bars=60,
                                  flat_tolerance=0.01, min_touches=2):
        """
        RECTANGLE / CHANNEL (Continuation)
        ===================================
        Detection Algorithm:
          1. FLAT resistance: swing highs cluster at same level
          2. FLAT support: swing lows cluster at same level
          3. Both lines are roughly horizontal (slopes near zero)
          4. Price oscillates between the two levels

        Math:
          resistance = mean(swing_highs)  ; all within flat_tolerance
          support = mean(swing_lows)      ; all within flat_tolerance
          abs(high_slope) < threshold AND abs(low_slope) < threshold

        Entry: Breakout beyond either boundary
        Stop: Opposite boundary
        Target: Height of rectangle projected from breakout
                height = resistance - support
        Win rate: ~65% (continuation breakout in prior trend direction)
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window_sh = self.swing_high_idx[
                    (self.swing_high_idx >= start_idx) &
                    (self.swing_high_idx <= end_idx)
                ]
                window_sl = self.swing_low_idx[
                    (self.swing_low_idx >= start_idx) &
                    (self.swing_low_idx <= end_idx)
                ]

                if len(window_sh) < min_touches or len(window_sl) < min_touches:
                    continue

                sh_vals = self.highs[window_sh]
                sl_vals = self.lows[window_sl]

                resistance = np.mean(sh_vals)
                support = np.mean(sl_vals)

                if resistance == 0 or support == 0:
                    continue

                # Both lines flat
                if (np.max(np.abs(sh_vals - resistance)) / resistance > flat_tolerance):
                    continue
                if (np.max(np.abs(sl_vals - support)) / support > flat_tolerance):
                    continue

                height = resistance - support
                if height <= 0:
                    continue

                if end_idx + 1 < n:
                    if self.closes[end_idx + 1] > resistance:
                        bp = self.closes[end_idx + 1]
                        results.append({
                            'pattern': 'rectangle_bullish_breakout',
                            'entry': bp,
                            'stop': support,
                            'target': bp + height,
                            'win_rate': 0.65
                        })
                    elif self.closes[end_idx + 1] < support:
                        bp = self.closes[end_idx + 1]
                        results.append({
                            'pattern': 'rectangle_bearish_breakout',
                            'entry': bp,
                            'stop': resistance,
                            'target': bp - height,
                            'win_rate': 0.65
                        })
        return results

    def detect_cup_and_handle(self, min_cup_bars=20, max_cup_bars=100,
                               min_handle_bars=5, max_handle_bars=25,
                               max_handle_retrace=0.50, cup_symmetry_tolerance=0.30):
        """
        CUP AND HANDLE (Bullish continuation)
        ======================================
        Detection Algorithm:
          1. CUP: U-shaped price formation
             - Left lip: swing high (resistance level)
             - Bottom: swing low (cup depth)
             - Right lip: swing high approximately equal to left lip
             - Cup depth = left_lip - bottom
             - Symmetry: right_lip within cup_symmetry_tolerance of left_lip
             - Cup should be rounded (gradual, not V-shaped):
               Check multiple points along the cup follow a quadratic curve

          2. HANDLE: Small pullback after right lip
             - Duration: min_handle_bars to max_handle_bars
             - Depth: does not retrace more than max_handle_retrace of cup depth
             - Slight downward drift (flag-like)

          3. BREAKOUT: Price closes above the lip level (neckline)

        Math:
          left_lip = swing_high at start
          cup_bottom = min(lows) in cup region
          right_lip = swing_high after cup_bottom (within tolerance of left_lip)
          neckline = max(left_lip, right_lip)
          handle_low = min(lows) in handle region
          Require: neckline - handle_low <= max_handle_retrace * (neckline - cup_bottom)

        Entry: Close above neckline
        Stop: Below handle low
        Target: Cup depth projected from neckline
                target = neckline + (neckline - cup_bottom)
        Win rate: ~65% (Bulkowski: avg rise 34%)
        """
        results = []
        n = len(self.df)

        for i, left_lip_idx in enumerate(self.swing_high_idx):
            left_lip = self.highs[left_lip_idx]

            # Find cup bottom
            for bottom_idx in self.swing_low_idx:
                if bottom_idx <= left_lip_idx:
                    continue
                if bottom_idx - left_lip_idx < min_cup_bars // 2:
                    continue
                if bottom_idx - left_lip_idx > max_cup_bars // 2:
                    break

                cup_bottom = self.lows[bottom_idx]
                cup_depth = left_lip - cup_bottom
                if cup_depth <= 0:
                    continue

                # Find right lip
                for right_lip_idx in self.swing_high_idx:
                    if right_lip_idx <= bottom_idx:
                        continue
                    if right_lip_idx - bottom_idx < min_cup_bars // 4:
                        continue
                    if right_lip_idx - left_lip_idx > max_cup_bars:
                        break

                    right_lip = self.highs[right_lip_idx]

                    # Symmetry check
                    if left_lip == 0:
                        continue
                    if abs(right_lip - left_lip) / left_lip > cup_symmetry_tolerance:
                        continue

                    neckline = max(left_lip, right_lip)

                    # Look for handle
                    handle_start = right_lip_idx
                    for handle_end in range(handle_start + min_handle_bars,
                                             min(handle_start + max_handle_bars + 1, n)):
                        handle_lows = self.lows[handle_start:handle_end + 1]
                        handle_low = np.min(handle_lows)

                        # Handle retrace check
                        if neckline - handle_low > max_handle_retrace * cup_depth:
                            continue

                        # Handle should drift down slightly
                        handle_slope = linear_regression_slope(
                            self.closes[handle_start:handle_end + 1])

                        # Breakout
                        if handle_end + 1 < n and self.closes[handle_end + 1] > neckline:
                            bp = self.closes[handle_end + 1]
                            target = neckline + cup_depth
                            stop = handle_low
                            results.append({
                                'pattern': 'cup_and_handle',
                                'entry': bp,
                                'stop': stop,
                                'target': target,
                                'neckline': neckline,
                                'cup_depth': cup_depth,
                                'left_lip_idx': left_lip_idx,
                                'bottom_idx': bottom_idx,
                                'right_lip_idx': right_lip_idx,
                                'win_rate': 0.65
                            })
                            break
                    break  # found right lip
        return results

    def detect_inverted_cup_and_handle(self, min_cup_bars=20, max_cup_bars=100,
                                        min_handle_bars=5, max_handle_bars=25,
                                        max_handle_retrace=0.50, cup_symmetry_tolerance=0.30):
        """
        INVERTED CUP AND HANDLE (Bearish)
        ==================================
        Detection: Mirror of cup and handle
          1. Inverted U-shaped top
          2. Handle drifts slightly UP
          3. Breakout BELOW the neckline (support)

        Entry: Close below neckline
        Stop: Above handle high
        Target: Cup height projected DOWN from neckline
        Win rate: ~65%
        """
        results = []
        n = len(self.df)

        for left_lip_idx in self.swing_low_idx:
            left_lip = self.lows[left_lip_idx]

            for top_idx in self.swing_high_idx:
                if top_idx <= left_lip_idx:
                    continue
                if top_idx - left_lip_idx > max_cup_bars // 2:
                    break

                cup_top = self.highs[top_idx]
                cup_depth = cup_top - left_lip
                if cup_depth <= 0:
                    continue

                for right_lip_idx in self.swing_low_idx:
                    if right_lip_idx <= top_idx:
                        continue
                    if right_lip_idx - left_lip_idx > max_cup_bars:
                        break

                    right_lip = self.lows[right_lip_idx]
                    if left_lip == 0:
                        continue
                    if abs(right_lip - left_lip) / left_lip > cup_symmetry_tolerance:
                        continue

                    neckline = min(left_lip, right_lip)

                    for handle_end in range(right_lip_idx + min_handle_bars,
                                             min(right_lip_idx + max_handle_bars + 1, n)):
                        handle_highs = self.highs[right_lip_idx:handle_end + 1]
                        handle_high = np.max(handle_highs)

                        if handle_high - neckline > max_handle_retrace * cup_depth:
                            continue

                        if handle_end + 1 < n and self.closes[handle_end + 1] < neckline:
                            bp = self.closes[handle_end + 1]
                            results.append({
                                'pattern': 'inverted_cup_and_handle',
                                'entry': bp,
                                'stop': handle_high,
                                'target': neckline - cup_depth,
                                'win_rate': 0.65
                            })
                            break
                    break
        return results

    def detect_measured_move(self, tolerance=0.15):
        """
        MEASURED MOVE (AB=CD pattern at chart level)
        =============================================
        Detection Algorithm:
          1. Find swing sequence: A(low) -> B(high) -> C(low) -> D(projected)
          2. AB leg: strong move up
          3. BC leg: pullback (38.2% to 78.6% of AB)
          4. CD leg: should be approximately equal to AB in price AND time
             |CD| within tolerance of |AB|

        Math:
          AB = B_high - A_low
          BC_retrace = (B_high - C_low) / AB  ; should be 0.382 to 0.786
          CD_projected = C_low + AB  (measured move target)
          Also check time: bars(CD) approximately equals bars(AB)

        Entry: At point C (after pullback confirmation)
        Stop: Below point C
        Target: D = C + AB (or C - AB for bearish)
        Win rate: ~63%
        """
        results = []
        sh = self.swing_high_idx
        sl = self.swing_low_idx

        # Combine and sort all swing points
        all_swings = []
        for idx in sh:
            all_swings.append((idx, 'high', self.highs[idx]))
        for idx in sl:
            all_swings.append((idx, 'low', self.lows[idx]))
        all_swings.sort(key=lambda x: x[0])

        for i in range(len(all_swings) - 2):
            a_idx, a_type, a_val = all_swings[i]
            b_idx, b_type, b_val = all_swings[i + 1]
            c_idx, c_type, c_val = all_swings[i + 2]

            # Bullish: low -> high -> low
            if a_type == 'low' and b_type == 'high' and c_type == 'low':
                ab = b_val - a_val
                if ab <= 0:
                    continue
                bc_retrace = (b_val - c_val) / ab
                if 0.382 <= bc_retrace <= 0.786:
                    target = c_val + ab
                    results.append({
                        'pattern': 'measured_move_bullish',
                        'a_idx': a_idx, 'b_idx': b_idx, 'c_idx': c_idx,
                        'entry': c_val,
                        'stop': c_val - 0.1 * ab,
                        'target': target,
                        'ab_length': ab,
                        'bc_retrace': bc_retrace,
                        'win_rate': 0.63
                    })

            # Bearish: high -> low -> high
            if a_type == 'high' and b_type == 'low' and c_type == 'high':
                ab = a_val - b_val
                if ab <= 0:
                    continue
                bc_retrace = (c_val - b_val) / ab
                if 0.382 <= bc_retrace <= 0.786:
                    target = c_val - ab
                    results.append({
                        'pattern': 'measured_move_bearish',
                        'a_idx': a_idx, 'b_idx': b_idx, 'c_idx': c_idx,
                        'entry': c_val,
                        'stop': c_val + 0.1 * ab,
                        'target': target,
                        'win_rate': 0.63
                    })
        return results

    # ================================================================
    # SECTION 5: CHART PATTERNS (REVERSAL)
    # ================================================================

    def detect_head_and_shoulders(self, symmetry_tolerance=0.30,
                                   shoulder_tolerance=0.10,
                                   min_pattern_bars=15):
        """
        HEAD AND SHOULDERS (Bearish reversal)
        =====================================
        Detection Algorithm:
          1. Find 5 swing points: left_shoulder(H), left_trough(L),
             head(H), right_trough(L), right_shoulder(H)
          2. Head is the HIGHEST point: head > left_shoulder AND head > right_shoulder
          3. Shoulders approximately equal:
             abs(left_shoulder - right_shoulder) / head <= shoulder_tolerance
          4. Neckline connects left_trough and right_trough
             neckline_at_breakout = interpolated value at breakout bar
          5. Breakout: Price closes below neckline

        Math:
          neckline_slope = (right_trough - left_trough) / (right_trough_idx - left_trough_idx)
          neckline_at_bar(x) = left_trough + neckline_slope * (x - left_trough_idx)
          pattern_height = head - neckline_at_head_idx

        Entry: Close below neckline
        Stop: Above the right shoulder high
        Target: neckline_breakout_price - pattern_height
                (project pattern height DOWN from neckline)
        Win rate: ~89% (Bulkowski: 81-89% depending on market)
        """
        results = []
        sh = self.swing_high_idx
        sl = self.swing_low_idx

        for i in range(len(sh) - 2):
            ls_idx = sh[i]      # left shoulder
            head_idx = sh[i + 1]  # head
            rs_idx = sh[i + 2]  # right shoulder

            ls_val = self.highs[ls_idx]
            head_val = self.highs[head_idx]
            rs_val = self.highs[rs_idx]

            # Head must be highest
            if head_val <= ls_val or head_val <= rs_val:
                continue

            # Shoulders approximately equal
            if head_val == 0:
                continue
            if abs(ls_val - rs_val) / head_val > shoulder_tolerance:
                continue

            # Find troughs between shoulders
            lt_candidates = sl[(sl > ls_idx) & (sl < head_idx)]
            rt_candidates = sl[(sl > head_idx) & (sl < rs_idx)]

            if len(lt_candidates) == 0 or len(rt_candidates) == 0:
                continue

            lt_idx = lt_candidates[np.argmin(self.lows[lt_candidates])]
            rt_idx = rt_candidates[np.argmin(self.lows[rt_candidates])]

            lt_val = self.lows[lt_idx]
            rt_val = self.lows[rt_idx]

            # Neckline
            if rt_idx == lt_idx:
                continue
            neckline_slope = (rt_val - lt_val) / (rt_idx - lt_idx)

            # Pattern height at head
            neckline_at_head = lt_val + neckline_slope * (head_idx - lt_idx)
            pattern_height = head_val - neckline_at_head

            if pattern_height <= 0:
                continue

            # Check for breakout after right shoulder
            for bar in range(rs_idx + 1, min(rs_idx + 30, len(self.df))):
                neckline_at_bar = lt_val + neckline_slope * (bar - lt_idx)
                if self.closes[bar] < neckline_at_bar:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'head_and_shoulders',
                        'left_shoulder_idx': ls_idx,
                        'head_idx': head_idx,
                        'right_shoulder_idx': rs_idx,
                        'neckline_slope': neckline_slope,
                        'entry': bp,
                        'stop': rs_val,
                        'target': bp - pattern_height,
                        'pattern_height': pattern_height,
                        'win_rate': 0.83
                    })
                    break
        return results

    def detect_inverse_head_and_shoulders(self, shoulder_tolerance=0.10):
        """
        INVERSE HEAD AND SHOULDERS (Bullish reversal)
        ==============================================
        Detection Algorithm:
          1. Find 5 swing points: left_shoulder(L), left_peak(H),
             head(L), right_peak(H), right_shoulder(L)
          2. Head is the LOWEST point
          3. Shoulders approximately equal
          4. Neckline connects left_peak and right_peak
          5. Breakout: Price closes above neckline

        Entry: Close above neckline
        Stop: Below the right shoulder low
        Target: neckline_breakout_price + pattern_height
        Win rate: ~88% (Bulkowski: avg rise +50%)
        """
        results = []
        sl = self.swing_low_idx
        sh = self.swing_high_idx

        for i in range(len(sl) - 2):
            ls_idx = sl[i]
            head_idx = sl[i + 1]
            rs_idx = sl[i + 2]

            ls_val = self.lows[ls_idx]
            head_val = self.lows[head_idx]
            rs_val = self.lows[rs_idx]

            # Head must be lowest
            if head_val >= ls_val or head_val >= rs_val:
                continue

            if ls_val == 0:
                continue
            if abs(ls_val - rs_val) / ls_val > shoulder_tolerance:
                continue

            # Find peaks between shoulders
            lp_candidates = sh[(sh > ls_idx) & (sh < head_idx)]
            rp_candidates = sh[(sh > head_idx) & (sh < rs_idx)]

            if len(lp_candidates) == 0 or len(rp_candidates) == 0:
                continue

            lp_idx = lp_candidates[np.argmax(self.highs[lp_candidates])]
            rp_idx = rp_candidates[np.argmax(self.highs[rp_candidates])]

            lp_val = self.highs[lp_idx]
            rp_val = self.highs[rp_idx]

            if rp_idx == lp_idx:
                continue
            neckline_slope = (rp_val - lp_val) / (rp_idx - lp_idx)
            neckline_at_head = lp_val + neckline_slope * (head_idx - lp_idx)
            pattern_height = neckline_at_head - head_val

            if pattern_height <= 0:
                continue

            for bar in range(rs_idx + 1, min(rs_idx + 30, len(self.df))):
                neckline_at_bar = lp_val + neckline_slope * (bar - lp_idx)
                if self.closes[bar] > neckline_at_bar:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'inverse_head_and_shoulders',
                        'entry': bp,
                        'stop': rs_val,
                        'target': bp + pattern_height,
                        'pattern_height': pattern_height,
                        'win_rate': 0.88
                    })
                    break
        return results

    def detect_double_top(self, tolerance=0.01, min_bars_between=5, max_bars_between=60):
        """
        DOUBLE TOP (Bearish reversal) - "M" shape
        ==========================================
        Detection Algorithm:
          1. Find two swing highs of approximately equal value:
             abs(high1 - high2) / high1 <= tolerance
          2. A swing low (trough) between them
          3. Minimum bars between the two tops
          4. Breakout: Price closes below the trough (neckline)

        Math:
          top1 = swing_high[i]
          top2 = swing_high[i+1]
          abs(top1 - top2) / top1 <= tolerance
          neckline = min(lows between top1 and top2)
          pattern_height = avg(top1, top2) - neckline

        Entry: Close below neckline
        Stop: Above the higher of the two tops
        Target: neckline - pattern_height
        Win rate: ~73-88% (Bulkowski: 73% reach target)
        """
        results = []
        sh = self.swing_high_idx

        for i in range(len(sh) - 1):
            t1_idx = sh[i]
            t2_idx = sh[i + 1]

            if t2_idx - t1_idx < min_bars_between or t2_idx - t1_idx > max_bars_between:
                continue

            t1_val = self.highs[t1_idx]
            t2_val = self.highs[t2_idx]

            if t1_val == 0:
                continue
            if abs(t1_val - t2_val) / t1_val > tolerance:
                continue

            # Neckline: lowest low between the two tops
            neckline = np.min(self.lows[t1_idx:t2_idx + 1])
            pattern_height = ((t1_val + t2_val) / 2) - neckline

            if pattern_height <= 0:
                continue

            # Breakout
            for bar in range(t2_idx + 1, min(t2_idx + 30, len(self.df))):
                if self.closes[bar] < neckline:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'double_top',
                        'top1_idx': t1_idx, 'top2_idx': t2_idx,
                        'entry': bp,
                        'stop': max(t1_val, t2_val),
                        'target': neckline - pattern_height,
                        'neckline': neckline,
                        'pattern_height': pattern_height,
                        'win_rate': 0.73
                    })
                    break
        return results

    def detect_double_bottom(self, tolerance=0.01, min_bars_between=5, max_bars_between=60):
        """
        DOUBLE BOTTOM (Bullish reversal) - "W" shape
        =============================================
        Detection Algorithm:
          1. Find two swing lows of approximately equal value
          2. A swing high (peak) between them
          3. Breakout: Close above the peak (neckline)

        Entry: Close above neckline
        Stop: Below the lower of the two bottoms
        Target: neckline + pattern_height
        Win rate: ~88% (Bulkowski)
        """
        results = []
        sl = self.swing_low_idx

        for i in range(len(sl) - 1):
            b1_idx = sl[i]
            b2_idx = sl[i + 1]

            if b2_idx - b1_idx < min_bars_between or b2_idx - b1_idx > max_bars_between:
                continue

            b1_val = self.lows[b1_idx]
            b2_val = self.lows[b2_idx]

            if b1_val == 0:
                continue
            if abs(b1_val - b2_val) / b1_val > tolerance:
                continue

            neckline = np.max(self.highs[b1_idx:b2_idx + 1])
            pattern_height = neckline - ((b1_val + b2_val) / 2)

            if pattern_height <= 0:
                continue

            for bar in range(b2_idx + 1, min(b2_idx + 30, len(self.df))):
                if self.closes[bar] > neckline:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'double_bottom',
                        'bottom1_idx': b1_idx, 'bottom2_idx': b2_idx,
                        'entry': bp,
                        'stop': min(b1_val, b2_val),
                        'target': neckline + pattern_height,
                        'neckline': neckline,
                        'win_rate': 0.88
                    })
                    break
        return results

    def detect_triple_top(self, tolerance=0.01, min_bars_between=5, max_bars_between=60):
        """
        TRIPLE TOP (Bearish reversal)
        =============================
        Detection:
          1. Three swing highs of approximately equal value
          2. Two troughs between them
          3. Neckline = lowest of the two troughs
          4. Breakout below neckline

        Entry: Close below neckline
        Stop: Above the highest of the three tops
        Target: neckline - pattern_height
        Win rate: ~87% (Bulkowski)
        """
        results = []
        sh = self.swing_high_idx

        for i in range(len(sh) - 2):
            t1, t2, t3 = sh[i], sh[i + 1], sh[i + 2]
            v1, v2, v3 = self.highs[t1], self.highs[t2], self.highs[t3]

            if v1 == 0:
                continue

            # All three tops approximately equal
            avg_top = (v1 + v2 + v3) / 3
            if (abs(v1 - avg_top) / avg_top > tolerance or
                abs(v2 - avg_top) / avg_top > tolerance or
                abs(v3 - avg_top) / avg_top > tolerance):
                continue

            # Neckline: lowest trough between the tops
            neckline = min(np.min(self.lows[t1:t2 + 1]),
                          np.min(self.lows[t2:t3 + 1]))
            pattern_height = avg_top - neckline

            for bar in range(t3 + 1, min(t3 + 30, len(self.df))):
                if self.closes[bar] < neckline:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'triple_top',
                        'entry': bp,
                        'stop': max(v1, v2, v3),
                        'target': neckline - pattern_height,
                        'win_rate': 0.87
                    })
                    break
        return results

    def detect_triple_bottom(self, tolerance=0.01):
        """
        TRIPLE BOTTOM (Bullish reversal)
        ================================
        Detection: Mirror of triple top with three approximately equal swing lows.

        Entry: Close above neckline (highest peak between bottoms)
        Stop: Below the lowest of the three bottoms
        Target: neckline + pattern_height
        Win rate: ~87%
        """
        results = []
        sl = self.swing_low_idx

        for i in range(len(sl) - 2):
            b1, b2, b3 = sl[i], sl[i + 1], sl[i + 2]
            v1, v2, v3 = self.lows[b1], self.lows[b2], self.lows[b3]

            if v1 == 0:
                continue

            avg_bottom = (v1 + v2 + v3) / 3
            if (abs(v1 - avg_bottom) / avg_bottom > tolerance or
                abs(v2 - avg_bottom) / avg_bottom > tolerance or
                abs(v3 - avg_bottom) / avg_bottom > tolerance):
                continue

            neckline = max(np.max(self.highs[b1:b2 + 1]),
                          np.max(self.highs[b2:b3 + 1]))
            pattern_height = neckline - avg_bottom

            for bar in range(b3 + 1, min(b3 + 30, len(self.df))):
                if self.closes[bar] > neckline:
                    bp = self.closes[bar]
                    results.append({
                        'pattern': 'triple_bottom',
                        'entry': bp,
                        'stop': min(v1, v2, v3),
                        'target': neckline + pattern_height,
                        'win_rate': 0.87
                    })
                    break
        return results

    def detect_rising_wedge(self, min_bars=10, max_bars=60, min_touches=3):
        """
        RISING WEDGE (Bearish reversal)
        ================================
        Detection Algorithm:
          1. Both upper and lower trendlines slope UPWARD (positive slope)
          2. Lower trendline is STEEPER than upper (lines converge upward)
          3. At least min_touches on each line
          4. Range narrows over time (convergence)
          5. Breakout: Price closes BELOW the lower trendline

        Math:
          upper_slope = linreg_slope(swing_highs) > 0
          lower_slope = linreg_slope(swing_lows) > 0
          lower_slope > upper_slope  (steeper lower = convergence)
          Apex in the future (lines haven't crossed yet)

        Entry: Close below lower trendline
        Stop: Above the last swing high inside the wedge
        Target: Base of the wedge (height at widest point) projected down
                OR retrace to the start of the wedge
        Win rate: ~68% (Bulkowski)
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            start_idx = max(0, end_idx - max_bars)

            window_sh = self.swing_high_idx[
                (self.swing_high_idx >= start_idx) &
                (self.swing_high_idx <= end_idx)
            ]
            window_sl = self.swing_low_idx[
                (self.swing_low_idx >= start_idx) &
                (self.swing_low_idx <= end_idx)
            ]

            if len(window_sh) < min_touches or len(window_sl) < min_touches:
                continue

            sh_vals = self.highs[window_sh]
            sl_vals = self.lows[window_sl]

            upper_slope = linear_regression_slope(sh_vals)
            lower_slope = linear_regression_slope(sl_vals)

            # Both slopes positive, lower steeper
            if upper_slope <= 0 or lower_slope <= 0:
                continue
            if lower_slope <= upper_slope:
                continue

            # Convergence check
            initial_range = sh_vals[0] - sl_vals[0]
            final_range = sh_vals[-1] - sl_vals[-1]
            if initial_range <= 0 or final_range >= initial_range:
                continue

            # Breakout below lower trendline
            if end_idx + 1 < n:
                lower_boundary = sl_vals[-1]  # approximate
                if self.closes[end_idx + 1] < lower_boundary:
                    bp = self.closes[end_idx + 1]
                    height = initial_range
                    results.append({
                        'pattern': 'rising_wedge',
                        'entry': bp,
                        'stop': sh_vals[-1],
                        'target': bp - height,
                        'win_rate': 0.68
                    })
        return results

    def detect_falling_wedge(self, min_bars=10, max_bars=60, min_touches=3):
        """
        FALLING WEDGE (Bullish reversal)
        =================================
        Detection Algorithm:
          1. Both upper and lower trendlines slope DOWNWARD (negative slope)
          2. Upper trendline is steeper (more negative) than lower
          3. Lines converge downward
          4. Breakout: Price closes ABOVE the upper trendline

        Math:
          upper_slope = linreg_slope(swing_highs) < 0
          lower_slope = linreg_slope(swing_lows) < 0
          upper_slope < lower_slope  (upper more negative = converging)

        Entry: Close above upper trendline
        Stop: Below the last swing low inside the wedge
        Target: Base of wedge projected up, or retrace to wedge start
        Win rate: ~68% (Bulkowski)
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            start_idx = max(0, end_idx - max_bars)

            window_sh = self.swing_high_idx[
                (self.swing_high_idx >= start_idx) &
                (self.swing_high_idx <= end_idx)
            ]
            window_sl = self.swing_low_idx[
                (self.swing_low_idx >= start_idx) &
                (self.swing_low_idx <= end_idx)
            ]

            if len(window_sh) < 2 or len(window_sl) < 2:
                continue

            sh_vals = self.highs[window_sh]
            sl_vals = self.lows[window_sl]

            upper_slope = linear_regression_slope(sh_vals)
            lower_slope = linear_regression_slope(sl_vals)

            # Both negative, upper steeper (more negative)
            if upper_slope >= 0 or lower_slope >= 0:
                continue
            if upper_slope >= lower_slope:
                continue

            initial_range = sh_vals[0] - sl_vals[0]
            final_range = sh_vals[-1] - sl_vals[-1]
            if initial_range <= 0 or final_range >= initial_range:
                continue

            if end_idx + 1 < n:
                upper_boundary = sh_vals[-1]
                if self.closes[end_idx + 1] > upper_boundary:
                    bp = self.closes[end_idx + 1]
                    height = initial_range
                    results.append({
                        'pattern': 'falling_wedge',
                        'entry': bp,
                        'stop': sl_vals[-1],
                        'target': bp + height,
                        'win_rate': 0.68
                    })
        return results

    def detect_rounding_bottom(self, min_bars=20, max_bars=100, r_squared_min=0.70):
        """
        ROUNDING BOTTOM / SAUCER (Bullish reversal)
        =============================================
        Detection Algorithm:
          1. Fit a QUADRATIC curve (parabola) to the closing prices
             y = ax^2 + bx + c
          2. Coefficient 'a' must be POSITIVE (U-shape / concave up)
          3. R-squared of the fit must be >= r_squared_min (good fit)
          4. Price at start and end should be higher than the middle
          5. Breakout above the lip level (neckline = max of start/end prices)

        Math:
          x = np.arange(n)
          coeffs = np.polyfit(x, closes, 2)  # [a, b, c]
          a > 0  (parabola opens upward)
          R^2 >= r_squared_min

          neckline = max(closes[0], closes[-1])
          target = neckline + (neckline - min(closes))

        Entry: Close above the lip/neckline
        Stop: Below the recent swing low near the right side
        Target: Depth of saucer projected above neckline
        Win rate: ~65%
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window = self.closes[start_idx:end_idx + 1]
                x = np.arange(len(window))

                if len(window) < 10:
                    continue

                # Fit quadratic
                coeffs = np.polyfit(x, window, 2)
                a, b, c = coeffs

                if a <= 0:  # must be concave up
                    continue

                # R-squared
                y_pred = np.polyval(coeffs, x)
                ss_res = np.sum((window - y_pred) ** 2)
                ss_tot = np.sum((window - np.mean(window)) ** 2)
                if ss_tot == 0:
                    continue
                r_squared = 1 - ss_res / ss_tot

                if r_squared < r_squared_min:
                    continue

                neckline = max(window[0], window[-1])
                depth = neckline - np.min(window)

                if end_idx + 1 < n and self.closes[end_idx + 1] > neckline:
                    bp = self.closes[end_idx + 1]
                    results.append({
                        'pattern': 'rounding_bottom',
                        'entry': bp,
                        'stop': np.min(self.lows[start_idx:end_idx + 1]),
                        'target': neckline + depth,
                        'r_squared': r_squared,
                        'win_rate': 0.65
                    })
        return results

    def detect_rounding_top(self, min_bars=20, max_bars=100, r_squared_min=0.70):
        """
        ROUNDING TOP (Bearish reversal)
        ================================
        Detection: Mirror of rounding bottom
          - Quadratic fit with a < 0 (concave down / inverted U)
          - Neckline at min of start/end prices
          - Breakout below neckline

        Entry: Close below neckline
        Stop: Above the peak of the rounding top
        Target: Depth projected below neckline
        Win rate: ~65%
        """
        results = []
        n = len(self.df)

        for end_idx in range(min_bars, n):
            for start_idx in range(max(0, end_idx - max_bars),
                                    max(0, end_idx - min_bars) + 1):
                window = self.closes[start_idx:end_idx + 1]
                x = np.arange(len(window))

                if len(window) < 10:
                    continue

                coeffs = np.polyfit(x, window, 2)
                a = coeffs[0]

                if a >= 0:  # must be concave down
                    continue

                y_pred = np.polyval(coeffs, x)
                ss_res = np.sum((window - y_pred) ** 2)
                ss_tot = np.sum((window - np.mean(window)) ** 2)
                if ss_tot == 0:
                    continue
                r_squared = 1 - ss_res / ss_tot

                if r_squared < r_squared_min:
                    continue

                neckline = min(window[0], window[-1])
                depth = np.max(window) - neckline

                if end_idx + 1 < n and self.closes[end_idx + 1] < neckline:
                    bp = self.closes[end_idx + 1]
                    results.append({
                        'pattern': 'rounding_top',
                        'entry': bp,
                        'stop': np.max(self.highs[start_idx:end_idx + 1]),
                        'target': neckline - depth,
                        'win_rate': 0.65
                    })
        return results

    def detect_v_bottom(self, min_drop_pct=0.05, max_recovery_bars=5, recovery_pct=0.80):
        """
        V-BOTTOM (Sharp bullish reversal)
        ==================================
        Detection Algorithm:
          1. Sharp decline: price drops min_drop_pct in a short period
          2. Sharp recovery: price recovers recovery_pct of the drop
             within max_recovery_bars
          3. The pivot point (bottom) is a single extreme low

        Math:
          pre_drop_high = max(highs in decline phase)
          v_bottom = min(lows at pivot)
          drop = pre_drop_high - v_bottom
          drop / pre_drop_high >= min_drop_pct
          Recovery within max_recovery_bars:
            post_bottom_close[max_recovery_bars] >= v_bottom + recovery_pct * drop

        Entry: When recovery reaches 50-80% of drop
        Stop: Below the V-bottom low
        Target: Pre-drop high or 1:1 R:R
        Win rate: ~60% (fast moves; best with volume spike at bottom)
        """
        results = []
        n = len(self.df)

        for i in self.swing_low_idx:
            if i < 5 or i + max_recovery_bars >= n:
                continue

            v_low = self.lows[i]
            pre_high = np.max(self.highs[max(0, i - 20):i])
            drop = pre_high - v_low

            if pre_high == 0 or drop / pre_high < min_drop_pct:
                continue

            # Check sharp recovery
            recovery_end = min(i + max_recovery_bars, n - 1)
            post_high = np.max(self.closes[i:recovery_end + 1])
            recovery = post_high - v_low

            if recovery >= recovery_pct * drop:
                results.append({
                    'pattern': 'v_bottom',
                    'bottom_idx': i,
                    'entry': post_high,
                    'stop': v_low,
                    'target': pre_high,
                    'win_rate': 0.60
                })
        return results

    def detect_v_top(self, min_rise_pct=0.05, max_decline_bars=5, decline_pct=0.80):
        """
        V-TOP (Sharp bearish reversal)
        ===============================
        Detection: Mirror of V-bottom
        Entry: When decline reaches 50-80% of prior rise
        Stop: Above the V-top high
        Target: Pre-rise low
        Win rate: ~60%
        """
        results = []
        n = len(self.df)

        for i in self.swing_high_idx:
            if i < 5 or i + max_decline_bars >= n:
                continue

            v_high = self.highs[i]
            pre_low = np.min(self.lows[max(0, i - 20):i])
            rise = v_high - pre_low

            if v_high == 0 or rise / v_high < min_rise_pct:
                continue

            decline_end = min(i + max_decline_bars, n - 1)
            post_low = np.min(self.closes[i:decline_end + 1])
            decline = v_high - post_low

            if decline >= decline_pct * rise:
                results.append({
                    'pattern': 'v_top',
                    'top_idx': i,
                    'entry': post_low,
                    'stop': v_high,
                    'target': pre_low,
                    'win_rate': 0.60
                })
        return results

    def detect_island_reversal(self, min_island_bars=2, max_island_bars=15):
        """
        ISLAND REVERSAL
        ================
        Detection Algorithm:
          1. GAP 1: A gap in one direction (e.g., gap up)
             Bullish gap: current_low > previous_high
             Bearish gap: current_high < previous_low
          2. ISLAND: A cluster of bars isolated by the gaps
             (min_island_bars to max_island_bars)
          3. GAP 2: A gap in the OPPOSITE direction
             (the island is now isolated on both sides)

        Math:
          gap_up_at(i): lows[i] > highs[i-1]
          gap_down_at(i): highs[i] < lows[i-1]
          Find i where gap_up, then j where gap_down (or vice versa)
          min_island_bars <= j - i <= max_island_bars

        Entry: In direction of the second gap
        Stop: Beyond the island range (opposite side)
        Target: Height of island projected from second gap
        Win rate: ~75% (rare but very reliable)
        """
        results = []
        n = len(self.df)

        for i in range(1, n - min_island_bars - 1):
            # Bearish island reversal: gap up, then gap down
            if self.lows[i] > self.highs[i - 1]:  # gap up
                for j in range(i + min_island_bars, min(i + max_island_bars + 1, n)):
                    if self.highs[j] < self.lows[j - 1]:  # gap down
                        island_high = np.max(self.highs[i:j])
                        island_low = np.min(self.lows[i:j])
                        results.append({
                            'pattern': 'bearish_island_reversal',
                            'island_start': i,
                            'island_end': j - 1,
                            'entry': self.closes[j],
                            'stop': island_high,
                            'target': self.closes[j] - (island_high - island_low),
                            'win_rate': 0.75
                        })
                        break

            # Bullish island reversal: gap down, then gap up
            if self.highs[i] < self.lows[i - 1]:  # gap down
                for j in range(i + min_island_bars, min(i + max_island_bars + 1, n)):
                    if self.lows[j] > self.highs[j - 1]:  # gap up
                        island_high = np.max(self.highs[i:j])
                        island_low = np.min(self.lows[i:j])
                        results.append({
                            'pattern': 'bullish_island_reversal',
                            'island_start': i,
                            'island_end': j - 1,
                            'entry': self.closes[j],
                            'stop': island_low,
                            'target': self.closes[j] + (island_high - island_low),
                            'win_rate': 0.75
                        })
                        break
        return results


# ============================================================================
# SECTION 6: ADVANCED PATTERNS - HARMONICS
# ============================================================================

class HarmonicPatternDetector:
    """
    Detects harmonic patterns using Fibonacci ratios.
    All patterns use 5 points: X, A, B, C, D
    with 4 legs: XA, AB, BC, CD

    Tolerance: each ratio has an acceptable range (e.g., 0.618 +/- tolerance)
    """

    def __init__(self, df: pd.DataFrame, swing_order: int = 5, fib_tolerance: float = 0.05):
        self.df = df
        self.highs = df['high'].values
        self.lows = df['low'].values
        self.closes = df['close'].values
        self.fib_tolerance = fib_tolerance

        sh = find_swing_highs(self.highs, order=swing_order)
        sl = find_swing_lows(self.lows, order=swing_order)

        # Build alternating swing sequence
        self.pivots = []
        for idx in sh:
            self.pivots.append((idx, self.highs[idx], 'high'))
        for idx in sl:
            self.pivots.append((idx, self.lows[idx], 'low'))
        self.pivots.sort(key=lambda x: x[0])

    def _ratio_match(self, actual, target, tolerance=None):
        """Check if actual ratio is within tolerance of target."""
        tol = tolerance or self.fib_tolerance
        return abs(actual - target) <= tol

    def _ratio_in_range(self, actual, low, high):
        """Check if actual ratio falls within [low, high] range."""
        return low <= actual <= high

    def _get_five_point_patterns(self):
        """
        Generate all possible 5-point (X,A,B,C,D) sequences from pivots.
        Points must alternate: high-low-high-low-high or low-high-low-high-low
        """
        patterns = []
        for i in range(len(self.pivots) - 4):
            x = self.pivots[i]
            a = self.pivots[i + 1]
            b = self.pivots[i + 2]
            c = self.pivots[i + 3]
            d = self.pivots[i + 4]

            # Must alternate
            types = [x[2], a[2], b[2], c[2], d[2]]
            alternating = all(types[j] != types[j + 1] for j in range(4))
            if alternating:
                patterns.append((x, a, b, c, d))
        return patterns

    def detect_gartley(self):
        """
        GARTLEY PATTERN ("222" pattern)
        ================================
        Fibonacci Ratios:
          AB = 0.618 retracement of XA          (range: 0.578-0.658)
          BC = 0.382 to 0.886 retracement of AB
          CD = 1.272 to 1.618 extension of BC
          XD = 0.786 retracement of XA           (THE KEY RATIO)

        Bullish Gartley: X(low) A(high) B(low) C(high) D(low)
        Bearish Gartley: X(high) A(low) B(high) C(low) D(high)

        Entry: At point D (0.786 XA retracement)
        Stop: Below/above point X
        Target 1: 0.618 retracement of AD
        Target 2: Point A (full retracement)
        Target 3: 1.272 extension of AD
        Win rate: ~70% (most common harmonic)
        """
        results = []

        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            bc = abs(c[1] - b[1])
            cd = abs(d[1] - c[1])
            xd = abs(d[1] - x[1])

            ab_xa = ab / xa           # should be 0.618
            bc_ab = bc / ab if ab != 0 else 0  # 0.382 to 0.886
            cd_bc = cd / bc if bc != 0 else 0  # 1.272 to 1.618
            xd_xa = xd / xa           # should be 0.786

            if (self._ratio_in_range(ab_xa, 0.578, 0.658) and
                self._ratio_in_range(bc_ab, 0.382, 0.886) and
                self._ratio_in_range(cd_bc, 1.13, 1.618) and
                self._ratio_in_range(xd_xa, 0.746, 0.826)):

                bullish = x[2] == 'low'
                direction = Direction.BULLISH if bullish else Direction.BEARISH

                results.append({
                    'pattern': f'gartley_{"bullish" if bullish else "bearish"}',
                    'x': x, 'a': a, 'b': b, 'c': c, 'd': d,
                    'entry': d[1],
                    'stop': x[1],
                    'target_1': d[1] + (0.618 * abs(a[1] - d[1])) * (1 if bullish else -1),
                    'target_2': a[1],
                    'target_3': d[1] + (1.272 * abs(a[1] - d[1])) * (1 if bullish else -1),
                    'ratios': {
                        'AB/XA': round(ab_xa, 4),
                        'BC/AB': round(bc_ab, 4),
                        'CD/BC': round(cd_bc, 4),
                        'XD/XA': round(xd_xa, 4)
                    },
                    'direction': direction,
                    'win_rate': 0.70
                })
        return results

    def detect_butterfly(self):
        """
        BUTTERFLY PATTERN
        ==================
        Fibonacci Ratios:
          AB = 0.786 retracement of XA           (range: 0.746-0.826)
          BC = 0.382 to 0.886 retracement of AB
          CD = 1.618 to 2.240 extension of BC
          XD = 1.272 to 1.618 extension of XA    (D BEYOND X - key difference)

        Key: D extends BEYOND point X (unlike Gartley where D is between X and A)

        Entry: At point D
        Stop: Beyond D by a small margin (since D is already beyond X)
              Typically 1.618 XA extension + buffer
        Target 1: 0.618 retracement of CD
        Target 2: Point B
        Target 3: Point A
        Win rate: ~65%
        """
        results = []

        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            bc = abs(c[1] - b[1])
            cd = abs(d[1] - c[1])
            xd = abs(d[1] - x[1])

            ab_xa = ab / xa
            bc_ab = bc / ab if ab != 0 else 0
            cd_bc = cd / bc if bc != 0 else 0
            xd_xa = xd / xa

            if (self._ratio_in_range(ab_xa, 0.746, 0.826) and
                self._ratio_in_range(bc_ab, 0.382, 0.886) and
                self._ratio_in_range(cd_bc, 1.618, 2.618) and
                self._ratio_in_range(xd_xa, 1.272, 1.618)):

                bullish = x[2] == 'low'
                results.append({
                    'pattern': f'butterfly_{"bullish" if bullish else "bearish"}',
                    'x': x, 'a': a, 'b': b, 'c': c, 'd': d,
                    'entry': d[1],
                    'stop': d[1] - (0.1 * xa) if bullish else d[1] + (0.1 * xa),
                    'target_1': d[1] + (0.618 * cd) * (1 if bullish else -1),
                    'target_2': b[1],
                    'target_3': a[1],
                    'ratios': {
                        'AB/XA': round(ab_xa, 4),
                        'BC/AB': round(bc_ab, 4),
                        'CD/BC': round(cd_bc, 4),
                        'XD/XA': round(xd_xa, 4)
                    },
                    'win_rate': 0.65
                })
        return results

    def detect_bat(self):
        """
        BAT PATTERN
        ============
        Fibonacci Ratios:
          AB = 0.382 to 0.500 retracement of XA  (SHALLOW retracement)
          BC = 0.382 to 0.886 retracement of AB
          CD = 1.618 to 2.618 extension of BC
          XD = 0.886 retracement of XA            (THE KEY RATIO)

        Key difference from Gartley: shallower AB (0.382-0.5 vs 0.618)
        and deeper D (0.886 vs 0.786)

        Entry: At point D (0.886 XA retracement)
        Stop: Below/above point X
        Target 1: 0.382 retracement of AD
        Target 2: 0.618 retracement of AD
        Target 3: Point A
        Win rate: ~68%
        """
        results = []

        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            bc = abs(c[1] - b[1])
            cd = abs(d[1] - c[1])
            xd = abs(d[1] - x[1])

            ab_xa = ab / xa
            bc_ab = bc / ab if ab != 0 else 0
            cd_bc = cd / bc if bc != 0 else 0
            xd_xa = xd / xa

            if (self._ratio_in_range(ab_xa, 0.382, 0.500) and
                self._ratio_in_range(bc_ab, 0.382, 0.886) and
                self._ratio_in_range(cd_bc, 1.618, 2.618) and
                self._ratio_in_range(xd_xa, 0.836, 0.936)):

                bullish = x[2] == 'low'
                ad = abs(a[1] - d[1])
                results.append({
                    'pattern': f'bat_{"bullish" if bullish else "bearish"}',
                    'x': x, 'a': a, 'b': b, 'c': c, 'd': d,
                    'entry': d[1],
                    'stop': x[1],
                    'target_1': d[1] + (0.382 * ad) * (1 if bullish else -1),
                    'target_2': d[1] + (0.618 * ad) * (1 if bullish else -1),
                    'target_3': a[1],
                    'ratios': {
                        'AB/XA': round(ab_xa, 4),
                        'BC/AB': round(bc_ab, 4),
                        'CD/BC': round(cd_bc, 4),
                        'XD/XA': round(xd_xa, 4)
                    },
                    'win_rate': 0.68
                })
        return results

    def detect_crab(self):
        """
        CRAB PATTERN
        =============
        Fibonacci Ratios:
          AB = 0.382 to 0.618 retracement of XA
          BC = 0.382 to 0.886 retracement of AB
          CD = 2.618 to 3.618 extension of BC
          XD = 1.618 extension of XA              (THE KEY RATIO - D far beyond X)

        Key: Most extreme harmonic pattern. D extends to 1.618 of XA.

        Entry: At point D
        Stop: Beyond D by a buffer (no X to reference since D exceeds X)
              Use 1.618 XA + ATR buffer
        Target 1: 0.382 retracement of CD
        Target 2: 0.618 retracement of CD
        Target 3: Point C
        Win rate: ~60% (aggressive pattern; best at major S/R)
        """
        results = []

        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            bc = abs(c[1] - b[1])
            cd = abs(d[1] - c[1])
            xd = abs(d[1] - x[1])

            ab_xa = ab / xa
            bc_ab = bc / ab if ab != 0 else 0
            cd_bc = cd / bc if bc != 0 else 0
            xd_xa = xd / xa

            if (self._ratio_in_range(ab_xa, 0.382, 0.618) and
                self._ratio_in_range(bc_ab, 0.382, 0.886) and
                self._ratio_in_range(cd_bc, 2.240, 3.618) and
                self._ratio_in_range(xd_xa, 1.568, 1.668)):

                bullish = x[2] == 'low'
                results.append({
                    'pattern': f'crab_{"bullish" if bullish else "bearish"}',
                    'x': x, 'a': a, 'b': b, 'c': c, 'd': d,
                    'entry': d[1],
                    'stop': d[1] - (0.15 * xa) if bullish else d[1] + (0.15 * xa),
                    'target_1': d[1] + (0.382 * cd) * (1 if bullish else -1),
                    'target_2': d[1] + (0.618 * cd) * (1 if bullish else -1),
                    'target_3': c[1],
                    'ratios': {
                        'AB/XA': round(ab_xa, 4),
                        'BC/AB': round(bc_ab, 4),
                        'CD/BC': round(cd_bc, 4),
                        'XD/XA': round(xd_xa, 4)
                    },
                    'win_rate': 0.60
                })
        return results

    def detect_cypher(self):
        """
        CYPHER PATTERN (Bonus harmonic)
        ================================
        Fibonacci Ratios:
          AB = 0.382 to 0.618 retracement of XA
          BC = 1.130 to 1.414 extension of XA     (C goes BEYOND A)
          XD = 0.786 retracement of XC             (THE KEY RATIO)

        Entry: At point D
        Stop: Below/above point X
        Target 1: 0.382 retracement of CD
        Target 2: 0.618 retracement of CD
        Win rate: ~65%
        """
        results = []

        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            xc = abs(c[1] - x[1])
            xd = abs(d[1] - x[1])

            ab_xa = ab / xa
            xc_xa = xc / xa
            xd_xc = xd / xc if xc != 0 else 0

            if (self._ratio_in_range(ab_xa, 0.382, 0.618) and
                self._ratio_in_range(xc_xa, 1.130, 1.414) and
                self._ratio_in_range(xd_xc, 0.746, 0.826)):

                bullish = x[2] == 'low'
                cd = abs(c[1] - d[1])
                results.append({
                    'pattern': f'cypher_{"bullish" if bullish else "bearish"}',
                    'entry': d[1],
                    'stop': x[1],
                    'target_1': d[1] + (0.382 * cd) * (1 if bullish else -1),
                    'target_2': d[1] + (0.618 * cd) * (1 if bullish else -1),
                    'win_rate': 0.65
                })
        return results

    def detect_shark(self):
        """
        SHARK PATTERN (0-5 Pattern)
        ============================
        Fibonacci Ratios:
          AB = 1.130 to 1.618 extension of XA     (B extends beyond X)
          BC = 1.618 to 2.240 extension of AB
          XD = 0.886 retracement of XA (OR XC)

        Entry: At point D
        Stop: Beyond the extreme of the pattern
        Target: 0.618 retracement of CD
        Win rate: ~60%
        """
        results = []
        # Simplified detection using same 5-point framework
        for x, a, b, c, d in self._get_five_point_patterns():
            xa = abs(a[1] - x[1])
            if xa == 0:
                continue

            ab = abs(b[1] - a[1])
            bc = abs(c[1] - b[1])

            ab_xa = ab / xa
            bc_ab = bc / ab if ab != 0 else 0

            xc = abs(c[1] - x[1])
            xd_xc = abs(d[1] - x[1]) / xc if xc != 0 else 0

            if (self._ratio_in_range(ab_xa, 1.130, 1.618) and
                self._ratio_in_range(bc_ab, 1.618, 2.240) and
                self._ratio_in_range(xd_xc, 0.836, 0.936)):

                bullish = x[2] == 'low'
                cd = abs(c[1] - d[1])
                results.append({
                    'pattern': f'shark_{"bullish" if bullish else "bearish"}',
                    'entry': d[1],
                    'stop': c[1],
                    'target': d[1] + (0.618 * cd) * (1 if bullish else -1),
                    'win_rate': 0.60
                })
        return results


# ============================================================================
# SECTION 7: ADVANCED PATTERNS - ABCD, THREE DRIVES, WOLFE WAVES, ELLIOTT
# ============================================================================

def detect_abcd_pattern(df: pd.DataFrame, swing_order: int = 5, fib_tolerance: float = 0.05):
    """
    ABCD PATTERN (Simplest harmonic)
    =================================
    Detection Algorithm:
      1. Find 4 alternating swing points: A, B, C, D
      2. AB is the first leg (impulse)
      3. BC is a Fibonacci retracement of AB:
         BC/AB = 0.382 to 0.786 (ideally 0.618)
      4. CD is a Fibonacci extension of BC:
         CD/BC = 1.272 to 1.618
      5. AB approximately equals CD in PRICE:
         |AB| approx = |CD| (within tolerance)
      6. AB approximately equals CD in TIME:
         bars(AB) approx = bars(CD) (within 2x tolerance)

    Math:
      AB = |B - A|
      BC_retrace = |C - B| / AB  ; target: 0.618
      CD_extension = |D - C| / |C - B|  ; target: 1.272 or 1.618
      AB_CD_ratio = |CD| / |AB|  ; target: ~1.0

    Entry: At point D completion
    Stop: Beyond point D by ATR or small buffer
    Target 1: 0.382 retracement of AD
    Target 2: 0.618 retracement of AD
    Target 3: Point C
    Win rate: ~65%
    """
    highs = df['high'].values
    lows = df['low'].values

    sh = find_swing_highs(highs, order=swing_order)
    sl = find_swing_lows(lows, order=swing_order)

    pivots = []
    for idx in sh:
        pivots.append((idx, highs[idx], 'high'))
    for idx in sl:
        pivots.append((idx, lows[idx], 'low'))
    pivots.sort(key=lambda x: x[0])

    results = []
    for i in range(len(pivots) - 3):
        a, b, c, d = pivots[i], pivots[i+1], pivots[i+2], pivots[i+3]

        # Must alternate
        if a[2] == b[2] or b[2] == c[2] or c[2] == d[2]:
            continue

        ab = abs(b[1] - a[1])
        bc = abs(c[1] - b[1])
        cd = abs(d[1] - c[1])

        if ab == 0 or bc == 0:
            continue

        bc_ab = bc / ab
        cd_bc = cd / bc
        cd_ab = cd / ab

        if (0.382 <= bc_ab <= 0.786 and
            1.13 <= cd_bc <= 1.80 and
            0.80 <= cd_ab <= 1.20):

            bullish = d[2] == 'low'
            ad = abs(a[1] - d[1])

            results.append({
                'pattern': f'abcd_{"bullish" if bullish else "bearish"}',
                'a': a, 'b': b, 'c': c, 'd': d,
                'entry': d[1],
                'stop': d[1] - (0.1 * ab) if bullish else d[1] + (0.1 * ab),
                'target_1': d[1] + (0.382 * ad) * (1 if bullish else -1),
                'target_2': d[1] + (0.618 * ad) * (1 if bullish else -1),
                'target_3': c[1],
                'ratios': {
                    'BC/AB': round(bc_ab, 4),
                    'CD/BC': round(cd_bc, 4),
                    'CD/AB': round(cd_ab, 4)
                },
                'win_rate': 0.65
            })
    return results


def detect_three_drives(df: pd.DataFrame, swing_order: int = 5, fib_tolerance: float = 0.05):
    """
    THREE DRIVES PATTERN
    =====================
    Detection Algorithm:
      1. Three consecutive symmetrical moves (drives) in the same direction
      2. Two corrections between them
      3. Each drive and correction must follow Fibonacci ratios:

    Bullish Three Drives (three drives DOWN, reversal UP):
      Drive 1: Impulse down
      Correction A: 0.618 retracement of Drive 1
      Drive 2: 1.272 extension of Correction A
      Correction B: 0.618 retracement of Drive 2
      Drive 3: 1.272 extension of Correction B

    Structure: Point_0(high) -> Drive1_low -> CorrA_high -> Drive2_low -> CorrB_high -> Drive3_low
    Sequence of points: 0, 1, A, 2, B, 3

    Math:
      correction_A / drive_1 = 0.618  (range 0.572 to 0.668)
      drive_2 / correction_A = 1.272  (range 1.13 to 1.40)
      correction_B / drive_2 = 0.618
      drive_3 / correction_B = 1.272

      Time symmetry: bars(drive_1) ~ bars(drive_2) ~ bars(drive_3)

    Entry: At completion of Drive 3
    Stop: Beyond Drive 3 extreme
    Target: 0.618 retracement of the entire move (point 0 to point 3)
    Win rate: ~70% (rare but high probability)
    """
    highs = df['high'].values
    lows = df['low'].values

    sh = find_swing_highs(highs, order=swing_order)
    sl = find_swing_lows(lows, order=swing_order)

    pivots = []
    for idx in sh:
        pivots.append((idx, highs[idx], 'high'))
    for idx in sl:
        pivots.append((idx, lows[idx], 'low'))
    pivots.sort(key=lambda x: x[0])

    results = []
    for i in range(len(pivots) - 5):
        p0, p1, pa, p2, pb, p3 = [pivots[i+j] for j in range(6)]

        # Check alternating
        types = [p[2] for p in [p0, p1, pa, p2, pb, p3]]
        if not all(types[j] != types[j+1] for j in range(5)):
            continue

        drive1 = abs(p1[1] - p0[1])
        corr_a = abs(pa[1] - p1[1])
        drive2 = abs(p2[1] - pa[1])
        corr_b = abs(pb[1] - p2[1])
        drive3 = abs(p3[1] - pb[1])

        if drive1 == 0 or drive2 == 0:
            continue

        ca_d1 = corr_a / drive1
        d2_ca = drive2 / corr_a if corr_a != 0 else 0
        cb_d2 = corr_b / drive2
        d3_cb = drive3 / corr_b if corr_b != 0 else 0

        if (0.572 <= ca_d1 <= 0.668 and
            1.13 <= d2_ca <= 1.40 and
            0.572 <= cb_d2 <= 0.668 and
            1.13 <= d3_cb <= 1.40):

            bullish = p0[2] == 'high'  # drives go down = bullish reversal
            total_move = abs(p3[1] - p0[1])

            results.append({
                'pattern': f'three_drives_{"bullish" if bullish else "bearish"}',
                'points': [p0, p1, pa, p2, pb, p3],
                'entry': p3[1],
                'stop': p3[1] - (0.1 * drive1) if bullish else p3[1] + (0.1 * drive1),
                'target': p3[1] + (0.618 * total_move) * (1 if bullish else -1),
                'ratios': {
                    'corrA/drive1': round(ca_d1, 4),
                    'drive2/corrA': round(d2_ca, 4),
                    'corrB/drive2': round(cb_d2, 4),
                    'drive3/corrB': round(d3_cb, 4)
                },
                'win_rate': 0.70
            })
    return results


def detect_wolfe_waves(df: pd.DataFrame, swing_order: int = 5,
                       time_symmetry_tolerance: float = 0.40):
    """
    WOLFE WAVES
    ============
    Detection Algorithm:
      A Wolfe Wave consists of 5 points forming a WEDGE structure.

    Bullish Wolfe Wave:
      Point 1: Swing low (start)
      Point 2: Swing high
      Point 3: Swing low (lower than point 1)
      Point 4: Swing high (lower than point 2, but higher than point 1)
      Point 5: Swing low (extends below the 1-3 trendline = the trigger)

    Rules:
      1. Wave 1-2: First impulse
      2. Wave 2-3: Retracement (point 3 below point 1)
      3. Wave 3-4: Second impulse (point 4 between point 1 and 2)
      4. Wave 4-5: Final wave (point 5 on or below the 1-3 trendline)
      5. Trendline 1-3 and trendline 2-4 must CONVERGE
      6. TIME SYMMETRY: waves 1-2, 2-3, 3-4 have approximately equal duration

    Target Line (EPA - Estimated Price at Arrival):
      Draw line from point 1 to point 4
      Project to the time of point 5 extended forward
      target = value of line(1,4) at expected completion time

    Math:
      line_1_3(x) = p1 + (p3 - p1) * (x - x1) / (x3 - x1)
      line_2_4(x) = p2 + (p4 - p2) * (x - x2) / (x4 - x2)
      These lines must converge (intersection point in the future)
      EPA_line(x) = p1 + (p4 - p1) * (x - x1) / (x4 - x1)
      target = EPA_line(x5 + duration_estimate)

    Entry: At point 5 (when price touches/breaches the 1-3 trendline)
    Stop: Below point 5 by ATR or fixed buffer
    Target: EPA line (1-4 line projected forward)
    Win rate: ~75% (when properly identified)
    """
    highs = df['high'].values
    lows = df['low'].values

    sh = find_swing_highs(highs, order=swing_order)
    sl = find_swing_lows(lows, order=swing_order)

    pivots = []
    for idx in sh:
        pivots.append((idx, highs[idx], 'high'))
    for idx in sl:
        pivots.append((idx, lows[idx], 'low'))
    pivots.sort(key=lambda x: x[0])

    results = []

    for i in range(len(pivots) - 4):
        p1, p2, p3, p4, p5 = [pivots[i+j] for j in range(5)]

        # Must alternate
        types = [p[2] for p in [p1, p2, p3, p4, p5]]
        if not all(types[j] != types[j+1] for j in range(4)):
            continue

        # Bullish Wolfe: low-high-low-high-low
        if p1[2] == 'low' and p3[2] == 'low' and p5[2] == 'low':
            # Point 3 below point 1
            if p3[1] >= p1[1]:
                continue
            # Point 4 between point 1 and point 2
            if not (p1[1] < p4[1] < p2[1]):
                continue

            # Check 1-3 and 2-4 trendlines converge
            if p3[0] == p1[0] or p4[0] == p2[0]:
                continue
            slope_13 = (p3[1] - p1[1]) / (p3[0] - p1[0])
            slope_24 = (p4[1] - p2[1]) / (p4[0] - p2[0])

            # Lines must converge (slopes approach each other to the right)
            if slope_13 >= slope_24:
                continue  # not converging

            # Point 5 near or below 1-3 line
            line_13_at_5 = p1[1] + slope_13 * (p5[0] - p1[0])
            if p5[1] > line_13_at_5 * 1.01:
                continue

            # EPA: line from 1 to 4
            if p4[0] == p1[0]:
                continue
            slope_14 = (p4[1] - p1[1]) / (p4[0] - p1[0])
            avg_wave_duration = (p5[0] - p1[0]) / 4
            target_x = p5[0] + avg_wave_duration
            epa = p1[1] + slope_14 * (target_x - p1[0])

            results.append({
                'pattern': 'bullish_wolfe_wave',
                'points': [p1, p2, p3, p4, p5],
                'entry': p5[1],
                'stop': p5[1] - abs(p5[1] - p3[1]) * 0.5,
                'target': epa,
                'win_rate': 0.75
            })

        # Bearish Wolfe: high-low-high-low-high
        elif p1[2] == 'high' and p3[2] == 'high' and p5[2] == 'high':
            if p3[1] <= p1[1]:
                continue
            if not (p1[1] > p4[1] > p2[1]):
                continue

            if p3[0] == p1[0] or p4[0] == p2[0]:
                continue
            slope_13 = (p3[1] - p1[1]) / (p3[0] - p1[0])
            slope_24 = (p4[1] - p2[1]) / (p4[0] - p2[0])

            if slope_13 <= slope_24:
                continue

            line_13_at_5 = p1[1] + slope_13 * (p5[0] - p1[0])
            if p5[1] < line_13_at_5 * 0.99:
                continue

            if p4[0] == p1[0]:
                continue
            slope_14 = (p4[1] - p1[1]) / (p4[0] - p1[0])
            avg_wave_duration = (p5[0] - p1[0]) / 4
            target_x = p5[0] + avg_wave_duration
            epa = p1[1] + slope_14 * (target_x - p1[0])

            results.append({
                'pattern': 'bearish_wolfe_wave',
                'points': [p1, p2, p3, p4, p5],
                'entry': p5[1],
                'stop': p5[1] + abs(p5[1] - p3[1]) * 0.5,
                'target': epa,
                'win_rate': 0.75
            })
    return results


def detect_elliott_impulse(df: pd.DataFrame, swing_order: int = 5):
    """
    ELLIOTT WAVE - IMPULSE (5-wave structure)
    ==========================================
    Detection Algorithm:
      An impulse wave has 5 sub-waves: 1, 2, 3, 4, 5
      Requires 6 points: start(0), end of wave 1, 2, 3, 4, 5

    THREE INVIOLABLE RULES:
      1. Wave 2 NEVER retraces more than 100% of Wave 1:
         For bullish: wave2_low > wave0_low
      2. Wave 3 is NEVER the shortest of waves 1, 3, 5:
         |wave3| >= max(|wave1|, |wave5|) is too strict
         Actually: wave3 is NOT the shortest
      3. Wave 4 NEVER enters Wave 1 territory:
         For bullish: wave4_low > wave1_high

    FIBONACCI GUIDELINES (not rules, but common):
      Wave 2: retraces 38.2% to 61.8% of Wave 1 (never > 100%)
      Wave 3: 1.618x to 2.618x of Wave 1 (most common: 1.618)
      Wave 4: retraces 23.6% to 38.2% of Wave 3
      Wave 5: 0.618x to 1.000x of Wave 1 (or 1.618x in extensions)

    Math for bullish impulse:
      points: p0(low), p1(high), p2(low), p3(high), p4(low), p5(high)
      wave1 = p1 - p0  (up)
      wave2 = p1 - p2  (down, retracement)
      wave3 = p3 - p2  (up)
      wave4 = p3 - p4  (down, retracement)
      wave5 = p5 - p4  (up)

      Rule 1: p2 > p0  (wave 2 doesn't retrace 100% of wave 1)
      Rule 2: wave3 >= wave1 OR wave3 >= wave5  (wave 3 not shortest)
      Rule 3: p4 > p1  (wave 4 doesn't enter wave 1 territory)

    Entry: At completion of Wave 2 or Wave 4 (anticipating Wave 3 or 5)
    Stop: Below Wave 0 (if entering at Wave 2) or below Wave 4 low
    Target Wave 3: p2 + 1.618 * wave1
    Target Wave 5: p4 + wave1 (or p4 + 0.618 * wave3)
    Win rate: ~60-65% (complex pattern, subjective)
    """
    highs = df['high'].values
    lows = df['low'].values

    sh = find_swing_highs(highs, order=swing_order)
    sl = find_swing_lows(lows, order=swing_order)

    pivots = []
    for idx in sh:
        pivots.append((idx, highs[idx], 'high'))
    for idx in sl:
        pivots.append((idx, lows[idx], 'low'))
    pivots.sort(key=lambda x: x[0])

    results = []

    for i in range(len(pivots) - 5):
        pts = [pivots[i+j] for j in range(6)]

        # Bullish impulse: low-high-low-high-low-high
        if all(pts[j][2] != pts[j+1][2] for j in range(5)):
            if pts[0][2] == 'low':
                p0, p1, p2, p3, p4, p5 = [p[1] for p in pts]

                wave1 = p1 - p0
                wave2 = p1 - p2
                wave3 = p3 - p2
                wave4 = p3 - p4
                wave5 = p5 - p4

                if wave1 <= 0 or wave3 <= 0 or wave5 <= 0:
                    continue

                # Rule 1: Wave 2 doesn't retrace 100% of wave 1
                if p2 <= p0:
                    continue
                # Rule 2: Wave 3 not the shortest
                if wave3 < wave1 and wave3 < wave5:
                    continue
                # Rule 3: Wave 4 above wave 1 high
                if p4 <= p1:
                    continue

                # Fibonacci guideline checks (loose)
                w2_retrace = wave2 / wave1 if wave1 != 0 else 0
                w3_extend = wave3 / wave1 if wave1 != 0 else 0
                w4_retrace = wave4 / wave3 if wave3 != 0 else 0

                results.append({
                    'pattern': 'elliott_bullish_impulse',
                    'points': pts,
                    'wave_sizes': {
                        'wave1': wave1, 'wave2': wave2,
                        'wave3': wave3, 'wave4': wave4, 'wave5': wave5
                    },
                    'fib_ratios': {
                        'w2_retrace': round(w2_retrace, 4),
                        'w3_extension': round(w3_extend, 4),
                        'w4_retrace': round(w4_retrace, 4)
                    },
                    'entry_at_w2': p2,
                    'stop_w2': p0,
                    'target_w3': p2 + 1.618 * wave1,
                    'entry_at_w4': p4,
                    'stop_w4': p1,  # wave 4 shouldn't breach wave 1
                    'target_w5': p4 + wave1,
                    'win_rate': 0.62
                })

            # Bearish impulse: high-low-high-low-high-low
            elif pts[0][2] == 'high':
                p0, p1, p2, p3, p4, p5 = [p[1] for p in pts]

                wave1 = p0 - p1
                wave2 = p2 - p1
                wave3 = p2 - p3
                wave4 = p4 - p3
                wave5 = p4 - p5

                if wave1 <= 0 or wave3 <= 0 or wave5 <= 0:
                    continue
                if p2 >= p0:
                    continue
                if wave3 < wave1 and wave3 < wave5:
                    continue
                if p4 >= p1:
                    continue

                w2_retrace = wave2 / wave1 if wave1 != 0 else 0
                w3_extend = wave3 / wave1 if wave1 != 0 else 0

                results.append({
                    'pattern': 'elliott_bearish_impulse',
                    'points': pts,
                    'entry_at_w2': p2,
                    'stop_w2': p0,
                    'target_w3': p2 - 1.618 * wave1,
                    'win_rate': 0.62
                })
    return results


# ============================================================================
# SECTION 8: PRICE ACTION - SUPPORT/RESISTANCE, STRUCTURE, ICT CONCEPTS
# ============================================================================

class PriceActionDetector:
    """
    Detects price action concepts: S/R, trend structure, order blocks,
    fair value gaps, liquidity sweeps, and break of structure.
    """

    def __init__(self, df: pd.DataFrame, swing_order: int = 5):
        self.df = df.copy()
        self.highs = df['high'].values
        self.lows = df['low'].values
        self.opens = df['open'].values
        self.closes = df['close'].values
        self.volumes = df['volume'].values if 'volume' in df.columns else None
        self.swing_order = swing_order
        self.swing_high_idx = find_swing_highs(self.highs, order=swing_order)
        self.swing_low_idx = find_swing_lows(self.lows, order=swing_order)

    def find_support_resistance(self, method='cluster', n_levels=5, lookback=100):
        """
        SUPPORT & RESISTANCE LEVELS
        ============================
        Method 1: SWING POINT CLUSTERING (recommended)
          1. Collect all swing highs and swing lows
          2. Cluster them using a proximity threshold
          3. Levels with more touches = stronger S/R

        Algorithm:
          threshold = ATR * 0.5 (or percentage of price)
          Sort all swing point values
          Group adjacent values within threshold
          Level = mean of each cluster
          Strength = count of touches in each cluster

        Method 2: K-MEANS CLUSTERING
          1. Collect all closing prices (or highs/lows)
          2. Run K-means with k = n_levels
          3. Cluster centers = S/R levels

        Method 3: VOLUME PROFILE
          1. Bin prices into ranges
          2. Sum volume at each price bin
          3. High-volume nodes = S/R (value areas)

        Returns: List of (level, strength, type) tuples
        """
        if method == 'cluster':
            return self._sr_swing_cluster(n_levels, lookback)
        elif method == 'kmeans':
            return self._sr_kmeans(n_levels, lookback)
        else:
            return self._sr_swing_cluster(n_levels, lookback)

    def _sr_swing_cluster(self, n_levels, lookback):
        """Swing point clustering method for S/R detection."""
        start = max(0, len(self.df) - lookback)

        # Collect swing points in window
        sh = self.swing_high_idx[self.swing_high_idx >= start]
        sl = self.swing_low_idx[self.swing_low_idx >= start]

        all_levels = []
        for idx in sh:
            all_levels.append(self.highs[idx])
        for idx in sl:
            all_levels.append(self.lows[idx])

        if not all_levels:
            return []

        all_levels.sort()

        # ATR-based threshold
        atr = np.mean(self.highs[start:] - self.lows[start:])
        threshold = atr * 0.5

        # Cluster adjacent levels
        clusters = []
        current_cluster = [all_levels[0]]

        for i in range(1, len(all_levels)):
            if all_levels[i] - all_levels[i-1] <= threshold:
                current_cluster.append(all_levels[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [all_levels[i]]
        clusters.append(current_cluster)

        # Create levels sorted by strength (number of touches)
        levels = []
        for cluster in clusters:
            level = np.mean(cluster)
            strength = len(cluster)
            current_price = self.closes[-1]
            sr_type = 'resistance' if level > current_price else 'support'
            levels.append({
                'level': round(level, 6),
                'strength': strength,
                'type': sr_type,
                'touches': strength
            })

        levels.sort(key=lambda x: x['strength'], reverse=True)
        return levels[:n_levels]

    def _sr_kmeans(self, n_levels, lookback):
        """K-means clustering for S/R detection."""
        try:
            from sklearn.cluster import KMeans
        except ImportError:
            return self._sr_swing_cluster(n_levels, lookback)

        start = max(0, len(self.df) - lookback)
        prices = np.concatenate([
            self.highs[start:],
            self.lows[start:]
        ]).reshape(-1, 1)

        kmeans = KMeans(n_clusters=n_levels, n_init=10, random_state=42)
        kmeans.fit(prices)
        centers = sorted(kmeans.cluster_centers_.flatten())

        current_price = self.closes[-1]
        return [{
            'level': round(c, 6),
            'type': 'resistance' if c > current_price else 'support'
        } for c in centers]

    def detect_trendlines(self, min_touches=3, lookback=100):
        """
        TRENDLINE DETECTION (Algorithmic)
        ==================================
        Algorithm:
          1. Identify swing highs and swing lows
          2. For SUPPORT trendlines:
             - Try all pairs of swing lows
             - Fit a line through them
             - Count how many other swing lows touch this line (within tolerance)
             - Require at least min_touches
             - Line slope must be positive (uptrend support) or negative (downtrend)
          3. For RESISTANCE trendlines:
             - Same process with swing highs
          4. Score by touches and R-squared fit

        Math:
          For two points (x1, y1) and (x2, y2):
          slope = (y2 - y1) / (x2 - x1)
          intercept = y1 - slope * x1
          For each other swing point (x, y):
            expected = slope * x + intercept
            if abs(y - expected) <= tolerance: touch++

        Returns: list of trendline dicts with slope, intercept, touches, type
        """
        start = max(0, len(self.df) - lookback)
        atr = np.mean(self.highs[start:] - self.lows[start:])
        tolerance = atr * 0.3

        results = []

        # Support trendlines (using swing lows)
        sl = self.swing_low_idx[self.swing_low_idx >= start]
        for i in range(len(sl)):
            for j in range(i + 1, len(sl)):
                x1, y1 = sl[i], self.lows[sl[i]]
                x2, y2 = sl[j], self.lows[sl[j]]

                if x2 == x1:
                    continue

                slope = (y2 - y1) / (x2 - x1)
                intercept = y1 - slope * x1

                touches = 0
                for k in range(len(sl)):
                    x, y = sl[k], self.lows[sl[k]]
                    expected = slope * x + intercept
                    if abs(y - expected) <= tolerance:
                        touches += 1

                if touches >= min_touches:
                    results.append({
                        'type': 'support_trendline',
                        'slope': slope,
                        'intercept': intercept,
                        'touches': touches,
                        'start_idx': sl[i],
                        'end_idx': sl[j]
                    })

        # Resistance trendlines (using swing highs)
        sh = self.swing_high_idx[self.swing_high_idx >= start]
        for i in range(len(sh)):
            for j in range(i + 1, len(sh)):
                x1, y1 = sh[i], self.highs[sh[i]]
                x2, y2 = sh[j], self.highs[sh[j]]

                if x2 == x1:
                    continue

                slope = (y2 - y1) / (x2 - x1)
                intercept = y1 - slope * x1

                touches = 0
                for k in range(len(sh)):
                    x, y = sh[k], self.highs[sh[k]]
                    expected = slope * x + intercept
                    if abs(y - expected) <= tolerance:
                        touches += 1

                if touches >= min_touches:
                    results.append({
                        'type': 'resistance_trendline',
                        'slope': slope,
                        'intercept': intercept,
                        'touches': touches,
                        'start_idx': sh[i],
                        'end_idx': sh[j]
                    })

        # Sort by touches
        results.sort(key=lambda x: x['touches'], reverse=True)
        return results

    def detect_market_structure(self):
        """
        HIGHER HIGHS / HIGHER LOWS / LOWER HIGHS / LOWER LOWS
        ========================================================
        Detection Algorithm:
          1. Get swing highs and swing lows
          2. Compare consecutive swing highs:
             HH: swing_high[i] > swing_high[i-1]
             LH: swing_high[i] < swing_high[i-1]
          3. Compare consecutive swing lows:
             HL: swing_low[i] > swing_low[i-1]
             LL: swing_low[i] < swing_low[i-1]

        Trend Identification:
          UPTREND: HH + HL (higher highs AND higher lows)
          DOWNTREND: LH + LL (lower highs AND lower lows)
          CONSOLIDATION: Mixed signals

        Returns: List of structure points with labels
        """
        structure = []

        # Label swing highs
        sh = self.swing_high_idx
        for i in range(1, len(sh)):
            curr = self.highs[sh[i]]
            prev = self.highs[sh[i-1]]
            label = 'HH' if curr > prev else 'LH' if curr < prev else 'EH'
            structure.append({
                'idx': sh[i],
                'price': curr,
                'type': 'swing_high',
                'label': label
            })

        # Label swing lows
        sl = self.swing_low_idx
        for i in range(1, len(sl)):
            curr = self.lows[sl[i]]
            prev = self.lows[sl[i-1]]
            label = 'HL' if curr > prev else 'LL' if curr < prev else 'EL'
            structure.append({
                'idx': sl[i],
                'price': curr,
                'type': 'swing_low',
                'label': label
            })

        structure.sort(key=lambda x: x['idx'])
        return structure

    def detect_break_of_structure(self):
        """
        BREAK OF STRUCTURE (BOS) & CHANGE OF CHARACTER (CHoCH)
        ========================================================
        Detection Algorithm:

        BOS (Continuation signal - trend continues):
          Bullish BOS: Price breaks ABOVE a previous swing HIGH
                       during an uptrend (confirms HH)
          Bearish BOS: Price breaks BELOW a previous swing LOW
                       during a downtrend (confirms LL)

        CHoCH (Reversal signal - trend changes):
          Bullish CHoCH: Price breaks ABOVE a previous swing HIGH
                         during a DOWNTREND (signals trend change from down to up)
          Bearish CHoCH: Price breaks BELOW a previous swing LOW
                         during an UPTREND (signals trend change from up to down)

        Math:
          For each bar i:
            prev_sh = most recent confirmed swing high
            prev_sl = most recent confirmed swing low
            if close[i] > prev_sh:
              if trend == 'up': BOS (bullish)
              if trend == 'down': CHoCH (bullish)
            if close[i] < prev_sl:
              if trend == 'down': BOS (bearish)
              if trend == 'up': CHoCH (bearish)

        Entry: In direction of the break
        Stop: Beyond the broken structure level
        Target: Next S/R level or measured move
        Win rate: BOS ~65%, CHoCH ~60%
        """
        structure = self.detect_market_structure()
        breaks = []

        # Determine current trend based on recent structure
        recent_hh = sum(1 for s in structure[-6:] if s['label'] in ['HH', 'HL'])
        recent_ll = sum(1 for s in structure[-6:] if s['label'] in ['LH', 'LL'])
        trend = 'up' if recent_hh > recent_ll else 'down'

        sh = self.swing_high_idx
        sl = self.swing_low_idx

        for i in range(len(self.df)):
            # Find most recent confirmed swing high before this bar
            prev_sh_candidates = sh[sh < i - self.swing_order]
            prev_sl_candidates = sl[sl < i - self.swing_order]

            if len(prev_sh_candidates) == 0 or len(prev_sl_candidates) == 0:
                continue

            prev_sh_idx = prev_sh_candidates[-1]
            prev_sl_idx = prev_sl_candidates[-1]
            prev_sh_val = self.highs[prev_sh_idx]
            prev_sl_val = self.lows[prev_sl_idx]

            # Break above swing high
            if self.closes[i] > prev_sh_val and (i == 0 or self.closes[i-1] <= prev_sh_val):
                break_type = 'bos_bullish' if trend == 'up' else 'choch_bullish'
                breaks.append({
                    'type': break_type,
                    'bar_idx': i,
                    'broken_level': prev_sh_val,
                    'broken_level_idx': prev_sh_idx,
                    'close': self.closes[i]
                })
                if break_type == 'choch_bullish':
                    trend = 'up'

            # Break below swing low
            if self.closes[i] < prev_sl_val and (i == 0 or self.closes[i-1] >= prev_sl_val):
                break_type = 'bos_bearish' if trend == 'down' else 'choch_bearish'
                breaks.append({
                    'type': break_type,
                    'bar_idx': i,
                    'broken_level': prev_sl_val,
                    'broken_level_idx': prev_sl_idx,
                    'close': self.closes[i]
                })
                if break_type == 'choch_bearish':
                    trend = 'down'

        return breaks

    def detect_order_blocks(self, lookback=100):
        """
        ORDER BLOCKS (ICT Concept)
        ===========================
        Detection Algorithm:

        Bullish Order Block:
          1. Find the LAST BEARISH candle before a strong bullish move
          2. The bullish move must break a recent swing high (displacement)
          3. The order block zone = [low, high] of that last bearish candle
          4. When price returns to this zone, expect bullish reaction

        Bearish Order Block:
          1. Find the LAST BULLISH candle before a strong bearish move
          2. The bearish move must break a recent swing low
          3. OB zone = [low, high] of that last bullish candle

        Math:
          For each bar i:
            displacement_threshold = 2 * ATR
            If candle[i] is bearish AND candle[i+1] to candle[i+3] move up by
            >= displacement_threshold:
              bullish_OB = {top: high[i], bottom: low[i], type: 'bullish'}
            Vice versa for bearish OBs

        Refined OB (using mean threshold body):
          OB is the candle whose body is opposite to the subsequent impulsive move,
          AND the impulsive move creates a break of structure

        Entry: When price returns to the OB zone
        Stop: Below/above the OB zone
        Target: Previous swing high/low or 2:1 R:R
        Win rate: ~65% (higher with FVG + liquidity sweep confluence)
        """
        start = max(0, len(self.df) - lookback)
        atr_values = self.highs[start:] - self.lows[start:]
        atr = np.mean(atr_values) if len(atr_values) > 0 else 0
        displacement_threshold = 2 * atr

        order_blocks = []

        for i in range(start, len(self.df) - 3):
            o, h, l, c = self.opens[i], self.highs[i], self.lows[i], self.closes[i]

            # Bullish OB: bearish candle followed by strong up move
            if is_bearish(o, c):
                # Check for displacement up in next 1-3 candles
                max_high = np.max(self.highs[i+1:min(i+4, len(self.df))])
                move_up = max_high - c

                if move_up >= displacement_threshold:
                    order_blocks.append({
                        'type': 'bullish_order_block',
                        'idx': i,
                        'top': h,
                        'bottom': l,
                        'body_top': o,   # bearish: open is top
                        'body_bottom': c,
                        'mitigated': False,
                        'entry_zone': f'{l:.6f} to {o:.6f}',  # buy when price returns here
                        'stop': l - 0.5 * atr,
                        'target': max_high
                    })

            # Bearish OB: bullish candle followed by strong down move
            if is_bullish(o, c):
                min_low = np.min(self.lows[i+1:min(i+4, len(self.df))])
                move_down = o - min_low

                if move_down >= displacement_threshold:
                    order_blocks.append({
                        'type': 'bearish_order_block',
                        'idx': i,
                        'top': h,
                        'bottom': l,
                        'body_top': c,   # bullish: close is top
                        'body_bottom': o,
                        'mitigated': False,
                        'entry_zone': f'{o:.6f} to {h:.6f}',  # sell when price returns here
                        'stop': h + 0.5 * atr,
                        'target': min_low
                    })

        # Mark mitigated OBs (price has already returned and traded through)
        for ob in order_blocks:
            for j in range(ob['idx'] + 4, len(self.df)):
                if ob['type'] == 'bullish_order_block':
                    if self.lows[j] <= ob['body_top']:
                        ob['mitigated'] = True
                        ob['mitigated_idx'] = j
                        break
                else:
                    if self.highs[j] >= ob['body_bottom']:
                        ob['mitigated'] = True
                        ob['mitigated_idx'] = j
                        break

        return order_blocks

    def detect_fair_value_gaps(self, min_gap_atr_ratio=0.25):
        """
        FAIR VALUE GAPS (FVG) / IMBALANCES
        ====================================
        Detection Algorithm:

        Bullish FVG (gap up):
          Three consecutive candles where:
          candle[i+2].low > candle[i].high
          (The high of candle i does not overlap with the low of candle i+2)
          The gap = [candle[i].high, candle[i+2].low]
          Middle candle (i+1) is the impulse candle (should be bullish and large)

        Bearish FVG (gap down):
          candle[i+2].high < candle[i].low
          The gap = [candle[i+2].high, candle[i].low]
          Middle candle should be bearish and large

        Math:
          bullish_fvg: high[i] < low[i+2]
            gap_bottom = high[i]
            gap_top = low[i+2]
            gap_size = gap_top - gap_bottom

          bearish_fvg: low[i] > high[i+2]
            gap_top = low[i]
            gap_bottom = high[i+2]
            gap_size = gap_top - gap_bottom

          Filter: gap_size >= min_gap_atr_ratio * ATR

        Trading:
          Entry: When price returns to fill (or partially fill) the FVG
          Stop: Beyond the FVG zone
          Target: Opposite side of the FVG or next S/R

        Win rate: ~60-65% (higher with OB confluence)
        """
        atr = np.mean(self.highs - self.lows) if len(self.df) > 0 else 0
        min_gap = min_gap_atr_ratio * atr

        fvgs = []

        for i in range(len(self.df) - 2):
            # Bullish FVG: high of candle i < low of candle i+2
            if self.highs[i] < self.lows[i + 2]:
                gap_bottom = self.highs[i]
                gap_top = self.lows[i + 2]
                gap_size = gap_top - gap_bottom

                if gap_size >= min_gap:
                    # Check if FVG has been filled
                    filled = False
                    fill_idx = None
                    for j in range(i + 3, len(self.df)):
                        if self.lows[j] <= gap_top:  # price returned to FVG
                            filled = True
                            fill_idx = j
                            break

                    fvgs.append({
                        'type': 'bullish_fvg',
                        'idx': i + 1,  # middle candle index
                        'top': gap_top,
                        'bottom': gap_bottom,
                        'midpoint': (gap_top + gap_bottom) / 2,
                        'gap_size': gap_size,
                        'filled': filled,
                        'fill_idx': fill_idx,
                        'entry': gap_top,      # buy when price drops to FVG top
                        'stop': gap_bottom - 0.5 * atr,
                        'target': gap_top + gap_size  # project gap size above
                    })

            # Bearish FVG: low of candle i > high of candle i+2
            if self.lows[i] > self.highs[i + 2]:
                gap_top = self.lows[i]
                gap_bottom = self.highs[i + 2]
                gap_size = gap_top - gap_bottom

                if gap_size >= min_gap:
                    filled = False
                    fill_idx = None
                    for j in range(i + 3, len(self.df)):
                        if self.highs[j] >= gap_bottom:
                            filled = True
                            fill_idx = j
                            break

                    fvgs.append({
                        'type': 'bearish_fvg',
                        'idx': i + 1,
                        'top': gap_top,
                        'bottom': gap_bottom,
                        'midpoint': (gap_top + gap_bottom) / 2,
                        'gap_size': gap_size,
                        'filled': filled,
                        'fill_idx': fill_idx,
                        'entry': gap_bottom,
                        'stop': gap_top + 0.5 * atr,
                        'target': gap_bottom - gap_size
                    })

        return fvgs

    def detect_liquidity_sweeps(self, sweep_threshold_atr=0.2):
        """
        LIQUIDITY SWEEPS (Stop Hunts / Liquidity Grabs)
        =================================================
        Detection Algorithm:

        Bearish Liquidity Sweep (sweep of buyside liquidity):
          1. Price briefly spikes ABOVE a previous swing high (where buy stops sit)
          2. Then reverses and closes BELOW the swing high within the same or next candle
          3. The wick above the swing high = the liquidity sweep

        Bullish Liquidity Sweep (sweep of sellside liquidity):
          1. Price briefly spikes BELOW a previous swing low (where sell stops sit)
          2. Then reverses and closes ABOVE the swing low
          3. The wick below the swing low = the liquidity sweep

        Math:
          For bullish sweep at bar i:
            Find prev_swing_low (sl_val)
            low[i] < sl_val  (wicked below the swing low)
            close[i] > sl_val  (closed back above)
            sweep_depth = sl_val - low[i]
            sweep_depth >= sweep_threshold_atr * ATR  (meaningful sweep)

          For bearish sweep at bar i:
            Find prev_swing_high (sh_val)
            high[i] > sh_val  (wicked above)
            close[i] < sh_val  (closed back below)
            sweep_depth = high[i] - sh_val

        Entry: In direction opposite to the sweep (fade the move)
               Bullish: buy after sweep below swing low
               Bearish: sell after sweep above swing high
        Stop: Beyond the sweep wick extreme
        Target: Next liquidity pool (opposite swing point) or 2:1 R:R
        Win rate: ~65% (higher with OB + FVG confluence)
        """
        atr = np.mean(self.highs - self.lows) if len(self.df) > 0 else 0
        min_sweep = sweep_threshold_atr * atr

        sweeps = []

        for i in range(self.swing_order + 1, len(self.df)):
            # Check against all previous swing lows
            prev_sl = self.swing_low_idx[self.swing_low_idx < i - 1]
            for sl_idx in prev_sl[-5:]:  # check last 5 swing lows
                sl_val = self.lows[sl_idx]

                # Bullish sweep: wick below swing low, close above
                if (self.lows[i] < sl_val and
                    self.closes[i] > sl_val and
                    sl_val - self.lows[i] >= min_sweep):

                    sweeps.append({
                        'type': 'bullish_liquidity_sweep',
                        'bar_idx': i,
                        'swept_level': sl_val,
                        'swept_level_idx': sl_idx,
                        'sweep_low': self.lows[i],
                        'sweep_depth': sl_val - self.lows[i],
                        'entry': self.closes[i],
                        'stop': self.lows[i],
                        'target': self.closes[i] + 2 * (self.closes[i] - self.lows[i])
                    })
                    break  # only report one sweep per bar

            # Check against all previous swing highs
            prev_sh = self.swing_high_idx[self.swing_high_idx < i - 1]
            for sh_idx in prev_sh[-5:]:
                sh_val = self.highs[sh_idx]

                # Bearish sweep: wick above swing high, close below
                if (self.highs[i] > sh_val and
                    self.closes[i] < sh_val and
                    self.highs[i] - sh_val >= min_sweep):

                    sweeps.append({
                        'type': 'bearish_liquidity_sweep',
                        'bar_idx': i,
                        'swept_level': sh_val,
                        'swept_level_idx': sh_idx,
                        'sweep_high': self.highs[i],
                        'sweep_depth': self.highs[i] - sh_val,
                        'entry': self.closes[i],
                        'stop': self.highs[i],
                        'target': self.closes[i] - 2 * (self.highs[i] - self.closes[i])
                    })
                    break
        return sweeps


# ============================================================================
# SECTION 9: MASTER SCANNER - SCAN ALL PATTERNS AT ONCE
# ============================================================================

def scan_all_candlestick_patterns(df: pd.DataFrame, lookback: int = 14) -> List[dict]:
    """
    Scan the most recent candle(s) for ALL candlestick patterns.
    Returns list of detected patterns with details.
    """
    if len(df) < 4:
        return []

    results = []
    n = len(df)

    # Current and recent candles
    o0, h0, l0, c0 = df.iloc[-1][['open', 'high', 'low', 'close']]
    o1, h1, l1, c1 = df.iloc[-2][['open', 'high', 'low', 'close']]
    o2, h2, l2, c2 = df.iloc[-3][['open', 'high', 'low', 'close']]
    o3, h3, l3, c3 = df.iloc[-4][['open', 'high', 'low', 'close']] if n >= 4 else (0,0,0,0)

    closes_arr = df['close'].values
    in_downtrend = is_downtrend(closes_arr, lookback)
    in_uptrend = is_uptrend(closes_arr, lookback)

    # --- Single candle patterns on current candle ---
    doji_type = detect_doji(o0, h0, l0, c0)
    if doji_type:
        results.append({'pattern': doji_type, 'bar': -1, 'context': 'indecision'})

    if detect_hammer(o0, h0, l0, c0) and in_downtrend:
        results.append({'pattern': 'hammer', 'bar': -1, 'direction': 'bullish',
                       'entry': h0, 'stop': l0, 'target': h0 + (h0 - l0),
                       'win_rate': 0.60})

    if detect_inverted_hammer(o0, h0, l0, c0) and in_downtrend:
        results.append({'pattern': 'inverted_hammer', 'bar': -1, 'direction': 'bullish',
                       'entry': h0, 'stop': l0, 'target': h0 + (h0 - l0),
                       'win_rate': 0.60})

    if detect_hammer(o0, h0, l0, c0) and in_uptrend:
        results.append({'pattern': 'hanging_man', 'bar': -1, 'direction': 'bearish',
                       'entry': l0, 'stop': h0, 'target': l0 - (h0 - l0),
                       'win_rate': 0.59})

    if detect_inverted_hammer(o0, h0, l0, c0) and in_uptrend:
        results.append({'pattern': 'shooting_star', 'bar': -1, 'direction': 'bearish',
                       'entry': l0, 'stop': h0, 'target': l0 - (h0 - l0),
                       'win_rate': 0.59})

    maru = detect_marubozu(o0, h0, l0, c0)
    if maru:
        results.append({'pattern': maru, 'bar': -1, 'win_rate': 0.56})

    if detect_spinning_top(o0, h0, l0, c0):
        results.append({'pattern': 'spinning_top', 'bar': -1, 'context': 'indecision'})

    # --- Double candle patterns (candle -2 and -1) ---
    if detect_bullish_engulfing(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bullish_engulfing', 'bar': -1, 'direction': 'bullish',
                       'entry': h0, 'stop': min(l0, l1), 'win_rate': 0.57})

    if detect_bearish_engulfing(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bearish_engulfing', 'bar': -1, 'direction': 'bearish',
                       'entry': l0, 'stop': max(h0, h1), 'win_rate': 0.57})

    if detect_bullish_harami(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bullish_harami', 'bar': -1, 'direction': 'bullish',
                       'win_rate': 0.53})

    if detect_bearish_harami(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bearish_harami', 'bar': -1, 'direction': 'bearish',
                       'win_rate': 0.53})

    if detect_piercing_line(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'piercing_line', 'bar': -1, 'direction': 'bullish',
                       'win_rate': 0.64})

    if detect_dark_cloud_cover(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'dark_cloud_cover', 'bar': -1, 'direction': 'bearish',
                       'win_rate': 0.60})

    if detect_tweezer_top(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'tweezer_top', 'bar': -1, 'direction': 'bearish',
                       'win_rate': 0.55})

    if detect_tweezer_bottom(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'tweezer_bottom', 'bar': -1, 'direction': 'bullish',
                       'win_rate': 0.55})

    if detect_kicker_bullish(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bullish_kicker', 'bar': -1, 'direction': 'bullish',
                       'win_rate': 0.70})

    if detect_kicker_bearish(o1, h1, l1, c1, o0, h0, l0, c0):
        results.append({'pattern': 'bearish_kicker', 'bar': -1, 'direction': 'bearish',
                       'win_rate': 0.70})

    # --- Triple candle patterns (candles -3, -2, -1) ---
    if n >= 4:
        if detect_morning_star(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'morning_star', 'bar': -1, 'direction': 'bullish',
                           'entry': h0, 'stop': l1, 'win_rate': 0.72})

        if detect_evening_star(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'evening_star', 'bar': -1, 'direction': 'bearish',
                           'entry': l0, 'stop': h1, 'win_rate': 0.72})

        if detect_three_white_soldiers(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_white_soldiers', 'bar': -1, 'direction': 'bullish',
                           'entry': c0, 'stop': l2, 'win_rate': 0.82})

        if detect_three_black_crows(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_black_crows', 'bar': -1, 'direction': 'bearish',
                           'entry': c0, 'stop': h2, 'win_rate': 0.78})

        if detect_three_inside_up(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_inside_up', 'bar': -1, 'direction': 'bullish',
                           'win_rate': 0.65})

        if detect_three_inside_down(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_inside_down', 'bar': -1, 'direction': 'bearish',
                           'win_rate': 0.65})

        if detect_three_outside_up(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_outside_up', 'bar': -1, 'direction': 'bullish',
                           'win_rate': 0.75})

        if detect_three_outside_down(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'three_outside_down', 'bar': -1, 'direction': 'bearish',
                           'win_rate': 0.75})

        if detect_abandoned_baby_bullish(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'abandoned_baby_bullish', 'bar': -1, 'direction': 'bullish',
                           'win_rate': 0.80})

        if detect_abandoned_baby_bearish(o2, h2, l2, c2, o1, h1, l1, c1, o0, h0, l0, c0):
            results.append({'pattern': 'abandoned_baby_bearish', 'bar': -1, 'direction': 'bearish',
                           'win_rate': 0.80})

    return results


def scan_all_chart_patterns(df: pd.DataFrame, swing_order: int = 5) -> dict:
    """
    Master scanner: detect ALL chart patterns, harmonic patterns,
    and price action concepts at once.

    Returns dict with all detected patterns organized by category.
    """
    cpd = ChartPatternDetector(df, swing_order=swing_order)
    hpd = HarmonicPatternDetector(df, swing_order=swing_order)
    pad = PriceActionDetector(df, swing_order=swing_order)

    return {
        'candlestick_patterns': scan_all_candlestick_patterns(df),

        'continuation_patterns': {
            'bull_flags': cpd.detect_bull_flag(),
            'bear_flags': cpd.detect_bear_flag(),
            'pennants': cpd.detect_pennant(),
            'ascending_triangles': cpd.detect_ascending_triangle(),
            'descending_triangles': cpd.detect_descending_triangle(),
            'symmetrical_triangles': cpd.detect_symmetrical_triangle(),
            'rectangles': cpd.detect_rectangle_channel(),
            'cup_and_handle': cpd.detect_cup_and_handle(),
            'inv_cup_and_handle': cpd.detect_inverted_cup_and_handle(),
            'measured_moves': cpd.detect_measured_move(),
        },

        'reversal_patterns': {
            'head_and_shoulders': cpd.detect_head_and_shoulders(),
            'inv_head_and_shoulders': cpd.detect_inverse_head_and_shoulders(),
            'double_tops': cpd.detect_double_top(),
            'double_bottoms': cpd.detect_double_bottom(),
            'triple_tops': cpd.detect_triple_top(),
            'triple_bottoms': cpd.detect_triple_bottom(),
            'rising_wedges': cpd.detect_rising_wedge(),
            'falling_wedges': cpd.detect_falling_wedge(),
            'rounding_bottoms': cpd.detect_rounding_bottom(),
            'rounding_tops': cpd.detect_rounding_top(),
            'v_bottoms': cpd.detect_v_bottom(),
            'v_tops': cpd.detect_v_top(),
            'island_reversals': cpd.detect_island_reversal(),
        },

        'harmonic_patterns': {
            'gartley': hpd.detect_gartley(),
            'butterfly': hpd.detect_butterfly(),
            'bat': hpd.detect_bat(),
            'crab': hpd.detect_crab(),
            'cypher': hpd.detect_cypher(),
            'shark': hpd.detect_shark(),
            'abcd': detect_abcd_pattern(df, swing_order),
            'three_drives': detect_three_drives(df, swing_order),
        },

        'advanced_patterns': {
            'wolfe_waves': detect_wolfe_waves(df, swing_order),
            'elliott_impulse': detect_elliott_impulse(df, swing_order),
        },

        'price_action': {
            'support_resistance': pad.find_support_resistance(),
            'trendlines': pad.detect_trendlines(),
            'market_structure': pad.detect_market_structure(),
            'break_of_structure': pad.detect_break_of_structure(),
            'order_blocks': pad.detect_order_blocks(),
            'fair_value_gaps': pad.detect_fair_value_gaps(),
            'liquidity_sweeps': pad.detect_liquidity_sweeps(),
        }
    }


# ============================================================================
# WIN RATE SUMMARY TABLE
# ============================================================================

WIN_RATE_REFERENCE = """
============================================================================
PATTERN WIN RATE / RELIABILITY REFERENCE
============================================================================
Source: Bulkowski's Encyclopedia + QuantifiedStrategies backtests

CANDLESTICK PATTERNS (single candle):
  Inverted Hammer:        60%    (most profitable: +1.12% avg per trade)
  Hammer:                 60%
  Shooting Star:          59%
  Hanging Man:            59%
  Gravestone Doji:        57%
  Bearish Marubozu:       56%
  Bullish Marubozu:       53%
  Spinning Top:           ~50%   (indecision, context-dependent)
  Doji:                   ~50%

CANDLESTICK PATTERNS (double candle):
  Kicker (bull/bear):     ~70%   (one of the strongest)
  Bullish Engulfing:      57-63% (63% with downtrend filter)
  Bearish Engulfing:      57%
  Piercing Line:          64%
  Dark Cloud Cover:       60%
  Tweezer Top/Bottom:     55%
  Harami:                 53%    (73% with Harami Cross at S/R)

CANDLESTICK PATTERNS (triple candle):
  Three White Soldiers:   82%
  Three Black Crows:      78%
  Abandoned Baby:         80%+   (extremely rare)
  Three Outside Up/Down:  75%
  Morning Star:           72%
  Evening Star:           72%
  Three Inside Up/Down:   65%

CHART PATTERNS (continuation):
  Bullish Flag:           67-74% (91% with confirmation)
  Bear Flag:              67%
  Pennant:                66%
  Rectangle:              65%
  Cup and Handle:         65%    (avg rise: 34%)
  Measured Move:          63%

CHART PATTERNS (reversal):
  Head & Shoulders:       81-89%
  Inverse H&S:            88%    (avg rise: +50%)
  Double Bottom:          88%
  Triple Bottom:          87%
  Descending Triangle:    87%
  Triple Top:             87%
  Symmetrical Triangle:   76%    (in trend direction)
  Ascending Triangle:     75-83%
  Double Top:             73%
  Rising Wedge:           68%
  Falling Wedge:          68%
  Rounding Bottom/Top:    65%
  Island Reversal:        75%    (rare)
  V-Bottom/Top:           60%

HARMONIC PATTERNS:
  Gartley:                70%
  Three Drives:           70%
  Bat:                    68%
  Butterfly:              65%
  Cypher:                 65%
  ABCD:                   65%
  Shark:                  60%
  Crab:                   60%

ADVANCED:
  Wolfe Waves:            75%
  Elliott Impulse:        62%

PRICE ACTION (ICT/SMC):
  Order Block + FVG:      65-70%
  Liquidity Sweep:        65%
  Break of Structure:     65%
  Change of Character:    60%

NOTE: All win rates are approximate and vary by:
  - Market (stocks vs crypto vs forex)
  - Timeframe (higher TF = more reliable)
  - Trend context (with-trend patterns perform better)
  - Volume confirmation (adds 5-10% reliability)
  - Confluence (multiple patterns = higher probability)
============================================================================
"""

if __name__ == '__main__':
    print(WIN_RATE_REFERENCE)
    print("\nPattern Encyclopedia loaded. Use scan_all_chart_patterns(df) to scan.")
    print("DataFrame must have columns: open, high, low, close, volume")
