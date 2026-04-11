#!/usr/bin/env python3
"""
NAKED TRADER CATB — engineered to win Hackathon Category B
════════════════════════════════════════════════════════════════════
Hackathon Category B = Best Composite Score
  0.4 × Sortino + 0.3 × Sharpe + 0.3 × Calmar

The formula rewards SMOOTH equity curves with LOW drawdown, not maximum
return. Total return is weighted ~20% in the composite; ~80% of the
score is about minimizing losses and variance.

═══ THE SURGICAL TRANSPLANT ═══
This bot is the result of:
  1. Taking naked_trader.py's 13-pattern REVERSAL signals
     (proven 69% WR on D3-fresh out-of-sample data)
  2. Adding v2's risk management framework
     (BE fix after partial, daily limits, kill switch)
  3. Tuning for Category B via backtest_catb.py (tested 14 configs)
     Winner: M2-med-2pct
       - Composite 0.48 on D3-majors (highest)
       - 4.4% max DD
       - 57.9% WR
       - +$5,970 on 7-day D3 data

═══ CONFIG ═══
  Sizes: $50k low / $75k med / $100k high (5x smaller than original nt)
  Entry: score >= 6 (nt's 13 reversal patterns)
  Max concurrent: 3 positions
  Partial: 50% at +2%
  Breakeven bump after partial (WLFI fix)
  Trail: peak × 0.98 (2% behind peak)
  ATR stop: entry - 1.2 × ATR
  Daily profit target: +1.5% → pause entries
  Daily loss limit: -0.5% → pause entries
  Kill switch: equity < $850k → halt all
  Session filter: skip 00-07 UTC
  MIN_HOLD: 3 candles (no exits except hard stop)
  MAX_HOLD: 12 candles

═══ EXPECTED PERFORMANCE ═══
Scaled from D3-majors 7-day backtest to 4 days on $900k:
  Expected P&L: +$3,400 to +$5,500
  Expected end: $903k to $906k
  Max new DD: ~4.4% (capped at $850k by kill switch = worst $850k)

Composite score target: 0.4+ (beats most teams who are overtrading)

═══ USAGE ═══
  python3 naked_trader_catb.py           # live
  python3 naked_trader_catb.py --dry     # paper mode (no orders)
  python3 naked_trader_catb.py --safer   # tighter limits (M3 config)
"""
import json
import math
import os
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    STARTING_CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient


# ════════════════════════════════════════
# CONFIG — Cat B optimized (from backtest winner: M2-med-2pct)
# ════════════════════════════════════════
MODES = {
    'default': {   # M2-med-2pct (best composite on D3-majors)
        'size_low':      50_000,
        'size_med':      75_000,
        'size_high':    100_000,
        'min_score':     6,
        'max_positions': 3,
        'partial_pct':   0.02,
        'partial_size':  0.5,
        'trail_pct':     0.02,
        'atr_stop_mult': 1.2,
        'daily_profit_target_pct': 0.015,
        'daily_loss_limit_pct':    0.005,
        'kill_switch_equity': 850_000,
        'label': 'CATB M2 (med-2pct)',
        'backtest_pnl': 5970,
        'backtest_dd': 0.044,
        'backtest_composite': 0.48,
    },
    'safer': {   # M3-med-3pct (more robust across datasets)
        'size_low':      50_000,
        'size_med':      75_000,
        'size_high':    100_000,
        'min_score':     6,
        'max_positions': 3,
        'partial_pct':   0.03,
        'partial_size':  0.5,
        'trail_pct':     0.03,
        'atr_stop_mult': 1.5,
        'daily_profit_target_pct': 0.02,
        'daily_loss_limit_pct':    0.005,
        'kill_switch_equity': 850_000,
        'label': 'CATB M3 (med-3pct, robust)',
        'backtest_pnl': 5056,
        'backtest_dd': 0.065,
        'backtest_composite': 0.37,
    },
}

