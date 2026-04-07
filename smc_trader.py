"""
SMC Trader — Smart Money Concepts + Full Naked Chart Trading
Combines all 16 techniques from ICT/SMC framework:

1.  Fair Value Gaps (FVG)         9.  Fibonacci retracements
2.  Order Blocks (OB)             10. Support/Resistance zones
3.  Break of Structure (BOS)      11. Trend lines
4.  Change of Character (CHoCH)   12. Measured moves
5.  Liquidity sweeps              13. Elliott wave (basic)
6.  Liquidity zones               14. Wyckoff accumulation
7.  Market structure (HH/HL)      15. Supply/Demand zones
8.  Optimal Trade Entry (OTE)     16. Candlestick patterns

Uses Binance data for analysis, trades on Roostoo.
"""

import os
import time
import math
import json
import logging
import requests
import threading
from datetime import datetime, timezone
from collections import deque

from roostoo_client import RoostooClient

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

try:
    os.makedirs("logs", exist_ok=True)
    _lf = "logs/smc_trader.log"
except:
    _lf = "smc_trader.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()

EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD',
            'WLD/USD', 'ETH/USD', 'XRP/USD', 'BTC/USD'}

# ── Config ──
TICK_INTERVAL = 10
CANDLE_SECONDS = 3600       # 1H candles
MAX_POSITIONS = 3
POSITION_SIZE = 150000
HARD_STOP_PCT = 0.015       # 1.5% hard stop
TRAIL_STOP_PCT = 0.02       # 2% trailing
PROFIT_TRAIL_PCT = 0.008    # 0.8% tight trail once up 1.5%+
COOLDOWN_SECONDS = 3600
MIN_SCORE = 8               # need strong confluence
MAX_HOLD_CANDLES = 12       # 12 hours
MIN_CASH_RESERVE = 200000
BREADTH_MIN = 0.25          # need 25%+ green
FEE_PCT = 0.001             # 0.1% per side

COINS = {
    'SOL/USD':'SOLUSDT','BNB/USD':'BNBUSDT',
    'AVAX/USD':'AVAXUSDT','LINK/USD':'LINKUSDT','FET/USD':'FETUSDT',
    'TAO/USD':'TAOUSDT','APT/USD':'APTUSDT','SUI/USD':'SUIUSDT','NEAR/USD':'NEARUSDT',
    'PENDLE/USD':'PENDLEUSDT','ADA/USD':'ADAUSDT','DOT/USD':'DOTUSDT',
    'UNI/USD':'UNIUSDT','HBAR/USD':'HBARUSDT','ARB/USD':'ARBUSDT',
    'ENA/USD':'ENAUSDT','CAKE/USD':'CAKEUSDT','CFX/USD':'CFXUSDT','CRV/USD':'CRVUSDT',
    'FIL/USD':'FILUSDT','TRUMP/USD':'TRUMPUSDT','ONDO/USD':'ONDOUSDT',
    'AAVE/USD':'AAVEUSDT','ICP/USD':'ICPUSDT','LTC/USD':'LTCUSDT',
    'TON/USD':'TONUSDT','TRX/USD':'TRXUSDT','SEI/USD':'SEIUSDT','DOGE/USD':'DOGEUSDT',
    'VIRTUAL/USD':'VIRTUALUSDT',
}

# ── State ──
candles = {}        # pair -> list of {o,h,l,c,v,t}
tick_buffer = {}
positions = {}
cooldowns = {}
trade_history = []
total_pnl = 0
exinfo_cache = None


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    def _send():
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=3)
        except: pass
    threading.Thread(target=_send, daemon=True).start()


def get_exinfo():
    global exinfo_cache
    if exinfo_cache:
        return exinfo_cache
    try:
        exinfo_cache = client.get_exchange_info().get('TradePairs', {})
    except:
        exinfo_cache = {}
    return exinfo_cache


# ══════════════════════════════════════════════════════
#  SMC ANALYSIS ENGINE — All 16 techniques
# ══════════════════════════════════════════════════════

def is_green(c): return c['c'] > c['o']
def is_red(c): return c['c'] < c['o']
def body(c): return abs(c['c'] - c['o'])
def wick_upper(c): return c['h'] - max(c['o'], c['c'])
def wick_lower(c): return min(c['o'], c['c']) - c['l']
def mid(c): return (c['h'] + c['l']) / 2
def rng(c): return c['h'] - c['l']


