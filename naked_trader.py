"""
Naked Trader — Pure price action pattern recognition.
No indicators. No RSI, BB, MACD. Just reading the candles.

Patterns detected:
1. Bullish Engulfing — red candle followed by bigger green candle (reversal)
2. Hammer — long lower wick, small body, buyers rejected the dip
3. Three White Soldiers — 3 consecutive strong green candles (momentum)
4. Inside Bar Breakout — tight consolidation then expansion
5. Higher High + Higher Low — trend structure confirmed
6. Double Bottom — same support level tested twice and held
7. Volume Explosion — volume 3x+ avg with green candle (big buyer)
8. Range Breakout — flat for X candles then breaks out

Builds its own 1-min candles from 10-second ticks.
Scans all coins. Buys on pattern. Rides. Exits on reversal pattern or stop.
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
    _lf = "logs/naked_trader.log"
except:
    _lf = "naked_trader.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()
EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD',
            'WLD/USD', 'ETH/USD', 'XRP/USD'}  # these consistently lose on patterns

# ── Config (optimized from backtest) ──
TICK_INTERVAL = 10          # poll every 10 sec
CANDLE_SECONDS = 3600       # 1-hour candles (backtested best)
MAX_POSITIONS = 4
POSITION_SIZE = 150000      # $150k per trade
HARD_STOP_PCT = 0.005       # 0.5% hard stop
TRAIL_STOP_PCT = 0.015      # 1.5% trailing stop
COOLDOWN_SECONDS = 3600     # 1 hour cooldown per coin
MIN_PATTERN_SCORE = 6       # score >= 6 (engulfing + marubozu/piercing/tweezer combos)
MAX_HOLD_CANDLES = 8        # 8 hours max hold
SKIP_VOLUME_SPIKE = 2.0     # skip if volume >= 2x avg (traps)
SKIP_TREND_PCT = 0.02       # skip if coin already up +2% (don't chase)

# ── State ──
tick_buffer = {}            # pair -> list of {t, p, b, a, v} within current candle
candles = {}                # pair -> deque of {o, h, l, c, v, t} completed candles
positions = {}              # pair -> {entry, qty, peak, candle_count, pattern, stop}
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
    if exinfo_cache:
        return exinfo_cache
    try:
        exinfo_cache = client.get_exchange_info().get('TradePairs', {})
    except Exception:
        exinfo_cache = {}
    return exinfo_cache


def update_candles(td):
    """Build 1-min OHLCV candles from ticks."""
    now = time.time()
    current_minute = int(now / CANDLE_SECONDS)

    for pair, info in td.items():
        if pair in EXCLUDED:
            continue
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        ask = float(info.get('MinAsk', 0))
        vol = float(info.get('CoinTradeValue', 0))
        if px <= 0:
            continue

        if pair not in tick_buffer:
            tick_buffer[pair] = []
            candles[pair] = deque(maxlen=100)

        tick_buffer[pair].append({'t': now, 'p': px, 'b': bid, 'a': ask, 'v': vol})

        # Check if we should close the current candle
        ticks = tick_buffer[pair]
        if not ticks:
            continue

        first_minute = int(ticks[0]['t'] / CANDLE_SECONDS)
        if current_minute > first_minute and len(ticks) >= 2:
            # Close candle from ticks in the previous minute
            candle_ticks = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) == first_minute]
            remaining = [t for t in ticks if int(t['t'] / CANDLE_SECONDS) > first_minute]

            if candle_ticks:
                candle = {
                    'o': candle_ticks[0]['p'],
                    'h': max(t['p'] for t in candle_ticks),
                    'l': min(t['p'] for t in candle_ticks),
                    'c': candle_ticks[-1]['p'],
                    'v': candle_ticks[-1]['v'],  # use last volume reading
                    't': first_minute * CANDLE_SECONDS,
                    'spread': (candle_ticks[-1]['a'] - candle_ticks[-1]['b']) / candle_ticks[-1]['b'] * 100 if candle_ticks[-1]['b'] > 0 else 0.1,
                }
                candles[pair].append(candle)

            tick_buffer[pair] = remaining


# ══════════════════════════════════════════════════════
#  PATTERN RECOGNITION — Pure Price Action
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


def detect_patterns(pair):
    """
    Scan candle history for bullish patterns.
    Returns (score, pattern_name, details).
    """
    cl = list(candles.get(pair, []))
    if len(cl) < 5:
        return 0, '', {}

    score = 0
    patterns = []
    c = cl[-1]  # current candle
    p = cl[-2]  # previous candle
    pp = cl[-3]  # 2 candles ago

    # ── 1. BULLISH ENGULFING ──
    # Previous red, current green, current body covers previous body entirely
    if is_red(p) and is_green(c):
        if c['o'] <= p['c'] and c['c'] >= p['o']:
            if body_size(c) > body_size(p) * 1.2:  # at least 20% bigger
                score += 3
                patterns.append('ENGULF')

    # ── 2. HAMMER ──
    # Small body at top, long lower wick (2x+ body), buyers rejected dip
    if candle_range(c) > 0:
        lw = lower_wick(c)
        bs = body_size(c)
        uw = upper_wick(c)
        if bs > 0 and lw > bs * 2 and uw < bs * 0.5:
            if is_green(c):  # green hammer is stronger
                score += 3
                patterns.append('HAMMER')
            else:
                score += 2
                patterns.append('HAMMER_RED')

    # ── 3. THREE WHITE SOLDIERS ──
    # 3 consecutive green candles, each closing higher
    if len(cl) >= 4:
        c1, c2, c3 = cl[-3], cl[-2], cl[-1]
        if is_green(c1) and is_green(c2) and is_green(c3):
            if c2['c'] > c1['c'] and c3['c'] > c2['c']:
                if body_size(c2) > 0 and body_size(c3) > 0:
                    score += 3
                    patterns.append('3SOLDIERS')

    # ── 4. INSIDE BAR BREAKOUT ──
    # Previous candle's range contains current candle's range (consolidation)
    # Then price breaks above previous high
    if len(cl) >= 4:
        mother = cl[-3]
        inside = cl[-2]
        breakout = cl[-1]
        if inside['h'] <= mother['h'] and inside['l'] >= mother['l']:
            # Inside bar confirmed
            if breakout['c'] > mother['h']:
                score += 3
                patterns.append('INSIDE_BREAK')

    # ── 5. HIGHER HIGH + HIGHER LOW ──
    # Structure: cl[-4].low < cl[-2].low AND cl[-3].high < cl[-1].high
    if len(cl) >= 5:
        if cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h']:
            if is_green(cl[-1]):
                score += 2
                patterns.append('HH_HL')

    # ── 6. DOUBLE BOTTOM ──
    # Two lows at similar price, second low held, now bouncing
    if len(cl) >= 10:
        lows = [(i, cl[i]['l']) for i in range(len(cl)-10, len(cl)-2)]
        lows.sort(key=lambda x: x[1])
        if len(lows) >= 2:
            low1 = lows[0]
            low2 = lows[1]
            # Within 0.3% of each other and at least 3 candles apart
            if abs(low1[0] - low2[0]) >= 3:
                diff = abs(low1[1] - low2[1]) / low1[1] * 100
                if diff < 0.3 and is_green(cl[-1]) and cl[-1]['c'] > cl[-1]['o']:
                    score += 2
                    patterns.append('DBL_BOTTOM')

    # ── 7. VOLUME EXPLOSION + GREEN ──
    # Volume 3x+ average with green candle
    if len(cl) >= 10:
        vols = [x['v'] for x in cl[-10:-1]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        if avg_vol > 0 and cl[-1]['v'] > avg_vol * 3 and is_green(cl[-1]):
            score += 2
            patterns.append('VOL_EXPLODE')

    # ── 8. RANGE BREAKOUT ──
    # Price flat for 10+ candles then breaks above range high
    if len(cl) >= 12:
        range_candles = cl[-12:-2]
        range_high = max(x['h'] for x in range_candles)
        range_low = min(x['l'] for x in range_candles)
        range_pct = (range_high - range_low) / range_high * 100 if range_high > 0 else 99
        if range_pct < 0.5:  # was flat
            if cl[-1]['c'] > range_high and is_green(cl[-1]):
                score += 3
                patterns.append('RANGE_BREAK')

    # ── BONUS: Momentum confirmation ──
    # Price rose 1%+ recently (our proven edge from real data)
    if len(cl) >= 6:
        move_5 = (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100 if cl[-6]['c'] > 0 else 0
        if move_5 >= 1.0:
            score += 2
            patterns.append(f'MOM_1%')

    # ── FILTER: spread too wide ──
    if cl[-1].get('spread', 0) > 0.2:
        score = max(0, score - 5)  # heavily penalize wide spreads

    pattern_name = '+'.join(patterns) if patterns else 'NONE'
    return score, pattern_name, {'move_5': f"{(cl[-1]['c']-cl[-6]['c'])/cl[-6]['c']*100:.2f}%" if len(cl) >= 6 else '?'}


def detect_exit_patterns(pair):
    """
    Detect bearish reversal patterns — signals to exit.
    """
    cl = list(candles.get(pair, []))
    if len(cl) < 3:
        return False, ''

    c = cl[-1]
    p = cl[-2]

    # Bearish engulfing
    if is_green(p) and is_red(c):
        if c['o'] >= p['c'] and c['c'] <= p['o']:
            if body_size(c) > body_size(p):
                return True, 'BEAR_ENGULF'

    # Shooting star (long upper wick at top)
    if candle_range(c) > 0:
        uw = upper_wick(c)
        bs = body_size(c)
        lw = lower_wick(c)
        if bs > 0 and uw > bs * 2 and lw < bs * 0.5:
            return True, 'SHOOTING_STAR'

    # Three black crows
    if len(cl) >= 4:
        c1, c2, c3 = cl[-3], cl[-2], cl[-1]
        if is_red(c1) and is_red(c2) and is_red(c3):
            if c2['c'] < c1['c'] and c3['c'] < c2['c']:
                return True, '3CROWS'

    # Lower low + lower high (structure break)
    if len(cl) >= 4:
        if cl[-1]['l'] < cl[-3]['l'] and cl[-1]['h'] < cl[-3]['h']:
            if is_red(cl[-1]):
                return True, 'LL_LH'

    return False, ''


def check_exits(td):
    """Check positions for exit signals."""
    for pair in list(positions.keys()):
        pos = positions[pair]
        info = td.get(pair, {})
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        if px <= 0 or bid <= 0:
            continue

        if px > pos['peak']:
            pos['peak'] = px

        pnl_pct = (px - pos['entry']) / pos['entry']

        # Count candles held
        cl = list(candles.get(pair, []))
        pos['candle_count'] = len(cl) - pos.get('entry_candle_idx', len(cl))

        sell = False
        reason = ''

        # 1. Hard stop
        if pnl_pct <= -HARD_STOP_PCT:
            sell = True
            reason = 'HARD_STOP'

        # 2. Bearish pattern detected
        if not sell:
            bear, bear_pattern = detect_exit_patterns(pair)
            if bear and pnl_pct > -0.005:  # don't exit on pattern if deep in loss (let stop handle)
                sell = True
                reason = f'PATTERN_{bear_pattern}'

        # 3. Profit lock — if up 1%+, trail 0.4% from peak
        if not sell and pnl_pct > 0.01:
            trail = pos['peak'] * (1 - 0.004)
            if px <= trail:
                sell = True
                reason = 'PROFIT_TRAIL'

        # 4. Breakeven stop — if up 0.3%+, move stop to entry
        if not sell and pnl_pct > 0.003 and pos.get('stop', 0) < pos['entry']:
            pos['stop'] = pos['entry'] * 1.001

        # 5. Dynamic stop
        if not sell and pos.get('stop', 0) > 0 and px <= pos['stop']:
            sell = True
            reason = 'STOP'

        # 6. Max hold
        if not sell and pos['candle_count'] > MAX_HOLD_CANDLES:
            sell = True
            reason = 'MAX_TIME'

        if sell:
            exinfo = get_exinfo()
            pi = exinfo.get(pair, {})
            pp = int(pi.get('PricePrecision', 4))

            try:
                order = client.place_order(pair, 'SELL', 'LIMIT', pos['qty'], round(bid, pp))
                det = order.get('OrderDetail', order)
                exit_px = float(det.get('FilledAverPrice', 0) or bid)
            except Exception:
                exit_px = bid

            pnl_pct_final = (exit_px - pos['entry']) / pos['entry'] * 100
            pnl_usd = (exit_px - pos['entry']) * pos['qty']
            fees = pos['entry'] * pos['qty'] * 0.0005 + exit_px * pos['qty'] * 0.0005
            pnl_usd -= fees

            trade_history.append({
                'pair': pair, 'pnl': pnl_usd, 'pnl_pct': pnl_pct_final,
                'reason': reason, 'pattern': pos.get('pattern', ''),
                'hold_candles': pos['candle_count'],
            })

            marker = 'WIN' if pnl_usd > 0 else 'LOSS'
            alert(
                f'<b>NAKED {reason} {pair}</b>\n'
                f'P&L: ${pnl_usd:+,.2f} ({pnl_pct_final:+.2f}%)\n'
                f'Entry: ${pos["entry"]:.4f} Exit: ${exit_px:.4f}\n'
                f'Pattern: {pos.get("pattern", "?")} | Held {pos["candle_count"]} candles [{marker}]'
            )

            cooldowns[pair] = time.time() + COOLDOWN_SECONDS
            del positions[pair]


def check_entries(td):
    """Scan for entry patterns."""
    if len(positions) >= MAX_POSITIONS:
        return

    candidates = []
    for pair, info in td.items():
        if pair in EXCLUDED or pair in positions:
            continue
        if pair in cooldowns and time.time() < cooldowns[pair]:
            continue

        score, pattern, details = detect_patterns(pair)
        if score >= MIN_PATTERN_SCORE:
            spread = float(info.get('MinAsk', 0)) - float(info.get('MaxBid', 0))
            bid = float(info.get('MaxBid', 0))
            spread_pct = spread / bid * 100 if bid > 0 else 99
            if spread_pct < 0.2:  # tight spread only
                candidates.append((score, pair, info, pattern, details))

    candidates.sort(key=lambda x: -x[0])

    for score, pair, info, pattern, details in candidates[:1]:  # 1 entry per tick
        if len(positions) >= MAX_POSITIONS:
            break

        ask = float(info.get('MinAsk', 0))
        if ask <= 0:
            continue

        exinfo = get_exinfo()
        pi = exinfo.get(pair, {})
        pp = int(pi.get('PricePrecision', 4))
        ap = int(pi.get('AmountPrecision', 2))

        qty = math.floor(POSITION_SIZE / ask * 10**ap) / 10**ap
        if qty <= 0:
            continue

        try:
            order = client.place_order(pair, 'BUY', 'LIMIT', qty, round(ask, pp))
            det = order.get('OrderDetail', order)
            status = (det.get('Status') or '').upper()
            filled = float(det.get('FilledQuantity', 0) or 0)
            fill_px = float(det.get('FilledAverPrice', 0) or ask)

            if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
                continue

            fill_qty = filled or qty
            cl = list(candles.get(pair, []))

            positions[pair] = {
                'entry': fill_px, 'qty': fill_qty,
                'peak': fill_px,
                'stop': fill_px * (1 - HARD_STOP_PCT),
                'time': time.time(),
                'pattern': pattern, 'score': score,
                'entry_candle_idx': len(cl),
                'candle_count': 0,
            }

            alert(
                f'<b>NAKED BUY {pair}</b>\n'
                f'Pattern: {pattern} (score={score})\n'
                f'Price: ${fill_px:.4f} | Size: ${fill_qty*fill_px:,.0f}\n'
                f'Stop: ${fill_px*(1-HARD_STOP_PCT):.4f} (-{HARD_STOP_PCT*100:.0f}%)\n'
                f'{details.get("move_5", "")}'
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
    except Exception:
        pass


def main():
    log.info('=' * 60)
    log.info('NAKED TRADER — Pure Price Action')
    log.info(f'Patterns: engulf, hammer, 3soldiers, inside_break, HH/HL, dbl_bottom, vol_explode, range_break')
    log.info(f'Min score: {MIN_PATTERN_SCORE} | Max pos: {MAX_POSITIONS} | Size: ${POSITION_SIZE:,}')
    log.info(f'Stop: {HARD_STOP_PCT*100:.0f}% | Max hold: {MAX_HOLD_CANDLES} candles')
    log.info('=' * 60)

    alert(
        '<b>NAKED TRADER ONLINE</b>\n'
        'Pure price action — no indicators\n'
        f'8 patterns | Score >= {MIN_PATTERN_SCORE} to enter\n'
        f'Exit on reversal pattern or {HARD_STOP_PCT*100:.1f}% stop'
    )

    # Bootstrap candle data from Binance (so we can trade immediately)
    log.info('Bootstrapping candle data from Binance...')
    COIN_TO_BINANCE = {
        'BTC/USD':'BTCUSDT','ETH/USD':'ETHUSDT','SOL/USD':'SOLUSDT','BNB/USD':'BNBUSDT',
        'XRP/USD':'XRPUSDT','AVAX/USD':'AVAXUSDT','LINK/USD':'LINKUSDT','FET/USD':'FETUSDT',
        'TAO/USD':'TAOUSDT','APT/USD':'APTUSDT','SUI/USD':'SUIUSDT','NEAR/USD':'NEARUSDT',
        'WIF/USD':'WIFUSDT','PENDLE/USD':'PENDLEUSDT','ADA/USD':'ADAUSDT','DOT/USD':'DOTUSDT',
        'UNI/USD':'UNIUSDT','HBAR/USD':'HBARUSDT','ARB/USD':'ARBUSDT','EIGEN/USD':'EIGENUSDT',
        'ENA/USD':'ENAUSDT','CAKE/USD':'CAKEUSDT','CFX/USD':'CFXUSDT','CRV/USD':'CRVUSDT',
        'FIL/USD':'FILUSDT','FORM/USD':'FORMUSDT','VIRTUAL/USD':'VIRTUALUSDT',
        'TRUMP/USD':'TRUMPUSDT','ONDO/USD':'ONDOUSDT','WLD/USD':'WLDUSDT',
        'AAVE/USD':'AAVEUSDT','ICP/USD':'ICPUSDT','LTC/USD':'LTCUSDT','XLM/USD':'XLMUSDT',
        'TON/USD':'TONUSDT','TRX/USD':'TRXUSDT','SEI/USD':'SEIUSDT','DOGE/USD':'DOGEUSDT',
    }
    bootstrapped = 0
    for pair, symbol in COIN_TO_BINANCE.items():
        if pair in EXCLUDED:
            continue
        try:
            url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=50'
            r = requests.get(url, timeout=5)
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                candles[pair] = deque(maxlen=200)
                for k in data[:-1]:  # skip current incomplete candle
                    candles[pair].append({
                        'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                        'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000,
                    })
                bootstrapped += 1
        except:
            pass
        time.sleep(0.1)
    log.info(f'Bootstrapped {bootstrapped} coins with ~50 hourly candles each — ready to trade')

    import fcntl, sys
    lock = open('/tmp/naked_trader.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print('Naked Trader already running!')
        sys.exit(1)

    tick = 0
    while True:
        try:
            for _retry in range(3):
                try:
                    td = client.get_ticker().get('Data', {})
                    if td:
                        break
                except Exception:
                    if _retry < 2:
                        time.sleep(2)
                    else:
                        raise

            if not td:
                time.sleep(TICK_INTERVAL)
                continue

            tick += 1

            # Build candles
            update_candles(td)

            # Check exits first
            if positions:
                check_exits(td)

            # Check entries
            check_entries(td)

            # Save state every 2 min
            if tick % 12 == 0:
                save_state()

            # Status every 5 min
            if tick % 30 == 0:
                total_pnl = sum(t['pnl'] for t in trade_history)
                wins = sum(1 for t in trade_history if t['pnl'] > 0)
                n = len(trade_history)
                wr = wins / n * 100 if n > 0 else 0
                candle_counts = {p: len(c) for p, c in candles.items() if len(c) > 0}
                min_c = min(candle_counts.values()) if candle_counts else 0
                max_c = max(candle_counts.values()) if candle_counts else 0

                pos_str = ''
                if positions:
                    parts = []
                    for p in positions:
                        px = float(td.get(p, {}).get('LastPrice', 0))
                        if px > 0:
                            pnl = (px - positions[p]['entry']) / positions[p]['entry'] * 100
                            parts.append(f'{p.split("/")[0]}({pnl:+.2f}%)')
                    pos_str = ' | ' + ', '.join(parts)

                log.info(
                    f'Status: {len(positions)} pos{pos_str} | '
                    f'{n} trades ({wins}W {wr:.0f}%) | P&L=${total_pnl:+,.0f} | '
                    f'Candles={min_c}-{max_c}'
                )

        except Exception as e:
            log.info(f'Error: {e}')

        time.sleep(TICK_INTERVAL)


if __name__ == '__main__':
    main()
