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
EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD'}  # skip gold + meme coins (wide spreads, whipsaw)

# ── Config ──
TICK_INTERVAL = 10
CANDLE_SECONDS = 3600       # 1h candles
MAX_POSITIONS = 4
HARD_STOP_PCT = 0.015  # 1.5% — 0.5% was noise on 1H candles, caused 90% of losses
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
#  SMC ANALYSIS ENGINE — 16 techniques (from smc_trader.py)
#  Replaces broken pattern detection with correct implementations
# ══════════════════════════════════════════════════════

def _b(c): return c['c'] - c['o']
def _bs(c): return abs(_b(c))
def _gr(c): return c['c'] > c['o']
def _rd(c): return c['c'] < c['o']
def _uw(c): return c['h'] - max(c['o'], c['c'])
def _lw(c): return min(c['o'], c['c']) - c['l']
def _rng(c): return c['h'] - c['l']


# ── SMC 1: Fair Value Gaps (correct implementation) ──
def find_fvg(cl):
    gaps = []
    for i in range(2, len(cl)):
        if cl[i-2]['h'] < cl[i]['l']:
            gaps.append({'type':'bull','top':cl[i]['l'],'bottom':cl[i-2]['h'],'idx':i,'filled':False})
        if cl[i-2]['l'] > cl[i]['h']:
            gaps.append({'type':'bear','top':cl[i-2]['l'],'bottom':cl[i]['h'],'idx':i,'filled':False})
    for g in gaps:
        for j in range(g['idx']+1, len(cl)):
            if g['type']=='bull' and cl[j]['l']<=g['bottom']: g['filled']=True; break
            if g['type']=='bear' and cl[j]['h']>=g['top']: g['filled']=True; break
    return [g for g in gaps if not g['filled']]


# ── SMC 2: Order Blocks (1%+ move, correct) ──
def find_order_blocks(cl):
    obs = []
    for i in range(1, len(cl)-1):
        move = (cl[i+1]['c']-cl[i+1]['o'])/cl[i]['c']*100 if cl[i]['c']>0 else 0
        if _rd(cl[i]) and move > 1.0:
            obs.append({'type':'bull','top':cl[i]['o'],'bottom':cl[i]['c'],'idx':i,'mitigated':False})
        if _gr(cl[i]) and move < -1.0:
            obs.append({'type':'bear','top':cl[i]['c'],'bottom':cl[i]['o'],'idx':i,'mitigated':False})
    for ob in obs:
        for j in range(ob['idx']+2, len(cl)):
            if ob['type']=='bull' and cl[j]['l']<=ob['bottom']: ob['mitigated']=True; break
            if ob['type']=='bear' and cl[j]['h']>=ob['top']: ob['mitigated']=True; break
    return [ob for ob in obs if not ob['mitigated']]


# ── SMC 3: Swing Highs and Lows ──
def find_swings(cl, length=5):
    swings = []
    for i in range(length, len(cl)-length):
        is_high = all(cl[i]['h']>=cl[i+j]['h'] and cl[i]['h']>=cl[i-j]['h']
                      for j in range(1,length+1) if i+j<len(cl) and i-j>=0)
        is_low = all(cl[i]['l']<=cl[i+j]['l'] and cl[i]['l']<=cl[i-j]['l']
                     for j in range(1,length+1) if i+j<len(cl) and i-j>=0)
        if is_high: swings.append({'type':'high','price':cl[i]['h'],'idx':i})
        if is_low: swings.append({'type':'low','price':cl[i]['l'],'idx':i})
    return swings


# ── SMC 4: BOS ──
def detect_bos(swings):
    if len(swings)<3: return None
    recent=swings[-3:]
    highs=[s for s in recent if s['type']=='high']
    lows=[s for s in recent if s['type']=='low']
    if len(highs)>=2 and highs[-1]['price']>highs[-2]['price']: return 'bullish_bos'
    if len(lows)>=2 and lows[-1]['price']<lows[-2]['price']: return 'bearish_bos'
    return None


# ── SMC 5: CHoCH (uses swing highs, not all highs) ──
def detect_choch(swings):
    if len(swings)<4: return None
    highs=[s for s in swings if s['type']=='high']
    if len(highs)>=3:
        if highs[-3]['price']>highs[-2]['price'] and highs[-1]['price']>highs[-2]['price']:
            return 'bullish_choch'
        if highs[-3]['price']<highs[-2]['price'] and highs[-1]['price']<highs[-2]['price']:
            return 'bearish_choch'
    return None


