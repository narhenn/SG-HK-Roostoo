"""
NAKED TRADER V7 ULTIMATE — The World's Best Pattern Day Trading Bot

35 Bullish Entry Patterns:
  CANDLESTICK REVERSALS (12):
    1. Bullish Engulfing        — red then bigger green engulfs
    2. Hammer                   — long lower wick, rejected dip
    3. Morning Star             — 3-bar: red, doji, green
    4. Piercing Line            — opens below, closes above midpoint
    5. Tweezer Bottom           — same lows, second green
    6. Bullish Kicker           — gap up after red, strong green
    7. Three Inside Up          — harami then breakout above
    8. Bullish Harami           — small green inside big red
    9. Dragonfly Doji           — long lower shadow, no upper
   10. Inverted Hammer          — long upper wick in downtrend
   11. Abandoned Baby           — gap down doji then gap up green
   12. Belt Hold                — opens at low, closes at high

  CANDLESTICK CONTINUATION (5):
    13. Three White Soldiers    — 3 consecutive strong greens
    14. Rising Three Methods    — big green, 3 small reds inside, big green
    15. Mat Hold                — big green, 2-3 small reds, big green breakout
    16. Marubozu                — full green body, no wicks
    17. Rising Window           — gap up with green candle

  STRUCTURE (4):
    18. Inside Bar Breakout     — consolidation then expansion above
    19. Higher High + Higher Low — uptrend structure
    20. Double Bottom           — W pattern at same support
    21. Range Breakout          — flat for 10+ bars then breaks out

  SMART MONEY CONCEPTS (4):
    22. Bullish Fair Value Gap  — gap between candle 1 high and candle 3 low
    23. Bullish Order Block     — last red before explosive green move
    24. Liquidity Sweep + Reclaim — wick below support then close above
    25. Change of Character     — first higher high after downtrend

  CONTEXT BONUSES (10):
    26. SMA20 Bounce            — price within 0.5% of SMA20
    27. Squeeze Breakout        — low 5-bar range then expansion
    28. RSI Oversold Recovery   — RSI was <35, now recovering
    29. Mean Reversion          — 3+ red candles before green
    30. Volume Confirmation     — current vol > prev vol
    31. Near Support            — within 1.5% of 20-bar low
    32. Momentum                — up 1%+ in last 6 bars
    33. Volume Breakout         — vol 2.5x+ avg with strong green
    34. Trend Alignment         — price above EMA21
    35. Candle Body Strength    — body > 60% of total range

REGIME: BTC above EMA20 = BULL (4 pos, $200k, score>=6)
        BTC below EMA20 + RSI<40 = BEAR (1 pos, $100k, score>=8)

EXITS: ATR stop | 1% trail | Partial at +1% | 12h max hold | 3h min hold
SAFETY: position overwrite block | cash check | orphan detector | MARKET sells

Dynamic sizing: score 6-7=$200k | 8-9=$250k | 10+=$350k | BEAR=half
"""

import time
import math
import json
import logging
import requests
import numpy as np
from collections import deque
from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

try:
    import os; os.makedirs("logs", exist_ok=True)
    _lf = "logs/naked_trader.log"
except:
    _lf = "naked_trader.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()
EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD',
            'WLD/USD', 'ETH/USD', 'XRP/USD', 'BTC/USD'}

# ── Config ──
TICK_INTERVAL = 10
CANDLE_SECONDS = 3600       # 1h candles
MAX_POSITIONS = 4
HARD_STOP_PCT = 0.005
TRAIL_STOP_PCT = 0.020
PROFIT_TRAIL_PCT = 0.01     # 1% trail
COOLDOWN_SECONDS = 3600
MIN_PATTERN_SCORE = 6
MAX_HOLD_CANDLES = 12
MIN_HOLD_CANDLES = 3
MAX_PORTFOLIO_EXPOSURE = 0.60
MIN_CASH_RESERVE = 200000

# Dynamic sizing
SIZE_LOW = 200000    # score 6-7
SIZE_MED = 250000    # score 8-9
SIZE_HIGH = 350000   # score 10+
SIZE_BEAR = 100000   # bear regime

# ── State ──
tick_buffer = {}
candles = {}
positions = {}
cooldowns = {}
trade_history = []
exinfo_cache = None


def alert(msg):
    import threading
    def _send():
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=3)
        except: pass
    threading.Thread(target=_send, daemon=True).start()
    log.info(msg.replace('<b>', '').replace('</b>', ''))


def get_exinfo():
    global exinfo_cache
    if exinfo_cache: return exinfo_cache
    try:
        exinfo_cache = client.get_exchange_info().get('TradePairs', {})
    except:
        exinfo_cache = {}
    return exinfo_cache


def get_cash():
    try:
        bal = client.get_balance()
        w = bal.get('SpotWallet', bal.get('Data', bal))
        if isinstance(w, dict):
            usd = w.get('USD', {})
            if isinstance(usd, dict):
                return float(usd.get('Free', 0))
            return float(usd)
    except: pass
    return 0


