"""
PRO PATTERN DETECTION
Implements the 21 professional price action patterns from Karthik Forex Business Plan 2015
(based on Steven Drummond's Tactical Trader Boot Camp + Naked Forex book by Nekritin/Peters)

Each detector returns: (fired: bool, entry: float, stop: float, pattern_name: str, quality: int)
- quality 1-10 rates the pattern strength
- entry = buy stop price (candle high + cushion)
- stop = stop loss price (candle low - cushion)

Only BULLISH patterns (long-only on Roostoo — no shorting allowed).
"""
import numpy as np


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


def avg_range(cl, n=14):
    """Average True Range approximation."""
    if len(cl) < n:
        return candle_range(cl[-1]) if cl else 0.0001
    return sum(candle_range(x) for x in cl[-n:]) / n


def avg_body(cl, n=14):
    if len(cl) < n:
        return body_size(cl[-1]) if cl else 0.0001
    return sum(body_size(x) for x in cl[-n:]) / n


def find_swing_highs(cl, length=2):
    """Return indices of swing highs (pivots with `length` lower highs on each side)."""
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


def is_uptrend(cl, lookback=20):
    """Simple trend check: higher highs AND higher lows over lookback."""
    if len(cl) < lookback:
        return False
    recent = cl[-lookback:]
    half = lookback // 2
    first_high = max(x['h'] for x in recent[:half])
    second_high = max(x['h'] for x in recent[half:])
    first_low = min(x['l'] for x in recent[:half])
    second_low = min(x['l'] for x in recent[half:])
    return second_high > first_high and second_low > first_low


def is_downtrend(cl, lookback=20):
    if len(cl) < lookback:
        return False
    recent = cl[-lookback:]
    half = lookback // 2
    first_high = max(x['h'] for x in recent[:half])
    second_high = max(x['h'] for x in recent[half:])
    first_low = min(x['l'] for x in recent[:half])
    second_low = min(x['l'] for x in recent[half:])
    return second_high < first_high and second_low < first_low


def find_zones(cl, lookback=50, threshold_pct=0.003):
    """Find support/resistance zones based on repeated touches.
    Returns list of {'level', 'touches', 'type': 'support'/'resistance'}
    """
    if len(cl) < lookback:
        return []

    recent = cl[-lookback:]
    lows = sorted([x['l'] for x in recent])
    highs = sorted([x['h'] for x in recent])

    zones = []

    def cluster(prices, zone_type):
        if not prices:
            return []
        clusters = []
        current = [prices[0]]
        for p in prices[1:]:
            if (p - current[0]) / current[0] < threshold_pct:
                current.append(p)
            else:
                if len(current) >= 2:
                    clusters.append({
                        'level': sum(current) / len(current),
                        'touches': len(current),
                        'type': zone_type,
                    })
                current = [p]
        if len(current) >= 2:
            clusters.append({
                'level': sum(current) / len(current),
                'touches': len(current),
                'type': zone_type,
            })
        return clusters

    zones.extend(cluster(lows, 'support'))
    zones.extend(cluster(highs, 'resistance'))
    zones.sort(key=lambda z: -z['touches'])
    return zones[:10]


def is_in_zone(price, zones, threshold_pct=0.005):
    """Check if price is near a support zone (for buy setups)."""
    for z in zones:
        if z['type'] == 'support' and abs(price - z['level']) / price < threshold_pct:
            return True, z
    return False, None


def pip_cushion(c, pct=0.001):
    """Entry cushion above the candle high. 0.1% by default for crypto."""
    return c['h'] * pct


# ══════════════════════════════════════════════════════
#  THE 21 PRO PATTERNS
# ══════════════════════════════════════════════════════