# ── SMC 6: Liquidity Sweep ──
def detect_liquidity_sweep(cl, swings):
    if len(cl)<3 or len(swings)<2: return None
    current=cl[-1]; prev=cl[-2]
    recent_lows=[s for s in swings if s['type']=='low' and s['idx']<len(cl)-2]
    if not recent_lows: return None
    last_low=recent_lows[-1]
    if prev['l']<last_low['price'] and current['c']>last_low['price'] and _gr(current):
        return {'type':'bull_sweep','level':last_low['price']}
    return None


# ── SMC 7: Market Structure ──
def analyze_structure(swings):
    if len(swings)<4: return 'unknown'
    highs=[s for s in swings if s['type']=='high'][-3:]
    lows=[s for s in swings if s['type']=='low'][-3:]
    hh=len(highs)>=2 and highs[-1]['price']>highs[-2]['price']
    hl=len(lows)>=2 and lows[-1]['price']>lows[-2]['price']
    lh=len(highs)>=2 and highs[-1]['price']<highs[-2]['price']
    ll=len(lows)>=2 and lows[-1]['price']<lows[-2]['price']
    if hh and hl: return 'bullish'
    if lh and ll: return 'bearish'
    return 'ranging'


# ── SMC 8: Fibonacci ──
def fib_analysis(cl, swings):
    if len(cl)<10 or len(swings)<2: return None, 999
    highs=[s for s in swings if s['type']=='high']
    lows=[s for s in swings if s['type']=='low']
    if not highs or not lows: return None, 999
    rh=max(s['price'] for s in highs[-3:]); rl=min(s['price'] for s in lows[-3:])
    if rh<=rl: return None, 999
    diff=rh-rl; current=cl[-1]['c']
    fibs={0.236:rl+diff*0.236, 0.382:rl+diff*0.382, 0.500:rl+diff*0.500, 0.618:rl+diff*0.618, 0.786:rl+diff*0.786}
    nearest=None; ndist=999
    for level,price in fibs.items():
        dist=abs(current-price)/current*100
        if dist<ndist: ndist=dist; nearest=level
    return nearest, ndist


# ── SMC 9: Support/Resistance ──
def find_sr_levels(cl, n=3):
    if len(cl)<15: return [], []
    def cluster(prices, threshold_pct=0.5):
        if not prices: return []
        prices=sorted(prices); clusters=[]; current=[prices[0]]
        for p in prices[1:]:
            if (p-current[0])/current[0]*100<threshold_pct: current.append(p)
            else:
                if len(current)>=2: clusters.append({'level':sum(current)/len(current),'touches':len(current)})
                current=[p]
        if len(current)>=2: clusters.append({'level':sum(current)/len(current),'touches':len(current)})
        return sorted(clusters, key=lambda x:x['touches'], reverse=True)[:n]
    return cluster([c['l'] for c in cl]), cluster([c['h'] for c in cl])


# ── SMC 10: Supply/Demand Zones ──
def find_supply_demand(cl):
    zones=[]
    for i in range(2, len(cl)-2):
        move=(cl[i+1]['c']-cl[i]['c'])/cl[i]['c']*100 if cl[i]['c']>0 else 0
        if move>1.5:
            zones.append({'type':'demand','top':max(cl[i]['o'],cl[i]['c']),'bottom':min(cl[i]['o'],cl[i]['c']),'idx':i})
        if move<-1.5:
            zones.append({'type':'supply','top':max(cl[i]['o'],cl[i]['c']),'bottom':min(cl[i]['o'],cl[i]['c']),'idx':i})
    fresh=[]
    for z in zones:
        revisited=False
        for j in range(z['idx']+2, len(cl)):
            if z['type']=='demand' and cl[j]['l']<=z['bottom']: revisited=True; break
            if z['type']=='supply' and cl[j]['h']>=z['top']: revisited=True; break
        if not revisited: fresh.append(z)
    return fresh