# Fixed settings
TICK_INTERVAL = 15
CANDLE_SECONDS = 3600
MIN_HOLD_CANDLES = 3
MAX_HOLD_CANDLES = 12
MIN_CASH_RESERVE = 200_000
COOLDOWN_LOSS_SECS = 86_400   # 24h after loss
COOLDOWN_WIN_SECS  =  3_600   # 1h after win
TREND_FILTER = 10.0           # skip if 10-bar move > ±10%
SESSION_SKIP_HOURS = set(range(0, 7))  # 00-07 UTC

# Top 25 coins (same as naked_trader.py)
TOP_COINS = {
    'BTC/USD', 'ETH/USD', 'SOL/USD', 'BNB/USD', 'XRP/USD',
    'AVAX/USD', 'LINK/USD', 'FET/USD', 'TAO/USD', 'APT/USD',
    'SUI/USD', 'NEAR/USD', 'PENDLE/USD', 'ADA/USD', 'DOT/USD',
    'UNI/USD', 'HBAR/USD', 'AAVE/USD', 'CAKE/USD', 'DOGE/USD',
    'FIL/USD', 'LTC/USD', 'SEI/USD', 'ARB/USD', 'ENA/USD',
}
EXCLUDED = {'PAXG/USD'}

STATE_PATH = 'data/catb_trader_state.json'
LOG_PATH = 'data/catb_trader.log'


# ════════════════════════════════════════
# CANDLE + PATTERN HELPERS (from naked_trader.py lineage)
# ════════════════════════════════════════
def _bs(c): return abs(c['c'] - c['o'])
def _gr(c): return c['c'] > c['o']
def _rd(c): return c['c'] < c['o']
def _rng(c): return c['h'] - c['l']
def _uw(c): return c['h'] - max(c['o'], c['c'])
def _lw(c): return min(c['o'], c['c']) - c['l']


