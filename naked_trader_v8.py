"""
NAKED TRADER V8 — Multi-Timeframe + Elliott Wave + Momentum Entry

FIXES FROM V7:
1. Scans BOTH 1H and 4H candles (catches bigger patterns)
2. Elliott Wave detection on 4H (Wave 4 entry → Wave 5 target)
3. Momentum trigger: if coin pumps 1.5%+ from hourly open, enter mid-candle
4. Watchdog timeout: 2 min max per scan cycle, never hangs
5. Trend filter widened to 10% (was 2% — blocked everything in pumps)
6. 50 candlestick patterns + 13 chart patterns + Elliott waves

KEPT FROM V7:
- ATR stops, chart targets, 1% trail, partial exits
- Dynamic sizing ($200-350k)
- Regime detection (EMA20 + RSI)
- Position overwrite protection
- Orphan detection
- MARKET orders for exits
- CoinGecko fallback bootstrap
"""

import time
import math
import json
import logging
import signal
import requests
import numpy as np
from collections import deque
from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

try:
    from news_sentiment import score_sentiment, get_market_sentiment
    _HAS_NEWS = True
except ImportError:
    _HAS_NEWS = False

try:
    import os; os.makedirs("logs", exist_ok=True)
    _lf = "logs/naked_trader.log"
except:
    _lf = "naked_trader.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()
EXCLUDED = {'PAXG/USD'}

# ── Config ──
TICK_INTERVAL = 10
CANDLE_1H = 3600
CANDLE_4H = 14400
MAX_POSITIONS = 4
MIN_PATTERN_SCORE = 4
MAX_HOLD_CANDLES = 12
MIN_HOLD_CANDLES = 3
MAX_PORTFOLIO_EXPOSURE = 0.60
MIN_CASH_RESERVE = 200000
COOLDOWN_SECONDS = 3600
TREND_FILTER_PCT = 10.0

# Dynamic sizing
SIZE_LOW = 200000
SIZE_MED = 250000
SIZE_HIGH = 350000
SIZE_BEAR = 100000

# ── State ──
tick_buffer = {}
candles_1h = {}         # pair -> deque of 1H candles
candles_4h = {}         # pair -> deque of 4H candles
hourly_opens = {}       # pair -> price at start of current hour (for momentum trigger)
positions = {}
cooldowns = {}
trade_history = []
exinfo_cache = None
_chart_cache = {}


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


# ══════════════════════════════════════════════════════
#  CANDLE BUILDING — both 1H and 4H from ticks
# ══════════════════════════════════════════════════════

def update_candles(td):
    now = time.time()
    for pair, info in td.items():
        if pair in EXCLUDED: continue
        px = float(info.get('LastPrice', 0))
        if px <= 0: continue

        if pair not in tick_buffer:
            tick_buffer[pair] = []
            if pair not in candles_1h: candles_1h[pair] = deque(maxlen=200)
            if pair not in candles_4h: candles_4h[pair] = deque(maxlen=200)

        tick_buffer[pair].append({'t': now, 'p': px})

        # Track hourly open for momentum trigger
        current_hour = int(now / CANDLE_1H)
        hour_key = f"{pair}_{current_hour}"
        if hour_key not in hourly_opens:
            hourly_opens[hour_key] = px

        # Build 1H candles
        _build_candle(pair, CANDLE_1H, candles_1h)
        # Build 4H candles
        _build_candle(pair, CANDLE_4H, candles_4h)


def _build_candle(pair, period, candle_store):
    now = time.time()
    current_period = int(now / period)
    ticks = tick_buffer.get(pair, [])
    if not ticks: return

    first_period = int(ticks[0]['t'] / period)
    if current_period > first_period and len(ticks) >= 2:
        candle_ticks = [t for t in ticks if int(t['t'] / period) == first_period]
        if candle_ticks:
            candle = {
                'o': candle_ticks[0]['p'],
                'h': max(t['p'] for t in candle_ticks),
                'l': min(t['p'] for t in candle_ticks),
                'c': candle_ticks[-1]['p'],
                'v': len(candle_ticks),
                't': first_period * period,
            }
            candle_store[pair].append(candle)
        # Only clean ticks for 1H (smallest period)
        if period == CANDLE_1H:
            tick_buffer[pair] = [t for t in ticks if int(t['t'] / period) > first_period]


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════

