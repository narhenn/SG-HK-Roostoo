"""
SNIPER BOT — One big trade at a time. Waits for high-conviction
1h pattern, enters with max size, tight trail, fast exit.

Goal: catch 1-2% moves on $500-700k = $5-14k per trade.
Max risk: 0.5% stop = $3.5-4.5k loss.

Runs alongside naked_trader but takes priority when signal fires.
"""

import time
import math
import json
import logging
import requests
from collections import deque
from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

try:
    import os; os.makedirs("logs", exist_ok=True)
    _lf = "logs/sniper.log"
except:
    _lf = "sniper.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()

# ── Config ──
TICK_INTERVAL = 10
CANDLE_SECONDS = 3600           # 1h candles (proven edge)
MIN_SCORE = 8                   # HIGH conviction only (engulf+marubozu+HH etc)
POSITION_SIZE_PCT = 0.70        # 70% of available cash
MAX_POSITION_SIZE = 700000      # cap at $700k
MIN_POSITION_SIZE = 200000      # don't bother below $200k
HARD_STOP_PCT = 0.005           # 0.5% hard stop ($3.5k on $700k)
PROFIT_TRAIL_PCT = 0.004        # 0.4% trail once in profit
BREAKEVEN_PCT = 0.003           # move stop to entry once up 0.3%
MAX_HOLD_CANDLES = 8            # 8 hours max — sniper, not holder
COOLDOWN_AFTER_LOSS = 7200      # 2 hour cooldown after a loss (don't revenge trade)
COOLDOWN_AFTER_WIN = 1800       # 30 min cooldown after win

EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD',
            'FLOKI/USD', 'WLD/USD', 'ETH/USD', 'XRP/USD', 'BTC/USD'}

# ── State ──
tick_buffer = {}
candles = {}
position = None       # only ONE position at a time
cooldown_until = 0
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
    except:
        pass
    return 0


def update_candles(td):
    now = time.time()
    current_period = int(now / CANDLE_SECONDS)

    for pair, info in td.items():
        if pair in EXCLUDED:
            continue
        px = float(info.get('LastPrice', 0))
        if px <= 0:
            continue

        if pair not in tick_buffer:
            tick_buffer[pair] = []
            if pair not in candles:
                candles[pair] = deque(maxlen=200)

        tick_buffer[pair].append({'t': now, 'p': px})

        ticks = tick_buffer[pair]
        if not ticks:
            continue

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
                    't': first_period * CANDLE_SECONDS,
                }
                candles[pair].append(candle)

            tick_buffer[pair] = remaining


def body(c): return c['c'] - c['o']
def body_size(c): return abs(body(c))
def is_green(c): return c['c'] > c['o']
def is_red(c): return c['c'] < c['o']
def upper_wick(c): return c['h'] - max(c['o'], c['c'])
def lower_wick(c): return min(c['o'], c['c']) - c['l']
def candle_range(c): return c['h'] - c['l']