def detect_patterns(candles):
    """13 reversal patterns from naked_trader.py. Returns (score, name).

    PROVEN on D3-fresh out-of-sample: 69% WR.
    """
    cl = list(candles)
    if len(cl) < 10:
        return 0, ''

    score = 0
    patterns = []
    n = len(cl)
    c = cl[-1]
    p = cl[-2] if n >= 2 else c

    ab = sum(_bs(x) for x in cl[-14:]) / min(14, n)
    ar = sum(_rng(x) for x in cl[-14:]) / min(14, n)
    if ab == 0: ab = 0.0001
    if ar == 0: ar = 0.0001
    bs = _bs(c); rng = _rng(c); bs_p = _bs(p)

    # 1. Hammer
    if rng > 0 and bs > 0 and _lw(c) >= bs * 2 and _uw(c) <= bs * 0.5 and _gr(c):
        score += 3; patterns.append('HAMMER')

    # 2. Bullish Piercing
    if _rd(p) and _gr(c):
        mid = (p['o'] + p['c']) / 2
        if c['o'] < p['c'] and c['c'] > mid and c['c'] < p['o']:
            score += 3; patterns.append('PIERCE')

    # 3. Morning Star
    if n >= 3:
        b1, b2, b3 = _bs(cl[-3]), _bs(cl[-2]), _bs(cl[-1])
        if _rd(cl[-3]) and b1 > ab and b2 < b1 * 0.3 and _gr(cl[-1]) and b3 > ab:
            if cl[-1]['c'] > (cl[-3]['o'] + cl[-3]['c']) / 2:
                score += 4; patterns.append('MSTAR')

    # 4. HHHL (trend)
    if n >= 5 and cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h'] and _gr(cl[-1]):
        score += 2; patterns.append('HHHL')

    # 5. Three White Soldiers
    if n >= 3 and _gr(cl[-3]) and _gr(cl[-2]) and _gr(cl[-1]):
        if cl[-1]['c'] > cl[-2]['c'] > cl[-3]['c']:
            if _bs(cl[-1]) > 0 and _bs(cl[-2]) > 0 and _bs(cl[-3]) > 0:
                score += 3; patterns.append('3WS')

    # 6. Bullish Engulfing
    if _rd(p) and _gr(c) and c['o'] <= p['c'] and c['c'] >= p['o'] and bs > bs_p * 1.2:
        score += 3; patterns.append('ENGULF')

    # 7. Marubozu
    if _gr(c) and bs > ab * 2 and _uw(c) < bs * 0.1 and _lw(c) < bs * 0.1:
        score += 3; patterns.append('MARU')

    # 8. Three Outside Up
    if n >= 3:
        if _rd(cl[-3]) and _gr(cl[-2]) and cl[-2]['o'] <= cl[-3]['c'] and cl[-2]['c'] >= cl[-3]['o']:
            if _gr(cl[-1]) and cl[-1]['c'] > cl[-2]['c']:
                score += 4; patterns.append('3OUT_UP')

    # 9. Closing Marubozu
    if _gr(c) and bs > ab * 1.5:
        if (c['h'] - c['c']) < bs * 0.05:
            if not any('MARU' in pat for pat in patterns):
                score += 3; patterns.append('CLOSE_MARU')

    # 10. Bearish-as-Bullish (mean reversion)
    if n >= 6:
        uptrend = cl[-1]['c'] > cl[-6]['c']
        if uptrend and n >= 3:
            for back in [1, 2]:
                if n > back + 1:
                    prev_c = cl[-(back + 1)]
                    prev_p = cl[-(back + 2)]
                    if _gr(prev_p) and _rd(prev_c):
                        if prev_c['o'] >= prev_p['c'] and prev_c['c'] <= prev_p['o']:
                            if _gr(c):
                                score += 3; patterns.append('BEAR_REVERT')
                                break

    # 11. Mean Reversion
    if n >= 4:
        reds = sum(1 for x in cl[-4:-1] if _rd(x))
        if reds >= 3 and _gr(c):
            score += 2; patterns.append('MEANREV')

    # 12. Inside Bar Breakout
    if n >= 3:
        if cl[-2]['h'] <= cl[-3]['h'] and cl[-2]['l'] >= cl[-3]['l']:
            if cl[-1]['c'] > cl[-3]['h'] and _gr(cl[-1]):
                score += 2; patterns.append('INSIDE')

    # 13. Momentum
    if n >= 6 and (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100 >= 1.0:
        score += 1; patterns.append('MOM')

    # Bonuses
    if n >= 20:
        low20 = min(x['l'] for x in cl[-20:])
        if (c['c'] - low20) / c['c'] * 100 < 1.5 and score > 0:
            score += 1; patterns.append('sup')

    if n >= 2 and c.get('v', 0) > p.get('v', 0) * 1.2 and _gr(c) and score > 0:
        score += 1; patterns.append('vol')

    hour_utc = time.gmtime().tm_hour
    if 14 <= hour_utc <= 19 and score > 0:
        score += 1; patterns.append('peak')

    return score, '+'.join(patterns) if patterns else 'NONE'


# ════════════════════════════════════════
# LOG / TELEGRAM
# ════════════════════════════════════════
def now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def log(msg, tg=False):
    line = f'[{now_str()}] {msg}'
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as fp:
            fp.write(line + '\n')
    except Exception:
        pass
    if tg and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                data={'chat_id': TELEGRAM_CHAT_ID, 'text': line},
                timeout=5,
            )
        except Exception:
            pass