# ── SMC 11: Wyckoff ──
def detect_wyckoff(cl):
    if len(cl)<20: return None
    window=cl[-15:-2]
    h=max(c['h'] for c in window); l=min(c['l'] for c in window)
    range_pct=(h-l)/l*100 if l>0 else 99
    if range_pct>4.0: return None
    if cl[-2]['l']<l and cl[-1]['c']>l and _gr(cl[-1]):
        return {'type':'spring','range_low':l,'range_high':h}
    if cl[-1]['c']>h and _gr(cl[-1]) and _bs(cl[-1])>_rng(cl[-1])*0.5:
        return {'type':'markup','range_high':h}
    return None


# ── EMA helper ──
def calc_ema(values, period):
    if len(values)<period: return values[-1] if values else 0
    k=2/(period+1); ema=values[0]
    for v in values[1:]: ema=v*k+ema*(1-k)
    return ema


# ── ADX for regime ──
def calc_adx(cl, period=14):
    if len(cl)<period*2: return 0
    plus_dm=[]; minus_dm=[]; tr_list=[]
    for i in range(1,len(cl)):
        hd=cl[i]['h']-cl[i-1]['h']; ld=cl[i-1]['l']-cl[i]['l']
        plus_dm.append(hd if hd>ld and hd>0 else 0)
        minus_dm.append(ld if ld>hd and ld>0 else 0)
        tr_list.append(max(cl[i]['h']-cl[i]['l'],abs(cl[i]['h']-cl[i-1]['c']),abs(cl[i]['l']-cl[i-1]['c'])))
    if len(tr_list)<period: return 0
    atr=sum(tr_list[:period])/period
    pds=sum(plus_dm[:period])/period; mds=sum(minus_dm[:period])/period
    dx_list=[]
    for i in range(period,len(tr_list)):
        atr=(atr*(period-1)+tr_list[i])/period
        pds=(pds*(period-1)+plus_dm[i])/period; mds=(mds*(period-1)+minus_dm[i])/period
        if atr==0: continue
        pdi=(pds/atr)*100; mdi=(mds/atr)*100; di_sum=pdi+mdi
        if di_sum==0: continue
        dx_list.append(abs(pdi-mdi)/di_sum*100)
    if len(dx_list)<period: return 0
    adx=sum(dx_list[:period])/period
    for i in range(period,len(dx_list)): adx=(adx*(period-1)+dx_list[i])/period
    return adx


# ══════════════════════════════════════════════════════
#  REGIME DETECTION — EMA slope + ADX + Breadth
# ══════════════════════════════════════════════════════

def detect_regime(pair='BTC/USD'):
    """BTC EMA50 slope + ADX for trend strength + breadth."""
    cl = list(candles.get(pair, []))
    if len(cl) < 60: return 'CHOP'  # don't trade until we have enough data

    closes = [c['c'] for c in cl]
    ema50_now = calc_ema(closes, 50)
    ema50_prev = calc_ema(closes[:-5], 50)
    slope_pct = (ema50_now - ema50_prev) / ema50_prev * 100 if ema50_prev > 0 else 0
    adx = calc_adx(cl, 14)

    if slope_pct > 0.1 and adx >= 20:
        return 'BULL'
    elif slope_pct < -0.1 and adx >= 20:
        return 'BEAR'
    else:
        return 'CHOP'


# ══════════════════════════════════════════════════════
#  UNIFIED SCORER — SMC + Candlestick combined
# ══════════════════════════════════════════════════════

