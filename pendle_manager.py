#!/usr/bin/env python3
"""
PENDLE MANAGER — tiny trailing stop + take profit bot
════════════════════════════════════════════════════════════════════
Manages the PENDLE orphan position independently of V3.2.

Logic:
  - Entry reference: $1.08 (from Roostoo portfolio)
  - Hard stop: -3% below peak
  - T1:  +7%  → sell 30%, move stop to +1%
  - T2:  +15% → sell 30%, trail at -4%
  - Runner: 40% trails at -5%
  - Max hold: until hackathon close
  - Auto-exit: at Apr 14 11:30 UTC (30 min before close)

Runs autonomously. Telegram alerts on every action.

USAGE:
  nohup python3 pendle_manager.py > logs/pendle.log 2>&1 &
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient
from naked_v3_2 import binance_klines


# ═══ CONFIG ═══
COIN = 'PENDLE'
ENTRY_REF = 1.08        # from Roostoo portfolio avg cost
HARD_STOP_PCT = 0.03    # 3% below peak
T1_PCT = 0.07           # +7% take first partial
T1_SIZE = 0.30          # sell 30%
T2_PCT = 0.15           # +15% take second partial
T2_SIZE = 0.30          # sell 30%
RUNNER_TRAIL_PCT = 0.05 # 5% trail on runner

# Hackathon auto-exit
HACKATHON_END = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
AUTO_EXIT_AT = HACKATHON_END - timedelta(minutes=30)

STATE_FILE = 'data/pendle_manager_state.json'


def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg},
            timeout=4,
        )
    except Exception:
        pass


def log(msg):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as fp:
        json.dump(state, fp, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def get_pendle_qty(client):
    bal = client.get_balance()
    wallet = bal.get('SpotWallet', {})
    info = wallet.get(COIN, {})
    return float(info.get('Free', 0)) + float(info.get('Lock', 0))


def get_price(coin):
    bars = binance_klines(coin, '1m', 2)
    return bars[-1]['c'] if bars else None


def sell_pendle(client, qty, dry=False):
    if dry:
        px = get_price(COIN)
        log(f"DRY SELL {COIN} qty={qty} @ ${px}")
        return {'ok': True, 'price': px}
    try:
        r = client.place_order(f"{COIN}/USD", 'SELL', 'MARKET', qty)
        if not r.get('Success', False):
            return {'ok': False, 'err': r.get('ErrMsg', 'unknown')}
        return {
            'ok': True,
            'price': float(r.get('FilledAverPrice', 0)) or get_price(COIN),
            'qty': float(r.get('FilledQuantity', qty)),
        }
    except Exception as e:
        return {'ok': False, 'err': str(e)}


def main():
    log("═" * 60)
    log("PENDLE MANAGER starting")
    log("═" * 60)

    client = RoostooClient()

    # Initial state check
    state = load_state()
    current_qty = get_pendle_qty(client)
    log(f"PENDLE qty on Roostoo: {current_qty:,.4f}")

    if current_qty < 100:
        log("⚠️  No meaningful PENDLE position to manage. Exiting.")
        tg("⚠️ PENDLE manager: no position to manage, exiting")
        return

    if state is None:
        # Fresh start
        current_px = get_price(COIN)
        state = {
            'entry_ref': ENTRY_REF,
            'qty_initial': current_qty,
            'qty_remaining': current_qty,
            'peak': current_px,
            'stop': min(current_px * (1 - HARD_STOP_PCT), ENTRY_REF * (1 - HARD_STOP_PCT)),
            't1_done': False,
            't2_done': False,
            'closes': [],
            'started_at': int(time.time()),
        }
        save_state(state)
        log(f"Fresh state: entry_ref=${ENTRY_REF} peak=${current_px} stop=${state['stop']:.4f}")
        tg(f"🟢 PENDLE MANAGER online\n"
           f"qty: {current_qty:,.0f}\n"
           f"entry_ref: ${ENTRY_REF}\n"
           f"current: ${current_px}\n"
           f"stop: ${state['stop']:.4f}\n"
           f"T1 @ +7% / T2 @ +15% / runner trail 5%")

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Auto-exit 30 min before hackathon close
            if now >= AUTO_EXIT_AT and state['qty_remaining'] > 0:
                log(f"⏰ AUTO-EXIT (hackathon close approaching)")
                r = sell_pendle(client, state['qty_remaining'])
                if r['ok']:
                    state['closes'].append({
                        'qty': state['qty_remaining'],
                        'px': r['price'],
                        'reason': 'AUTO_EXIT',
                    })
                    state['qty_remaining'] = 0
                    tg(f"⏰ AUTO-EXITED PENDLE @ ${r['price']:.4f}")
                save_state(state)
                break

            # Fetch current price
            px = get_price(COIN)
            if not px:
                time.sleep(60)
                continue

            # Update peak
            if px > state['peak']:
                state['peak'] = px

            # Check exit conditions
            closed = False
            reason = None
            exit_px = px

            # Hard stop (trailing)
            if px <= state['stop']:
                closed = True
                reason = 'STOP'
                exit_px = state['stop']

            # T1 take profit @ +7% from entry_ref
            if not closed and not state['t1_done']:
                t1p = state['entry_ref'] * (1 + T1_PCT)
                if px >= t1p:
                    sell_qty = min(state['qty_initial'] * T1_SIZE, state['qty_remaining'])
                    r = sell_pendle(client, sell_qty)
                    if r['ok']:
                        state['closes'].append({
                            'qty': sell_qty, 'px': r['price'], 'reason': 'T1',
                        })
                        state['qty_remaining'] -= sell_qty
                        state['t1_done'] = True
                        state['stop'] = max(state['stop'], state['entry_ref'] * 1.01)
                        log(f"✅ T1 hit @ ${r['price']}, new stop=${state['stop']:.4f}")
                        tg(f"✅ PENDLE T1 +7% hit @ ${r['price']:.4f}\n"
                           f"sold {sell_qty:,.0f}, stop→${state['stop']:.4f}")

            # T2 @ +15% from entry_ref
            if not closed and state['t1_done'] and not state['t2_done']:
                t2p = state['entry_ref'] * (1 + T2_PCT)
                if px >= t2p:
                    sell_qty = min(state['qty_initial'] * T2_SIZE, state['qty_remaining'])
                    r = sell_pendle(client, sell_qty)
                    if r['ok']:
                        state['closes'].append({
                            'qty': sell_qty, 'px': r['price'], 'reason': 'T2',
                        })
                        state['qty_remaining'] -= sell_qty
                        state['t2_done'] = True
                        log(f"✅ T2 hit @ ${r['price']}")
                        tg(f"✅ PENDLE T2 +15% hit @ ${r['price']:.4f}")

            # Trailing stop after T1
            if not closed and state['t1_done']:
                new_stop = state['peak'] * (1 - RUNNER_TRAIL_PCT)
                if new_stop > state['stop']:
                    state['stop'] = new_stop

            # Final exit check (after trail update)
            if not closed and px <= state['stop']:
                closed = True
                reason = 'TRAIL'
                exit_px = state['stop']

            if closed and state['qty_remaining'] > 1e-6:
                r = sell_pendle(client, state['qty_remaining'])
                if r['ok']:
                    state['closes'].append({
                        'qty': state['qty_remaining'],
                        'px': r['price'],
                        'reason': reason,
                    })
                    state['qty_remaining'] = 0
                    log(f"🏁 PENDLE closed @ ${r['price']} reason={reason}")
                    total_pnl = sum(c['px'] * c['qty'] for c in state['closes']) - state['entry_ref'] * state['qty_initial']
                    tg(f"🏁 PENDLE CLOSED\n"
                       f"reason: {reason}\n"
                       f"exit: ${r['price']:.4f}\n"
                       f"estimated pnl: ${total_pnl:+,.2f}")
                save_state(state)
                break

            save_state(state)

            # Status log every 5 minutes
            if int(time.time()) % 300 < 60:
                log(f"PENDLE ${px:.4f} (peak ${state['peak']:.4f}, stop ${state['stop']:.4f})")

        except Exception as e:
            log(f"loop err: {e}")
            traceback.print_exc()

        time.sleep(60)

    log("PENDLE MANAGER shutting down")


if __name__ == '__main__':
    main()