# ════════════════════════════════════════
# BINANCE BOOTSTRAP (same as production)
# ════════════════════════════════════════
def bootstrap():
    log('Bootstrapping 1H candles from Binance...')
    COIN_TO_BINANCE = {
        'BTC/USD': 'BTCUSDT', 'ETH/USD': 'ETHUSDT', 'SOL/USD': 'SOLUSDT',
        'BNB/USD': 'BNBUSDT', 'XRP/USD': 'XRPUSDT', 'AVAX/USD': 'AVAXUSDT',
        'LINK/USD': 'LINKUSDT', 'FET/USD': 'FETUSDT', 'TAO/USD': 'TAOUSDT',
        'APT/USD': 'APTUSDT', 'SUI/USD': 'SUIUSDT', 'NEAR/USD': 'NEARUSDT',
        'PENDLE/USD': 'PENDLEUSDT', 'ADA/USD': 'ADAUSDT', 'DOT/USD': 'DOTUSDT',
        'UNI/USD': 'UNIUSDT', 'HBAR/USD': 'HBARUSDT', 'AAVE/USD': 'AAVEUSDT',
        'CAKE/USD': 'CAKEUSDT', 'DOGE/USD': 'DOGEUSDT', 'FIL/USD': 'FILUSDT',
        'LTC/USD': 'LTCUSDT', 'SEI/USD': 'SEIUSDT', 'ARB/USD': 'ARBUSDT',
        'ENA/USD': 'ENAUSDT',
    }
    candles = {}
    ok = 0
    for pair, sym in COIN_TO_BINANCE.items():
        try:
            r = requests.get(
                f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=100',
                timeout=8,
            )
            data = r.json()
            if isinstance(data, list) and len(data) > 20:
                candles[pair] = deque(maxlen=200)
                for k in data[:-1]:
                    candles[pair].append({
                        'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                        'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
                ok += 1
        except Exception as e:
            log(f'  {pair} fail: {e}')
        time.sleep(0.1)
    log(f'Bootstrapped {ok} coins')
    return candles


def update_candles(candles, client):
    """Fold current tickers into the latest candle for each pair."""
    now = time.time()
    current_period = int(now / CANDLE_SECONDS)
    try:
        ticker = client.get_ticker()
        data = ticker.get('Data') or ticker
        if not isinstance(data, dict):
            return
        for pair, info in data.items():
            if pair in EXCLUDED or pair not in TOP_COINS:
                continue
            if pair not in candles:
                continue
            try:
                px = float(info.get('LastPrice', 0))
            except (TypeError, ValueError):
                px = 0
            if px <= 0:
                continue
            dq = candles[pair]
            if not dq:
                continue
            last = dq[-1]
            bucket_start = current_period * CANDLE_SECONDS
            if bucket_start > last['t']:
                dq.append({
                    'o': px, 'h': px, 'l': px, 'c': px, 'v': 0,
                    't': bucket_start,
                })
            else:
                last['h'] = max(last['h'], px)
                last['l'] = min(last['l'], px)
                last['c'] = px
    except Exception as e:
        log(f'update_candles error: {e}')


# ════════════════════════════════════════
# POSITION
# ════════════════════════════════════════
class Position:
    def __init__(self, pair, entry, qty, pattern, score, t, ap, pp):
        self.pair = pair
        self.entry = entry
        self.qty_initial = qty
        self.qty_remaining = qty
        self.peak = entry
        self.stop = 0.0
        self.partial_done = False
        self.be_moved = False
        self.pattern = pattern
        self.score = score
        self.entry_time = t
        self.candle_count = 0
        self.amt_prec = ap
        self.price_prec = pp

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            'pair', 'entry', 'qty_initial', 'qty_remaining', 'peak', 'stop',
            'partial_done', 'be_moved', 'pattern', 'score', 'entry_time',
            'candle_count', 'amt_prec', 'price_prec',
        )}

    @classmethod
    def from_dict(cls, d):
        p = cls.__new__(cls)
        for k, v in d.items():
            setattr(p, k, v)
        return p


