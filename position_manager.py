"""
Position Manager Bot — Buys AVAX + FET, manages with trailing stops.
- AVAX: $400k (ETF + institutional catalyst)
- FET: $150k (AI narrative momentum)
- 2% trailing stop on both
- Breakeven stop when +1%
- Telegram alerts on every action
- Checks every 30 seconds
"""

import time
import math
import json
import logging
import sys
import requests

sys.path.insert(0, '/Users/narhen/Desktop/quant-hackathon')
from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.FileHandler('logs/position_manager.log'), logging.StreamHandler()])
log = logging.getLogger('PosMgr')

client = RoostooClient()

# ── CONFIG ──
POSITIONS_TO_OPEN = {
    'AVAX/USD': {'size': 400000, 'price_prec': 2, 'amt_prec': 2},
    'FET/USD':  {'size': 150000, 'price_prec': 4, 'amt_prec': 1},
}

TRAIL_STOP_PCT = 0.02       # 2% trailing stop
BREAKEVEN_TRIGGER = 0.01    # move stop to breakeven at +1%
MAX_HOLD_HOURS = 48         # max hold 48h (competition ends Apr 14)
CHECK_INTERVAL = 30         # check every 30 seconds

STATE_FILE = 'data/position_manager_state.json'

# ── STATE ──
positions = {}  # pair -> {entry, qty, size, peak, stop, entry_time}


def send_alert(msg):
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                      json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
                      timeout=5)
    except:
        pass


def save_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        log.error(f'Failed to save state: {e}')


def load_state():
    global positions
    try:
        with open(STATE_FILE) as f:
            positions = json.load(f)
        log.info(f'Loaded {len(positions)} positions from state')
    except:
        positions = {}


def buy_positions():
    """Buy AVAX and FET."""
    tickers = client.get_ticker().get('Data', {})
    ei = client.get_exchange_info()
    trade_pairs = ei.get('TradePairs', ei)

    for pair, cfg in POSITIONS_TO_OPEN.items():
        if pair in positions:
            log.info(f'{pair} already held, skipping')
            continue

        info = tickers.get(pair, {})
        ask = float(info.get('MinAsk', 0))
        if ask <= 0:
            log.error(f'{pair}: no ask price')
            continue

        pair_info = trade_pairs.get(pair, {})
        price_prec = int(pair_info.get('PricePrecision', cfg['price_prec']))
        amt_prec = int(pair_info.get('AmountPrecision', cfg['amt_prec']))

        # Calculate quantity
        amt_mult = 10 ** amt_prec
        qty = math.floor(cfg['size'] / ask * amt_mult) / amt_mult
        if qty <= 0:
            log.error(f'{pair}: qty=0')
            continue

        limit_price = round(ask, price_prec)

        log.info(f'BUYING {pair}: {qty} @ ${limit_price} = ${qty * limit_price:,.0f}')

        try:
            order = client.place_order(pair, 'BUY', 'LIMIT', qty, limit_price)

            # Check top-level Success FIRST
            if not order.get('Success', False):
                err = order.get('ErrMsg', 'unknown error')
                log.error(f'{pair}: order rejected: {err}')
                send_alert(f'❌ {pair} ORDER REJECTED: {err}')
                continue

            det = order.get('OrderDetail', order)
            status = (det.get('Status') or '').upper()
            filled = float(det.get('FilledQuantity', 0) or 0)
            avg_px = float(det.get('FilledAverPrice', 0) or 0)

            if status not in ('FILLED', 'COMPLETED', '') and filled <= 0:
                log.error(f'{pair}: order not filled. Status={status} Response={det}')
                send_alert(f'❌ {pair} ORDER FAILED: {status}')
                continue

            fill_price = avg_px if avg_px > 0 else limit_price
            fill_qty = filled if filled > 0 else qty

            positions[pair] = {
                'entry': fill_price,
                'qty': fill_qty,
                'size': fill_qty * fill_price,
                'peak': fill_price,
                'stop': fill_price * (1 - TRAIL_STOP_PCT),
                'entry_time': time.time(),
                'price_prec': price_prec,
                'amt_prec': amt_prec,
            }

            save_state()
            log.info(f'FILLED {pair}: {fill_qty} @ ${fill_price:.4f} = ${fill_qty * fill_price:,.0f}')
            send_alert(
                f'🟢 <b>BOUGHT {pair}</b>\n'
                f'Price: ${fill_price:.4f}\n'
                f'Size: ${fill_qty * fill_price:,.0f}\n'
                f'Stop: ${fill_price * (1 - TRAIL_STOP_PCT):.4f} (-{TRAIL_STOP_PCT:.0%})\n'
                f'Target: ride momentum with trailing stop'
            )

        except Exception as e:
            log.error(f'{pair}: buy failed: {e}')
            send_alert(f'❌ {pair} BUY ERROR: {e}')