def detect_patterns(pair):
    """Combined SMC + candlestick scoring. Returns (score, pattern_name)."""
    cl = list(candles.get(pair, []))
    if len(cl) < 10: return 0, ''

    score = 0
    patterns = []
    c = cl[-1]; p = cl[-2] if len(cl) >= 2 else c
    n = len(cl)
    current = c['c']

    # Averages
    bodies = [_bs(x) for x in cl[-14:]]
    ranges_list = [_rng(x) for x in cl[-14:]]
    avg_body = sum(bodies) / len(bodies) if bodies else 0.0001
    avg_range = sum(ranges_list) / len(ranges_list) if ranges_list else 0.0001
    if avg_body == 0: avg_body = 0.0001
    if avg_range == 0: avg_range = 0.0001
    bs = _bs(c); rng = _rng(c); bs_p = _bs(p)

    # ═══ CANDLESTICK PATTERNS (proven 10 from V7) ═══
    if _rd(p) and _gr(c) and c['o']<=p['c'] and c['c']>=p['o'] and bs>bs_p*1.2:
        score+=3; patterns.append('ENGULF')
    if rng>0 and bs>0 and _lw(c)>=bs*2 and _uw(c)<=bs*0.5 and _gr(c):
        score+=3; patterns.append('HAMMER')
    if n>=3:
        b1,b2,b3=_bs(cl[-3]),_bs(cl[-2]),_bs(cl[-1])
        if _rd(cl[-3]) and b1>avg_body and b2<b1*0.3 and _gr(cl[-1]) and b3>avg_body and cl[-1]['c']>(cl[-3]['o']+cl[-3]['c'])/2:
            score+=4; patterns.append('MSTAR')
    if _rd(p) and _gr(c):
        mid=(p['o']+p['c'])/2
        if c['o']<p['c'] and c['c']>mid and c['c']<p['o']: score+=3; patterns.append('PIERCE')
    if n>=2 and avg_range>0 and abs(c['l']-p['l'])/avg_range<0.05 and _rd(p) and _gr(c):
        score+=3; patterns.append('TWZR')
    if n>=3 and _gr(cl[-3]) and _gr(cl[-2]) and _gr(cl[-1]) and cl[-1]['c']>cl[-2]['c']>cl[-3]['c']:
        score+=3; patterns.append('3WS')
    if n>=3 and cl[-2]['h']<=cl[-3]['h'] and cl[-2]['l']>=cl[-3]['l'] and cl[-1]['c']>cl[-3]['h'] and _gr(cl[-1]):
        score+=3; patterns.append('INSIDE')
    if n>=5 and cl[-2]['l']>cl[-4]['l'] and cl[-1]['h']>cl[-3]['h'] and _gr(cl[-1]):
        score+=2; patterns.append('HHHL')
    if _gr(c) and bs>avg_body*2 and _uw(c)<bs*0.1 and _lw(c)<bs*0.1:
        score+=3; patterns.append('MARU')
    if n>=6 and (cl[-1]['c']-cl[-6]['c'])/cl[-6]['c']*100>=1.0:
        score+=2; patterns.append('MOM')

    # ═══ SMC TECHNIQUES (correct implementations) ═══

    # Swings & Structure
    swings = find_swings(cl, length=3)
    structure = analyze_structure(swings)
    if structure == 'bullish': score+=2; patterns.append('BULL_STRUCT')
    elif structure == 'bearish': score-=3

    # BOS
    bos = detect_bos(swings)
    if bos == 'bullish_bos': score+=2; patterns.append('BOS')

    # CHoCH
    choch = detect_choch(swings)
    if choch == 'bullish_choch': score+=3; patterns.append('CHoCH')

    # FVG — price inside unfilled gap
    fvgs = find_fvg(cl)
    for f in [f for f in fvgs if f['type']=='bull']:
        if f['bottom']<=current<=f['top']: score+=2; patterns.append('IN_FVG'); break
        if current<f['bottom'] and (f['bottom']-current)/current*100<1.0: score+=1; patterns.append('NEAR_FVG'); break

    # Order Blocks — price at institutional entry
    obs = find_order_blocks(cl)
    for ob in [ob for ob in obs if ob['type']=='bull']:
        if ob['bottom']<=current<=ob['top']: score+=3; patterns.append('IN_OB'); break
        if current<ob['top'] and (ob['top']-current)/current*100<1.0: score+=1; patterns.append('NEAR_OB'); break

    # Liquidity Sweep
    sweep = detect_liquidity_sweep(cl, swings)
    if sweep and sweep['type']=='bull_sweep': score+=3; patterns.append('LIQ_SWEEP')

    # Fibonacci
    fib_level, fib_dist = fib_analysis(cl, swings)
    if fib_level in (0.618, 0.786) and fib_dist<1.0: score+=3; patterns.append(f'FIB_{fib_level}')
    elif fib_level==0.500 and fib_dist<1.0: score+=2; patterns.append('FIB_0.5')

    # S/R
    supports, _ = find_sr_levels(cl)
    for s in supports:
        dist=(current-s['level'])/current*100
        if -0.5<dist<=0: score+=3; patterns.append('AT_SUP'); break
        if 0<dist<1.5: score+=2; patterns.append('NEAR_SUP'); break

    # Supply/Demand
    for z in [z for z in find_supply_demand(cl) if z['type']=='demand']:
        if z['bottom']<=current<=z['top']: score+=2; patterns.append('IN_DEMAND'); break

    # Wyckoff
    wyck = detect_wyckoff(cl)
    if wyck:
        if wyck['type']=='spring': score+=3; patterns.append('WYCKOFF_SPRING')
        elif wyck['type']=='markup': score+=2; patterns.append('WYCKOFF_MARKUP')

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

    # Only trade in confirmed bull trends — bear/chop = sit in cash
    if regime != 'BULL':
        return

    max_pos = MAX_POSITIONS
    min_score = MIN_PATTERN_SCORE

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
        'BTC/USD':'BTCUSDT','ETH/USD':'ETHUSDT','SOL/USD':'SOLUSDT','BNB/USD':'BNBUSDT',
        'XRP/USD':'XRPUSDT','AVAX/USD':'AVAXUSDT','LINK/USD':'LINKUSDT','FET/USD':'FETUSDT',
        'TAO/USD':'TAOUSDT','APT/USD':'APTUSDT','SUI/USD':'SUIUSDT','NEAR/USD':'NEARUSDT',
        'WIF/USD':'WIFUSDT','PENDLE/USD':'PENDLEUSDT','ADA/USD':'ADAUSDT','DOT/USD':'DOTUSDT',
        'UNI/USD':'UNIUSDT','HBAR/USD':'HBARUSDT','ARB/USD':'ARBUSDT','EIGEN/USD':'EIGENUSDT',
        'ENA/USD':'ENAUSDT','CAKE/USD':'CAKEUSDT','CFX/USD':'CFXUSDT','CRV/USD':'CRVUSDT',
        'FIL/USD':'FILUSDT','TRUMP/USD':'TRUMPUSDT','ONDO/USD':'ONDOUSDT','WLD/USD':'WLDUSDT',
        'AAVE/USD':'AAVEUSDT','ICP/USD':'ICPUSDT','LTC/USD':'LTCUSDT','XLM/USD':'XLMUSDT',
        'TON/USD':'TONUSDT','TRX/USD':'TRXUSDT','SEI/USD':'SEIUSDT','DOGE/USD':'DOGEUSDT',
        'ZEC/USD':'ZECUSDT','ZEN/USD':'ZENUSDT','POL/USD':'POLUSDT','BIO/USD':'BIOUSDT',
        'BONK/USD':'BONKUSDT','SHIB/USD':'SHIBUSDT','PEPE/USD':'PEPEUSDT','FLOKI/USD':'FLOKIUSDT',
        '1000CHEEMS/USD':'1000CHEEMSUSDT',
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
    log.info(f'Bootstrapped {bootstrapped} coins from Binance')

    # CoinGecko fallback for coins not on Binance
    COINGECKO_IDS = {
        'HEMI/USD': 'hemi',
        'VIRTUAL/USD': 'virtual-protocol',
        'LINEA/USD': 'linea',
        'STO/USD': 'stakestone',
        'PLUME/USD': 'plume',
        'ASTER/USD': 'aster-2',
        'BMT/USD': 'bubblemaps',
        'LISTA/USD': 'lista',
        'MIRA/USD': 'mira-3',
        'PENGU/USD': 'pudgy-penguins',
        'PUMP/USD': 'pump-fun',
        'SOMI/USD': 'somnia',
        'WLFI/USD': 'world-liberty-financial',
        'XPL/USD': 'plasma',
        'S/USD': 'sonic-3',
    }
    # AVNT, FORM, EDEN, OPEN, TUT not on CoinGecko — build from Roostoo ticks
    cg_count = 0
    for pair, cg_id in COINGECKO_IDS.items():
        if pair in candles:
            continue  # already have from Binance
        try:
            import urllib.request, json as jn
            url = f'https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=7'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = jn.loads(resp.read())
            if isinstance(data, list) and len(data) > 10:
                candles[pair] = deque(maxlen=200)
                for d in data[:-1]:
                    if isinstance(d, list) and len(d) >= 5:
                        candles[pair].append({
                            'o': d[1], 'h': d[2], 'l': d[3], 'c': d[4],
                            'v': 1.0, 't': d[0] / 1000,
                        })
                cg_count += 1
        except Exception as e:
            log.info(f'  CoinGecko {pair} ({cg_id}): failed - {e}')
        time.sleep(7)  # CoinGecko free tier: ~10 req/min, need 15 requests
    if cg_count > 0:
        log.info(f'Bootstrapped {cg_count} more coins from CoinGecko')
    log.info(f'Total: {bootstrapped + cg_count} coins ready')

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