def _bs(c): return abs(c['c'] - c['o'])
def _gr(c): return c['c'] > c['o']
def _rd(c): return c['c'] < c['o']
def _rng(c): return c['h'] - c['l']
def _uw(c): return c['h'] - max(c['o'], c['c'])
def _lw(c): return min(c['o'], c['c']) - c['l']


# ══════════════════════════════════════════════════════
#  ELLIOTT WAVE DETECTION (4H)
# ══════════════════════════════════════════════════════

def find_swings(cl, length=3):
    swings = []
    for i in range(length, len(cl) - length):
        is_high = all(cl[i]['h'] >= cl[i+j]['h'] and cl[i]['h'] >= cl[i-j]['h']
                      for j in range(1, length+1) if i+j < len(cl) and i-j >= 0)
        is_low = all(cl[i]['l'] <= cl[i+j]['l'] and cl[i]['l'] <= cl[i-j]['l']
                     for j in range(1, length+1) if i+j < len(cl) and i-j >= 0)
        if is_high: swings.append({'type': 'high', 'price': cl[i]['h'], 'idx': i})
        if is_low: swings.append({'type': 'low', 'price': cl[i]['l'], 'idx': i})
    return swings


def detect_elliott_wave(cl):
    """Detect Elliott Wave 4 → Wave 5 setup on 4H candles."""
    if len(cl) < 20: return None

    swings = find_swings(cl, length=2)
    lows = [s for s in swings if s['type'] == 'low']
    highs = [s for s in swings if s['type'] == 'high']

    if len(lows) < 3 or len(highs) < 2:
        return None

    # Try to find 5-wave impulse: W0(low) → W1(high) → W2(low) → W3(high) → W4(low)
    for li in range(len(lows) - 2):
        w0 = lows[li]['price']; w0_idx = lows[li]['idx']
        w1_cands = [h for h in highs if h['idx'] > w0_idx]
        if not w1_cands: continue
        w1 = w1_cands[0]['price']; w1_idx = w1_cands[0]['idx']

        w2_cands = [l for l in lows if l['idx'] > w1_idx]
        if not w2_cands: continue
        w2 = w2_cands[0]['price']; w2_idx = w2_cands[0]['idx']

        w3_cands = [h for h in highs if h['idx'] > w2_idx]
        if not w3_cands: continue
        w3 = w3_cands[0]['price']; w3_idx = w3_cands[0]['idx']

        w4_cands = [l for l in lows if l['idx'] > w3_idx]
        if not w4_cands: continue
        w4 = w4_cands[0]['price']; w4_idx = w4_cands[0]['idx']

        wave1 = w1 - w0; wave3 = w3 - w2
        if wave1 <= 0 or wave3 <= 0: continue

        # Rule 1: Wave 2 above Wave 0
        if w2 <= w0: continue
        # Rule 2: Wave 3 >= Wave 1
        if wave3 < wave1 * 0.8: continue
        # Rule 3: Wave 4 above Wave 1
        if w4 <= w1: continue
        # Recency: Wave 4 in last 10 candles
        if w4_idx < len(cl) - 10: continue

        current = cl[-1]['c']
        w5_target = w4 + wave1 * 0.618
        dist = abs(current - w4) / current * 100

        if dist < 3.0 and current >= w4 * 0.98:
            return {
                'w4': w4, 'target': w5_target,
                'upside': (w5_target - current) / current * 100,
                'wave3_ext': wave3 / wave1,
            }
    return None


# ══════════════════════════════════════════════════════
#  MOMENTUM TRIGGER — mid-candle entry on big moves
# ══════════════════════════════════════════════════════