def check_positions():
    """Check all positions for trailing stop, breakeven, time exit."""
    if not positions:
        return

    tickers = client.get_ticker().get('Data', {})
    to_sell = []

    for pair, pos in list(positions.items()):
        info = tickers.get(pair, {})
        px = float(info.get('LastPrice', 0))
        bid = float(info.get('MaxBid', 0))
        if px <= 0 or bid <= 0:
            continue

        entry = pos['entry']
        pnl_pct = (px - entry) / entry
        pnl_usd = (px - entry) * pos['qty']

        # Update peak
        if px > pos['peak']:
            pos['peak'] = px
            # Update trailing stop
            new_stop = px * (1 - TRAIL_STOP_PCT)
            if new_stop > pos['stop']:
                pos['stop'] = new_stop
                log.info(f'{pair}: new peak ${px:.4f}, stop raised to ${new_stop:.4f}')

        # Breakeven stop at +1%
        if pnl_pct >= BREAKEVEN_TRIGGER and pos['stop'] < entry * 1.001:
            pos['stop'] = entry * 1.001
            log.info(f'{pair}: breakeven stop activated at ${pos["stop"]:.4f}')
            send_alert(f'🔒 <b>{pair} BREAKEVEN LOCKED</b>\nPrice: ${px:.4f} ({pnl_pct:+.2%})\nStop moved to ${pos["stop"]:.4f}')

        # Check stop hit
        if px <= pos['stop']:
            to_sell.append((pair, bid, 'STOP'))
        # Time exit
        elif time.time() - pos['entry_time'] > MAX_HOLD_HOURS * 3600:
            to_sell.append((pair, bid, 'TIME'))

        save_state()

    for pair, bid, reason in to_sell:
        sell_position(pair, bid, reason)


def sell_position(pair, bid_price, reason):
    """Sell a position."""
    pos = positions.get(pair)
    if not pos:
        return

    qty = pos['qty']
    price_prec = pos.get('price_prec', 4)
    limit_price = round(bid_price, price_prec)

    log.info(f'SELLING {pair}: {qty} @ ${limit_price} reason={reason}')

    try:
        order = client.place_order(pair, 'SELL', 'LIMIT', qty, limit_price)

        if not order.get('Success', False):
            err = order.get('ErrMsg', 'unknown error')
            log.error(f'{pair}: sell rejected: {err}')
            send_alert(f'❌ {pair} SELL REJECTED: {err}')
            return

        det = order.get('OrderDetail', order)
        status = (det.get('Status') or '').upper()
        filled = float(det.get('FilledQuantity', 0) or 0)
        avg_px = float(det.get('FilledAverPrice', 0) or 0)

        exit_price = avg_px if avg_px > 0 else limit_price
        pnl = (exit_price - pos['entry']) * pos['qty']
        pnl_pct = (exit_price - pos['entry']) / pos['entry'] * 100
        hold_hours = (time.time() - pos['entry_time']) / 3600

        del positions[pair]
        save_state()

        emoji = '🟢' if pnl > 0 else '🔴'
        log.info(f'SOLD {pair}: P&L=${pnl:+,.2f} ({pnl_pct:+.2f}%) reason={reason} hold={hold_hours:.1f}h')
        send_alert(
            f'{emoji} <b>SOLD {pair} ({reason})</b>\n'
            f'Entry: ${pos["entry"]:.4f} → Exit: ${exit_price:.4f}\n'
            f'P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%)\n'
            f'Hold: {hold_hours:.1f}h\n'
            f'Peak: ${pos["peak"]:.4f}'
        )

    except Exception as e:
        log.error(f'{pair}: sell failed: {e}')
        send_alert(f'❌ {pair} SELL ERROR: {e}')


def print_status():
    """Log current status."""
    if not positions:
        log.info('No positions open')
        return

    tickers = client.get_ticker().get('Data', {})
    total_pnl = 0
    for pair, pos in positions.items():
        px = float(tickers.get(pair, {}).get('LastPrice', 0))
        if px <= 0:
            continue
        pnl = (px - pos['entry']) * pos['qty']
        pnl_pct = (px - pos['entry']) / pos['entry'] * 100
        hold_h = (time.time() - pos['entry_time']) / 3600
        total_pnl += pnl
        log.info(f'  {pair}: ${px:.4f} ({pnl_pct:+.2f}%) P&L=${pnl:+,.0f} stop=${pos["stop"]:.4f} peak=${pos["peak"]:.4f} hold={hold_h:.1f}h')

    log.info(f'  TOTAL P&L: ${total_pnl:+,.0f}')


def main():
    log.info('=' * 60)
    log.info('POSITION MANAGER STARTING')
    log.info(f'AVAX: $400k | FET: $150k | Stop: {TRAIL_STOP_PCT:.0%} trailing')
    log.info('=' * 60)

    send_alert(
        '<b>🤖 POSITION MANAGER ONLINE</b>\n'
        'Buying: AVAX $400k + FET $150k\n'
        f'Trailing stop: {TRAIL_STOP_PCT:.0%}\n'
        'Breakeven lock at +1%\n'
        'Checking every 30s'
    )

    # Load any saved state
    load_state()

    # Buy if not already holding
    buy_positions()

    # Process lock
    import fcntl
    lock = open('/tmp/position_manager.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log.error('Position Manager already running!')
        sys.exit(1)

    tick = 0
    while True:
        try:
            tick += 1
            check_positions()

            # Status every 2 minutes
            if tick % 4 == 0:
                print_status()

        except Exception as e:
            log.error(f'Error: {e}')
            if tick % 20 == 0:
                send_alert(f'⚠️ Position Manager error: {e}')

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
