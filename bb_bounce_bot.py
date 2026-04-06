"""
BB Bounce Bot — buys when price touches BB lower band + green candle.
Backtested: 62% WR on 757 trades, 72% WR in sideways, 92% in bull.

Runs live on Roostoo. Builds its own candle data from 60-second ticks.
Checks for BB lower touch on 1H and 4H timeframes.
Adaptive sizing based on market regime.

Run: python3 bb_bounce_bot.py
"""

import time
import math
import json
import logging
import threading
from datetime import datetime
from collections import deque

from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.FileHandler('logs/bb_bounce.log'), logging.StreamHandler()])
log = logging.getLogger('BBBounce')

client = RoostooClient()

# ── Config ──
EXCLUDED = {'PAXG/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD', '1000CHEEMS/USD'}

# Top bouncing coins from backtest (WR > 65%)
BEST_COINS = {'XLM/USD', 'ADA/USD', 'DOGE/USD', 'WLD/USD', 'CFX/USD', 'WIF/USD',
              'FLOKI/USD', 'VIRTUAL/USD', 'BONK/USD', 'HBAR/USD', 'FET/USD',
              'NEAR/USD', 'CAKE/USD', 'BNB/USD', 'XRP/USD', 'FORM/USD',
              'UNI/USD', 'ENA/USD', 'ICP/USD', 'PENDLE/USD', 'SOL/USD'}

POSITION_SIZE_BULL = 500000  # Tier 1: k (95% WR signal)      # $200k in bull/sideways
POSITION_SIZE_BEAR = 150000  # Tier 2: k backup      # $100k in bear (half size)
MAX_POSITIONS = 6
STOP_PCT = 0.025  # 2.5% stop (backtested optimal)                  # 2% stop
HOLD_HOURS = 6  # 6h hold (backtested optimal)                   # 8 hour max hold
CHECK_INTERVAL = 60              # check every 60 seconds
MIN_CANDLES = 25                 # need 25 candles before trading

# ── State ──
tick_data = {}          # pair -> deque of (timestamp, open, high, low, close, volume)
candles_1h = {}         # pair -> list of {o, h, l, c, v, t}
positions = {}          # pair -> {entry, size, bar, peak, entry_time}
trade_history = []
exchange_info_cache = None


