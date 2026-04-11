"""
CRYPTO-NATIVE PATTERNS
════════════════════════════════════════════════════════════════════
Patterns that actually work on 24/7 crypto markets (as opposed to the
forex-specific patterns in pro_patterns.py which lost money in backtest).

Key differences from the Karthik forex plan:
  - NO kangaroo tail (crypto has wick noise every hour from whale dumps)
  - NO last kiss (retests are noisy on 1H crypto)
  - NO busted KT (biggest loser in backtest)
  - NO round-number S/R (swing points matter more in crypto)
  - ALL patterns require volume confirmation (forex doesn't need this)
  - ALL patterns use trend alignment (EMA20 > EMA50)
  - ALL patterns use ATR-based stops (volatility adapts)

Each detector returns: (fired, entry, stop, name, quality)
  quality 1-10 = signal strength
  entry = buy stop price (candle high + cushion)
  stop = stop loss price (swing low or ATR-based)

Only BULLISH patterns — Roostoo is long-only.
"""
from statistics import mean


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════
def body(c):
    return c['c'] - c['o']


def body_size(c):
    return abs(body(c))


def is_green(c):
    return c['c'] > c['o']


def is_red(c):
    return c['c'] < c['o']


def upper_wick(c):
    return c['h'] - max(c['o'], c['c'])


def lower_wick(c):
    return min(c['o'], c['c']) - c['l']


def candle_range(c):
    return c['h'] - c['l']


def atr(cl, period=14):
    """True Average True Range — uses high-low + gaps."""
    if len(cl) < period + 1:
        return candle_range(cl[-1]) if cl else 0.0001
    trs = []
    for i in range(1, period + 1):
        c = cl[-i]
        p = cl[-i - 1]
        tr = max(
            c['h'] - c['l'],
            abs(c['h'] - p['c']),
            abs(c['l'] - p['c']),
        )
        trs.append(tr)
    return sum(trs) / period


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def avg_volume(cl, n=20):
    if len(cl) < n:
        return 0.0001
    vols = [x.get('v', 0) for x in cl[-n:]]
    m = mean(vols)
    return m if m > 0 else 0.0001


def avg_range(cl, n=14):
    if len(cl) < n:
        return candle_range(cl[-1]) if cl else 0.0001
    return sum(candle_range(x) for x in cl[-n:]) / n


def is_uptrend(cl):
    """Strict multi-EMA uptrend: price > EMA20 > EMA50."""
    if len(cl) < 50:
        return False
    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return False
    return cl[-1]['c'] > e20 > e50


def strong_uptrend(cl):
    """Multi-timeframe uptrend: price > EMA20 > EMA50 > EMA200."""
    if len(cl) < 200:
        return is_uptrend(cl)  # fall back
    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    if e20 is None or e50 is None or e200 is None:
        return False
    return cl[-1]['c'] > e20 > e50 > e200


def find_swing_highs(cl, length=2):
    swings = []
    for i in range(length, len(cl) - length):
        is_high = all(cl[i]['h'] > cl[i + j]['h'] for j in range(1, length + 1)) and \
                  all(cl[i]['h'] > cl[i - j]['h'] for j in range(1, length + 1))
        if is_high:
            swings.append(i)
    return swings


def find_swing_lows(cl, length=2):
    swings = []
    for i in range(length, len(cl) - length):
        is_low = all(cl[i]['l'] < cl[i + j]['l'] for j in range(1, length + 1)) and \
                 all(cl[i]['l'] < cl[i - j]['l'] for j in range(1, length + 1))
        if is_low:
            swings.append(i)
    return swings


def vol_confirm(cl, mult=1.3):
    """Current candle volume >= mult * 20-bar avg."""
    if len(cl) < 21:
        return False
    cur_vol = cl[-1].get('v', 0)
    av = avg_volume(cl[:-1], 20)
    return cur_vol >= av * mult


def atr_cushion(cl, pct=0.25):
    """ATR-based entry cushion (crypto-scale, not fixed 0.1%)."""
    a = atr(cl, 14)
    return a * pct