# 1. FAIR VALUE GAPS
def find_fvg(cl):
    """Find unfilled fair value gaps (imbalances). Price tends to return to fill them."""
    gaps = []
    for i in range(2, len(cl)):
        # Bullish FVG: candle[i-2] high < candle[i] low (gap up)
        if cl[i-2]['h'] < cl[i]['l']:
            gaps.append({'type': 'bull', 'top': cl[i]['l'], 'bottom': cl[i-2]['h'],
                         'idx': i, 'filled': False})
        # Bearish FVG: candle[i-2] low > candle[i] high (gap down)
        if cl[i-2]['l'] > cl[i]['h']:
            gaps.append({'type': 'bear', 'top': cl[i-2]['l'], 'bottom': cl[i]['h'],
                         'idx': i, 'filled': False})

    # Check if gaps are filled
    for g in gaps:
        for j in range(g['idx'] + 1, len(cl)):
            if g['type'] == 'bull' and cl[j]['l'] <= g['bottom']:
                g['filled'] = True
                break
            if g['type'] == 'bear' and cl[j]['h'] >= g['top']:
                g['filled'] = True
                break

    return [g for g in gaps if not g['filled']]


# 2. ORDER BLOCKS
def find_order_blocks(cl):
    """Last candle before a big move = institutional entry zone."""
    obs = []
    for i in range(1, len(cl) - 1):
        move = (cl[i+1]['c'] - cl[i+1]['o']) / cl[i]['c'] * 100 if cl[i]['c'] > 0 else 0
        # Bullish OB: last red candle before explosive green
        if is_red(cl[i]) and move > 1.0:
            obs.append({'type': 'bull', 'top': cl[i]['o'], 'bottom': cl[i]['c'],
                        'idx': i, 'mitigated': False})
        # Bearish OB: last green candle before big red
        if is_green(cl[i]) and move < -1.0:
            obs.append({'type': 'bear', 'top': cl[i]['c'], 'bottom': cl[i]['o'],
                        'idx': i, 'mitigated': False})

    # Check mitigation
    for ob in obs:
        for j in range(ob['idx'] + 2, len(cl)):
            if ob['type'] == 'bull' and cl[j]['l'] <= ob['bottom']:
                ob['mitigated'] = True
                break
            if ob['type'] == 'bear' and cl[j]['h'] >= ob['top']:
                ob['mitigated'] = True
                break

    return [ob for ob in obs if not ob['mitigated']]


# 3. SWING HIGHS AND LOWS (foundation for BOS, CHoCH, structure)
def find_swings(cl, length=5):
    """Find swing highs and swing lows."""
    swings = []
    for i in range(length, len(cl) - length):
        is_high = all(cl[i]['h'] >= cl[i+j]['h'] and cl[i]['h'] >= cl[i-j]['h']
                       for j in range(1, length + 1) if i+j < len(cl) and i-j >= 0)
        is_low = all(cl[i]['l'] <= cl[i+j]['l'] and cl[i]['l'] <= cl[i-j]['l']
                      for j in range(1, length + 1) if i+j < len(cl) and i-j >= 0)
        if is_high:
            swings.append({'type': 'high', 'price': cl[i]['h'], 'idx': i})
        if is_low:
            swings.append({'type': 'low', 'price': cl[i]['l'], 'idx': i})
    return swings


# 4. BREAK OF STRUCTURE (BOS)
def detect_bos(swings):
    """BOS = price breaks a previous swing high (bullish) or swing low (bearish)."""
    if len(swings) < 3:
        return None
    # Check last few swings
    recent = swings[-3:]
    highs = [s for s in recent if s['type'] == 'high']
    lows = [s for s in recent if s['type'] == 'low']

    if len(highs) >= 2 and highs[-1]['price'] > highs[-2]['price']:
        return 'bullish_bos'
    if len(lows) >= 2 and lows[-1]['price'] < lows[-2]['price']:
        return 'bearish_bos'
    return None


# 5. CHANGE OF CHARACTER (CHoCH)
def detect_choch(swings):
    """CHoCH = first higher high after a series of lower highs (trend reversal)."""
    if len(swings) < 4:
        return None
    highs = [s for s in swings if s['type'] == 'high']
    lows = [s for s in swings if s['type'] == 'low']

    if len(highs) >= 3:
        # Was making lower highs, now made higher high
        if highs[-3]['price'] > highs[-2]['price'] and highs[-1]['price'] > highs[-2]['price']:
            return 'bullish_choch'
        # Was making higher highs, now made lower high
        if highs[-3]['price'] < highs[-2]['price'] and highs[-1]['price'] < highs[-2]['price']:
            return 'bearish_choch'
    return None


# 6. LIQUIDITY SWEEPS
def detect_liquidity_sweep(cl, swings):
    """Price spikes through a swing level then reverses = stop hunt."""
    if len(cl) < 3 or len(swings) < 2:
        return None

    current = cl[-1]
    prev = cl[-2]

    # Find recent swing lows
    recent_lows = [s for s in swings if s['type'] == 'low' and s['idx'] < len(cl) - 2]
    if not recent_lows:
        return None

    last_low = recent_lows[-1]

    # Bullish sweep: wick below swing low, close back above
    if prev['l'] < last_low['price'] and current['c'] > last_low['price'] and is_green(current):
        return {'type': 'bull_sweep', 'level': last_low['price'],
                'swept_to': prev['l'], 'recovered': current['c']}

    return None