def detect_patterns(pair):
    cl = list(candles.get(pair, []))
    if len(cl) < 10:
        return 0, '', {}

    score = 0
    patterns = []
    c = cl[-1]; p = cl[-2]; pp = cl[-3] if len(cl) >= 3 else None

    avg_body = sum(body_size(x) for x in cl[-14:]) / 14

    # ── ENGULFING ──
    if is_red(p) and is_green(c):
        if c['o'] <= p['c'] and c['c'] >= p['o'] and body_size(c) > body_size(p) * 1.2:
            score += 3
            patterns.append('ENGULF')

    # ── HAMMER ──
    if candle_range(c) > 0:
        lw = lower_wick(c); bs = body_size(c); uw = upper_wick(c)
        if bs > 0 and lw > bs * 2 and uw < bs * 0.5 and is_green(c):
            score += 3
            patterns.append('HAMMER')

    # ── 3 WHITE SOLDIERS ──
    if len(cl) >= 4:
        c1, c2, c3 = cl[-3], cl[-2], cl[-1]
        if is_green(c1) and is_green(c2) and is_green(c3) and c2['c'] > c1['c'] and c3['c'] > c2['c']:
            if body_size(c2) > 0 and body_size(c3) > 0:
                score += 3
                patterns.append('3SOLDIERS')

    # ── INSIDE BAR BREAKOUT ──
    if len(cl) >= 4:
        mother = cl[-3]; inside = cl[-2]; breakout = cl[-1]
        if inside['h'] <= mother['h'] and inside['l'] >= mother['l'] and breakout['c'] > mother['h']:
            score += 3
            patterns.append('INSIDE_BRK')

    # ── HH + HL ──
    if len(cl) >= 5:
        if cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h'] and is_green(cl[-1]):
            score += 2
            patterns.append('HH_HL')

    # ── MARUBOZU ──
    if is_green(c) and body_size(c) > avg_body * 2:
        uw2 = upper_wick(c); lw2 = lower_wick(c)
        if uw2 < body_size(c) * 0.1 and lw2 < body_size(c) * 0.1:
            score += 3
            patterns.append('MARUBOZU')

    # ── MORNING STAR ──
    if len(cl) >= 4 and pp:
        b1 = body_size(cl[-3]); b2 = body_size(cl[-2]); b3 = body_size(cl[-1])
        if is_red(cl[-3]) and b1 > avg_body and b2 < b1 * 0.3 and is_green(cl[-1]) and b3 > avg_body:
            if cl[-1]['c'] > (cl[-3]['o'] + cl[-3]['c']) / 2:
                score += 4
                patterns.append('MORN_STAR')

    # ── PIERCING ──
    if is_red(p) and is_green(c):
        mid = (p['o'] + p['c']) / 2
        if c['o'] < p['c'] and c['c'] > mid and c['c'] < p['o']:
            score += 3
            patterns.append('PIERCING')

    # ── TWEEZER BOTTOM ──
    if len(cl) >= 3:
        if candle_range(c) > 0:
            avg_range = sum(candle_range(x) for x in cl[-14:]) / 14
            if avg_range > 0 and abs(c['l'] - p['l']) / avg_range < 0.05:
                if is_red(p) and is_green(c):
                    score += 3
                    patterns.append('TWZR_BOT')

    # ── KICKER ──
    if is_red(p) and is_green(c) and c['o'] > p['o'] and body_size(c) > avg_body * 1.5:
        score += 4
        patterns.append('KICKER')

    # ── DOUBLE BOTTOM ──
    if len(cl) >= 20:
        lows = [x['l'] for x in cl[-20:]]
        sorted_idx = sorted(range(len(lows)), key=lambda i: lows[i])
        i1, i2 = sorted_idx[0], sorted_idx[1]
        if abs(i1 - i2) >= 3 and abs(lows[i1] - lows[i2]) / lows[i1] < 0.005:
            if is_green(c):
                score += 3
                patterns.append('DBL_BOT')

    # ── FILTER: spread/trend ──
    if len(cl) >= 6:
        move_5 = (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100
        if move_5 > 3.0:
            score -= 3  # don't chase
            patterns.append('CHASING')
        if move_5 < -5.0:
            score -= 2  # falling knife
            patterns.append('FALLING')

    pattern_name = '+'.join(patterns) if patterns else 'NONE'
    return score, pattern_name, {}


def check_exit(td):
    global position, cooldown_until
    if not position:
        return

    pair = position['pair']
    info = td.get(pair, {})
    px = float(info.get('LastPrice', 0))
    bid = float(info.get('MaxBid', 0))
    if px <= 0 or bid <= 0:
        return

    if px > position['peak']:
        position['peak'] = px

    pnl_pct = (px - position['entry']) / position['entry']
    hold = int((time.time() - position['time']) / CANDLE_SECONDS)

    sell = False
    reason = ''

    # 1. Hard stop
    if pnl_pct <= -HARD_STOP_PCT:
        sell = True
        reason = 'HARD_STOP'

    # 2. Profit trail — once up 0.5%+, trail 0.4% from peak
    if not sell and pnl_pct > 0.005:
        trail = position['peak'] * (1 - PROFIT_TRAIL_PCT)
        if px <= trail:
            sell = True
            reason = 'PROFIT_TRAIL'

    # 3. Breakeven stop — once up 0.3%, move stop to entry+0.1%
    if not sell and pnl_pct > BREAKEVEN_PCT and position.get('stop', 0) < position['entry']:
        position['stop'] = position['entry'] * 1.001
        log.info(f'{pair} stop moved to breakeven ${position["stop"]:.4f}')

    # 4. Dynamic stop
    if not sell and position.get('stop', 0) > 0 and px <= position['stop']:
        sell = True
        reason = 'STOP'

    # 5. Max hold
    if not sell and hold >= MAX_HOLD_CANDLES:
        sell = True
        reason = 'MAX_TIME'

    if sell:
        exinfo = get_exinfo()
        pi = exinfo.get(pair, {})
        pp = int(pi.get('PricePrecision', 4))

        try:
            order = client.place_order(pair, 'SELL', 'MARKET', position['qty'], round(bid, pp))
            det = order.get('OrderDetail', order)
            exit_px = float(det.get('FilledAverPrice', 0) or bid)
        except:
            try:
                order = client.place_order(pair, 'SELL', 'LIMIT', position['qty'], round(bid, pp))
                det = order.get('OrderDetail', order)
                exit_px = float(det.get('FilledAverPrice', 0) or bid)
            except:
                exit_px = bid

        pnl_usd = (exit_px - position['entry']) * position['qty']
        fee = position['entry'] * position['qty'] * 0.001 + exit_px * position['qty'] * 0.001
        pnl_usd -= fee

        trade_history.append({'pair': pair, 'pnl': pnl_usd, 'reason': reason})
        marker = 'WIN' if pnl_usd > 0 else 'LOSS'

        alert(
            f'<b>SNIPER {reason} {pair}</b>\n'
            f'P&L: ${pnl_usd:+,.2f} ({(exit_px-position["entry"])/position["entry"]*100:+.2f}%)\n'
            f'Entry: ${position["entry"]:.4f} → Exit: ${exit_px:.4f}\n'
            f'Size: ${position["qty"]*position["entry"]:,.0f} | Held {hold}h [{marker}]'
        )

        cooldown_until = time.time() + (COOLDOWN_AFTER_LOSS if pnl_usd < 0 else COOLDOWN_AFTER_WIN)
        position = None


def check_entry(td):
    global position, cooldown_until
    if position:
        return
    if time.time() < cooldown_until:
        return

    # Check cash
    cash = get_cash()
    if cash < MIN_POSITION_SIZE:
        return

    size = min(cash * POSITION_SIZE_PCT, MAX_POSITION_SIZE)
    if size < MIN_POSITION_SIZE:
        return

    # Scan all coins for the BEST signal
    best = None
    for pair, info in td.items():
        if pair in EXCLUDED:
            continue

        score, pattern, details = detect_patterns(pair)
        if score >= MIN_SCORE:
            spread = float(info.get('MinAsk', 0)) - float(info.get('MaxBid', 0))
            bid = float(info.get('MaxBid', 0))
            if bid > 0:
                spread_pct = spread / bid * 100
                if spread_pct < 0.15:  # tight spread only for big size
                    if not best or score > best[0]:
                        best = (score, pair, info, pattern)

    if not best:
        return

    score, pair, info, pattern = best
    ask = float(info.get('MinAsk', 0))
    if ask <= 0:
        return

    exinfo = get_exinfo()
    pi = exinfo.get(pair, {})
    pp = int(pi.get('PricePrecision', 4))
    ap = int(pi.get('AmountPrecision', 2))

    qty = math.floor(size / ask * 10**ap) / 10**ap
    if qty <= 0:
        return

    try:
        order = client.place_order(pair, 'BUY', 'MARKET', qty, round(ask, pp))
        det = order.get('OrderDetail', order)
        status = (det.get('Status') or '').upper()
        filled = float(det.get('FilledQuantity', 0) or 0)
        fill_px = float(det.get('FilledAverPrice', 0) or ask)

        if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
            return

        fill_qty = filled or qty
        position = {
            'pair': pair,
            'entry': fill_px,
            'qty': fill_qty,
            'peak': fill_px,
            'stop': fill_px * (1 - HARD_STOP_PCT),
            'time': time.time(),
            'pattern': pattern,
            'score': score,
        }

        alert(
            f'<b>🎯 SNIPER BUY {pair}</b>\n'
            f'Pattern: {pattern} (score={score})\n'
            f'Price: ${fill_px:.4f} | Size: ${fill_qty*fill_px:,.0f}\n'
            f'Stop: ${fill_px*(1-HARD_STOP_PCT):.4f} | Target: +1-2%\n'
            f'Cash remaining: ${cash - fill_qty*fill_px:,.0f}'
        )

    except Exception as e:
        log.info(f'Sniper buy {pair} failed: {e}')


def main():
    log.info('=' * 60)
    log.info('SNIPER BOT — One big trade, high conviction')
    log.info(f'Min score: {MIN_SCORE} | Size: {POSITION_SIZE_PCT*100:.0f}% of cash (max ${MAX_POSITION_SIZE:,})')
    log.info(f'Stop: {HARD_STOP_PCT*100:.1f}% | Trail: {PROFIT_TRAIL_PCT*100:.1f}% | Max hold: {MAX_HOLD_CANDLES}h')
    log.info('=' * 60)

    alert(
        '<b>🎯 SNIPER BOT ONLINE</b>\n'
        f'Waiting for score >= {MIN_SCORE} pattern on 1h candles\n'
        f'Size: up to ${MAX_POSITION_SIZE:,} per trade\n'
        f'Stop: {HARD_STOP_PCT*100:.1f}% | Trail: {PROFIT_TRAIL_PCT*100:.1f}%\n'
        'One trade at a time. Patience is profit.'
    )

    # Bootstrap from Binance
    log.info('Bootstrapping candles from Binance...')
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
    }
    bootstrapped = 0
    for pair, symbol in COIN_TO_BINANCE.items():
        try:
            import urllib.request
            url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=50'
            r = requests.get(url, timeout=5)
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                candles[pair] = deque(maxlen=200)
                for k in data[:-1]:
                    candles[pair].append({
                        'o': float(k[1]), 'h': float(k[2]),
                        'l': float(k[3]), 'c': float(k[4]),
                        't': int(k[0]) / 1000,
                    })
                bootstrapped += 1
        except:
            pass
        time.sleep(0.1)
    log.info(f'Bootstrapped {bootstrapped} coins — scanning for signals')

    # Lock file
    import fcntl, sys
    lock = open('/tmp/sniper_bot.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print('Sniper Bot already running!')
        sys.exit(1)

    tick = 0
    while True:
        try:
            td = client.get_ticker().get('Data', {})
            if not td:
                time.sleep(TICK_INTERVAL)
                continue

            tick += 1
            update_candles(td)

            if position:
                check_exit(td)
            else:
                check_entry(td)

            # Status every 5 min
            if tick % 30 == 0:
                total_pnl = sum(t['pnl'] for t in trade_history)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history)
                wr = wins / n * 100 if n > 0 else 0
                cash = get_cash()

                pos_str = ''
                if position:
                    px = float(td.get(position['pair'], {}).get('LastPrice', 0))
                    if px > 0:
                        pnl = (px - position['entry']) / position['entry'] * 100
                        pos_str = f' | {position["pair"].split("/")[0]}({pnl:+.2f}%) ${position["qty"]*position["entry"]:,.0f}'

                cd_left = max(0, cooldown_until - time.time())
                cd_str = f' | cooldown {cd_left/60:.0f}min' if cd_left > 0 else ''

                log.info(
                    f'${cash:,.0f} cash{pos_str} | '
                    f'{n} trades ({wins}W {wr:.0f}%) | P&L=${total_pnl:+,.0f}{cd_str}'
                )

        except Exception as e:
            log.info(f'Error: {e}')

        time.sleep(TICK_INTERVAL)


if __name__ == '__main__':
    main()