# ══════════════════════════════════════════════════════
#  PATTERN 1: FAILED BREAKDOWN (liquidity sweep reclaim)
# ══════════════════════════════════════════════════════
def detect_failed_breakdown(cl):
    """
    Price breaks below a recent swing low (sweeps resting stops),
    then reclaims the level within 1-3 candles = trapped shorts forced
    to cover. Very reliable crypto pattern.

    Rules:
    - Last 30 candles contained a swing low X
    - A later candle wicked below X (sweep)
    - Current candle closed back above X (reclaim)
    - Current candle is green and in uptrend
    - Volume confirmation on reclaim
    """
    if len(cl) < 40:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    # Find swing low in candles -30..-5 (must be older than the sweep)
    history = cl[-30:-4]
    sl_idxs = find_swing_lows(history, length=2)
    if not sl_idxs:
        return False, 0, 0, '', 0

    # Check if recent candles (-4..-1) wicked below the swing low
    swing_low = min(history[i]['l'] for i in sl_idxs)
    sweep_occurred = False
    sweep_wick = None
    for x in cl[-4:-1]:
        if x['l'] < swing_low:
            sweep_occurred = True
            if sweep_wick is None or x['l'] < sweep_wick:
                sweep_wick = x['l']
    # Also allow sweep on current candle's wick
    if c['l'] < swing_low:
        sweep_occurred = True
        if sweep_wick is None or c['l'] < sweep_wick:
            sweep_wick = c['l']

    if not sweep_occurred:
        return False, 0, 0, '', 0

    # Current candle must close BACK ABOVE the swing low (reclaim)
    if c['c'] <= swing_low:
        return False, 0, 0, '', 0

    # Volume confirmation
    if not vol_confirm(cl, mult=1.2):
        return False, 0, 0, '', 0

    # Trend context — prefer uptrend but allow counter-trend reclaims
    quality = 9 if is_uptrend(cl) else 7

    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = sweep_wick - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'FAIL_BRK', quality


# ══════════════════════════════════════════════════════
#  PATTERN 2: BULL FLAG BREAKOUT
# ══════════════════════════════════════════════════════
def detect_bull_flag(cl):
    """
    Classic bull flag:
    - Flagpole: strong impulsive up move (5+ consecutive bullish candles or
      single candle > 3x avg range)
    - Flag: 5-12 candles of small-range pullback or sideways
    - Breakout: current candle closes above the flag high with volume
    """
    if len(cl) < 25:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0
    if not is_uptrend(cl):
        return False, 0, 0, '', 0

    ar = avg_range(cl, 14)

    # Find the flagpole: impulsive move in candles -20..-10
    pole_region = cl[-20:-8]
    if not pole_region:
        return False, 0, 0, '', 0
    pole_high = max(x['h'] for x in pole_region)
    pole_low = min(x['l'] for x in pole_region)
    pole_height = pole_high - pole_low
    if pole_height < ar * 3:
        return False, 0, 0, '', 0

    # Flag: candles -8..-1 should be tight consolidation
    flag_region = cl[-8:-1]
    if not flag_region:
        return False, 0, 0, '', 0
    flag_high = max(x['h'] for x in flag_region)
    flag_low = min(x['l'] for x in flag_region)
    flag_height = flag_high - flag_low

    # Flag must be tighter than pole (< 40% of pole height)
    if flag_height > pole_height * 0.5:
        return False, 0, 0, '', 0

    # Flag low should be ABOVE pole midpoint (shallow pullback)
    if flag_low < (pole_high + pole_low) / 2:
        return False, 0, 0, '', 0

    # Current candle breaks above flag high
    if c['c'] <= flag_high:
        return False, 0, 0, '', 0

    # Volume confirmation
    if not vol_confirm(cl, mult=1.3):
        return False, 0, 0, '', 0

    quality = 9
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = flag_low - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'BULL_FLAG', quality


# ══════════════════════════════════════════════════════
#  PATTERN 3: RECLAIM HIGH (failed high → reclaim)
# ══════════════════════════════════════════════════════
def detect_reclaim_high(cl):
    """
    - Price breaks above a recent swing high
    - Pulls back below it (failed breakout)
    - Reclaims the high within 3-5 candles
    - Strong trend continuation signal
    """
    if len(cl) < 30:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0
    if not is_uptrend(cl):
        return False, 0, 0, '', 0

    # Find a swing high in candles -30..-8
    history = cl[-30:-7]
    sh_idxs = find_swing_highs(history, length=2)
    if not sh_idxs:
        return False, 0, 0, '', 0
    swing_high = max(history[i]['h'] for i in sh_idxs)

    # An earlier candle must have broken above swing_high (the failed break)
    broken = False
    for x in cl[-10:-2]:
        if x['h'] > swing_high:
            broken = True
            break
    if not broken:
        return False, 0, 0, '', 0

    # Then pulled back below it
    pulled_back = False
    for x in cl[-5:-1]:
        if x['c'] < swing_high:
            pulled_back = True
            break
    if not pulled_back:
        return False, 0, 0, '', 0

    # Current candle reclaims above swing_high
    if c['c'] <= swing_high:
        return False, 0, 0, '', 0

    # Volume confirmation
    if not vol_confirm(cl, mult=1.2):
        return False, 0, 0, '', 0

    quality = 8
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = min(x['l'] for x in cl[-5:]) - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'RECLAIM_H', quality