# 7. LIQUIDITY ZONES
def find_liquidity_zones(cl):
    """Consolidation ranges with multiple touches = liquidity."""
    if len(cl) < 20:
        return []

    zones = []
    for i in range(len(cl) - 15):
        window = cl[i:i+10]
        h = max(c['h'] for c in window)
        l = min(c['l'] for c in window)
        range_pct = (h - l) / l * 100 if l > 0 else 99

        if range_pct < 3.0:
            zones.append({'high': h, 'low': l, 'range': range_pct, 'idx': i})

    # Deduplicate
    if not zones:
        return []
    zones.sort(key=lambda z: z['range'])
    return zones[:3]


# 8. MARKET STRUCTURE
def analyze_structure(swings):
    """Determine if structure is bullish (HH+HL) or bearish (LH+LL)."""
    if len(swings) < 4:
        return 'unknown'

    highs = [s for s in swings if s['type'] == 'high'][-3:]
    lows = [s for s in swings if s['type'] == 'low'][-3:]

    hh = len(highs) >= 2 and highs[-1]['price'] > highs[-2]['price']
    hl = len(lows) >= 2 and lows[-1]['price'] > lows[-2]['price']
    lh = len(highs) >= 2 and highs[-1]['price'] < highs[-2]['price']
    ll = len(lows) >= 2 and lows[-1]['price'] < lows[-2]['price']

    if hh and hl:
        return 'bullish'
    if lh and ll:
        return 'bearish'
    return 'ranging'


# 9. FIBONACCI RETRACEMENTS
def calc_fib(high, low):
    diff = high - low
    return {
        0.236: low + diff * 0.236,
        0.382: low + diff * 0.382,
        0.500: low + diff * 0.500,
        0.618: low + diff * 0.618,
        0.786: low + diff * 0.786,
    }


def fib_analysis(cl, swings):
    """Check if price is at a key fib level."""
    if len(cl) < 10 or len(swings) < 2:
        return None, 999

    # Find recent swing high and low
    highs = [s for s in swings if s['type'] == 'high']
    lows = [s for s in swings if s['type'] == 'low']

    if not highs or not lows:
        return None, 999

    recent_high = max(s['price'] for s in highs[-3:])
    recent_low = min(s['price'] for s in lows[-3:])

    if recent_high <= recent_low:
        return None, 999

    fibs = calc_fib(recent_high, recent_low)
    current = cl[-1]['c']

    # Find nearest fib
    nearest = None
    nearest_dist = 999
    for level, price in fibs.items():
        dist = abs(current - price) / current * 100
        if dist < nearest_dist:
            nearest_dist = dist
            nearest = level

    return nearest, nearest_dist


# 10. SUPPORT/RESISTANCE
def find_sr_levels(cl, n=3):
    """Find price levels with multiple touches."""
    if len(cl) < 15:
        return [], []

    all_lows = [c['l'] for c in cl]
    all_highs = [c['h'] for c in cl]

    # Cluster nearby prices
    def cluster(prices, threshold_pct=0.5):
        if not prices:
            return []
        prices = sorted(prices)
        clusters = []
        current = [prices[0]]
        for p in prices[1:]:
            if (p - current[0]) / current[0] * 100 < threshold_pct:
                current.append(p)
            else:
                if len(current) >= 2:  # need at least 2 touches
                    clusters.append({'level': sum(current)/len(current), 'touches': len(current)})
                current = [p]
        if len(current) >= 2:
            clusters.append({'level': sum(current)/len(current), 'touches': len(current)})
        return sorted(clusters, key=lambda x: x['touches'], reverse=True)[:n]

    supports = cluster(all_lows)
    resistances = cluster(all_highs)
    return supports, resistances


# 11. TREND LINES
def calc_trend_line(cl):
    """Simple trend line from recent swing lows."""
    if len(cl) < 20:
        return None

    # Find recent lows
    lows_with_idx = []
    for i in range(2, len(cl) - 2):
        if cl[i]['l'] <= cl[i-1]['l'] and cl[i]['l'] <= cl[i+1]['l']:
            lows_with_idx.append((i, cl[i]['l']))

    if len(lows_with_idx) < 2:
        return None

    # Slope from last two swing lows
    p1 = lows_with_idx[-2]
    p2 = lows_with_idx[-1]
    if p2[0] == p1[0]:
        return None

    slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
    # Project to current candle
    projected = p2[1] + slope * (len(cl) - 1 - p2[0])
    return {'slope': slope, 'projected': projected, 'bullish': slope > 0}