# Pattern 1: KANGAROO TAIL (KT) - The crown jewel
def detect_kangaroo_tail(cl, min_idx=6):
    """Bullish KT rules (Karthik doc + Naked Forex book):
    - Open and close are in top 33% of the candle range
    - Body is small relative to the wick
    - Current candle has the largest range of the last 6 candles
    - Space to the left (no recent bars at this level)
    - Open and close contained within previous bar's range
    Returns (fired, entry, stop, name, quality).
    """
    if len(cl) < min_idx + 1:
        return False, 0, 0, '', 0

    c = cl[-1]
    p = cl[-2]
    rng = candle_range(c)
    if rng == 0:
        return False, 0, 0, '', 0

    # Rule 1: open and close in top 33%
    body_top = max(c['o'], c['c'])
    if (c['h'] - body_top) / rng > 0.33:
        return False, 0, 0, '', 0

    # Rule 2: open and close contained within previous bar
    if c['o'] < p['l'] or c['c'] < p['l'] or c['o'] > p['h'] or c['c'] > p['h']:
        return False, 0, 0, '', 0

    # Rule 3: largest range of the last min_idx candles
    prev_ranges = [candle_range(x) for x in cl[-min_idx - 1:-1]]
    if rng < max(prev_ranges) * 1.1:
        return False, 0, 0, '', 0

    # Rule 4: space to the left (no recent bars at this low level)
    left_candles = cl[-min_idx - 1:-1]
    for x in left_candles:
        if x['l'] < c['l'] + rng * 0.3:
            # previous candle dipped into the tail area — not enough "space"
            return False, 0, 0, '', 0

    # Quality: how small the body is relative to range
    bs = body_size(c)
    body_pct = bs / rng
    if body_pct <= 0.10:
        quality = 10  # picture perfect
    elif body_pct <= 0.20:
        quality = 8
    elif body_pct <= 0.33:
        quality = 6
    else:
        return False, 0, 0, '', 0

    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'KT', quality


# Pattern 2: BIG SHADOW (BS) - Two-candle outside bar
def detect_big_shadow(cl):
    if len(cl) < 7:
        return False, 0, 0, '', 0

    c = cl[-1]  # the BS candle
    p = cl[-2]  # the prior candle

    # Must be a bullish BS
    if not is_green(c):
        return False, 0, 0, '', 0

    # Must have higher high AND lower low than previous
    if c['h'] <= p['h'] or c['l'] >= p['l']:
        return False, 0, 0, '', 0

    rng = candle_range(c)
    if rng == 0:
        return False, 0, 0, '', 0

    # Close must be in top half
    if (c['c'] - c['l']) / rng < 0.5:
        return False, 0, 0, '', 0

    # Should be the largest range in last 6 candles
    prev_ranges = [candle_range(x) for x in cl[-7:-1]]
    if rng < max(prev_ranges) * 1.1:
        return False, 0, 0, '', 0

    # Close near high = higher quality
    close_pct = (c['c'] - c['l']) / rng
    if close_pct >= 0.85:
        quality = 10
    elif close_pct >= 0.7:
        quality = 8
    else:
        quality = 6

    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'BS', quality