# ══════════════════════════════════════════════════════
#  PATTERN 4: VOLUME CLIMAX REVERSAL
# ══════════════════════════════════════════════════════
def detect_volume_climax(cl):
    """
    Big red capitulation candle on massive volume, followed by
    green reversal with normal volume = buyers stepping in.

    - 2 candles ago: large red candle with volume > 2x average
    - 1 candle ago (or current): green candle reclaiming
    """
    if len(cl) < 25:
        return False, 0, 0, '', 0

    c = cl[-1]
    climax = cl[-2]

    # Climax candle: must be red with huge range + volume
    if not is_red(climax):
        return False, 0, 0, '', 0

    ar = avg_range(cl[:-1], 14)
    av = avg_volume(cl[:-1], 20)

    if candle_range(climax) < ar * 1.8:
        return False, 0, 0, '', 0
    if climax.get('v', 0) < av * 2.0:
        return False, 0, 0, '', 0

    # Current candle: green and close above climax close
    if not is_green(c):
        return False, 0, 0, '', 0
    if c['c'] <= climax['c']:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = climax['l'] - atr_cushion(cl, 0.3)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'VOL_CLIMAX', quality


# ══════════════════════════════════════════════════════
#  PATTERN 5: DOUBLE BOTTOM
# ══════════════════════════════════════════════════════
def detect_double_bottom(cl):
    """
    Two lows at similar levels (within 1%) with a pivot high between.
    Current candle breaks above the pivot high = confirmation.
    """
    if len(cl) < 30:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    # Find swing lows
    history = cl[-30:-1]
    sl_idxs = find_swing_lows(history, length=2)
    if len(sl_idxs) < 2:
        return False, 0, 0, '', 0

    # Last 2 swing lows
    l1_idx = sl_idxs[-2]
    l2_idx = sl_idxs[-1]

    # Must be at least 5 candles apart
    if l2_idx - l1_idx < 5:
        return False, 0, 0, '', 0

    l1 = history[l1_idx]['l']
    l2 = history[l2_idx]['l']

    # Similar lows (within 1.5%)
    if abs(l2 - l1) / l1 > 0.015:
        return False, 0, 0, '', 0

    # Pivot high between the two lows
    between = history[l1_idx:l2_idx + 1]
    if not between:
        return False, 0, 0, '', 0
    pivot_high = max(x['h'] for x in between)

    # Current candle must close above pivot high
    if c['c'] <= pivot_high:
        return False, 0, 0, '', 0

    # Volume confirmation
    if not vol_confirm(cl, mult=1.2):
        return False, 0, 0, '', 0

    quality = 8
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = min(l1, l2) - atr_cushion(cl, 0.3)

    if (entry - stop) / entry > 0.06:
        return False, 0, 0, '', 0

    return True, entry, stop, 'DBL_BOT', quality


# ══════════════════════════════════════════════════════
#  PATTERN 6: INSIDE BAR BREAKOUT (volatility expansion)
# ══════════════════════════════════════════════════════
def detect_inside_bar_break(cl):
    """
    - 2 candles ago: mother candle (normal or large range)
    - 1 candle ago: inside bar (fully contained within mother's H/L)
    - Current: breaks above the mother's high
    Compression → expansion = explosive crypto move.
    """
    if len(cl) < 25:
        return False, 0, 0, '', 0

    c = cl[-1]
    inside = cl[-2]
    mother = cl[-3]

    if not is_green(c):
        return False, 0, 0, '', 0
    if not is_uptrend(cl):
        return False, 0, 0, '', 0

    # Inside bar check
    if inside['h'] >= mother['h'] or inside['l'] <= mother['l']:
        return False, 0, 0, '', 0

    # Mother range must be meaningful
    ar = avg_range(cl[:-2], 14)
    if candle_range(mother) < ar * 0.7:
        return False, 0, 0, '', 0

    # Inside bar must be tight (< 70% of mother range)
    if candle_range(inside) > candle_range(mother) * 0.7:
        return False, 0, 0, '', 0

    # Current breaks above mother high
    if c['c'] <= mother['h']:
        return False, 0, 0, '', 0

    # Volume confirmation
    if not vol_confirm(cl, mult=1.3):
        return False, 0, 0, '', 0

    quality = 8
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = min(inside['l'], mother['l']) - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'INSIDE_BRK', quality