# 12. MEASURED MOVES
def measured_move(cl):
    """If first leg dropped X%, expect second leg ~X%."""
    if len(cl) < 20:
        return None

    # Find the biggest drop in recent history
    max_drop = 0
    drop_start = 0
    for i in range(len(cl) - 20, len(cl) - 5):
        for j in range(i + 1, min(i + 10, len(cl))):
            drop = (cl[j]['l'] - cl[i]['h']) / cl[i]['h'] * 100
            if drop < max_drop:
                max_drop = drop
                drop_start = i

    if max_drop > -2:
        return None

    # Project measured move target (recovery = same size as drop)
    current = cl[-1]['c']
    target = current * (1 + abs(max_drop) / 100)
    return {'drop_pct': max_drop, 'target': target}


# 13. ELLIOTT WAVE (basic)
def detect_elliott(cl):
    """Detect basic 5-wave impulse pattern. Simplified."""
    if len(cl) < 20:
        return None

    swings = find_swings(cl, length=3)
    if len(swings) < 5:
        return None

    # Look for: low, high, higher_low, higher_high, higher_low pattern
    # That's waves 1-2-3-4-5 starting
    recent = swings[-5:]
    types = [s['type'] for s in recent]
    prices = [s['price'] for s in recent]

    # Pattern: low, high, low, high, low (alternating) with wave 3 highest
    if types == ['low', 'high', 'low', 'high', 'low']:
        if prices[2] > prices[0] and prices[3] > prices[1]:
            # Wave 5 correction — potential buy
            return {'wave': 'wave_5_correction', 'level': prices[4]}

    # Pattern: high, low, high, low — could be starting wave 3
    if len(recent) >= 4 and types[-4:] == ['low', 'high', 'low', 'high']:
        if prices[-2] > prices[-4]:  # higher low
            return {'wave': 'wave_3_start', 'level': prices[-2]}

    return None


# 14. WYCKOFF ACCUMULATION
def detect_wyckoff(cl):
    """Detect Wyckoff accumulation (tight range + spring + markup)."""
    if len(cl) < 20:
        return None

    # Check for range (last 15 candles within 3%)
    window = cl[-15:-2]
    h = max(c['h'] for c in window)
    l = min(c['l'] for c in window)
    range_pct = (h - l) / l * 100 if l > 0 else 99

    if range_pct > 4.0:
        return None

    # Spring: last 2 candles dip below range then recover
    if cl[-2]['l'] < l and cl[-1]['c'] > l and is_green(cl[-1]):
        return {'type': 'spring', 'range_low': l, 'range_high': h, 'spring_low': cl[-2]['l']}

    # Markup start: breakout above range
    if cl[-1]['c'] > h and is_green(cl[-1]) and body(cl[-1]) > rng(cl[-1]) * 0.5:
        return {'type': 'markup', 'range_high': h, 'breakout': cl[-1]['c']}

    return None


# 15. SUPPLY/DEMAND ZONES
def find_supply_demand(cl):
    """Supply = zone before big drop, Demand = zone before big rally."""
    zones = []
    for i in range(2, len(cl) - 2):
        # Demand: consolidation then big green
        move_after = (cl[i+1]['c'] - cl[i]['c']) / cl[i]['c'] * 100 if cl[i]['c'] > 0 else 0
        if move_after > 1.5:
            zones.append({'type': 'demand', 'top': max(cl[i]['o'], cl[i]['c']),
                          'bottom': min(cl[i]['o'], cl[i]['c']), 'strength': move_after, 'idx': i})
        if move_after < -1.5:
            zones.append({'type': 'supply', 'top': max(cl[i]['o'], cl[i]['c']),
                          'bottom': min(cl[i]['o'], cl[i]['c']), 'strength': abs(move_after), 'idx': i})

    # Only fresh zones (not yet revisited)
    fresh = []
    for z in zones:
        revisited = False
        for j in range(z['idx'] + 2, len(cl)):
            if z['type'] == 'demand' and cl[j]['l'] <= z['bottom']:
                revisited = True
                break
            if z['type'] == 'supply' and cl[j]['h'] >= z['top']:
                revisited = True
                break
        if not revisited:
            fresh.append(z)

    return fresh