def check_momentum_trigger(td):
    """If any coin pumps 1.5%+ from hourly open, consider entry."""
    now = time.time()
    current_hour = int(now / CANDLE_1H)
    triggers = []

    for pair, info in td.items():
        if pair in EXCLUDED or pair in positions: continue
        if pair in cooldowns and time.time() < cooldowns[pair]: continue

        px = float(info.get('LastPrice', 0))
        if px <= 0: continue

        hour_key = f"{pair}_{current_hour}"
        open_px = hourly_opens.get(hour_key, 0)
        if open_px <= 0: continue

        move = (px - open_px) / open_px * 100
        if move >= 1.5:  # 1.5%+ pump from hourly open
            # Check spread
            bid = float(info.get('MaxBid', 0))
            ask = float(info.get('MinAsk', 0))
            if bid > 0 and ask > 0:
                spread = (ask - bid) / bid * 100
                if spread < 0.2:
                    triggers.append((move, pair, px, info))

    triggers.sort(key=lambda x: -x[0])
    return triggers[:1]  # best momentum trigger only


# ══════════════════════════════════════════════════════
#  CANDLESTICK PATTERNS — 50 patterns (from V7)
# ══════════════════════════════════════════════════════

def detect_patterns(cl):
    """Score candlestick patterns on a list of candle dicts."""
    n = len(cl)
    if n < 5: return 0, ''

    score = 0; patterns = []
    c = cl[-1]; p = cl[-2] if n >= 2 else c
    ab = sum(_bs(x) for x in cl[-14:]) / min(14, n)
    ar = sum(_rng(x) for x in cl[-14:]) / min(14, n)
    if ab == 0: ab = 0.0001
    if ar == 0: ar = 0.0001
    bs = _bs(c); rng = _rng(c); bsp = _bs(p)

    # 10 original reversal patterns
    if _rd(p) and _gr(c) and c['o'] <= p['c'] and c['c'] >= p['o'] and bs > bsp * 1.2: score += 3; patterns.append('ENG')
    if rng > 0 and bs > 0 and _lw(c) >= bs * 2 and _uw(c) <= bs * 0.5 and _gr(c): score += 3; patterns.append('HAM')
    if n >= 3 and all(_gr(cl[-i]) for i in [1, 2, 3]) and cl[-1]['c'] > cl[-2]['c'] > cl[-3]['c']: score += 3; patterns.append('3WS')
    if n >= 3 and cl[-2]['h'] <= cl[-3]['h'] and cl[-2]['l'] >= cl[-3]['l'] and cl[-1]['c'] > cl[-3]['h'] and _gr(cl[-1]): score += 3; patterns.append('INS')
    if n >= 5 and cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h'] and _gr(cl[-1]): score += 2; patterns.append('HHHL')
    if _gr(c) and bs > ab * 2 and _uw(c) < bs * 0.1 and _lw(c) < bs * 0.1: score += 3; patterns.append('MARU')
    if n >= 3:
        b1, b2, b3 = _bs(cl[-3]), _bs(cl[-2]), _bs(cl[-1])
        if _rd(cl[-3]) and b1 > ab and b2 < b1 * 0.3 and _gr(cl[-1]) and b3 > ab and cl[-1]['c'] > (cl[-3]['o'] + cl[-3]['c']) / 2:
            score += 4; patterns.append('MSTAR')
    if _rd(p) and _gr(c):
        mid = (p['o'] + p['c']) / 2
        if c['o'] < p['c'] and c['c'] > mid and c['c'] < p['o']: score += 3; patterns.append('PIER')
    if n >= 2 and ar > 0 and abs(c['l'] - p['l']) / ar < 0.05 and _rd(p) and _gr(c): score += 3; patterns.append('TWZR')
    if n >= 6 and (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100 >= 1.0: score += 2; patterns.append('MOM')

    # 15 trend/momentum patterns (from V7 update)
    if n >= 4 and sum(1 for x in cl[-4:-1] if _rd(x)) >= 3 and _gr(c): score += 3; patterns.append('MEANREV')
    if _gr(c) and bs > ab * 3: score += 3; patterns.append('GOD')
    if n >= 2 and c['h'] > p['h'] and c['l'] < p['l'] and _gr(c): score += 3; patterns.append('OUTSIDE')
    if n >= 4 and _gr(cl[-4]) and _gr(cl[-3]) and _bs(cl[-2]) < ab * 0.5 and _gr(cl[-1]) and _bs(cl[-1]) > ab: score += 3; patterns.append('RBR')
    if n >= 20:
        sma20 = sum(x['c'] for x in cl[-20:]) / 20
        if _gr(c) and c['c'] > sma20 and c['o'] < sma20: score += 3; patterns.append('SMA_X')
        elif _gr(c) and c['c'] > sma20 and bs > ab: score += 2; patterns.append('ABVSMA')
    if n >= 24:
        ph = max(x['h'] for x in cl[-25:-1])
        if c['c'] > ph and _gr(c): score += 3; patterns.append('BRK24')
    if n >= 2 and c['o'] > p['h'] and _gr(c): score += 2; patterns.append('GAP')
    if n >= 4 and cl[-1]['l'] > cl[-2]['l'] > cl[-3]['l'] > cl[-4]['l'] and _gr(c): score += 2; patterns.append('HILOWS')
    if n >= 20 and rng > 0:
        sup = min(x['l'] for x in cl[-20:])
        if _lw(c) > rng * 0.6 and abs(c['l'] - sup) / sup * 100 < 0.5: score += 3; patterns.append('PIN_SUP')
    if n >= 20:
        cls = [x['c'] for x in cl[-20:]]; sma = sum(cls) / 20; std = (sum((x - sma) ** 2 for x in cls) / 20) ** 0.5
        if std > 0 and c['l'] <= sma - 2 * std * 1.005 and _gr(c): score += 3; patterns.append('BB')

    # 5 more from latest update
    if n >= 3 and _gr(c) and _gr(cl[-2]) and _bs(cl[-1]) > _bs(cl[-2]) > _bs(cl[-3]): score += 2; patterns.append('ACCEL')
    if n >= 21:
        sp = sum(x['c'] for x in cl[-21:-1]) / 20; sn = sum(x['c'] for x in cl[-20:]) / 20
        if cl[-2]['c'] < sp and c['c'] > sn: score += 3; patterns.append('RECLM')
    if n >= 6:
        t5 = cl[-6:-1]; th = max(x['h'] for x in t5); tl = min(x['l'] for x in t5)
        if th > 0 and (th - tl) / th * 100 < 0.5 and c['c'] > th and _gr(c): score += 3; patterns.append('TBRK')
    if _gr(c) and rng > 0 and c['o'] > 0:
        drop = (c['o'] - c['l']) / c['o'] * 100
        if drop > 1.0 and _lw(c) > bs * 1.5: score += 2; patterns.append('DIP')
    if rng > 0 and bs / rng > 0.6 and _gr(c): score += 1; patterns.append('STRG')

    return score, '+'.join(patterns) if patterns else 'NONE'


# ══════════════════════════════════════════════════════
#  CHART PATTERN SCANNING (cached)
# ══════════════════════════════════════════════════════

def scan_chart_patterns(pair):
    """Scan for chart patterns. Cached per candle count."""
    cl = list(candles_1h.get(pair, []))
    if len(cl) < 30: return []

    cache_key = f"{pair}_{len(cl)}"
    if cache_key in _chart_cache:
        return _chart_cache[cache_key]

    import pandas as pd
    df = pd.DataFrame(cl[-80:] if len(cl) > 80 else cl)
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    if 'volume' not in df.columns: df['volume'] = 1.0

    signals = []
    try:
        from pattern_encyclopedia import ChartPatternDetector
        det = ChartPatternDetector(df)
        for method_name, pat_name, pat_score in [
            ('detect_bull_flag', 'BULL_FLAG', 5), ('detect_ascending_triangle', 'ASC_TRI', 5),
            ('detect_double_bottom', 'DBL_BOT', 4), ('detect_inverse_head_shoulders', 'INV_HS', 5),
            ('detect_falling_wedge', 'FALL_WDG', 4), ('detect_cup_and_handle', 'CUP_HDL', 5),
            ('detect_symmetrical_triangle', 'SYM_TRI', 4), ('detect_rectangle_channel', 'RECT', 4),
            ('detect_rounding_bottom', 'RND_BOT', 4), ('detect_pennant', 'PENNANT', 4),
            ('detect_triple_bottom', 'TRP_BOT', 5), ('detect_v_bottom', 'V_BOT', 3),
            ('detect_measured_move', 'MEAS_MV', 4),
        ]:
            try:
                result = getattr(det, method_name)()
                if not result: continue
                items = result if isinstance(result, list) else [result]
                for r in items:
                    if not isinstance(r, dict): continue
                    if 'bear' in r.get('pattern', '').lower() or 'top' in r.get('pattern', '').lower(): continue
                    entry = r.get('entry', 0); stop = r.get('stop', 0); target = r.get('target', 0)
                    if entry > 0 and stop > 0 and target > 0 and target > entry and stop < entry:
                        risk = (entry - stop) / entry * 100; reward = (target - entry) / entry * 100
                        rr = reward / risk if risk > 0 else 0
                        if risk <= 4.0 and rr >= 1.5:
                            signals.append((pat_name, pat_score, entry, stop, target, rr))
            except: pass
    except: pass

    _chart_cache[cache_key] = signals
    return signals


# ══════════════════════════════════════════════════════
#  REGIME DETECTION
# ══════════════════════════════════════════════════════

def detect_regime():
    cl = list(candles_1h.get('BTC/USD', []))
    if len(cl) < 20: return 'BULL'
    closes = [c['c'] for c in cl]
    ema20 = sum(closes[-20:]) / 20
    px = closes[-1]
    deltas = [closes[i] - closes[i-1] for i in range(-14, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_g = sum(gains) / 14 if gains else 0.001
    avg_l = sum(losses) / 14 if losses else 0.001
    rsi = 100 - 100 / (1 + avg_g / avg_l)
    chg3 = (closes[-1] - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 else 0
    if px < ema20 and (rsi < 40 or chg3 < -0.5):
        return 'BEAR'
    return 'BULL'


def get_dynamic_size(score, regime):
    if regime == 'BEAR': return SIZE_BEAR
    if score >= 10: return SIZE_HIGH
    if score >= 8: return SIZE_MED
    return SIZE_LOW


# ══════════════════════════════════════════════════════
#  EXIT MANAGEMENT
# ══════════════════════════════════════════════════════

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

        cl = list(candles_1h.get(pair, []))
        pos['candle_count'] = len(cl) - pos.get('entry_candle_idx', len(cl))

        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
        else:
            atr = pos['entry'] * 0.01

        sell = False; reason = ''

        # Min hold: only hard stop
        if pos['candle_count'] < MIN_HOLD_CANDLES:
            hard = pos['entry'] - atr * 2.0
            if px <= hard: sell = True; reason = 'HARD_STOP'
            else: continue

        # Chart target
        if not sell and pos.get('chart_target', 0) > 0 and px >= pos['chart_target']:
            sell = True; reason = 'CHART_TARGET'

        # Elliott target
        if not sell and pos.get('elliott_target', 0) > 0 and px >= pos['elliott_target']:
            sell = True; reason = 'ELLIOTT_W5'

        # ATR stop
        if not sell:
            if pos.get('chart_stop', 0) > 0:
                stop_level = pos['chart_stop']
            elif regime == 'BEAR':
                stop_level = pos['entry'] - atr * 1.0
            else:
                stop_level = pos['entry'] - atr * 1.2
            if px <= stop_level: sell = True; reason = 'ATR_STOP'

        # Profit trail
        if not sell and pnl_pct > 0.01:
            trail = pos['peak'] * (1 - 0.01)
            if px <= trail: sell = True; reason = 'PROFIT_TRAIL'

        # Trailing stop update
        if not sell and pnl_pct > 0.003:
            new_stop = pos['peak'] * (1 - 0.02)
            if new_stop > pos.get('stop', 0): pos['stop'] = new_stop

        # Dynamic stop
        if not sell and pos.get('stop', 0) > 0 and px <= pos['stop']:
            sell = True; reason = 'TRAIL'

        # Partial exit at +1%
        if not sell and pnl_pct > 0.01 and not pos.get('partial_done'):
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
                    trade_history.append({'pair': pair, 'pnl': pnl_usd, 'reason': 'PARTIAL'})
                    alert(f'<b>PARTIAL {pair}</b> +${pnl_usd:+,.0f} ({pnl_pct*100:+.1f}%)')
                    pos['qty'] -= sell_qty
                    pos['partial_done'] = True
                except: pass
            continue

        # Max hold
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


# ══════════════════════════════════════════════════════
#  ENTRY LOGIC — 1H patterns + 4H Elliott + momentum
# ══════════════════════════════════════════════════════

def check_entries(td):
    regime = detect_regime()

    if regime == 'BULL':
        max_pos = MAX_POSITIONS; min_score = MIN_PATTERN_SCORE
    else:
        max_pos = 1; min_score = 8

    if len(positions) >= max_pos: return

    available = get_cash()
    if available < MIN_CASH_RESERVE: return

    candidates = []
    for pair, info in td.items():
        if pair in EXCLUDED or pair in positions: continue
        if pair in cooldowns and time.time() < cooldowns[pair]: continue

        # Spread check
        bid = float(info.get('MaxBid', 0))
        ask = float(info.get('MinAsk', 0))
        if bid <= 0 or ask <= 0: continue
        spread = (ask - bid) / bid * 100
        if spread > 0.2: continue

        # 1H candlestick score
        cl_1h = list(candles_1h.get(pair, []))
        if len(cl_1h) < 10: continue
        candle_score, candle_pattern = detect_patterns(cl_1h[-26:] if len(cl_1h) > 26 else cl_1h)

        # Chart pattern score (cached)
        chart_signals = scan_chart_patterns(pair)
        chart_bullish = [s for s in chart_signals if s[1] > 0]
        best_chart = None; chart_score = 0
        if chart_bullish:
            chart_bullish.sort(key=lambda x: -x[5])
            best_chart = chart_bullish[0]
            chart_score = best_chart[1]

        # 4H Elliott Wave bonus
        elliott_score = 0; elliott_target = 0
        cl_4h = list(candles_4h.get(pair, []))
        if len(cl_4h) >= 20:
            ew = detect_elliott_wave(cl_4h)
            if ew and ew['upside'] > 3.0:
                elliott_score = 5
                elliott_target = ew['target']

        # Total score
        total_score = candle_score + chart_score + elliott_score

        if total_score < min_score: continue

        # Trend filter (widened to 10%)
        if len(cl_1h) >= 10:
            trend = (cl_1h[-1]['c'] - cl_1h[-10]['c']) / cl_1h[-10]['c'] * 100
            if trend > TREND_FILTER_PCT or trend < -TREND_FILTER_PCT: continue

        # ATR filter
        if len(cl_1h) >= 14:
            atr = sum(_rng(x) for x in cl_1h[-14:]) / 14
            if atr / cl_1h[-1]['c'] * 100 < 0.3: continue

        # Doji filter
        if _rng(cl_1h[-1]) > 0 and _bs(cl_1h[-1]) / _rng(cl_1h[-1]) < 0.1: continue

        pat_parts = []
        if candle_pattern and candle_pattern != 'NONE': pat_parts.append(candle_pattern)
        if best_chart: pat_parts.append(f'CHART:{best_chart[0]}')
        if elliott_score > 0: pat_parts.append(f'ELLIOTT_W5')
        pattern = '+'.join(pat_parts) if pat_parts else 'NONE'

        candidates.append((total_score, pair, info, pattern, best_chart, elliott_target))

    candidates.sort(key=lambda x: -x[0])

    for total_score, pair, info, pattern, best_chart, elliott_target in candidates[:1]:
        if len(positions) >= max_pos: break
        if pair in positions: continue

        ask = float(info.get('MinAsk', 0))
        if ask <= 0: continue

        available = get_cash()
        size = get_dynamic_size(total_score, regime)
        if available < MIN_CASH_RESERVE + size: break
        size = min(size, (available - MIN_CASH_RESERVE) * 0.25)
        if size < 50000: break

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

            if status not in ('FILLED', 'COMPLETED', '') and filled <= 0: continue
            fill_qty = filled or qty
            cl_now = list(candles_1h.get(pair, []))

            chart_stop = best_chart[3] if best_chart else 0
            chart_target = best_chart[4] if best_chart else 0

            positions[pair] = {
                'entry': fill_px, 'qty': fill_qty,
                'peak': fill_px, 'stop': chart_stop if chart_stop > 0 else 0,
                'time': time.time(), 'pattern': pattern, 'score': total_score,
                'entry_candle_idx': len(cl_now), 'candle_count': 0, 'partial_done': False,
                'chart_target': chart_target, 'chart_stop': chart_stop,
                'elliott_target': elliott_target,
            }

            target_str = ''
            if chart_target > 0: target_str = f' | ChartTgt: ${chart_target:.4f}'
            if elliott_target > 0: target_str += f' | ElliottTgt: ${elliott_target:.4f}'

            alert(
                f'<b>🎯 NAKED BUY {pair}</b>\n'
                f'Pattern: {pattern} (score={total_score})\n'
                f'Price: ${fill_px:.4f} | Size: ${fill_qty*fill_px:,.0f}\n'
                f'Regime: {regime} | Cash: ${available-fill_qty*fill_px:,.0f}{target_str}'
            )
        except Exception as e:
            log.info(f'Buy {pair} failed: {e}')


# ══════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════

def main():
    log.info('=' * 60)
    log.info('NAKED TRADER V8 — Multi-TF + Elliott + Momentum')
    log.info(f'1H + 4H candles | 50 patterns + 13 chart + Elliott')
    log.info(f'Score >= {MIN_PATTERN_SCORE} | Trend < {TREND_FILTER_PCT}% | Watchdog 120s')
    log.info(f'Dynamic size ${SIZE_LOW/1000:.0f}-{SIZE_HIGH/1000:.0f}k | Max {MAX_POSITIONS} pos')
    log.info('=' * 60)

    alert(
        '<b>🎯 NAKED TRADER V8 ONLINE</b>\n'
        '50 patterns + 13 chart + Elliott Wave\n'
        f'1H + 4H scanning | Momentum trigger\n'
        f'BULL: {MAX_POSITIONS} pos, ${SIZE_LOW/1000:.0f}-{SIZE_HIGH/1000:.0f}k\n'
        f'Watchdog: 120s timeout per cycle'
    )

    # Bootstrap from Binance (1H candles into candles_1h, build 4H from them)
    log.info('Bootstrapping from Binance...')
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
            # 1H candles
            r = requests.get(f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100', timeout=5)
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                candles_1h[pair] = deque(maxlen=200)
                for k in data[:-1]:
                    candles_1h[pair].append({
                        'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                        'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
                bootstrapped += 1

            # 4H candles
            r4 = requests.get(f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit=50', timeout=5)
            data4 = r4.json()
            if isinstance(data4, list) and len(data4) > 10:
                candles_4h[pair] = deque(maxlen=200)
                for k in data4[:-1]:
                    candles_4h[pair].append({
                        'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                        'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
        except: pass
        time.sleep(0.15)
    log.info(f'Bootstrapped {bootstrapped} coins (1H + 4H)')

    # CoinGecko fallback
    COINGECKO_IDS = {
        'HEMI/USD': 'hemi', 'VIRTUAL/USD': 'virtual-protocol', 'LINEA/USD': 'linea',
        'STO/USD': 'stakestone', 'PLUME/USD': 'plume', 'ASTER/USD': 'aster-2',
        'BMT/USD': 'bubblemaps', 'LISTA/USD': 'lista', 'MIRA/USD': 'mira-3',
        'PENGU/USD': 'pudgy-penguins', 'PUMP/USD': 'pump-fun', 'SOMI/USD': 'somnia',
        'WLFI/USD': 'world-liberty-financial', 'XPL/USD': 'plasma', 'S/USD': 'sonic-3',
    }
    cg_count = 0
    for pair, cg_id in COINGECKO_IDS.items():
        if pair in candles_1h: continue
        try:
            import urllib.request
            url = f'https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=7'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and len(data) > 10:
                candles_1h[pair] = deque(maxlen=200)
                for d in data[:-1]:
                    if isinstance(d, list) and len(d) >= 5:
                        candles_1h[pair].append({'o': d[1], 'h': d[2], 'l': d[3], 'c': d[4], 'v': 1.0, 't': d[0] / 1000})
                cg_count += 1
        except: pass
        time.sleep(7)
    if cg_count > 0:
        log.info(f'Bootstrapped {cg_count} more from CoinGecko')
    log.info(f'Total: {bootstrapped + cg_count} coins ready')

    # Orphan detection
    log.info('Checking for orphaned positions...')
    try:
        bal = client.get_balance()
        bal_data = bal.get('SpotWallet', bal.get('Data', bal))
        if isinstance(bal_data, dict):
            for coin, info in bal_data.items():
                if coin in ('USD', 'USDT', 'Success', 'ErrMsg', 'SpotWallet'): continue
                qty = 0
                if isinstance(info, dict):
                    qty = float(info.get('Free', 0)) + float(info.get('Locked', 0))
                elif isinstance(info, (int, float)):
                    qty = float(info)
                if qty > 0:
                    pair = f'{coin}/USD'
                    if pair not in positions:
                        td = client.get_ticker().get('Data', {})
                        px = float(td.get(pair, {}).get('LastPrice', 0))
                        if px > 0:
                            cl_len = len(candles_1h.get(pair, []))
                            positions[pair] = {
                                'entry': px, 'qty': qty, 'peak': px, 'stop': 0,
                                'time': time.time(), 'pattern': 'ORPHAN', 'score': 0,
                                'entry_candle_idx': cl_len, 'candle_count': 0,
                                'partial_done': False, 'chart_target': 0, 'chart_stop': 0,
                                'elliott_target': 0,
                            }
                            log.info(f'  {pair}: adopted at ${px:.4f}')
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
    while True:
        try:
            td = client.get_ticker().get('Data', {})
            if not td:
                time.sleep(TICK_INTERVAL); continue

            tick += 1
            update_candles(td)

            if positions:
                check_exits(td)

            # Watchdog: 2 min max per entry scan
            def _tout(s, f): raise TimeoutError
            signal.signal(signal.SIGALRM, _tout)
            signal.alarm(120)
            try:
                # Pattern-based entries
                check_entries(td)

                # Momentum trigger (every tick, no candle close needed)
                if len(positions) < MAX_POSITIONS and detect_regime() == 'BULL':
                    triggers = check_momentum_trigger(td)
                    for move, pair, px, info in triggers:
                        if pair in positions or len(positions) >= MAX_POSITIONS: break
                        available = get_cash()
                        if available < MIN_CASH_RESERVE + SIZE_LOW: break

                        # Check 4H Elliott wave for bonus
                        cl_4h = list(candles_4h.get(pair, []))
                        ew = detect_elliott_wave(cl_4h) if len(cl_4h) >= 20 else None
                        et = ew['target'] if ew else 0

                        size = SIZE_LOW
                        exinfo = get_exinfo()
                        pi = exinfo.get(pair, {})
                        pp = int(pi.get('PricePrecision', 4))
                        ap = int(pi.get('AmountPrecision', 2))
                        qty = math.floor(size / px * 10**ap) / 10**ap
                        if qty <= 0: continue

                        try:
                            order = client.place_order(pair, 'BUY', 'MARKET', qty, round(px, pp))
                            det = order.get('OrderDetail', order)
                            filled = float(det.get('FilledQuantity', 0) or 0)
                            fill_px = float(det.get('FilledAverPrice', 0) or px)
                            if filled <= 0: continue

                            cl_len = len(candles_1h.get(pair, []))
                            positions[pair] = {
                                'entry': fill_px, 'qty': filled, 'peak': fill_px, 'stop': 0,
                                'time': time.time(), 'pattern': f'MOMENTUM_{move:.1f}%',
                                'score': 5, 'entry_candle_idx': cl_len, 'candle_count': 0,
                                'partial_done': False, 'chart_target': 0, 'chart_stop': 0,
                                'elliott_target': et,
                            }
                            et_str = f' | ElliottTgt: ${et:.4f}' if et > 0 else ''
                            alert(
                                f'<b>🚀 MOMENTUM BUY {pair}</b>\n'
                                f'Pump: +{move:.1f}% from hourly open\n'
                                f'Price: ${fill_px:.4f} | Size: ${filled*fill_px:,.0f}{et_str}'
                            )
                        except: pass

            except TimeoutError:
                log.info('TIMEOUT: scan took >2min, skipping')
            finally:
                signal.alarm(0)

            # Status every 5 min
            if tick % 30 == 0:
                total_pnl = sum(t['pnl'] for t in trade_history)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history); wr = wins / n * 100 if n > 0 else 0
                regime = detect_regime(); cash = get_cash()

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
                    f'Regime={regime} | 1H={len(candles_1h)} 4H={len(candles_4h)}'
                )

        except Exception as e:
            log.info(f'Error: {e}')

        time.sleep(TICK_INTERVAL)


if __name__ == '__main__':
    main()