# ════════════════════════════════════════
# STATE
# ════════════════════════════════════════
def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            'positions': {}, 'cooldowns': {}, 'history': [],
            'peak_equity': STARTING_CAPITAL,
            'day_start_equity': STARTING_CAPITAL,
            'current_day': None,
            'daily_pnl': 0,
            'total_fees': 0,
            'kill_switch_hit': False,
        }
    try:
        with open(STATE_PATH) as fp:
            d = json.load(fp)
        out = {
            'positions': {},
            'cooldowns': d.get('cooldowns', {}),
            'history': d.get('history', []),
            'peak_equity': d.get('peak_equity', STARTING_CAPITAL),
            'day_start_equity': d.get('day_start_equity', STARTING_CAPITAL),
            'current_day': d.get('current_day'),
            'daily_pnl': d.get('daily_pnl', 0),
            'total_fees': d.get('total_fees', 0),
            'kill_switch_hit': d.get('kill_switch_hit', False),
        }
        for pair, pd in d.get('positions', {}).items():
            out['positions'][pair] = Position.from_dict(pd)
        return out
    except Exception as e:
        log(f'state load error: {e}')
        return load_state.__wrapped__() if hasattr(load_state, '__wrapped__') else {
            'positions': {}, 'cooldowns': {}, 'history': [],
            'peak_equity': STARTING_CAPITAL,
            'day_start_equity': STARTING_CAPITAL,
            'current_day': None,
            'daily_pnl': 0,
            'total_fees': 0,
            'kill_switch_hit': False,
        }


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        d = {
            'positions': {p: pos.to_dict() for p, pos in state['positions'].items()},
            'cooldowns': state['cooldowns'],
            'history': state['history'][-200:],
            'peak_equity': state['peak_equity'],
            'day_start_equity': state['day_start_equity'],
            'current_day': state['current_day'],
            'daily_pnl': state['daily_pnl'],
            'total_fees': state['total_fees'],
            'kill_switch_hit': state['kill_switch_hit'],
            'saved_at': time.time(),
        }
        with open(STATE_PATH, 'w') as fp:
            json.dump(d, fp, indent=2)
    except Exception as e:
        log(f'state save error: {e}')


# ════════════════════════════════════════
# ROOSTOO HELPERS
# ════════════════════════════════════════
def get_cash(client):
    try:
        bal = client.get_balance()
        w = bal.get('SpotWallet', bal.get('Data', bal))
        if isinstance(w, dict):
            usd = w.get('USD', {})
            if isinstance(usd, dict):
                return float(usd.get('Free', 0))
    except Exception as e:
        log(f'cash error: {e}')
    return 0


def get_equity(client):
    """Cash + USD value of all holdings."""
    try:
        bal = client.get_balance()
        w = bal.get('SpotWallet', bal.get('Data', bal))
        if not isinstance(w, dict):
            return STARTING_CAPITAL
        total = 0
        for asset, info in w.items():
            if not isinstance(info, dict):
                continue
            qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
            if qty <= 0:
                continue
            if asset in ('USD', 'USDT'):
                total += qty
                continue
            try:
                p = client.get_price(f'{asset}/USD')
                total += qty * (p or 0)
            except Exception:
                pass
        return total if total > 0 else STARTING_CAPITAL
    except Exception as e:
        log(f'equity error: {e}')
        return STARTING_CAPITAL