# 16. CANDLESTICK PATTERNS
def detect_candle_patterns(cl):
    """Classic candlestick patterns with scoring."""
    if len(cl) < 5:
        return 0, []

    score = 0
    patterns = []
    c = cl[-1]; p = cl[-2]; pp = cl[-3]

    # Bullish Engulfing
    if is_red(p) and is_green(c) and c['o'] <= p['c'] and c['c'] >= p['o'] and body(c) > body(p) * 1.2:
        score += 3; patterns.append('ENGULF')

    # Hammer
    if rng(c) > 0:
        lw = wick_lower(c)
        bs = body(c)
        uw = wick_upper(c)
        if bs > 0 and lw > bs * 2 and uw < bs * 0.5 and is_green(c):
            score += 3; patterns.append('HAMMER')

    # Morning Star
    if len(cl) >= 4 and is_red(pp) and body(p) < rng(p) * 0.3 and is_green(c) and c['c'] > (pp['o'] + pp['c']) / 2:
        score += 3; patterns.append('MSTAR')

    # Three White Soldiers
    if len(cl) >= 4:
        c1, c2, c3 = cl[-3], cl[-2], cl[-1]
        if is_green(c1) and is_green(c2) and is_green(c3) and c2['c'] > c1['c'] and c3['c'] > c2['c']:
            score += 3; patterns.append('3WS')

    # Inside Bar Breakout
    if len(cl) >= 4:
        mother = cl[-3]; inside = cl[-2]; bo = cl[-1]
        if inside['h'] <= mother['h'] and inside['l'] >= mother['l'] and bo['c'] > mother['h']:
            score += 3; patterns.append('INSIDE_BRK')

    # Piercing Line
    if is_red(p) and is_green(c) and c['o'] < p['l'] and c['c'] > (p['o'] + p['c']) / 2:
        score += 2; patterns.append('PIERCE')

    # Bullish Kicker
    if is_red(p) and is_green(c) and c['o'] > p['o']:
        score += 3; patterns.append('KICKER')

    # Marubozu (full body, no wicks)
    if is_green(c) and body(c) > rng(c) * 0.8:
        score += 2; patterns.append('MARU')

    # Double Bottom
    if len(cl) >= 10:
        lows = [(i, cl[i]['l']) for i in range(len(cl)-10, len(cl)-2)]
        lows.sort(key=lambda x: x[1])
        if len(lows) >= 2 and abs(lows[0][0] - lows[1][0]) >= 3:
            diff = abs(lows[0][1] - lows[1][1]) / lows[0][1] * 100
            if diff < 0.5 and is_green(cl[-1]):
                score += 3; patterns.append('DBLBOT')

    return score, patterns


# ══════════════════════════════════════════════════════
#  CONFLUENCE SCORER — combines all 16 techniques
# ══════════════════════════════════════════════════════