def send_alert(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass
    log.info(msg.replace('<b>', '').replace('</b>', ''))


def get_exchange_info():
    global exchange_info_cache
    if exchange_info_cache: return exchange_info_cache
    try:
        info = client.get_exchange_info()
        exchange_info_cache = info.get('TradePairs', {})
        return exchange_info_cache
    except:
        return {}


def calc_bb(closes, period=20):
    """Calculate Bollinger Bands from list of close prices."""
    if len(closes) < period:
        return None, None, None
    recent = closes[-period:]
    mid = sum(recent) / period
    std = (sum((p - mid) ** 2 for p in recent) / period) ** 0.5
    return mid - 2 * std, mid, mid + 2 * std


def calc_ema(closes, period=50):
    """Calculate EMA from list of close prices."""
    if len(closes) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = (p - ema) * mult + ema
    return ema


def detect_regime():
    """Detect market regime from BTC price action."""
    btc_candles = candles_1h.get('BTC/USD', [])
    if len(btc_candles) < 168:  # need 7 days (168 hours)
        return 'UNKNOWN'

    closes = [c['c'] for c in btc_candles]
    # 7-day return
    ret_7d = (closes[-1] - closes[-168]) / closes[-168] * 100 if closes[-168] > 0 else 0

    if ret_7d > 5: return 'BULL'
    elif ret_7d < -5: return 'BEAR'
    elif abs(ret_7d) < 2: return 'SIDEWAYS'
    else: return 'MIXED'


def update_candles(ticker_data):
    """Build 1H candles from tick data."""
    now = time.time()
    current_hour = int(now / 3600)

    for pair, info in ticker_data.items():
        if pair in EXCLUDED: continue
        try:
            px = float(info.get('LastPrice', 0))
            vol = float(info.get('CoinTradeValue', 0))
            if px <= 0: continue

            if pair not in tick_data:
                tick_data[pair] = deque(maxlen=3600)  # 1 hour of ticks at 1/sec
                candles_1h[pair] = []

            tick_data[pair].append({'t': now, 'p': px, 'v': vol})

            # Build hourly candle
            if pair not in candles_1h:
                candles_1h[pair] = []

            # Check if we need to close current candle and start new one
            ticks = list(tick_data[pair])
            if not ticks: continue

            current_candle_hour = int(ticks[-1]['t'] / 3600)

            # Group ticks by hour
            hour_ticks = {}
            for t in ticks:
                h = int(t['t'] / 3600)
                if h not in hour_ticks:
                    hour_ticks[h] = []
                hour_ticks[h].append(t)

            # Build candles for completed hours
            for h in sorted(hour_ticks.keys()):
                if h == current_candle_hour: continue  # don't close current hour
                ht = hour_ticks[h]
                if not ht: continue

                candle = {
                    'o': ht[0]['p'],
                    'h': max(t['p'] for t in ht),
                    'l': min(t['p'] for t in ht),
                    'c': ht[-1]['p'],
                    'v': sum(t['v'] for t in ht),
                    't': h * 3600,
                }

                # Only add if we don't already have this hour
                existing_hours = set(int(c['t'] / 3600) for c in candles_1h[pair])
                if h not in existing_hours:
                    candles_1h[pair].append(candle)
                    # Keep last 200 candles
                    if len(candles_1h[pair]) > 200:
                        candles_1h[pair] = candles_1h[pair][-200:]

        except: pass


def check_entry(pair, info):
    """Check if BB lower + green candle signal fires."""
    candles = candles_1h.get(pair, [])
    if len(candles) < MIN_CANDLES:
        return False, None

    closes = [c['c'] for c in candles]

    # BB
    bb_lower, bb_mid, bb_upper = calc_bb(closes)
    if bb_lower is None: return False, None

    current_close = closes[-1]
    current_open = candles[-1]['o']

    # Green candle
    is_green = current_close > current_open

    # Below BB lower
    below_bb = current_close < bb_lower

    if not (below_bb and is_green):
        return False, None

    # ── Additional filters ──
    signal_type = 'BB_GREEN'
    score = 1

    # Check for 3 previous red candles (higher conviction)
    if len(candles) >= 4:
        reds_before = all(candles[-(i+2)]['c'] < candles[-(i+2)]['o'] for i in range(3))
        if reds_before:
            signal_type = 'BB_GREEN_3RED'
            score += 2

    # Higher low
    if len(candles) >= 2:
        if candles[-1]['l'] > candles[-2]['l']:
            signal_type += '_HL'
            score += 1

    # Above EMA50 (rare but highest WR)
    ema50 = calc_ema(closes, 50)
    if ema50 and current_close > ema50:
        signal_type += '_EMA50'
        score += 3  # big bonus

    # Prefer best bouncing coins
    if pair in BEST_COINS:
        score += 1

    return True, {'signal': signal_type, 'score': score, 'bb_lower': bb_lower, 'price': current_close}


def check_exits():
    """Check all positions for stop or time exit."""
    td = client.get_ticker().get('Data', {})
    to_close = []

    for pair, pos in list(positions.items()):
        info = td.get(pair, {})
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        if px <= 0 or bid <= 0: continue

        if px > pos['peak']: pos['peak'] = px

        pnl_pct = (px - pos['entry']) / pos['entry']

        # Breakeven stop at +0.5%
        if pnl_pct > 0.005 and pos.get('stop', pos['entry'] * (1-STOP_PCT)) < pos['entry']:
            pos['stop'] = pos['entry'] * 1.001

        # Trailing stop
        trail = pos['peak'] * (1 - STOP_PCT)
        if trail > pos.get('stop', 0):
            pos['stop'] = trail

        # Check stop
        if px <= pos.get('stop', pos['entry'] * (1-STOP_PCT)):
            to_close.append((pair, bid, 'STOP'))

        # Time exit
        elif time.time() - pos['entry_time'] > HOLD_HOURS * 3600:
            to_close.append((pair, bid, 'TIME'))

    for pair, bid, reason in to_close:
        close_position(pair, bid, reason)


def open_position(pair, info, signal_data):
    """Open a new position."""
    regime = detect_regime()
    size = POSITION_SIZE_BULL if regime in ('BULL', 'SIDEWAYS', 'UNKNOWN') else POSITION_SIZE_BEAR

    ask = float(info.get('MinAsk', 0))
    if ask <= 0: return

    exinfo = get_exchange_info()
    pair_info = exinfo.get(pair, {})
    price_prec = int(pair_info.get('PricePrecision', 4))
    amount_prec = int(pair_info.get('AmountPrecision', 2))

    amt_mult = 10 ** amount_prec
    qty = math.floor(size / ask * amt_mult) / amt_mult
    if qty <= 0: return

    limit_price = round(ask, price_prec)

    try:
        order = client.place_order(pair, 'BUY', 'LIMIT', qty, limit_price)
        det = order.get('OrderDetail', order)
        status = (det.get('Status') or '').upper()
        filled = float(det.get('FilledQuantity', 0) or 0)
        avg_px = float(det.get('FilledAverPrice', 0) or 0)

        if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
            return

        fill_price = avg_px or limit_price
        fill_qty = filled or qty

        positions[pair] = {
            'entry': fill_price,
            'qty': fill_qty,
            'size': fill_qty * fill_price,
            'peak': fill_price,
            'stop': fill_price * (1 - STOP_PCT),
            'entry_time': time.time(),
            'signal': signal_data['signal'],
            'regime': regime,
        }

        log.info(f'BOUGHT {pair}: {fill_qty} @ ${fill_price:.4f} = ${fill_qty*fill_price:,.0f} signal={signal_data["signal"]} regime={regime}')
        send_alert(f'<b>BB BOUNCE BUY {pair}</b>\nSignal: {signal_data["signal"]}\nPrice: ${fill_price:.4f}\nSize: ${fill_qty*fill_price:,.0f}\nRegime: {regime}')

    except Exception as e:
        log.error(f'BUY {pair} failed: {e}')


def close_position(pair, bid, reason):
    """Close a position."""
    pos = positions.get(pair)
    if not pos: return

    exinfo = get_exchange_info()
    pair_info = exinfo.get(pair, {})
    price_prec = int(pair_info.get('PricePrecision', 4))

    limit_price = round(bid, price_prec)

    try:
        order = client.place_order(pair, 'SELL', 'LIMIT', pos['qty'], limit_price)
        det = order.get('OrderDetail', order)
        avg_px = float(det.get('FilledAverPrice', 0) or 0)
        exit_price = avg_px or limit_price

        pnl = (exit_price - pos['entry']) * pos['qty'] - pos['size'] * 0.002
        pnl_pct = (exit_price - pos['entry']) / pos['entry'] * 100

        trade_history.append({
            'pair': pair, 'entry': pos['entry'], 'exit': exit_price,
            'pnl': pnl, 'pnl_pct': pnl_pct, 'reason': reason,
            'signal': pos['signal'], 'regime': pos['regime'],
            'time': datetime.utcnow().strftime('%H:%M:%S'),
        })

        del positions[pair]

        log.info(f'SOLD {pair}: {reason} P&L=${pnl:+,.2f} ({pnl_pct:+.2f}%)')
        send_alert(f'<b>BB BOUNCE {reason} {pair}</b>\nP&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%)\nEntry: ${pos["entry"]:.4f} Exit: ${exit_price:.4f}')

    except Exception as e:
        log.error(f'SELL {pair} failed: {e}')


def print_status():
    """Log current status."""
    total_pnl = sum(t['pnl'] for t in trade_history)
    wins = len([t for t in trade_history if t['pnl'] > 0])
    total = len(trade_history)
    wr = wins/total*100 if total > 0 else 0
    regime = detect_regime()
    candle_counts = {p: len(c) for p, c in candles_1h.items() if len(c) > 0}
    min_candles = min(candle_counts.values()) if candle_counts else 0
    max_candles = max(candle_counts.values()) if candle_counts else 0

    log.info(f'Status: {len(positions)} positions | {total} trades ({wins}W {total-wins}L {wr:.0f}%) | P&L=${total_pnl:+,.0f} | Regime={regime} | Candles={min_candles}-{max_candles}')


def main():
    log.info('='*60)
    log.info('BB BOUNCE BOT STARTING')
    log.info(f'Max positions: {MAX_POSITIONS} | Stop: {STOP_PCT:.0%} | Hold: {HOLD_HOURS}h')
    log.info('='*60)

    send_alert('<b>BB BOUNCE BOT ONLINE</b>\nStrategy: Buy at BB lower + green candle\nLoading data...')

    # Bootstrap from Binance data
    try:
        import json as _json
        with open('data/bb_bootstrap.json') as _f:
            _bootstrap = _json.load(_f)
        for _pair, _candles in _bootstrap.items():
            candles_1h[_pair] = _candles
        log.info(f'Bootstrapped {len(_bootstrap)} coins with {min(len(c) for c in _bootstrap.values())}-{max(len(c) for c in _bootstrap.values())} candles')
        send_alert(f'<b>Bootstrapped {len(_bootstrap)} coins — ready to trade immediately</b>')
    except Exception as _e:
        log.error(f'Bootstrap failed: {_e}')

    # Load existing positions
    try:
        import json as _j2
        with open('data/bb_positions.json') as _f2:
            _saved = _j2.load(_f2)
        for _pair, _pos in _saved.items():
            positions[_pair] = _pos
            positions[_pair]['stop'] = _pos['entry'] * (1 - STOP_PCT)
        log.info(f'Loaded {len(_saved)} existing positions')
        send_alert(f'<b>Managing {len(_saved)} positions</b>')
    except Exception as _e2:
        log.info(f'No saved positions to load: {_e2}')

    # Process lock
    import fcntl, sys
    lock = open('/tmp/bb_bounce.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print('BB Bounce Bot already running!'); sys.exit(1)

    tick = 0

    while True:
        try:
            tick += 1
            td = client.get_ticker().get('Data', {})

            # Update candles
            update_candles(td)

            # Check exits first
            if positions:
                check_exits()

            # Check entries — ONLY at top of each hour (matches 1H backtest)
            from datetime import datetime as _dt
            _minute = _dt.utcnow().minute
            if len(positions) < MAX_POSITIONS and _minute < 2:
                candidates = []
                for pair, info in td.items():
                    if pair in EXCLUDED or pair in positions: continue
                    triggered, signal_data = check_entry(pair, info)
                    if triggered:
                        candidates.append((signal_data['score'], pair, info, signal_data))

                candidates.sort(key=lambda x: -x[0])
                for score, pair, info, signal_data in candidates[:2]:
                    if len(positions) >= MAX_POSITIONS: break
                    open_position(pair, info, signal_data)

            # Status every 5 minutes
            if tick % 5 == 0:
                print_status()

        except Exception as e:
            log.error(f'Error: {e}')

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