# Pattern 3: WAMMIE - Higher low double bottom
def detect_wammie(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Find swing lows in last 20 candles
    recent = cl[-20:]
    swing_idxs = find_swing_lows(recent, length=2)
    if len(swing_idxs) < 2:
        return False, 0, 0, '', 0

    # Get last 2 swing lows
    first = swing_idxs[-2]
    second = swing_idxs[-1]

    # Must have at least 6 candles between them
    if second - first < 6:
        return False, 0, 0, '', 0

    # Second swing low must be HIGHER than first (key Wammie rule)
    if recent[second]['l'] <= recent[first]['l']:
        return False, 0, 0, '', 0

    # Close to current candle?
    if second < len(recent) - 3:
        return False, 0, 0, '', 0

    # Confirmation: current candle must be bullish with moderate body
    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    ab = avg_body(cl)
    if body_size(c) < ab * 0.8:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    stop = recent[first]['l'] - pip_cushion(c)

    # Ensure risk is reasonable (<= 3% away)
    if (entry - stop) / entry > 0.03:
        return False, 0, 0, '', 0

    return True, entry, stop, 'WAMMIE', quality


# Pattern 4: LAST KISS (LK) - Break, retest, continue
def detect_last_kiss(cl):
    """Price consolidates, breaks above range, pulls back to retest, then continues up."""
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Find a consolidation range (last 15 candles excluding recent 5)
    cons = cl[-15:-5]
    if len(cons) < 6:
        return False, 0, 0, '', 0

    cons_high = max(x['h'] for x in cons)
    cons_low = min(x['l'] for x in cons)
    range_pct = (cons_high - cons_low) / cons_low

    # Must be a tight consolidation (< 2% range)
    if range_pct > 0.02:
        return False, 0, 0, '', 0

    # Check if we broke above in last 5 candles
    breakout_occurred = False
    breakout_idx = -1
    for i, candle in enumerate(cl[-5:]):
        if candle['c'] > cons_high:
            breakout_occurred = True
            breakout_idx = i
            break
    if not breakout_occurred:
        return False, 0, 0, '', 0

    # Price must have gone above 50% further than cons_high
    max_extension = max(x['h'] for x in cl[-5:])
    if max_extension < cons_high + (cons_high - cons_low) * 0.5:
        return False, 0, 0, '', 0

    # Current candle must pull back and touch/near cons_high (the "kiss")
    c = cl[-1]
    if c['l'] > cons_high * 1.005:
        return False, 0, 0, '', 0  # hasn't come back to kiss

    # Bullish confirmation candle
    if not is_green(c):
        return False, 0, 0, '', 0

    quality = 8
    entry = c['h'] + pip_cushion(c)
    stop = cons_low - pip_cushion(c)

    if (entry - stop) / entry > 0.04:
        return False, 0, 0, '', 0

    return True, entry, stop, 'LK', quality


# Pattern 5: TRENDY KT - KT in pullback of uptrend
def detect_trendy_kt(cl):
    if len(cl) < 25:
        return False, 0, 0, '', 0

    # Must be in uptrend (higher highs in lookback before current pullback)
    trend_window = cl[-25:-5]
    if len(trend_window) < 15:
        return False, 0, 0, '', 0

    trend_high = max(x['h'] for x in trend_window)
    trend_low = min(x['l'] for x in trend_window)
    if trend_window[-1]['c'] < (trend_high + trend_low) / 2:
        return False, 0, 0, '', 0  # not in uptrend

    # Pullback of 3-20 candles
    pullback = cl[-20:-1]
    if not pullback:
        return False, 0, 0, '', 0

    # Current candle should be a KT (use KT logic)
    kt_fired, entry, stop, _, quality = detect_kangaroo_tail(cl)
    if not kt_fired:
        return False, 0, 0, '', 0

    # The KT should tip a swing low (rejection)
    c = cl[-1]
    pullback_low = min(x['l'] for x in pullback)
    if c['l'] > pullback_low * 1.002:
        return False, 0, 0, '', 0

    return True, entry, stop, 'TKT', quality + 1  # bonus for trend alignment


# Pattern 6: TRENDY BIG SHADOW - BS in uptrend pullback
def detect_trendy_bs(cl):
    if len(cl) < 25:
        return False, 0, 0, '', 0

    trend_window = cl[-25:-5]
    trend_high = max(x['h'] for x in trend_window)
    trend_low = min(x['l'] for x in trend_window)
    if trend_window[-1]['c'] < (trend_high + trend_low) / 2:
        return False, 0, 0, '', 0

    bs_fired, entry, stop, _, quality = detect_big_shadow(cl)
    if not bs_fired:
        return False, 0, 0, '', 0

    # Must engulf prior swing lows in pullback
    pullback = cl[-15:-1]
    pullback_low = min(x['l'] for x in pullback)
    c = cl[-1]
    if c['l'] > pullback_low:
        return False, 0, 0, '', 0

    return True, entry, stop, 'TBS', quality + 1


# Pattern 7: POGO - Mother + inside + pogo candle
def detect_pogo(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Need 3 candles: mother, inside, pogo
    mother = cl[-3]
    inside = cl[-2]
    pogo = cl[-1]

    # Inside candle must be inside mother's range
    if inside['h'] > mother['h'] or inside['l'] < mother['l']:
        return False, 0, 0, '', 0

    # Pogo must take out mother's low (fakeout below)
    if pogo['l'] >= mother['l']:
        return False, 0, 0, '', 0

    # Pogo's open and close must be within inside candle
    if pogo['o'] < inside['l'] or pogo['c'] < inside['l']:
        return False, 0, 0, '', 0
    if pogo['o'] > inside['h'] or pogo['c'] > inside['h']:
        return False, 0, 0, '', 0

    # Must be in uptrend
    trend = cl[-20:-3]
    if max(x['h'] for x in trend) - min(x['l'] for x in trend) <= 0:
        return False, 0, 0, '', 0
    if trend[-1]['c'] < (max(x['h'] for x in trend) + min(x['l'] for x in trend)) / 2:
        return False, 0, 0, '', 0

    # Pogo should be bullish
    if not is_green(pogo):
        return False, 0, 0, '', 0

    quality = 9
    entry = pogo['h'] + pip_cushion(pogo)
    stop = pogo['l'] - pip_cushion(pogo)
    return True, entry, stop, 'POGO', quality


# Pattern 8: ACAPULCO - Cliff + diver through zone
def detect_acapulco(cl):
    if len(cl) < 15:
        return False, 0, 0, '', 0

    # Need to have a zone defined
    zones = find_zones(cl[:-2], lookback=40)
    bull_zones = [z for z in zones if z['type'] == 'resistance']  # resistance becomes support
    if not bull_zones:
        return False, 0, 0, '', 0

    cliff = cl[-2]
    diver = cl[-1]

    ab = avg_body(cl)
    if body_size(cliff) < ab * 1.5:
        return False, 0, 0, '', 0

    if not is_green(cliff):
        return False, 0, 0, '', 0

    # Cliff must push through a resistance zone
    pushed_zone = None
    for z in bull_zones:
        if cliff['o'] < z['level'] < cliff['c']:
            pushed_zone = z
            break
    if not pushed_zone:
        return False, 0, 0, '', 0

    # Diver must touch the zone and close above it (a shy candle)
    if diver['l'] > pushed_zone['level'] * 1.005:
        return False, 0, 0, '', 0
    if diver['c'] < pushed_zone['level']:
        return False, 0, 0, '', 0
    if body_size(diver) > ab * 0.7:
        return False, 0, 0, '', 0  # must be a small diver candle

    quality = 9
    entry = diver['h'] + pip_cushion(diver)
    stop = pushed_zone['level'] * 0.995
    return True, entry, stop, 'ACAPULCO', quality


# Pattern 9: TREND CONTINUATION - Break above recent swing high
def detect_trend_continuation(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Must be in uptrend
    if not is_uptrend(cl, lookback=20):
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    # Find recent swing high before current candle
    swings = find_swing_highs(cl[:-1], length=2)
    if not swings:
        return False, 0, 0, '', 0

    recent_swing_high = cl[swings[-1]]['h']

    # Current candle must close above recent swing high
    if c['c'] <= recent_swing_high:
        return False, 0, 0, '', 0

    # Strong body
    if body_size(c) < avg_body(cl) * 1.2:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    # Stop below the most recent swing low
    swing_lows = find_swing_lows(cl[-10:], length=2)
    if swing_lows:
        stop = cl[-10 + swing_lows[-1]]['l'] - pip_cushion(c)
    else:
        stop = c['l'] - pip_cushion(c) * 2

    if (entry - stop) / entry > 0.03:
        return False, 0, 0, '', 0

    return True, entry, stop, 'TC', quality


# Pattern 10: BOXED KT - KT at range boundary
def detect_boxed_kt(cl):
    if len(cl) < 15:
        return False, 0, 0, '', 0

    # Need a consolidation range
    box = cl[-12:-2]
    box_high = max(x['h'] for x in box)
    box_low = min(x['l'] for x in box)
    range_pct = (box_high - box_low) / box_low
    if range_pct > 0.03 or range_pct < 0.005:
        return False, 0, 0, '', 0

    # Must have at least 2 touches of each boundary
    touches_low = sum(1 for x in box if x['l'] <= box_low * 1.003)
    touches_high = sum(1 for x in box if x['h'] >= box_high * 0.997)
    if touches_low < 2 or touches_high < 2:
        return False, 0, 0, '', 0

    # Current candle must be a KT at bottom
    kt_fired, _, _, _, kt_q = detect_kangaroo_tail(cl)
    if not kt_fired:
        return False, 0, 0, '', 0

    c = cl[-1]
    # Wick must be outside the box (below)
    if c['l'] > box_low:
        return False, 0, 0, '', 0
    # Body (head) must be inside the box
    if c['c'] > box_low + (box_high - box_low) * 0.33:
        return False, 0, 0, '', 0  # head crossed 1/3 of range

    quality = 9
    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'BOXED_KT', quality


# Pattern 11: DROP TRADE (Parabolic trendline break bullish)
def detect_drop_trade(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Price must have been moving down with accelerating slope
    segment = cl[-20:-3]
    if len(segment) < 15:
        return False, 0, 0, '', 0
    first_q = segment[:5]
    last_q = segment[-5:]
    first_low = min(x['l'] for x in first_q)
    last_low = min(x['l'] for x in last_q)
    if last_low >= first_low:
        return False, 0, 0, '', 0

    # Recent candles must have broken above the downtrend
    recent = cl[-5:]
    recent_high = max(x['h'] for x in recent)
    if recent_high < segment[-1]['h']:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0
    if body_size(c) < avg_body(cl) * 1.0:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    stop = min(x['l'] for x in recent) - pip_cushion(c)
    if (entry - stop) / entry > 0.04:
        return False, 0, 0, '', 0

    return True, entry, stop, 'DROP', quality


# Pattern 12: RHINO - False breakout from wedge
def detect_rhino(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Find a descending wedge
    wedge = cl[-15:-2]
    if len(wedge) < 10:
        return False, 0, 0, '', 0

    first_half = wedge[:len(wedge) // 2]
    second_half = wedge[len(wedge) // 2:]
    fh_high = max(x['h'] for x in first_half)
    sh_high = max(x['h'] for x in second_half)
    fh_low = min(x['l'] for x in first_half)
    sh_low = min(x['l'] for x in second_half)

    # Both highs and lows must be compressing
    if sh_high >= fh_high:
        return False, 0, 0, '', 0
    if sh_low <= fh_low:
        return False, 0, 0, '', 0

    c = cl[-1]
    # Must be a KT that wicked BELOW the wedge lower bound
    kt_fired, _, _, _, kt_q = detect_kangaroo_tail(cl)
    if not kt_fired:
        return False, 0, 0, '', 0
    if c['l'] >= sh_low:
        return False, 0, 0, '', 0

    quality = 9
    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'RHINO', quality


# Pattern 13: BELT (gap fill reversal)
def detect_belt(cl):
    if len(cl) < 5:
        return False, 0, 0, '', 0

    c = cl[-1]
    p = cl[-2]

    # Gap down: p closed, c opened below p's low
    if c['o'] >= p['l']:
        return False, 0, 0, '', 0

    # Green belt: closed ABOVE the gap
    if not is_green(c):
        return False, 0, 0, '', 0
    if c['c'] <= p['l']:
        return False, 0, 0, '', 0

    # Body should be large (covering the gap)
    if body_size(c) < avg_body(cl) * 1.5:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'BELT', quality


# Pattern 14: SWORD - Inverted H&S with KT tail
def detect_sword(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    # Need to see an inverse H&S structure
    recent = cl[-20:]
    swing_lows = find_swing_lows(recent, length=2)
    if len(swing_lows) < 3:
        return False, 0, 0, '', 0

    # Take the last 3 swing lows — middle should be lowest
    last3 = swing_lows[-3:]
    l1, l2, l3 = recent[last3[0]]['l'], recent[last3[1]]['l'], recent[last3[2]]['l']
    if l2 >= l1 or l2 >= l3:
        return False, 0, 0, '', 0  # middle not the lowest
    # Shoulders should be roughly symmetric
    if abs(l1 - l3) / l1 > 0.01:
        return False, 0, 0, '', 0

    # Current candle should be a KT
    kt_fired, entry, stop, _, kt_q = detect_kangaroo_tail(cl)
    if not kt_fired:
        return False, 0, 0, '', 0

    return True, entry, stop, 'SWORD', kt_q + 1


# Pattern 15: 2-DAY KT (combined 2-candle KT)
def detect_2day_kt(cl):
    if len(cl) < 10:
        return False, 0, 0, '', 0

    a = cl[-2]
    b = cl[-1]

    # A must be large bearish
    ab = avg_body(cl)
    if not is_red(a) or body_size(a) < ab * 1.5:
        return False, 0, 0, '', 0

    # B must be large bullish recovering most of A
    if not is_green(b):
        return False, 0, 0, '', 0
    if b['c'] < (a['o'] + a['c']) / 2:
        return False, 0, 0, '', 0

    # Combined range > any recent single candle
    combined_low = min(a['l'], b['l'])
    combined_high = max(a['h'], b['h'])
    if combined_high - combined_low < avg_range(cl) * 1.5:
        return False, 0, 0, '', 0

    quality = 7
    entry = b['h'] + pip_cushion(b)
    stop = combined_low - pip_cushion(b)
    if (entry - stop) / entry > 0.03:
        return False, 0, 0, '', 0
    return True, entry, stop, '2D_KT', quality


# Pattern 16: BUSTED KT - Failed KT = continuation
def detect_busted_kt(cl):
    if len(cl) < 15:
        return False, 0, 0, '', 0

    # Look for an earlier KT that failed (2-3 candles ago)
    for lookback in [2, 3, 4]:
        if len(cl) <= lookback:
            continue
        # Check if that candle was a KT (bearish wedge tail)
        check_slice = cl[:-lookback + 1] if lookback > 1 else cl[:]
        if len(check_slice) < 7:
            continue
        old_c = check_slice[-1]
        rng = candle_range(old_c)
        if rng == 0:
            continue
        # Bearish-looking KT: body at bottom
        body_bot = min(old_c['o'], old_c['c'])
        if (body_bot - old_c['l']) / rng < 0.5:
            continue
        # Price has now closed ABOVE the KT high — KT busted
        c = cl[-1]
        if c['c'] > old_c['h'] and is_green(c):
            quality = 7
            entry = c['h'] + pip_cushion(c)
            stop = c['l'] - pip_cushion(c)
            if (entry - stop) / entry > 0.03:
                return False, 0, 0, '', 0
            return True, entry, stop, 'BUSTED_KT', quality
    return False, 0, 0, '', 0


# Pattern 17: BEND TRADE - Critical zone bounce
def detect_bend(cl):
    if len(cl) < 30:
        return False, 0, 0, '', 0

    # Find price levels that haven't been visited recently
    older = cl[:-10]
    recent = cl[-10:]
    older_low = min(x['l'] for x in older)
    older_high = max(x['h'] for x in older)
    recent_low = min(x['l'] for x in recent)
    recent_high = max(x['h'] for x in recent)

    c = cl[-1]
    # Price must be at older support that hasn't been tested in recent
    if not (c['l'] <= older_low * 1.005):
        return False, 0, 0, '', 0
    if recent_low < older_low * 0.99:
        return False, 0, 0, '', 0  # already visited

    if not is_green(c):
        return False, 0, 0, '', 0
    if body_size(c) < avg_body(cl):
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - pip_cushion(c)
    return True, entry, stop, 'BEND', quality


# Pattern 18: HOME RUN - Session trend reversal
def detect_home_run(cl):
    """Simplified crypto version: price drifted lower during low-vol period,
    then strong bullish candle on increasing volume."""
    if len(cl) < 10:
        return False, 0, 0, '', 0

    # Last 4-6 candles drifted down
    drift = cl[-6:-1]
    drift_low = min(x['l'] for x in drift)
    if drift[-1]['c'] < drift[0]['c']:
        pass  # drifted down — good
    else:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0
    # Close must be above the previous high
    if c['c'] <= max(x['h'] for x in drift):
        return False, 0, 0, '', 0

    # Close should be within 30 pips of drift_low
    move_pct = (c['c'] - drift_low) / drift_low
    if move_pct > 0.03:
        return False, 0, 0, '', 0

    quality = 7
    entry = c['h'] + pip_cushion(c)
    stop = drift_low - pip_cushion(c)
    if (entry - stop) / entry > 0.03:
        return False, 0, 0, '', 0
    return True, entry, stop, 'HOMERUN', quality


# Pattern 19: GHOST VALLEY - Lower low with bullish divergence
def detect_ghost_valley(cl):
    if len(cl) < 20:
        return False, 0, 0, '', 0

    recent = cl[-20:]
    swing_idxs = find_swing_lows(recent, length=2)
    if len(swing_idxs) < 2:
        return False, 0, 0, '', 0

    first = swing_idxs[-2]
    second = swing_idxs[-1]
    if second - first < 6:
        return False, 0, 0, '', 0

    # Second swing low must be LOWER than first (key Ghost Valley rule)
    if recent[second]['l'] >= recent[first]['l']:
        return False, 0, 0, '', 0

    # Look for bullish divergence in momentum
    first_close = recent[first]['c']
    second_close = recent[second]['c']
    # simple momentum proxy: close delta
    first_delta = recent[first]['c'] - recent[first - 1]['c'] if first > 0 else 0
    second_delta = recent[second]['c'] - recent[second - 1]['c'] if second > 0 else 0
    if second_delta <= first_delta:
        return False, 0, 0, '', 0

    c = cl[-1]
    if not is_green(c):
        return False, 0, 0, '', 0

    quality = 6
    entry = c['h'] + pip_cushion(c)
    stop = recent[second]['l'] - pip_cushion(c)
    if (entry - stop) / entry > 0.04:
        return False, 0, 0, '', 0
    return True, entry, stop, 'GHOST_VALLEY', quality


# ══════════════════════════════════════════════════════
#  MASTER DETECTOR
# ══════════════════════════════════════════════════════

ALL_DETECTORS = [
    detect_kangaroo_tail,
    detect_big_shadow,
    detect_wammie,
    detect_last_kiss,
    detect_trendy_kt,
    detect_trendy_bs,
    detect_pogo,
    detect_acapulco,
    detect_trend_continuation,
    detect_boxed_kt,
    detect_drop_trade,
    detect_rhino,
    detect_belt,
    detect_sword,
    detect_2day_kt,
    detect_busted_kt,
    detect_bend,
    detect_home_run,
    detect_ghost_valley,
]


def scan_all(cl):
    """Run all detectors, return the best signal (highest quality)."""
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