def score_coin(pair, cl):
    """Run all 16 analyses and return a confluence score."""
    if len(cl) < 20:
        return 0, [], {}

    score = 0
    signals = []
    details = {}
    current = cl[-1]['c']

    # 1. Candlestick patterns
    cs, cp = detect_candle_patterns(cl)
    score += cs
    signals.extend(cp)

    # 2. Swings & Market structure
    swings = find_swings(cl, length=3)
    structure = analyze_structure(swings)
    details['structure'] = structure
    if structure == 'bullish':
        score += 2; signals.append('BULL_STRUCT')
    elif structure == 'bearish':
        score -= 3  # penalize bearish structure

    # 3. BOS
    bos = detect_bos(swings)
    if bos == 'bullish_bos':
        score += 2; signals.append('BOS')

    # 4. CHoCH
    choch = detect_choch(swings)
    if choch == 'bullish_choch':
        score += 3; signals.append('CHoCH')

    # 5. Fair Value Gaps
    fvgs = find_fvg(cl)
    bull_fvgs = [f for f in fvgs if f['type'] == 'bull']
    for f in bull_fvgs:
        if f['bottom'] <= current <= f['top']:
            score += 2; signals.append('IN_FVG')
            break
        if current < f['bottom'] and (f['bottom'] - current) / current * 100 < 1.0:
            score += 1; signals.append('NEAR_FVG')
            break

    # 6. Order Blocks
    obs = find_order_blocks(cl)
    bull_obs = [ob for ob in obs if ob['type'] == 'bull']
    for ob in bull_obs:
        if ob['bottom'] <= current <= ob['top']:
            score += 3; signals.append('IN_OB')
            break
        if current < ob['top'] and (ob['top'] - current) / current * 100 < 1.0:
            score += 1; signals.append('NEAR_OB')
            break

    # 7. Liquidity Sweep
    sweep = detect_liquidity_sweep(cl, swings)
    if sweep and sweep['type'] == 'bull_sweep':
        score += 3; signals.append('LIQ_SWEEP')
        details['sweep'] = sweep

    # 8. Fibonacci
    fib_level, fib_dist = fib_analysis(cl, swings)
    if fib_level in (0.618, 0.786) and fib_dist < 1.0:
        score += 3; signals.append(f'FIB_{fib_level}')
    elif fib_level == 0.500 and fib_dist < 1.0:
        score += 2; signals.append('FIB_0.5')
    elif fib_level == 0.382 and fib_dist < 1.0:
        score += 1; signals.append('FIB_0.382')
    details['fib'] = f'{fib_level} (dist={fib_dist:.1f}%)'

    # 9. Support/Resistance
    supports, resistances = find_sr_levels(cl)
    for s in supports:
        dist = (current - s['level']) / current * 100
        if 0 < dist < 1.5:
            score += 2; signals.append(f'NEAR_SUP({s["touches"]}t)')
            break
        if -0.5 < dist <= 0:
            score += 3; signals.append(f'AT_SUP({s["touches"]}t)')
            break

    # 10. Supply/Demand
    sd_zones = find_supply_demand(cl)
    demand_zones = [z for z in sd_zones if z['type'] == 'demand']
    for z in demand_zones:
        if z['bottom'] <= current <= z['top']:
            score += 2; signals.append('IN_DEMAND')
            break

    # 11. Trend line
    tl = calc_trend_line(cl)
    if tl and tl['bullish'] and tl['projected'] > 0:
        dist = (current - tl['projected']) / current * 100
        if -1.0 < dist < 1.0:
            score += 1; signals.append('ON_TRENDLINE')

    # 12. Measured move
    mm = measured_move(cl)
    if mm:
        upside = (mm['target'] - current) / current * 100
        if upside > 2.0:
            score += 1; signals.append(f'MM_TARGET_{upside:.0f}%')

    # 13. Elliott wave
    ew = detect_elliott(cl)
    if ew:
        score += 2; signals.append(f'ELLIOTT_{ew["wave"]}')

    # 14. Wyckoff
    wyck = detect_wyckoff(cl)
    if wyck:
        if wyck['type'] == 'spring':
            score += 3; signals.append('WYCKOFF_SPRING')
        elif wyck['type'] == 'markup':
            score += 2; signals.append('WYCKOFF_MARKUP')

    # 15. Liquidity zones
    liq_zones = find_liquidity_zones(cl)
    for z in liq_zones:
        if z['low'] <= current <= z['high']:
            score += 1; signals.append('IN_LIQ_ZONE')
            break

    # 16. Momentum confirmation
    if len(cl) >= 6:
        mom6 = (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100
        if mom6 > 0.5:
            score += 1; signals.append(f'MOM+{mom6:.1f}%')
        elif mom6 < -2:
            score -= 1  # negative momentum penalty

    # Volume confirmation
    if len(cl) >= 10:
        avg_vol = sum(c['v'] for c in cl[-10:-1]) / 9
        if avg_vol > 0 and cl[-1]['v'] > avg_vol * 1.5 and is_green(cl[-1]):
            score += 1; signals.append('VOL_CONF')

    # Bouncing (green after red)
    if is_red(cl[-2]) and is_green(cl[-1]):
        score += 1; signals.append('BOUNCING')

    details['score'] = score
    details['signals'] = signals
    return score, signals, details


# ══════════════════════════════════════════════════════
#  TRADING ENGINE
# ══════════════════════════════════════════════════════

def update_candles_from_tick(td):
    """Build 1H candles from Roostoo ticks."""
    now = time.time()
    current_hour = int(now / CANDLE_SECONDS)

    for pair, info in td.items():
        if pair in EXCLUDED:
            continue
        px = float(info.get('LastPrice', 0))
        if px <= 0:
            continue

        bid = float(info.get('MaxBid', 0))
        ask = float(info.get('MinAsk', 0))
        vol = float(info.get('CoinTradeValue', 0))

        if pair not in tick_buffer:
            tick_buffer[pair] = []
            if pair not in candles:
                candles[pair] = []

        tick_buffer[pair].append({'t': now, 'p': px, 'b': bid, 'a': ask, 'v': vol})

        ticks = tick_buffer[pair]
        if not ticks:
            continue

        first_hour = int(ticks[0]['t'] / CANDLE_SECONDS)
        if current_hour > first_hour and len(ticks) >= 2:
            candle_ticks = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) == first_hour]
            remaining = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) > first_hour]

            if candle_ticks:
                candle = {
                    'o': candle_ticks[0]['p'],
                    'h': max(t['p'] for t in candle_ticks),
                    'l': min(t['p'] for t in candle_ticks),
                    'c': candle_ticks[-1]['p'],
                    'v': candle_ticks[-1]['v'],
                    't': first_hour * CANDLE_SECONDS,
                }
                candles[pair].append(candle)
                if len(candles[pair]) > 200:
                    candles[pair] = candles[pair][-200:]

            tick_buffer[pair] = remaining


def get_breadth(td):
    """Calculate market breadth."""
    total = green = 0
    for pair, info in td.items():
        chg = float(info.get('Change', 0))
        total += 1
        if chg > 0:
            green += 1
    return green / total if total > 0 else 0