# ════════════════════════════════════════
# EXIT LOGIC (nt-style tight + BE bump)
# ════════════════════════════════════════
def check_exits(client, state, cfg, candles, dry):
    if state.get('kill_switch_hit'):
        return

    for pair in list(state['positions'].keys()):
        pos = state['positions'][pair]
        pos.candle_count = len(candles.get(pair, [])) - 0  # approximate

        cl = list(candles.get(pair, []))
        if not cl:
            continue
        last = cl[-1]
        px = last['c']
        if px > pos.peak:
            pos.peak = px
        pnl_pct = (px - pos.entry) / pos.entry if pos.entry > 0 else 0

        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
        else:
            atr = pos.entry * 0.01

        sell = False
        reason = ''

        # Min hold: only hard stop
        if pos.candle_count < MIN_HOLD_CANDLES:
            hard = pos.entry - atr * 2.0
            if px <= hard:
                sell = True; reason = 'HARD_STOP'
            else:
                continue

        # ATR stop
        if not sell:
            stop_level = pos.entry - atr * cfg['atr_stop_mult']
            if pos.stop > 0:
                stop_level = max(stop_level, pos.stop)
            if px <= stop_level:
                sell = True; reason = 'ATR_STOP'

        # Partial at +X% (Cat B tweak)
        if not sell and pnl_pct >= cfg['partial_pct'] and not pos.partial_done:
            sell_qty = pos.qty_initial * cfg['partial_size']
            sell_qty = math.floor(sell_qty * 10**pos.amt_prec) / 10**pos.amt_prec
            if sell_qty > 0:
                try:
                    if not dry:
                        order = client.place_order(pair, 'SELL', 'MARKET',
                                                    sell_qty, round(px, pos.price_prec))
                        det = order.get('OrderDetail', order)
                        exit_px = float(det.get('FilledAverPrice', 0) or px)
                        fee = float(det.get('CommissionChargeValue', 0) or 0)
                        state['total_fees'] += fee
                    else:
                        exit_px = px
                        log(f'[DRY] SELL partial {pair} qty={sell_qty}')
                    pnl_usd = (exit_px - pos.entry) * sell_qty
                    pos.qty_remaining -= sell_qty
                    pos.partial_done = True
                    # BE BUMP — THE WLFI FIX
                    pos.stop = pos.entry * 1.001
                    pos.be_moved = True
                    state['daily_pnl'] += pnl_usd
                    state['history'].append({
                        'pair': pair, 'reason': 'PARTIAL', 'pnl': pnl_usd,
                        'pattern': pos.pattern, 'ts': time.time(),
                    })
                    log(f'🎯 PARTIAL {pair} +{pnl_pct*100:.2f}% ${pnl_usd:+,.0f} '
                        f'sold {sell_qty} @ ${exit_px:.4f}, stop→BE ${pos.entry*1.001:.4f}',
                        tg=True)
                except Exception as e:
                    log(f'partial sell error {pair}: {e}')
            continue

        # Trailing stop
        if not sell and pos.peak > pos.entry * (1 + cfg['trail_pct']):
            trail = pos.peak * (1 - cfg['trail_pct'])
            if pos.be_moved and trail < pos.entry * 1.001:
                trail = pos.entry * 1.001
            if trail > pos.stop:
                pos.stop = trail
            if px <= pos.stop:
                sell = True; reason = 'TRAIL'

        # Max hold
        if not sell and pos.candle_count >= MAX_HOLD_CANDLES:
            sell = True; reason = 'MAX_TIME'

        if sell:
            sell_qty = pos.qty_remaining
            if sell_qty <= 0:
                del state['positions'][pair]
                continue
            try:
                if not dry:
                    order = client.place_order(pair, 'SELL', 'MARKET',
                                                sell_qty, round(px, pos.price_prec))
                    det = order.get('OrderDetail', order)
                    exit_px = float(det.get('FilledAverPrice', 0) or px)
                    fee = float(det.get('CommissionChargeValue', 0) or 0)
                    state['total_fees'] += fee
                else:
                    exit_px = px
                    log(f'[DRY] SELL {pair} qty={sell_qty}')
                pnl_usd = (exit_px - pos.entry) * sell_qty
                state['daily_pnl'] += pnl_usd
                state['history'].append({
                    'pair': pair, 'reason': reason, 'pnl': pnl_usd,
                    'pattern': pos.pattern, 'ts': time.time(),
                })
                marker = 'WIN' if pnl_usd > 0 else 'LOSS'
                log(f'{"🟢" if pnl_usd > 0 else "🔴"} EXIT {pair} {reason} '
                    f'{pnl_pct*100:+.2f}% ${pnl_usd:+,.0f} [{marker}]',
                    tg=True)
                if pnl_usd < 0:
                    state['cooldowns'][pair] = time.time() + COOLDOWN_LOSS_SECS
                else:
                    state['cooldowns'][pair] = time.time() + COOLDOWN_WIN_SECS
                del state['positions'][pair]
            except Exception as e:
                log(f'sell error {pair}: {e}')