def update_candles(td):
    now = time.time()
    current_period = int(now / CANDLE_SECONDS)
    for pair, info in td.items():
        if pair in EXCLUDED: continue
        px = float(info.get('LastPrice', 0))
        if px <= 0: continue
        if pair not in tick_buffer:
            tick_buffer[pair] = []
            if pair not in candles:
                candles[pair] = deque(maxlen=200)
        tick_buffer[pair].append({'t': now, 'p': px})
        ticks = tick_buffer[pair]
        if not ticks: continue
        first_period = int(ticks[0]['t'] / CANDLE_SECONDS)
        if current_period > first_period and len(ticks) >= 2:
            candle_ticks = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) == first_period]
            remaining = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) > first_period]
            if candle_ticks:
                candle = {
                    'o': candle_ticks[0]['p'],
                    'h': max(t['p'] for t in candle_ticks),
                    'l': min(t['p'] for t in candle_ticks),
                    'c': candle_ticks[-1]['p'],
                    'v': len(candle_ticks),  # tick count as proxy
                    't': first_period * CANDLE_SECONDS,
                }
                candles[pair].append(candle)
            tick_buffer[pair] = remaining


# ══════════════════════════════════════════════════════
#  PATTERN DETECTION — 35 patterns
# ══════════════════════════════════════════════════════

def _b(c): return c['c'] - c['o']
def _bs(c): return abs(_b(c))
def _gr(c): return c['c'] > c['o']
def _rd(c): return c['c'] < c['o']
def _uw(c): return c['h'] - max(c['o'], c['c'])
def _lw(c): return min(c['o'], c['c']) - c['l']
def _rng(c): return c['h'] - c['l']