def check_exits(td):
    """Check all positions for exits."""
    global total_pnl
    to_close = []

    for pair, pos in positions.items():
        info = td.get(pair, {})
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        if px <= 0:
            continue

        if px > pos['peak']:
            pos['peak'] = px

        pnl_pct = (px - pos['entry']) / pos['entry']
        pos['candles_held'] = pos.get('candles_held', 0)

        reason = None

        # Hard stop
        if pnl_pct <= -HARD_STOP_PCT:
            reason = 'STOP'

        # Profit trail — once up 1.5%, trail tight
        elif pnl_pct > 0.015 and px <= pos['peak'] * (1 - PROFIT_TRAIL_PCT):
            reason = 'PROFIT_TRAIL'

        # Dynamic trailing stop
        elif pnl_pct > 0.003:
            new_stop = pos['peak'] * (1 - TRAIL_STOP_PCT)
            if new_stop > pos.get('stop', 0):
                pos['stop'] = new_stop
            if pos.get('stop', 0) > 0 and px <= pos['stop']:
                reason = 'TRAIL'

        # SMC exit: bearish FVG or order block
        if not reason and pair in candles and len(candles[pair]) >= 5:
            cl = candles[pair]
            if is_red(cl[-1]) and is_red(cl[-2]) and pnl_pct > 0:
                reason = 'BEAR_PATTERN'

        # Time stop
        if not reason and pos.get('candles_held', 0) >= MAX_HOLD_CANDLES:
            reason = 'TIME'

        if reason:
            # Sell
            exinfo = get_exinfo()
            pi = exinfo.get(pair, {})
            pp = int(pi.get('PricePrecision', 4))
            ap = int(pi.get('AmountPrecision', 2))
            qty = math.floor(pos['qty'] * 10**ap) / 10**ap

            try:
                order = client.place_order(pair, 'SELL', 'MARKET', qty, round(bid, pp))
                det = order.get('OrderDetail', order)
                exit_px = float(det.get('FilledAverPrice', 0) or bid)
            except:
                exit_px = bid

            pnl_usd = (exit_px - pos['entry']) * pos['qty']
            fees = pos['entry'] * pos['qty'] * FEE_PCT + exit_px * pos['qty'] * FEE_PCT
            pnl_usd -= fees
            total_pnl += pnl_usd

            trade_history.append({
                'pair': pair, 'pnl': pnl_usd, 'pnl_pct': pnl_pct * 100,
                'reason': reason, 'signals': pos.get('signals', []),
                'candles': pos.get('candles_held', 0),
            })

            marker = 'WIN' if pnl_usd > 0 else 'LOSS'
            log.info(f"SMC EXIT {pair} ({reason}): P&L=${pnl_usd:+,.0f} ({pnl_pct:+.2%}) [{marker}]")
            send_telegram(
                f"<b>SMC {reason} {pair}</b>\n"
                f"P&L: ${pnl_usd:+,.0f} ({pnl_pct:+.2%})\n"
                f"Signals: {', '.join(pos.get('signals', [])[:5])}\n"
                f"Held: {pos.get('candles_held', 0)} candles [{marker}]"
            )
            to_close.append(pair)
            cooldowns[pair] = time.time() + COOLDOWN_SECONDS

    for pair in to_close:
        positions.pop(pair, None)


def check_entries(td):
    """Scan all coins for SMC entry signals."""
    if len(positions) >= MAX_POSITIONS:
        return

    breadth = get_breadth(td)
    if breadth < BREADTH_MIN:
        return

    # Check cash
    try:
        bal = client.get_balance()
        wallet = bal.get('SpotWallet', {})
        cash = float(wallet.get('USD', {}).get('Free', 0))
    except:
        return

    if cash < MIN_CASH_RESERVE + POSITION_SIZE:
        return

    # Score all coins
    candidates = []
    for pair in candles:
        if pair in positions or pair in EXCLUDED:
            continue
        if pair in cooldowns and time.time() < cooldowns[pair]:
            continue

        cl = candles[pair]
        if len(cl) < 20:
            continue

        # Check spread
        info = td.get(pair, {})
        bid = float(info.get('MaxBid', 0))
        ask = float(info.get('MinAsk', 0))
        if bid <= 0 or ask <= 0:
            continue
        spread = (ask - bid) / bid * 100
        if spread > 0.2:
            continue

        score, signals, details = score_coin(pair, cl)

        if score >= MIN_SCORE:
            candidates.append((score, pair, signals, details, info))

    candidates.sort(key=lambda x: -x[0])

    # Enter top candidate
    for score, pair, signals, details, info in candidates[:1]:
        if len(positions) >= MAX_POSITIONS:
            break

        ask = float(info.get('MinAsk', 0))
        if ask <= 0:
            continue

        # Re-check cash
        try:
            bal = client.get_balance()
            cash = float(bal.get('SpotWallet', {}).get('USD', {}).get('Free', 0))
        except:
            break
        if cash < POSITION_SIZE * 1.01:
            break

        actual_size = min(POSITION_SIZE, cash * 0.25)
        if actual_size < 10000:
            break

        exinfo = get_exinfo()
        pi = exinfo.get(pair, {})
        pp = int(pi.get('PricePrecision', 4))
        ap = int(pi.get('AmountPrecision', 2))
        qty = math.floor(actual_size / ask * 10**ap) / 10**ap
        if qty <= 0:
            continue

        try:
            order = client.place_order(pair, 'BUY', 'MARKET', qty, round(ask, pp))
            det = order.get('OrderDetail', order)
            status = (det.get('Status') or '').upper()
            filled = float(det.get('FilledQuantity', 0) or 0)
            fill_px = float(det.get('FilledAverPrice', 0) or ask)

            if filled <= 0:
                continue

            positions[pair] = {
                'entry': fill_px, 'qty': filled,
                'peak': fill_px, 'stop': fill_px * (1 - HARD_STOP_PCT),
                'time': time.time(), 'score': score,
                'signals': signals, 'candles_held': 0,
            }

            log.info(f"SMC BUY {pair}: score={score} @ ${fill_px:.4f} signals={','.join(signals[:6])}")
            send_telegram(
                f"<b>SMC BUY {pair}</b>\n"
                f"Score: {score} | Signals: {len(signals)}\n"
                f"{', '.join(signals[:6])}\n"
                f"Price: ${fill_px:.4f} | Size: ${filled*fill_px:,.0f}\n"
                f"Stop: ${fill_px*(1-HARD_STOP_PCT):.4f}"
            )
        except Exception as e:
            log.error(f"SMC buy {pair} failed: {e}")