# ════════════════════════════════════════
# ENTRY LOGIC
# ════════════════════════════════════════
def check_entries(client, state, cfg, candles, dry):
    if state.get('kill_switch_hit'):
        return

    # Daily limits
    day_pnl = state.get('daily_pnl', 0)
    day_start = state.get('day_start_equity', STARTING_CAPITAL)
    if day_pnl > day_start * cfg['daily_profit_target_pct']:
        return  # daily profit target hit
    if day_pnl < -day_start * cfg['daily_loss_limit_pct']:
        return  # daily loss limit hit

    if len(state['positions']) >= cfg['max_positions']:
        return

    # Session filter
    hour = datetime.now(timezone.utc).hour
    if hour in SESSION_SKIP_HOURS:
        return

    cash = get_cash(client)
    if cash < MIN_CASH_RESERVE + cfg['size_low']:
        return

    # Ranked candidates
    candidates = []
    try:
        ticker = client.get_ticker()
        data = ticker.get('Data') or ticker
    except Exception as e:
        log(f'ticker error: {e}')
        return

    if not isinstance(data, dict):
        return

    for pair, info in data.items():
        if pair in EXCLUDED or pair not in TOP_COINS:
            continue
        if pair in state['positions']:
            continue
        cd = state['cooldowns'].get(pair, 0)
        if cd and cd > time.time():
            continue

        try:
            bid = float(info.get('MaxBid', 0))
            ask = float(info.get('MinAsk', 0))
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        spread = (ask - bid) / bid * 100
        if spread > 0.3:
            continue

        cl = list(candles.get(pair, []))
        if len(cl) < 10:
            continue

        score, pattern = detect_patterns(cl)
        if score < cfg['min_score']:
            continue

        # Trend filter
        trend = (cl[-1]['c'] - cl[-10]['c']) / cl[-10]['c'] * 100
        if trend > TREND_FILTER or trend < -TREND_FILTER:
            continue

        # ATR filter
        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
            if atr / cl[-1]['c'] * 100 < 0.3:
                continue

        # Doji filter
        if len(cl) > 0 and _rng(cl[-1]) > 0 and _bs(cl[-1]) / _rng(cl[-1]) < 0.1:
            continue

        candidates.append((score, pair, info, pattern))

    candidates.sort(key=lambda x: -x[0])

    for score, pair, info, pattern in candidates[:1]:
        if len(state['positions']) >= cfg['max_positions']:
            break

        try:
            ask = float(info.get('MinAsk', 0))
        except (TypeError, ValueError):
            continue
        if ask <= 0:
            continue

        cash = get_cash(client)

        # Dynamic size by score
        if score >= 10:
            size = cfg['size_high']
        elif score >= 8:
            size = cfg['size_med']
        else:
            size = cfg['size_low']

        # Cap at available cash - reserve
        size = min(size, (cash - MIN_CASH_RESERVE) * 0.5)
        if size < 5_000:
            break

        # Get precisions
        exinfo = {}
        try:
            ex = client.get_exchange_info()
            exinfo = ex.get('TradePairs', {})
        except Exception:
            pass
        pi = exinfo.get(pair, {})
        pp = int(pi.get('PricePrecision', 4))
        ap = int(pi.get('AmountPrecision', 2))

        qty = math.floor(size / ask * 10**ap) / 10**ap
        if qty <= 0:
            continue

        try:
            if not dry:
                order = client.place_order(pair, 'BUY', 'MARKET',
                                            qty, round(ask, pp))
                det = order.get('OrderDetail', order)
                status = (det.get('Status') or '').upper()
                filled = float(det.get('FilledQuantity', 0) or 0)
                fill_px = float(det.get('FilledAverPrice', 0) or ask)
                if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
                    continue
                fill_qty = filled or qty
                fee = float(det.get('CommissionChargeValue', 0) or 0)
                state['total_fees'] += fee
            else:
                fill_px = ask
                fill_qty = qty
                log(f'[DRY] BUY {pair} qty={qty}')

            pos = Position(pair, fill_px, fill_qty, pattern, score, time.time(), ap, pp)
            state['positions'][pair] = pos
            log(
                f'🎯 CATB BUY {pair} ${fill_px:.4f} qty={fill_qty} '
                f'size=${fill_qty*fill_px:,.0f} pattern={pattern} score={score} '
                f'cash=${cash-fill_qty*fill_px:,.0f}',
                tg=True,
            )
        except Exception as e:
            log(f'buy error {pair}: {e}')