# ══════════════════════════════════════════════════════
#  PATTERN 7: HIGHER HIGH CONTINUATION (trend riding)
# ══════════════════════════════════════════════════════
def detect_hh_continuation(cl):
    """
    Strong uptrend, pullback to EMA20, bounce + new high.
    Best pattern for riding crypto pumps.
    """
    if len(cl) < 50:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0
    if not is_uptrend(cl):
        return False, 0, 0, '', 0

    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    if e20 is None:
        return False, 0, 0, '', 0

    # Recent pullback touched or came close to EMA20
    pulled_back = False
    for x in cl[-8:-1]:
        if x['l'] <= e20 * 1.01:
            pulled_back = True
            break
    if not pulled_back:
        return False, 0, 0, '', 0

    # Current candle makes a new high relative to last 5 candles
    recent_high = max(x['h'] for x in cl[-6:-1])
    if c['c'] <= recent_high:
        return False, 0, 0, '', 0

    # Body size should be meaningful
    ar = avg_range(cl, 14)
    if body_size(c) < ar * 0.6:
        return False, 0, 0, '', 0

    # Volume confirmation (softer — trend already in motion)
    if not vol_confirm(cl, mult=1.1):
        return False, 0, 0, '', 0

    quality = 9 if strong_uptrend(cl) else 7
    entry = c['h'] + atr_cushion(cl, 0.1)
    # Stop below recent pullback low
    stop = min(x['l'] for x in cl[-8:]) - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'HH_CONT', quality


# ══════════════════════════════════════════════════════
#  PATTERN 8: BOLLINGER SQUEEZE BREAKOUT
# ══════════════════════════════════════════════════════
def detect_bb_squeeze_break(cl):
    """
    Volatility compression (BB width < avg) followed by expansion.
    BB width = 2 * stdev / mid * 100 (as percent).
    """
    if len(cl) < 40:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    # Compute BB(20, 2) on closes
    closes = [x['c'] for x in cl]
    period = 20
    window = closes[-period - 1:-1]  # BB from previous candle
    if len(window) < period:
        return False, 0, 0, '', 0
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    stdev = var ** 0.5
    upper = mid + 2 * stdev
    lower = mid - 2 * stdev
    bb_width_pct = (upper - lower) / mid if mid > 0 else 0

    # Compare with 40-bar average BB width
    widths = []
    for i in range(period, min(40, len(closes) - 1)):
        w = closes[-i - period:-i] if i > 0 else closes[-period:]
        if len(w) < period:
            continue
        m = sum(w) / period
        v = sum((x - m) ** 2 for x in w) / period
        s = v ** 0.5
        if m > 0:
            widths.append((4 * s) / m)
    if not widths:
        return False, 0, 0, '', 0
    avg_width = mean(widths)
    if bb_width_pct > avg_width * 0.8:
        return False, 0, 0, '', 0  # not squeezed enough

    # Current candle must close ABOVE upper band (expansion)
    if c['c'] <= upper:
        return False, 0, 0, '', 0

    # Prefer uptrend
    if not is_uptrend(cl):
        return False, 0, 0, '', 0

    if not vol_confirm(cl, mult=1.3):
        return False, 0, 0, '', 0

    quality = 8
    entry = c['h'] + atr_cushion(cl, 0.1)
    stop = lower - atr_cushion(cl, 0.2)

    if (entry - stop) / entry > 0.05:
        return False, 0, 0, '', 0

    return True, entry, stop, 'BB_SQZ', quality


# ══════════════════════════════════════════════════════
#  MASTER
# ══════════════════════════════════════════════════════
ALL_DETECTORS = [
    detect_failed_breakdown,
    detect_bull_flag,
    detect_reclaim_high,
    detect_volume_climax,
    detect_double_bottom,
    detect_inside_bar_break,
    detect_hh_continuation,
    detect_bb_squeeze_break,
]


def scan_all(cl):
    """Run all detectors, return the best signal."""
    best = None
    all_fired = []
    for detector in ALL_DETECTORS:
        try:
            fired, entry, stop, name, quality = detector(cl)
            if fired and entry > 0 and stop > 0 and entry > stop:
                all_fired.append((name, quality, entry, stop))
                if not best or quality > best[1]:
                    best = (name, quality, entry, stop)
        except Exception:
            pass
    if best:
        return True, best[2], best[3], best[0], best[1], all_fired
    return False, 0, 0, '', 0, []


# ══════════════════════════════════════════════════════
#  VOLATILITY REGIME FILTER (crypto replacement for session filter)
# ══════════════════════════════════════════════════════
def vol_regime_ok(cl, min_atr_ratio=0.7):
    """
    Skip entries when current ATR < min_atr_ratio * 50-bar avg ATR.
    This is the crypto-native replacement for forex session filters.
    Skip chop, trade expansion.
    """
    if len(cl) < 65:
        return True  # not enough history, allow
    cur_atr = atr(cl, 14)
    # 50-bar avg of 14-ATR
    historical_atrs = []
    for i in range(1, 51):
        if len(cl) > i + 14:
            historical_atrs.append(atr(cl[:-i], 14))
    if not historical_atrs:
        return True
    avg_atr = mean(historical_atrs)
    if avg_atr == 0:
        return True
    return cur_atr >= avg_atr * min_atr_ratio