# ══════════════════════════════════════════════════════
#  BOOTSTRAP & MAIN
# ══════════════════════════════════════════════════════

def bootstrap():
    """Load 100 hourly candles from Binance for all coins."""
    log.info("Bootstrapping from Binance...")
    count = 0
    for pair, symbol in COINS.items():
        if pair in EXCLUDED:
            continue
        try:
            url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100'
            r = requests.get(url, timeout=5)
            data = r.json()
            if isinstance(data, list) and len(data) > 20:
                candles[pair] = []
                for k in data[:-1]:
                    candles[pair].append({
                        'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                        'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
                count += 1
        except:
            pass
        time.sleep(0.1)
    log.info(f"Bootstrapped {count} coins with ~100 hourly candles each")
    return count


def main():
    log.info("=" * 60)
    log.info("SMC TRADER — Smart Money Concepts + Full Price Action")
    log.info(f"16 techniques | Min score: {MIN_SCORE} | Size: ${POSITION_SIZE:,}")
    log.info(f"Stop: {HARD_STOP_PCT*100}% | Trail: {TRAIL_STOP_PCT*100}% | Max hold: {MAX_HOLD_CANDLES}h")
    log.info(f"Breadth min: {BREADTH_MIN*100}% | Max pos: {MAX_POSITIONS}")
    log.info("=" * 60)

    send_telegram(
        "<b>SMC TRADER ONLINE</b>\n"
        "16 techniques: FVG, OB, BOS, CHoCH, sweeps,\n"
        "fib, S/R, Elliott, Wyckoff, patterns...\n"
        f"Min score: {MIN_SCORE} | Breadth > {BREADTH_MIN*100:.0f}%\n"
        f"Stop: {HARD_STOP_PCT*100}% | Max pos: {MAX_POSITIONS}"
    )

    count = bootstrap()
    if count == 0:
        log.error("Bootstrap failed — no candle data")
        return

    import fcntl, sys
    lock = open('/tmp/smc_trader.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("SMC Trader already running!")
        sys.exit(1)

    tick = 0
    while True:
        try:
            td = client.get_ticker().get('Data', {})
            if not td:
                time.sleep(TICK_INTERVAL)
                continue

            tick += 1
            update_candles_from_tick(td)

            if positions:
                check_exits(td)
                # Increment candle counters
                for pos in positions.values():
                    if tick % (CANDLE_SECONDS // TICK_INTERVAL) == 0:
                        pos['candles_held'] = pos.get('candles_held', 0) + 1

            check_entries(td)

            # Status
            if tick % 30 == 0:
                breadth = get_breadth(td)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history)
                wr = wins / n * 100 if n > 0 else 0

                pos_str = ''
                if positions:
                    parts = []
                    for p, pos in positions.items():
                        px = float(td.get(p, {}).get('LastPrice', 0))
                        if px > 0:
                            pnl = (px - pos['entry']) / pos['entry'] * 100
                            parts.append(f'{p.split("/")[0]}({pnl:+.1f}%)')
                    pos_str = ' | ' + ', '.join(parts)

                log.info(
                    f"SMC: {len(positions)} pos{pos_str} | "
                    f"{n} trades ({wins}W {wr:.0f}%) | P&L=${total_pnl:+,.0f} | "
                    f"breadth={breadth:.0%}"
                )

            # Hourly telegram
            if tick % 360 == 0:
                breadth = get_breadth(td)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history)
                send_telegram(
                    f"<b>SMC Status</b>\n"
                    f"Positions: {len(positions)}\n"
                    f"Trades: {n} ({wins}W)\n"
                    f"P&L: ${total_pnl:+,.0f}\n"
                    f"Breadth: {breadth:.0%}"
                )

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(TICK_INTERVAL)


if __name__ == '__main__':
    main()
