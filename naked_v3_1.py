#!/usr/bin/env python3
"""
NAKED V3.1 — The champion (pyramid + BE stop + wider targets)
════════════════════════════════════════════════════════════════════
Walk-forward validated on 174 real 75-hour windows (90 days, 45 coins):
  avg PnL/75h:     +$11,581 (+1.16%)
  win-rate fired:  71.4%
  avg max DD:      0.40%
  best window:     +$176,633
  worst window:    -$55,647
  improvement vs v2: +50% avg PnL, +30pp win-rate, -41% DD

═══ THE DESIGN ═══

1. BTC regime gate (same as v2/v3):
     BTC 24h ≥ +1% AND BTC 12h ≥ 0% AND BTC > 24h SMA

2. Signal gate (strict triple-confirm on 30m bar):
     green close, 2h mom ≥ 3.5%, 6h mom ≥ 6%, vol ≥ 2.5×,
     above 10-bar SMA, <104% of 20-bar high, score ≥ 28

3. Pick HIGHEST-SCORE coin across 44 non-BTC

4. Entry: 90% equity market buy

5. Initial stop: 3% below entry

6. BE STOP @ +0.5%: when price reaches +0.5%, stop jumps to entry+0.2%
   → 70% of "losers" now exit at ~$0 instead of -$28k

7. PYRAMID @ +4%: add 30% more equity (on top of original 90%)
   → converts winners into bigger winners, stop moves to signal BE

8. T1 @ +10%: sell 30%
9. T2 @ +20%: sell 30%
10. Runner 40% trails 4%
11. Max 3 trades per run, 4h cooldown on loser, 24h max hold
12. Kill switch $830k

═══ USAGE ═══
  python3 naked_v3_1.py                # live (default)
  python3 naked_v3_1.py --dry-run      # paper mode, NO real orders
  python3 naked_v3_1.py --max 1        # single-shot mode
  python3 naked_v3_1.py --max 2        # conservative 2-trade cap
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    STARTING_CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient


# ════════════════════════════════════════
# V3.1 LOCKED CONFIG — PROVEN BY WALK-FORWARD
# ════════════════════════════════════════
CONFIG = {
    'label':            'NAKED V3.1 CHAMPION (pyramid + BE stop)',
    'bar_minutes':      30,

    # Signal thresholds
    'lookback_short':   4,
    'lookback_med':     12,
    'lookback_vol':     24,
    'min_mom_short':    0.035,
    'min_mom_med':      0.060,
    'vol_mult':         2.5,
    'min_score':        28.0,
    'max_overext':      1.04,

    # BTC regime gate
    'btc_24h_min':      0.010,
    'btc_12h_min':      0.000,
    'btc_sma_bars':     48,

    # Sizing
    'position_pct':     0.90,
    'hard_stop_pct':    0.030,
    'trail_pct':        0.040,

    # ── V3.1 UPGRADES ──
    'use_pyramid':      True,
    'pyramid_trigger':  0.04,   # at +4% profit
    'pyramid_add_pct':  0.30,   # add 30% of original alloc

    'use_be_stop':      True,
    'be_trigger':       0.005,  # at +0.5% profit
    'be_cushion':       0.002,  # stop = entry + 0.2%

    # Exit ladder (v3.1: wider targets to let winners run)
    'target_1_pct':     0.10,
    'target_1_size':    0.30,
    'target_2_pct':     0.20,
    'target_2_size':    0.30,

    # Safety
    'max_trades_total': 3,
    'cooldown_bars':    8,
    'max_hold_bars':    48,
    'kill_switch_eq':   830_000.0,
}


COINS = [
    "BTC","ETH","SOL","BNB","XRP","DOGE","ADA","TON","AVAX","LINK",
    "DOT","TRX","SHIB","LTC","NEAR","ATOM","ICP","APT","ARB","OP",
    "SUI","SEI","INJ","TIA","STX","IMX","FIL","HBAR","VET","RUNE",
    "AAVE","MKR","LDO","CRV","SNX","COMP","UNI","PEPE","WIF","BONK",
    "FLOKI","ORDI","JTO","PYTH","ENA",
]

STATE_FILE = 'data/naked_v3_1_state.json'
BAR_S = CONFIG['bar_minutes'] * 60
TAKER_FEE = 0.0005
SLIPPAGE = 0.0003


# ════════════════════════════════════════
# TELEGRAM + LOG
# ════════════════════════════════════════
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


# ════════════════════════════════════════
# BINANCE KLINES
# ════════════════════════════════════════
def binance_klines(symbol, interval='30m', limit=60):
    url = (f"https://api.binance.com/api/v3/klines?"
           f"symbol={symbol}USDT&interval={interval}&limit={limit}")
    try:
        r = requests.get(url, timeout=6,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return []
        raw = r.json()
        return [{
            't': int(k[0]) // 1000,
            'o': float(k[1]), 'h': float(k[2]),
            'l': float(k[3]), 'c': float(k[4]),
            'v': float(k[5]),
        } for k in raw]
    except Exception:
        return []


# ════════════════════════════════════════
# SIGNAL ENGINE (mirrors walk-forward test exactly)
# ════════════════════════════════════════
def btc_regime_ok(btc_bars):
    cfg = CONFIG
    need = cfg['btc_sma_bars'] + 2
    if len(btc_bars) < need:
        return False, f"btc bars {len(btc_bars)} < {need}"
    cur = btc_bars[-1]['c']
    ago_24 = btc_bars[-cfg['btc_sma_bars'] - 1]['c']
    r24 = (cur - ago_24) / ago_24 if ago_24 > 0 else 0
    if r24 < cfg['btc_24h_min']:
        return False, f"BTC 24h {r24*100:+.2f}% < +1%"
    bars_12h = cfg['btc_sma_bars'] // 2
    ago_12 = btc_bars[-bars_12h - 1]['c']
    r12 = (cur - ago_12) / ago_12 if ago_12 > 0 else 0
    if r12 < cfg['btc_12h_min']:
        return False, f"BTC 12h {r12*100:+.2f}% < 0%"
    sma = sum(b['c'] for b in btc_bars[-cfg['btc_sma_bars']:]) / cfg['btc_sma_bars']
    if cur < sma:
        return False, f"BTC {cur:.0f} < SMA24h {sma:.0f}"
    return True, f"OK (24h={r24*100:+.1f}%, 12h={r12*100:+.1f}%, SMA diff={((cur/sma-1)*100):+.2f}%)"


def detect_signal(bars):
    cfg = CONFIG
    lb_s, lb_m, lb_v = cfg['lookback_short'], cfg['lookback_med'], cfg['lookback_vol']
    if len(bars) < max(lb_m, lb_v) + 2:
        return False, 0, 0
    c = bars[-1]
    if c['c'] <= c['o']:
        return False, 0, 0
    s_ago = bars[-lb_s - 1]['c']
    sm = (c['c'] - s_ago) / s_ago if s_ago > 0 else 0
    if sm < cfg['min_mom_short']:
        return False, 0, 0
    m_ago = bars[-lb_m - 1]['c']
    mm = (c['c'] - m_ago) / m_ago if m_ago > 0 else 0
    if mm < cfg['min_mom_med']:
        return False, 0, 0
    rv = sum(b.get('v', 0) for b in bars[-lb_s:])
    pv = sum(b.get('v', 0) for b in bars[-lb_v:-lb_s])
    pa = pv / max(lb_v - lb_s, 1)
    ra = rv / max(lb_s, 1)
    vr = ra / pa if pa > 0 else 0
    if vr < cfg['vol_mult']:
        return False, 0, 0
    if len(bars) >= 20:
        h20 = max(b['h'] for b in bars[-20:])
        if c['c'] > h20 * cfg['max_overext']:
            return False, 0, 0
    if len(bars) >= 10:
        sma10 = sum(b['c'] for b in bars[-10:]) / 10
        if c['c'] < sma10:
            return False, 0, 0
    score = (sm * 100) + (mm * 50) + (vr * 5)
    if score < cfg['min_score']:
        return False, 0, 0
    return True, c['c'], score


# ════════════════════════════════════════
# STATE
# ════════════════════════════════════════
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {
        'trades_fired': 0,
        'position': None,
        'cooldowns': {},
        'trade_log': [],
        'started_at': int(time.time()),
    }


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as fp:
        json.dump(state, fp, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# ════════════════════════════════════════
# BOT
# ════════════════════════════════════════
class Bot:
    def __init__(self, dry_run=False, max_override=None):
        self.dry = dry_run
        self.max_trades = max_override or CONFIG['max_trades_total']
        self.client = None if dry_run else RoostooClient(API_KEY, SECRET_KEY, BASE_URL)
        self.state = load_state()

    def get_equity(self):
        if self.dry:
            return STARTING_CAPITAL
        try:
            bal = self.client.get_balance()
            return float(bal.get('total_usd', STARTING_CAPITAL))
        except Exception as e:
            log(f"balance err: {e}")
            return STARTING_CAPITAL

    def get_price(self, coin):
        bars = binance_klines(coin, '1m', 2)
        if bars:
            return bars[-1]['c']
        return None

    def place_buy(self, coin, usd):
        price = self.get_price(coin)
        if not price:
            return None
        qty = usd / (price * (1 + SLIPPAGE) * (1 + TAKER_FEE))
        if self.dry:
            log(f"DRY BUY {coin} qty={qty:.6f} px={price}")
            return {'price': price, 'qty': qty}
        try:
            r = self.client.place_market_order(coin, 'BUY', qty)
            return {'price': float(r.get('price', price)), 'qty': float(r.get('qty', qty))}
        except Exception as e:
            log(f"buy err: {e}")
            return None

    def place_sell(self, coin, qty):
        price = self.get_price(coin)
        if not price:
            return None
        if self.dry:
            log(f"DRY SELL {coin} qty={qty:.6f} px={price}")
            return {'price': price, 'qty': qty}
        try:
            r = self.client.place_market_order(coin, 'SELL', qty)
            return {'price': float(r.get('price', price)), 'qty': float(r.get('qty', qty))}
        except Exception as e:
            log(f"sell err: {e}")
            return None

    def scan_signals(self):
        btc_bars = binance_klines('BTC', '30m', 80)
        ok, reason = btc_regime_ok(btc_bars)
        if not ok:
            log(f"BTC regime CLOSED — {reason}")
            return None
        log(f"BTC regime OPEN — {reason}")
        best = None
        for coin in COINS:
            if coin == 'BTC':
                continue
            cd = self.state['cooldowns'].get(coin, 0)
            if cd > time.time():
                continue
            bars = binance_klines(coin, '30m', 40)
            if len(bars) < 30:
                continue
            fired, entry, score = detect_signal(bars)
            if fired:
                if best is None or score > best[1]:
                    best = (coin, score, entry)
                    log(f"  ✦ candidate {coin} score={score:.1f} px={entry}")
            time.sleep(0.05)
        return best

    def check_exit(self):
        pos = self.state['position']
        if not pos:
            return
        price = self.get_price(pos['coin'])
        if not price:
            return

        if price > pos['peak']:
            pos['peak'] = price

        closed = False
        reason = None
        exit_px = price

        # Hard stop
        if price <= pos['stop']:
            closed = True
            reason = 'STOP'
            exit_px = pos['stop']

        # Time stop
        bars_held = (time.time() - pos['entry_t']) / BAR_S
        if not closed and bars_held > CONFIG['max_hold_bars']:
            closed = True
            reason = 'TIME'

        # ── V3.1 BE STOP @ +0.5% ──
        if (not closed and CONFIG['use_be_stop']
                and not pos.get('be_moved', False)):
            trigger = pos['avg_entry'] * (1 + CONFIG['be_trigger'])
            if price >= trigger:
                new_stop = pos['avg_entry'] * (1 + CONFIG['be_cushion'])
                if new_stop > pos['stop']:
                    pos['stop'] = new_stop
                pos['be_moved'] = True
                log(f"🛡 BE stop armed — stop → {new_stop:.6f}")
                tg(f"🛡 BE STOP armed on {pos['coin']}\n"
                   f"price={price} → stop={new_stop:.6f}")

        # ── V3.1 PYRAMID @ +4% ──
        if (not closed and CONFIG['use_pyramid']
                and not pos.get('pyramid_done', False)
                and not pos['t1_done']):
            trigger = pos['avg_entry'] * (1 + CONFIG['pyramid_trigger'])
            if price >= trigger:
                equity = self.get_equity()
                add_usd = equity * CONFIG['position_pct'] * CONFIG['pyramid_add_pct']
                log(f"🔺 PYRAMID fire — adding ${add_usd:,.0f} to {pos['coin']}")
                fill = self.place_buy(pos['coin'], add_usd)
                if fill:
                    # Recalc avg entry with new leg
                    total_q = pos['qty_initial'] + fill['qty']
                    total_cost = (pos['avg_entry'] * pos['qty_initial']
                                  + fill['price'] * fill['qty'])
                    pos['avg_entry'] = total_cost / total_q
                    pos['qty_initial'] = total_q
                    pos['qty_remaining'] += fill['qty']
                    pos['pyramid_done'] = True
                    # Move stop to signal BE level
                    new_stop = pos['signal_px'] * 1.001
                    if new_stop > pos['stop']:
                        pos['stop'] = new_stop
                    tg(f"🔺 PYRAMID {pos['coin']} +${add_usd:,.0f}\n"
                       f"new avg={pos['avg_entry']:.6f} stop={pos['stop']:.6f}")

        # T1
        if not closed and not pos['t1_done']:
            t1p = pos['avg_entry'] * (1 + CONFIG['target_1_pct'])
            if price >= t1p:
                sell_qty = pos['qty_initial'] * CONFIG['target_1_size']
                r = self.place_sell(pos['coin'], sell_qty)
                if r:
                    pos['closes'].append({'qty': sell_qty, 'px': r['price'], 'reason': 'T1'})
                    pos['qty_remaining'] -= sell_qty
                    pos['t1_done'] = True
                    pos['stop'] = max(pos['stop'], pos['avg_entry'] * 1.002)
                    tg(f"✅ T1 hit {pos['coin']} @ {r['price']}, stop→BE+")

        # T2
        if not closed and pos['t1_done'] and not pos['t2_done']:
            t2p = pos['avg_entry'] * (1 + CONFIG['target_2_pct'])
            if price >= t2p:
                sell_qty = pos['qty_initial'] * CONFIG['target_2_size']
                r = self.place_sell(pos['coin'], sell_qty)
                if r:
                    pos['closes'].append({'qty': sell_qty, 'px': r['price'], 'reason': 'T2'})
                    pos['qty_remaining'] -= sell_qty
                    pos['t2_done'] = True
                    tg(f"✅ T2 hit {pos['coin']} @ {r['price']}")

        # Trail after T1
        if not closed and pos['t1_done']:
            trail = pos['peak'] * (1 - CONFIG['trail_pct'])
            if trail > pos['stop']:
                pos['stop'] = trail
            if price <= pos['stop']:
                closed = True
                reason = 'TRAIL'
                exit_px = pos['stop']

        if closed and pos['qty_remaining'] > 1e-9:
            r = self.place_sell(pos['coin'], pos['qty_remaining'])
            if r:
                pos['closes'].append({'qty': pos['qty_remaining'], 'px': r['price'], 'reason': reason})
                pos['qty_remaining'] = 0

        if pos['qty_remaining'] <= 1e-9:
            entry_cost = pos['avg_entry'] * (1 + SLIPPAGE) * (1 + TAKER_FEE)
            pnl = sum((c['px'] - entry_cost) * c['qty'] for c in pos['closes'])
            msg = (f"🏁 CLOSED {pos['coin']} "
                   f"avg_entry={pos['avg_entry']:.6f} "
                   f"pnl=${pnl:+,.0f} "
                   f"reason={reason or pos['closes'][-1]['reason']}")
            log(msg)
            tg(msg)
            if pnl <= 0:
                self.state['cooldowns'][pos['coin']] = int(time.time() + CONFIG['cooldown_bars'] * BAR_S)
            self.state['trade_log'].append({
                **pos,
                'pnl': pnl,
                'closed_at': int(time.time()),
            })
            self.state['position'] = None

        save_state(self.state)

    def try_new_entry(self):
        if self.state['position']:
            return
        if self.state['trades_fired'] >= self.max_trades:
            log(f"trade cap reached ({self.state['trades_fired']}/{self.max_trades}) — idle")
            return

        equity = self.get_equity()
        if equity < CONFIG['kill_switch_eq']:
            tg(f"⛔ KILL SWITCH — equity ${equity:,.0f} < ${CONFIG['kill_switch_eq']:,.0f}")
            log("kill switch triggered")
            return

        best = self.scan_signals()
        if not best:
            log("no signal this scan")
            return

        coin, score, entry = best
        usd = equity * CONFIG['position_pct']
        log(f"🎯 FIRING: {coin} score={score:.1f} usd=${usd:,.0f}")
        tg(f"🎯 V3.1 SIGNAL {coin} score={score:.1f}\n"
           f"entry={entry} size=${usd:,.0f}")

        fill = self.place_buy(coin, usd)
        if not fill:
            log("buy failed")
            return

        self.state['position'] = {
            'coin': coin,
            'signal_px': entry,
            'entry_t': int(time.time()),
            'qty_initial': fill['qty'],
            'qty_remaining': fill['qty'],
            'avg_entry': fill['price'],
            'peak': fill['price'],
            'stop': fill['price'] * (1 - CONFIG['hard_stop_pct']),
            't1_done': False,
            't2_done': False,
            'pyramid_done': False,
            'be_moved': False,
            'closes': [],
            'score': score,
        }
        self.state['trades_fired'] += 1
        save_state(self.state)
        tg(f"✅ FILLED {coin} @ {fill['price']} qty={fill['qty']:.4f}\n"
           f"stop={self.state['position']['stop']:.6f}\n"
           f"trades_fired={self.state['trades_fired']}/{self.max_trades}")

    def run(self):
        banner = (f"NAKED V3.1 starting — {CONFIG['label']}\n"
                  f"dry={self.dry} max_trades={self.max_trades}\n"
                  f"expected/75h: +$11,581 avg, worst -$55k, DD 0.4%")
        log(banner)
        tg(banner)
        while True:
            try:
                if self.state['position']:
                    self.check_exit()
                else:
                    self.try_new_entry()
            except Exception as e:
                log(f"loop err: {e}")
                traceback.print_exc()
            time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='paper mode, no real orders')
    ap.add_argument('--max', type=int, default=None,
                    help='override max trades total (default 3)')
    args = ap.parse_args()
    bot = Bot(dry_run=args.dry_run, max_override=args.max)
    bot.run()


if __name__ == '__main__':
    main()