def detect_regime(pair='BTC/USD'):
    """BULL unless BTC below EMA20 AND (RSI<40 or 3h drop > 0.5%)"""
    cl = list(candles.get(pair, []))
    if len(cl) < 20: return 'BULL'
    closes = [c['c'] for c in cl]
    ema20 = sum(closes[-20:]) / 20
    px = closes[-1]
    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(-14, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_g = sum(gains) / 14 if gains else 0.001
    avg_l = sum(losses) / 14 if losses else 0.001
    rsi = 100 - 100 / (1 + avg_g / avg_l)
    # 3h change
    chg3 = (closes[-1] - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 else 0
    if px < ema20 and (rsi < 40 or chg3 < -0.5):
        return 'BEAR'
    return 'BULL'


def detect_patterns(pair):
    cl = list(candles.get(pair, []))
    if len(cl) < 10: return 0, ''

    score = 0
    patterns = []
    n = len(cl)
    c = cl[-1]; p = cl[-2] if n >= 2 else c; pp = cl[-3] if n >= 3 else p

    # Averages
    bodies = [_bs(x) for x in cl[-14:]]
    ranges = [_rng(x) for x in cl[-14:]]
    avg_body = sum(bodies) / len(bodies) if bodies else 0.0001
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0001
    if avg_body == 0: avg_body = 0.0001
    if avg_range == 0: avg_range = 0.0001

    bs = _bs(c); rng = _rng(c); bs_p = _bs(p)
    lw = _lw(c); uw = _uw(c)

    # ═══ REVERSALS ═══

    # 1. Bullish Engulfing
    if _rd(p) and _gr(c) and c['o'] <= p['c'] and c['c'] >= p['o'] and bs > bs_p * 1.2:
        score += 3; patterns.append('ENGULF')

    # 2. Hammer
    if rng > 0 and bs > 0 and lw >= bs * 2 and uw <= bs * 0.5 and _gr(c):
        score += 3; patterns.append('HAMMER')

    # 3. Morning Star
    if n >= 3:
        b1, b2, b3 = _bs(cl[-3]), _bs(cl[-2]), _bs(cl[-1])
        if _rd(cl[-3]) and b1 > avg_body and b2 < b1 * 0.3 and _gr(cl[-1]) and b3 > avg_body:
            if cl[-1]['c'] > (cl[-3]['o'] + cl[-3]['c']) / 2:
                score += 4; patterns.append('MSTAR')

    # 4. Piercing Line
    if _rd(p) and _gr(c):
        mid = (p['o'] + p['c']) / 2
        if c['o'] < p['c'] and c['c'] > mid and c['c'] < p['o']:
            score += 3; patterns.append('PIERCE')

    # 5. Tweezer Bottom
    if n >= 2 and avg_range > 0 and abs(c['l'] - p['l']) / avg_range < 0.05:
        if _rd(p) and _gr(c):
            score += 3; patterns.append('TWZR')

    # 6. Bullish Kicker
    if _rd(p) and _gr(c) and c['o'] > p['o'] and bs > avg_body * 1.5:
        score += 4; patterns.append('KICKER')

    # 7. Three Inside Up
    if n >= 3 and _rd(cl[-3]) and _bs(cl[-3]) > avg_body:
        if _gr(cl[-2]) and cl[-2]['o'] > cl[-3]['c'] and cl[-2]['c'] < cl[-3]['o']:
            if _gr(cl[-1]) and cl[-1]['c'] > cl[-3]['o']:
                score += 3; patterns.append('3INSIDE')

    # 8. Bullish Harami
    if _rd(p) and _gr(c) and c['o'] > p['c'] and c['c'] < p['o'] and bs < bs_p * 0.5:
        score += 2; patterns.append('HARAMI')

    # 9. Dragonfly Doji (in downtrend)
    if rng > 0 and bs <= rng * 0.1 and lw > rng * 0.6 and uw < rng * 0.1:
        if n >= 5 and cl[-1]['c'] < cl[-5]['c']:
            score += 3; patterns.append('DRAGON')

    # 10. Inverted Hammer
    if rng > 0 and bs > 0 and uw >= bs * 2 and lw <= bs * 0.3:
        if n >= 5 and cl[-1]['c'] < cl[-5]['c']:  # in downtrend
            score += 2; patterns.append('INVHAM')

    # 11. Abandoned Baby
    if n >= 3:
        b2 = _bs(cl[-2]); r2 = _rng(cl[-2])
        if _rd(cl[-3]) and r2 > 0 and b2 < r2 * 0.1:  # middle is doji
            if cl[-2]['h'] < cl[-3]['l']:  # gap down
                if _gr(cl[-1]) and cl[-1]['l'] > cl[-2]['h']:  # gap up
                    score += 4; patterns.append('BABY')

    # 12. Belt Hold
    if _gr(c) and bs > avg_body * 1.5:
        if (c['o'] - c['l']) < bs * 0.05:  # opened at low
            score += 2; patterns.append('BELT')

    # ═══ CONTINUATIONS ═══

    # 13. Three White Soldiers
    if n >= 3 and _gr(cl[-3]) and _gr(cl[-2]) and _gr(cl[-1]):
        if cl[-1]['c'] > cl[-2]['c'] > cl[-3]['c']:
            if _bs(cl[-1]) > 0 and _bs(cl[-2]) > 0 and _bs(cl[-3]) > 0:
                score += 3; patterns.append('3WS')

    # 14. Rising Three Methods
    if n >= 5:
        first = cl[-5]; last = cl[-1]
        middle = cl[-4:-1]
        if _gr(first) and _bs(first) > avg_body * 1.2:
            if all(_rd(m) or _bs(m) < _bs(first) * 0.5 for m in middle):
                if all(m['l'] >= first['l'] for m in middle):
                    if _gr(last) and last['c'] > first['h']:
                        score += 4; patterns.append('RISE3')

    # 15. Mat Hold
    if n >= 5:
        first = cl[-5]; last = cl[-1]
        middle = cl[-4:-1]
        if _gr(first) and _bs(first) > avg_body:
            small_middle = all(_bs(m) < _bs(first) * 0.5 for m in middle)
            in_range = all(m['l'] >= first['o'] for m in middle)
            if small_middle and in_range and _gr(last) and last['c'] > first['h']:
                score += 4; patterns.append('MATHOLD')

    # 16. Marubozu
    if _gr(c) and bs > avg_body * 2:
        if uw < bs * 0.1 and lw < bs * 0.1:
            score += 3; patterns.append('MARU')

    # 17. Rising Window (gap up)
    if n >= 2 and c['l'] > p['h'] and _gr(c):
        score += 3; patterns.append('WINDOW')

    # ═══ STRUCTURE ═══

    # 18. Inside Bar Breakout
    if n >= 3:
        mother = cl[-3]; inside = cl[-2]; breakout = cl[-1]
        if inside['h'] <= mother['h'] and inside['l'] >= mother['l']:
            if breakout['c'] > mother['h'] and _gr(breakout):
                score += 3; patterns.append('INSIDE')

    # 19. Higher High + Higher Low
    if n >= 5:
        if cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h'] and _gr(cl[-1]):
            score += 2; patterns.append('HHHL')

    # 20. Double Bottom
    if n >= 20:
        lows = [x['l'] for x in cl[-20:]]
        si = sorted(range(len(lows)), key=lambda i: lows[i])
        if len(si) >= 2 and abs(si[0] - si[1]) >= 3:
            if abs(lows[si[0]] - lows[si[1]]) / lows[si[0]] < 0.005:
                if _gr(cl[-1]):
                    score += 3; patterns.append('DBLBOT')

    # 21. Range Breakout
    if n >= 12:
        range_bars = cl[-12:-2]
        range_high = max(x['h'] for x in range_bars)
        range_low = min(x['l'] for x in range_bars)
        range_pct = (range_high - range_low) / range_high * 100 if range_high > 0 else 99
        if range_pct < 1.0 and cl[-1]['c'] > range_high and _gr(cl[-1]):
            score += 3; patterns.append('RNGBRK')

    # ═══ SMART MONEY CONCEPTS ═══

    # 22. Bullish Fair Value Gap (FVG)
    if n >= 3:
        c1_high = cl[-3]['h']; c3_low = cl[-1]['l']
        if c3_low > c1_high and _gr(cl[-1]):  # gap between candle 1 high and candle 3 low
            gap_size = (c3_low - c1_high) / c['c'] * 100
            if gap_size > 0.1:
                score += 2; patterns.append('FVG')

    # 23. Bullish Order Block
    if n >= 5:
        # Last red candle before a strong green move
        for j in range(-5, -2):
            if _rd(cl[j]) and _gr(cl[j+1]):
                move = (cl[j+1]['c'] - cl[j+1]['o']) / cl[j+1]['o'] * 100
                if move > 0.5:  # strong move after the red
                    # Price came back to the order block zone
                    if c['l'] <= cl[j]['h'] and c['c'] > cl[j]['h'] and _gr(c):
                        score += 3; patterns.append('OB')
                        break

    # 24. Liquidity Sweep + Reclaim
    if n >= 10:
        recent_low = min(x['l'] for x in cl[-10:-1])
        if c['l'] < recent_low and c['c'] > recent_low:  # swept below then closed above
            if _gr(c):
                score += 3; patterns.append('SWEEP')

    # 25. Change of Character (CHoCH)
    if n >= 10:
        # Downtrend: lower highs. Then first higher high = CHoCH
        highs = [x['h'] for x in cl[-10:]]
        is_downtrend = all(highs[i] <= highs[i-1] * 1.001 for i in range(1, len(highs)-1))
        if is_downtrend and cl[-1]['h'] > cl[-2]['h'] and _gr(cl[-1]):
            score += 3; patterns.append('CHOCH')

    # ═══ CONTEXT BONUSES ═══

    # 26. SMA20 Bounce
    if n >= 20:
        sma20 = sum(x['c'] for x in cl[-20:]) / 20
        if abs(c['c'] - sma20) / sma20 * 100 < 0.5 and _gr(c):
            score += 2; patterns.append('SMA')

    # 27. Squeeze Breakout
    if n >= 6:
        rng5 = max(x['h'] for x in cl[-6:-1]) - min(x['l'] for x in cl[-6:-1])
        rng5_pct = rng5 / c['c'] * 100
        if rng5_pct < 0.8 and rng / c['c'] * 100 > rng5_pct * 0.5 and _gr(c):
            score += 2; patterns.append('SQUEEZE')

    # 28. RSI Oversold Recovery
    if n >= 15:
        closes = [x['c'] for x in cl[-15:]]
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d for d in deltas if d > 0]
        losses_r = [-d for d in deltas if d < 0]
        avg_g = sum(gains) / 14 if gains else 0.001
        avg_l = sum(losses_r) / 14 if losses_r else 0.001
        rsi = 100 - 100 / (1 + avg_g / avg_l)
        if rsi < 40 and _gr(c):
            score += 2; patterns.append('RSIREC')

    # 29. Mean Reversion
    if n >= 4 and sum(1 for x in cl[-4:-1] if _rd(x)) >= 3 and _gr(c):
        score += 2; patterns.append('MEANREV')

    # 30. Volume Confirmation
    if n >= 2 and c.get('v', 0) > p.get('v', 0) * 1.2 and _gr(c):
        score += 1; patterns.append('VOLCONF')

    # 31. Near Support
    if n >= 20:
        low20 = min(x['l'] for x in cl[-20:])
        if (c['c'] - low20) / c['c'] * 100 < 1.5:
            score += 1; patterns.append('NEARSUP')

    # 32. Momentum
    if n >= 6:
        move = (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100
        if move >= 1.0:
            score += 2; patterns.append('MOM')

    # 33. Volume Breakout
    if n >= 10 and c.get('v', 0) > 0:
        avg_vol = sum(x.get('v', 0) for x in cl[-10:-1]) / 9
        if avg_vol > 0 and c['v'] > avg_vol * 2.5 and _gr(c) and bs > avg_body * 1.5:
            score += 2; patterns.append('VOLBRK')

    # 34. Trend Alignment (above EMA21)
    if n >= 21:
        ema = sum(x['c'] for x in cl[-21:]) / 21
        if c['c'] > ema:
            score += 1; patterns.append('TREND')

    # 35. Candle Body Strength
    if rng > 0 and bs / rng > 0.6 and _gr(c):
        score += 1; patterns.append('STRONG')

    # ── FILTER: spread too wide ──
    if cl[-1].get('spread', 0) > 0.2:
        score = max(0, score - 5)

    pattern_name = '+'.join(patterns) if patterns else 'NONE'
    return score, pattern_name


def get_dynamic_size(score, regime):
    if regime == 'BEAR':
        return SIZE_BEAR
    if score >= 10:
        return SIZE_HIGH
    if score >= 8:
        return SIZE_MED
    return SIZE_LOW


def check_exits(td):
    regime = detect_regime()
    for pair in list(positions.keys()):
        pos = positions[pair]
        info = td.get(pair, {})
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        if px <= 0 or bid <= 0: continue

        if px > pos['peak']: pos['peak'] = px
        pnl_pct = (px - pos['entry']) / pos['entry']

        cl = list(candles.get(pair, []))
        pos['candle_count'] = len(cl) - pos.get('entry_candle_idx', len(cl))

        # ATR for dynamic stops
        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
        else:
            atr = pos['entry'] * 0.01

        sell = False
        reason = ''

        # Min hold: only hard stop
        if pos['candle_count'] < MIN_HOLD_CANDLES:
            hard = pos['entry'] - atr * 1.5
            if px <= hard:
                sell = True; reason = 'HARD_STOP'
            else:
                continue

        # 1. ATR Stop — use chart stop if available (more precise), otherwise ATR
        if not sell:
            if pos.get('chart_stop', 0) > 0:
                stop_level = pos['chart_stop']  # pattern-defined stop (e.g. below double bottom)
            elif regime == 'BEAR':
                stop_level = pos['entry'] - atr * 1.0
            else:
                stop_level = pos['entry'] - atr * 1.2
            if px <= stop_level:
                sell = True; reason = 'ATR_STOP'

        # 2. Chart pattern target hit — measured move complete
        if not sell and pos.get('chart_target', 0) > 0:
            if px >= pos['chart_target']:
                sell = True; reason = 'CHART_TARGET'

        # 3. Profit trail — once up 1%+, trail from peak
        if not sell and pnl_pct > 0.01:
            trail = pos['peak'] * (1 - PROFIT_TRAIL_PCT)
            if px <= trail:
                sell = True; reason = 'PROFIT_TRAIL'

        # 3. Trailing stop update
        if not sell and pnl_pct > 0.003:
            new_stop = pos['peak'] * (1 - TRAIL_STOP_PCT)
            if new_stop > pos.get('stop', 0):
                pos['stop'] = new_stop

        # 4. Dynamic stop
        if not sell and pos.get('stop', 0) > 0 and px <= pos['stop']:
            sell = True; reason = 'TRAIL'

        # 5. Bearish chart pattern detected — exit if in profit
        if not sell and pnl_pct > 0.002:
            bear_charts = [s for s in scan_chart_patterns(pair) if s[1] < 0]
            if bear_charts:
                sell = True; reason = 'BEAR_CHART'

        # 6. Partial exit at +1%
        if not sell and pnl_pct > 0.01 and not pos.get('partial_done'):
            # Sell 50%
            half_qty = pos['qty'] * 0.5
            exinfo = get_exinfo()
            pi = exinfo.get(pair, {})
            pp = int(pi.get('PricePrecision', 4))
            ap = int(pi.get('AmountPrecision', 2))
            sell_qty = math.floor(half_qty * 10**ap) / 10**ap
            if sell_qty > 0:
                try:
                    order = client.place_order(pair, 'SELL', 'MARKET', sell_qty, round(bid, pp))
                    det = order.get('OrderDetail', order)
                    exit_px = float(det.get('FilledAverPrice', 0) or bid)
                    pnl_usd = (exit_px - pos['entry']) * sell_qty
                    fee = pos['entry'] * sell_qty * 0.001 + exit_px * sell_qty * 0.001
                    pnl_usd -= fee
                    trade_history.append({'pair': pair, 'pnl': pnl_usd, 'reason': 'PARTIAL'})
                    alert(f'<b>PARTIAL {pair}</b> +${pnl_usd:+,.0f} ({pnl_pct*100:+.1f}%)')
                    pos['qty'] -= sell_qty
                    pos['partial_done'] = True
                except Exception as e:
                    log.info(f'Partial sell {pair} failed: {e}')
            continue

        # 6. Max hold
        if not sell and pos['candle_count'] > MAX_HOLD_CANDLES:
            sell = True; reason = 'MAX_TIME'

        if sell:
            exinfo = get_exinfo()
            pi = exinfo.get(pair, {})
            pp = int(pi.get('PricePrecision', 4))
            try:
                order = client.place_order(pair, 'SELL', 'MARKET', pos['qty'], round(bid, pp))
                det = order.get('OrderDetail', order)
                exit_px = float(det.get('FilledAverPrice', 0) or bid)
            except:
                try:
                    order = client.place_order(pair, 'SELL', 'LIMIT', pos['qty'], round(bid, pp))
                    det = order.get('OrderDetail', order)
                    exit_px = float(det.get('FilledAverPrice', 0) or bid)
                except:
                    exit_px = bid

            pnl_usd = (exit_px - pos['entry']) * pos['qty']
            fee = pos['entry'] * pos['qty'] * 0.001 + exit_px * pos['qty'] * 0.001
            pnl_usd -= fee

            trade_history.append({'pair': pair, 'pnl': pnl_usd, 'reason': reason})

            marker = 'WIN' if pnl_usd > 0 else 'LOSS'
            alert(
                f'<b>NAKED {reason} {pair}</b>\n'
                f'P&L: ${pnl_usd:+,.2f} ({(exit_px-pos["entry"])/pos["entry"]*100:+.2f}%)\n'
                f'Entry: ${pos["entry"]:.4f} Exit: ${exit_px:.4f}\n'
                f'Pattern: {pos.get("pattern", "?")} | Held {pos["candle_count"]}h [{marker}]'
            )

            cooldowns[pair] = time.time() + COOLDOWN_SECONDS
            del positions[pair]


def scan_chart_patterns(pair):
    """Scan for chart patterns using the encyclopedia detector.
    Returns list of (pattern_name, score, entry, stop, target)."""
    import pandas as pd
    cl = list(candles.get(pair, []))
    if len(cl) < 30:
        return []

    # Build DataFrame for the detector
    df = pd.DataFrame(cl[-80:] if len(cl) > 80 else cl)
    if len(df) < 30:
        return []

    # Rename columns to match detector expectations
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    if 'volume' not in df.columns:
        df['volume'] = 1.0

    signals = []
    try:
        from pattern_encyclopedia import ChartPatternDetector
        det = ChartPatternDetector(df)

        # BULLISH CHART PATTERNS — each gives entry/stop/target
        bullish_methods = [
            ('detect_bull_flag', 'BULL_FLAG', 5),
            ('detect_ascending_triangle', 'ASC_TRI', 5),
            ('detect_symmetrical_triangle', 'SYM_TRI', 4),
            ('detect_double_bottom', 'DBL_BOT', 4),
            ('detect_inverse_head_shoulders', 'INV_H&S', 5),
            ('detect_falling_wedge', 'FALL_WEDGE', 4),
            ('detect_cup_and_handle', 'CUP_HANDLE', 5),
            ('detect_rectangle_channel', 'RECT_CHAN', 4),
            ('detect_rounding_bottom', 'ROUND_BOT', 4),
            ('detect_pennant', 'PENNANT', 4),
            ('detect_triple_bottom', 'TRIPLE_BOT', 5),
            ('detect_v_bottom', 'V_BOTTOM', 3),
            ('detect_island_reversal', 'ISLAND', 4),
            ('detect_measured_move', 'MEAS_MOVE', 4),
        ]

        for method_name, pat_name, pat_score in bullish_methods:
            try:
                result = getattr(det, method_name)()
                if not result:
                    continue
                if isinstance(result, list):
                    for r in result:
                        if not isinstance(r, dict):
                            continue
                        # Only bullish patterns
                        pat_type = r.get('pattern', '').lower()
                        if 'bear' in pat_type or 'top' in pat_type:
                            continue
                        entry = r.get('entry', 0)
                        stop = r.get('stop', 0)
                        target = r.get('target', 0)
                        if entry > 0 and stop > 0 and target > 0 and target > entry and stop < entry:
                            risk = (entry - stop) / entry * 100
                            reward = (target - entry) / entry * 100
                            rr = reward / risk if risk > 0 else 0
                            if risk <= 3.0 and rr >= 1.5:
                                signals.append((pat_name, pat_score, entry, stop, target, rr))
                elif isinstance(result, dict):
                    pat_type = result.get('pattern', '').lower()
                    if 'bear' in pat_type or 'top' in pat_type:
                        continue
                    entry = result.get('entry', 0)
                    stop = result.get('stop', 0)
                    target = result.get('target', 0)
                    if entry > 0 and stop > 0 and target > 0 and target > entry and stop < entry:
                        risk = (entry - stop) / entry * 100
                        reward = (target - entry) / entry * 100
                        rr = reward / risk if risk > 0 else 0
                        if risk <= 3.0 and rr >= 1.5:
                            signals.append((pat_name, pat_score, entry, stop, target, rr))
            except Exception:
                pass

        # BEARISH CHART PATTERNS — for exit signals
        bearish_methods = [
            'detect_bear_flag', 'detect_descending_triangle', 'detect_double_top',
            'detect_head_and_shoulders', 'detect_rising_wedge', 'detect_rounding_top',
            'detect_triple_top', 'detect_v_top', 'detect_inverted_cup_and_handle',
        ]
        for method_name in bearish_methods:
            try:
                result = getattr(det, method_name)()
                if result:
                    if isinstance(result, list) and result:
                        r = result[0]
                    elif isinstance(result, dict):
                        r = result
                    else:
                        continue
                    pat_type = r.get('pattern', '').lower()
                    if 'bear' in pat_type or 'top' in pat_type:
                        signals.append(('BEAR_CHART_' + method_name.replace('detect_', '').upper(), -5, 0, 0, 0, 0))
            except Exception:
                pass

    except Exception as e:
        pass

    return signals


def check_entries(td):
    regime = detect_regime()

    if regime == 'BULL':
        max_pos = MAX_POSITIONS
        min_score = MIN_PATTERN_SCORE
    else:
        max_pos = 1
        min_score = 8

    if len(positions) >= max_pos:
        return

    available = get_cash()
    if available < MIN_CASH_RESERVE:
        return

    candidates = []
    for pair, info in td.items():
        if pair in EXCLUDED or pair in positions:
            continue
        if pair in cooldowns and time.time() < cooldowns[pair]:
            continue

        # CANDLESTICK score
        candle_score, candle_pattern = detect_patterns(pair)

        # CHART PATTERN scan (runs less frequently — only if candles exist)
        chart_signals = scan_chart_patterns(pair)
        chart_bullish = [s for s in chart_signals if s[1] > 0]
        chart_bearish = [s for s in chart_signals if s[1] < 0]

        # If bearish chart pattern detected, skip entry entirely
        if chart_bearish:
            continue

        # Best chart pattern
        best_chart = None
        chart_score = 0
        if chart_bullish:
            chart_bullish.sort(key=lambda x: -x[5])  # sort by R:R
            best_chart = chart_bullish[0]
            chart_score = best_chart[1]  # pattern score (4-5)

        # Combined score: candlestick + chart pattern bonus
        total_score = candle_score + chart_score

        if total_score >= min_score:
            spread = float(info.get('MinAsk', 0)) - float(info.get('MaxBid', 0))
            bid = float(info.get('MaxBid', 0))
            spread_pct = spread / bid * 100 if bid > 0 else 99
            if spread_pct < 0.2:
                # Build pattern name
                pat_parts = []
                if candle_pattern and candle_pattern != 'NONE':
                    pat_parts.append(candle_pattern)
                if best_chart:
                    pat_parts.append(f'CHART:{best_chart[0]}')
                pattern = '+'.join(pat_parts) if pat_parts else 'NONE'

                candidates.append((total_score, pair, info, pattern, best_chart))

    candidates.sort(key=lambda x: -x[0])

    for total_score, pair, info, pattern, best_chart in candidates[:1]:
        if len(positions) >= max_pos:
            break
        if pair in positions:
            continue

        ask = float(info.get('MinAsk', 0))
        if ask <= 0: continue

        # Volume spike filter
        cl = list(candles.get(pair, []))
        if len(cl) >= 20:
            vols = [x.get('v', 0) for x in cl[-20:-1]]
            avg_vol = sum(vols) / len(vols) if vols else 0
            if avg_vol > 0 and cl[-1].get('v', 0) > avg_vol * 2.0:
                continue

        # Trend filter
        if len(cl) >= 10:
            trend = (cl[-1]['c'] - cl[-10]['c']) / cl[-10]['c'] * 100
            if trend > 2.0 or trend < -3.0:
                continue

        # 4H trend filter
        if len(cl) >= 24:
            ema_4h = sum(x['c'] for x in cl[-24:]) / 24
            ema_4h_prev = sum(x['c'] for x in cl[-28:-4]) / 24 if len(cl) >= 28 else ema_4h
            if ema_4h < ema_4h_prev:
                continue

        # ATR filter
        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
            atr_pct = atr / cl[-1]['c'] * 100
            if atr_pct < 0.3:
                continue

        # Doji filter
        if _rng(cl[-1]) > 0 and _bs(cl[-1]) / _rng(cl[-1]) < 0.1:
            continue

        # Cash check
        available = get_cash()
        size = get_dynamic_size(total_score, regime)
        if available < MIN_CASH_RESERVE + size:
            break
        size = min(size, (available - MIN_CASH_RESERVE) * 0.25)
        if size < 50000:
            break

        exinfo = get_exinfo()
        pi = exinfo.get(pair, {})
        pp = int(pi.get('PricePrecision', 4))
        ap = int(pi.get('AmountPrecision', 2))
        qty = math.floor(size / ask * 10**ap) / 10**ap
        if qty <= 0: continue

        try:
            order = client.place_order(pair, 'BUY', 'MARKET', qty, round(ask, pp))
            det = order.get('OrderDetail', order)
            status = (det.get('Status') or '').upper()
            filled = float(det.get('FilledQuantity', 0) or 0)
            fill_px = float(det.get('FilledAverPrice', 0) or ask)

            if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
                continue

            fill_qty = filled or qty
            cl_now = list(candles.get(pair, []))

            # If chart pattern provided entry/stop/target, use those
            chart_stop = 0
            chart_target = 0
            if best_chart:
                _, _, chart_entry, chart_stop, chart_target, chart_rr = best_chart

            positions[pair] = {
                'entry': fill_px, 'qty': fill_qty,
                'peak': fill_px,
                'stop': chart_stop if chart_stop > 0 else 0,
                'time': time.time(),
                'pattern': pattern, 'score': total_score,
                'entry_candle_idx': len(cl_now),
                'candle_count': 0,
                'partial_done': False,
                'chart_target': chart_target,
                'chart_stop': chart_stop,
            }

            target_str = f' | Target: ${chart_target:.4f}' if chart_target > 0 else ''
            alert(
                f'<b>🎯 NAKED BUY {pair}</b>\n'
                f'Pattern: {pattern} (score={total_score})\n'
                f'Price: ${fill_px:.4f} | Size: ${fill_qty*fill_px:,.0f}\n'
                f'Regime: {regime} | Cash: ${available-fill_qty*fill_px:,.0f}{target_str}'
            )

        except Exception as e:
            log.info(f'Buy {pair} failed: {e}')


def save_state():
    try:
        state = {
            'positions': {k: {kk: vv for kk, vv in v.items()} for k, v in positions.items()},
            'trade_history': trade_history[-50:],
            'cooldowns': {k: v for k, v in cooldowns.items() if v > time.time()},
        }
        with open('data/naked_state.json', 'w') as f:
            json.dump(state, f)
    except: pass


def main():
    log.info('=' * 60)
    log.info('NAKED TRADER V7 ULTIMATE — 35 Patterns')
    log.info(f'Score >= {MIN_PATTERN_SCORE} | Max {MAX_POSITIONS} pos | Dynamic size ${SIZE_LOW/1000:.0f}-{SIZE_HIGH/1000:.0f}k')
    log.info(f'ATR stop | {PROFIT_TRAIL_PCT*100:.0f}% trail | Partial at +1% | {MAX_HOLD_CANDLES}h hold')
    log.info(f'Regime: BULL=4pos BEAR=1pos half size')
    log.info('=' * 60)

    alert(
        '<b>🎯 NAKED TRADER V7 ULTIMATE ONLINE</b>\n'
        '35 patterns | Dynamic sizing | Regime adaptive\n'
        f'BULL: {MAX_POSITIONS} pos, ${SIZE_LOW/1000:.0f}-{SIZE_HIGH/1000:.0f}k\n'
        f'BEAR: 1 pos, ${SIZE_BEAR/1000:.0f}k, score >= 8\n'
        'ATR stops | 1% trail | Partial exits'
    )

    # Bootstrap from Binance
    log.info('Bootstrapping candle data from Binance...')
    COIN_TO_BINANCE = {
        'SOL/USD':'SOLUSDT','BNB/USD':'BNBUSDT','AVAX/USD':'AVAXUSDT',
        'LINK/USD':'LINKUSDT','FET/USD':'FETUSDT','SUI/USD':'SUIUSDT',
        'NEAR/USD':'NEARUSDT','PENDLE/USD':'PENDLEUSDT','ADA/USD':'ADAUSDT',
        'DOT/USD':'DOTUSDT','UNI/USD':'UNIUSDT','HBAR/USD':'HBARUSDT',
        'AAVE/USD':'AAVEUSDT','CAKE/USD':'CAKEUSDT','DOGE/USD':'DOGEUSDT',
        'FIL/USD':'FILUSDT','LTC/USD':'LTCUSDT','SEI/USD':'SEIUSDT',
        'ARB/USD':'ARBUSDT','ENA/USD':'ENAUSDT','ONDO/USD':'ONDOUSDT',
        'CRV/USD':'CRVUSDT','XLM/USD':'XLMUSDT','TRX/USD':'TRXUSDT',
        'CFX/USD':'CFXUSDT','APT/USD':'APTUSDT','ICP/USD':'ICPUSDT',
        'BTC/USD':'BTCUSDT',  # for regime detection only
    }
    bootstrapped = 0
    for pair, symbol in COIN_TO_BINANCE.items():
        try:
            url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=50'
            r = requests.get(url, timeout=5)
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                candles[pair] = deque(maxlen=200)
                for k in data[:-1]:
                    candles[pair].append({
                        'o': float(k[1]), 'h': float(k[2]),
                        'l': float(k[3]), 'c': float(k[4]),
                        'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
                bootstrapped += 1
        except: pass
        time.sleep(0.1)
    log.info(f'Bootstrapped {bootstrapped} coins — ready')

    # Orphan detection
    log.info('Checking for orphaned positions...')
    try:
        bal = client.get_balance()
        bal_data = bal.get('SpotWallet', bal.get('Data', bal))
        if isinstance(bal_data, dict):
            orphans = []
            for coin, info in bal_data.items():
                if coin in ('USD', 'USDT', 'Success', 'ErrMsg', 'SpotWallet'): continue
                qty = 0
                if isinstance(info, dict):
                    qty = float(info.get('Free', 0)) + float(info.get('Locked', 0))
                elif isinstance(info, (int, float)):
                    qty = float(info)
                if qty > 0:
                    pair = f'{coin}/USD'
                    orphans.append((pair, qty))
            if orphans:
                msg = ['<b>ORPHANED POSITIONS:</b>']
                for pair, qty in orphans:
                    if pair not in positions:
                        try:
                            td_check = client.get_ticker().get('Data', {})
                            px = float(td_check.get(pair, {}).get('LastPrice', 0))
                            if px > 0:
                                cl_len = len(candles.get(pair, []))
                                positions[pair] = {
                                    'entry': px, 'qty': qty, 'peak': px,
                                    'stop': 0, 'time': time.time(),
                                    'pattern': 'ORPHAN', 'score': 0,
                                    'entry_candle_idx': cl_len, 'candle_count': 0,
                                    'partial_done': False,
                                }
                                msg.append(f'  {pair}: adopted at ${px:.4f}')
                        except:
                            msg.append(f'  {pair}: FAILED to adopt')
                alert('\n'.join(msg))
    except Exception as e:
        log.info(f'Orphan check: {e}')

    # Lock
    import fcntl, sys
    lock = open('/tmp/naked_trader.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print('Already running!'); sys.exit(1)

    tick = 0
    consec_losses = 0
    while True:
        try:
            td = client.get_ticker().get('Data', {})
            if not td:
                time.sleep(TICK_INTERVAL); continue

            tick += 1
            update_candles(td)

            if positions:
                check_exits(td)

            # Loss pause: 3 consecutive losses = skip 3 candles
            recent = trade_history[-3:] if len(trade_history) >= 3 else []
            if len(recent) == 3 and all(t['pnl'] < 0 for t in recent):
                if tick % 30 == 0:
                    log.info('PAUSED: 3 consecutive losses, waiting for reset')
            else:
                check_entries(td)

            if tick % 12 == 0:
                save_state()

            if tick % 30 == 0:
                total_pnl = sum(t['pnl'] for t in trade_history)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history)
                wr = wins / n * 100 if n > 0 else 0
                regime = detect_regime()
                cash = get_cash()

                pos_str = ''
                if positions:
                    parts = []
                    for p in positions:
                        ppx = float(td.get(p, {}).get('LastPrice', 0))
                        if ppx > 0:
                            pnl = (ppx - positions[p]['entry']) / positions[p]['entry'] * 100
                            parts.append(f'{p.split("/")[0]}({pnl:+.2f}%)')
                    pos_str = ' | ' + ', '.join(parts)

                log.info(
                    f'${cash:,.0f}{pos_str} | '
                    f'{n} trades ({wins}W {wr:.0f}%) | P&L=${total_pnl:+,.0f} | '
                    f'Regime={regime}'
                )

        except Exception as e:
            log.info(f'Error: {e}')

        time.sleep(TICK_INTERVAL)


if __name__ == '__main__':
    main()