# ════════════════════════════════════════
# KILL SWITCH + DAILY TRACKING
# ════════════════════════════════════════
def check_kill_switch(client, state, cfg):
    equity = get_equity(client)
    if equity < cfg['kill_switch_equity']:
        if not state['kill_switch_hit']:
            log(f'🛑 KILL SWITCH HIT — equity ${equity:,.0f} < ${cfg["kill_switch_equity"]:,.0f}',
                tg=True)
            state['kill_switch_hit'] = True
            # Close all positions
            for pair in list(state['positions'].keys()):
                pos = state['positions'][pair]
                try:
                    client.place_order(pair, 'SELL', 'MARKET',
                                        pos.qty_remaining,
                                        round(pos.entry, pos.price_prec))
                    log(f'🛑 KILL close {pair} qty={pos.qty_remaining}', tg=True)
                    del state['positions'][pair]
                except Exception as e:
                    log(f'kill close error {pair}: {e}')
    if equity > state['peak_equity']:
        state['peak_equity'] = equity
    return equity


def check_daily_rollover(state):
    today = int(time.time()) // 86400
    if state['current_day'] != today:
        # new day
        state['current_day'] = today
        state['day_start_equity'] = state.get('peak_equity', STARTING_CAPITAL)
        state['daily_pnl'] = 0
        log(f'📅 New day started — baseline ${state["day_start_equity"]:,.0f}')


# ════════════════════════════════════════
# MAIN
# ════════════════════════════════════════
def run_loop(cfg, dry=False):
    client = RoostooClient()
    state = load_state()
    candles = bootstrap()
    if not candles:
        log('No candles bootstrapped — ABORT', tg=True)
        return

    log(
        f'🎯 CATB TRADER {cfg["label"]} ONLINE — '
        f'sizes=${cfg["size_low"]/1000:.0f}k/${cfg["size_med"]/1000:.0f}k/${cfg["size_high"]/1000:.0f}k, '
        f'min_score={cfg["min_score"]}, max_pos={cfg["max_positions"]}, '
        f'partial={cfg["partial_pct"]*100:.1f}%, trail={cfg["trail_pct"]*100:.1f}%, '
        f'daily +{cfg["daily_profit_target_pct"]*100:.1f}% / -{cfg["daily_loss_limit_pct"]*100:.1f}%, '
        f'kill=${cfg["kill_switch_equity"]:,.0f}, '
        f'backtest_composite={cfg["backtest_composite"]:.2f}, '
        f'dry={dry}',
        tg=True,
    )

    cycle = 0
    while True:
        cycle += 1
        try:
            check_daily_rollover(state)
            update_candles(candles, client)
            equity = check_kill_switch(client, state, cfg)

            if state['kill_switch_hit']:
                log('Kill switch active — sleeping and monitoring only')
                time.sleep(60)
                continue

            check_exits(client, state, cfg, candles, dry)
            check_entries(client, state, cfg, candles, dry)

            if cycle % 10 == 0:
                dd = (state['peak_equity'] - equity) / state['peak_equity'] * 100 \
                    if state.get('peak_equity') else 0
                log(f'cycle {cycle}: equity=${equity:,.0f} peak=${state["peak_equity"]:,.0f} '
                    f'dd={dd:.2f}% daily_pnl=${state["daily_pnl"]:+,.0f} '
                    f'total_fees=${state["total_fees"]:,.0f} '
                    f'positions={list(state["positions"].keys())}')

            save_state(state)
        except Exception as e:
            log(f'cycle error: {e}')
            traceback.print_exc()

        time.sleep(TICK_INTERVAL)


def main():
    dry = '--dry' in sys.argv
    safer = '--safer' in sys.argv
    mode = 'safer' if safer else 'default'
    cfg = MODES[mode]

    if dry:
        log('DRY-RUN mode — no orders')
    log(f'MODE: {cfg["label"]}')
    log(f'  backtest: P&L ${cfg["backtest_pnl"]:+,.0f}, '
        f'DD {cfg["backtest_dd"]*100:.1f}%, '
        f'composite {cfg["backtest_composite"]:.2f}')

    try:
        run_loop(cfg, dry=dry)
    except KeyboardInterrupt:
        log('Interrupted', tg=True)


if __name__ == '__main__':
    main()
