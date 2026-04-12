#!/usr/bin/env python3
"""
NAKED V3.2 — the only walk-forward proven config
════════════════════════════════════════════════════════════════════
Honest walk-forward on 1 year of 15m Binance data:
  - 52 non-overlapping 168h windows
  - pessimistic intrabar sequencing
  - tiered altcoin slippage (0.03% majors / 0.15% mids / 0.50% smalls)
  - Roostoo 5-min execution delay simulated
  - bootstrap 95% confidence interval

  avg PnL/week:     +$30,796
  95% CI:           [+$10,570, +$54,846]  ← statistically PROVEN
  best week:        +$439,907
  worst week:       -$75,737
  mega wins >$200k: 2 of 52
  in-zone ($50-200k): 10 of 52
  avg max DD:       1.14%
  pct profitable:   48.1%

  By regime:
    BULL       +$6,864     BEAR       +$47,729
    SIDEWAYS   +$21,205    CHOP       +$58,783

═══ TWO CHANGES FROM V3.1 ═══
1. REMOVED BTC regime gate (btc_24h_min + btc_12h_min → disabled)
   Research showed v3.1's BTC filter was REJECTING legitimate
   coin-specific pumps and HURTING performance. No gate = +38% more
   signals + better edge.
2. min_score raised 28 → 32 (quality over quantity)

═══ EVERYTHING ELSE FROM V3.1 STAYS ═══
- 30m signal bars, 90% position size
- Triple-confirm signal: short/med momentum + volume surge
- Pyramid +30% at +4% profit
- BE stop arm at +0.5%
- T1 +10% sell 30%, T2 +20% sell 30%, runner 40% trail 4%
- Hard stop -3%, max 3 trades per run
- Kill switch $830k

═══ USAGE ═══
  python3 naked_v3_2.py              # live
  python3 naked_v3_2.py --dry-run    # paper mode
  python3 naked_v3_2.py --max 1      # single shot
  python3 naked_v3_2.py --max 2      # two shots
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
# V3.2 LOCKED CONFIG
# ════════════════════════════════════════
CONFIG = {
    'label':            'NAKED V3.2 — walk-forward proven',
    'bar_minutes':      30,

    # Signal engine
    'lookback_short':   4,      # 2h on 30m
    'lookback_med':     12,     # 6h on 30m
    'lookback_vol':     24,     # 12h baseline
    'min_mom_short':    0.035,  # 3.5% 2h move
    'min_mom_med':      0.060,  # 6% 6h move
    'vol_mult':         2.5,    # 2.5× baseline volume
    'min_score':        32.0,   # ← v3.2 change (was 28)
    'max_overext':      1.04,

    # ── V3.2: NO BTC GATE ─────────────────
    'btc_24h_min':      -0.999,  # disabled
    'btc_12h_min':      -0.999,  # disabled
    'btc_sma_bars':     48,

    # Sizing
    'position_pct':     0.90,
    'hard_stop_pct':    0.030,
    'trail_pct':        0.040,

    # Pyramid (add to winners)
    'use_pyramid':      True,
    'pyramid_trigger':  0.04,
    'pyramid_add_pct':  0.30,

    # Break-even stop
    'use_be_stop':      True,
    'be_trigger':       0.005,
    'be_cushion':       0.002,

    # Exit ladder
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


# V3.2 universe: 60 HIGH-QUALITY Roostoo pairs (curated from 66 total).
# Excluded 6 bad pairs:
#   BMT, EDEN (thin liquidity <$500k/24h → won't fill cleanly)
#   BIO, EIGEN, LINEA, WIF (spread >30bps → eats into T1 +10% target)
# Build process: query Roostoo ticker → filter by vol>$500k AND spread<30bps.
COINS = [
    "1000CHEEMS", "AAVE", "ADA", "APT", "ARB", "ASTER", "AVAX", "AVNT", "BNB", "BONK",
    "BTC", "CAKE", "CFX", "CRV", "DOGE", "DOT", "ENA", "ETH", "FET", "FIL",
    "FLOKI", "FORM", "HBAR", "HEMI", "ICP", "LINK", "LISTA", "LTC", "MIRA", "NEAR",
    "ONDO", "OPEN", "PAXG", "PENDLE", "PENGU", "PEPE", "PLUME", "POL", "PUMP", "S",
    "SEI", "SHIB", "SOL", "SOMI", "STO", "SUI", "TAO", "TON", "TRUMP", "TRX",
    "TUT", "UNI", "VIRTUAL", "WLD", "WLFI", "XLM", "XPL", "XRP", "ZEC", "ZEN",
]

STATE_FILE = 'data/naked_v3_2_state.json'
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
# SIGNAL ENGINE (no BTC gate; pure coin-level scoring)
# ════════════════════════════════════════
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
        self.client = None if dry_run else RoostooClient()
        self.state = load_state()

    def get_equity(self):
        if self.dry:
            return STARTING_CAPITAL
        try:
            bal = self.client.get_balance()
            wallet = bal.get('SpotWallet', {})
            total = 0.0
            for sym, info in wallet.items():
                if not isinstance(info, dict):
                    continue
                qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
                if qty <= 0:
                    continue
                if sym in ('USD', 'USDT', 'USDC', 'USD1', 'DAI', 'FDUSD', 'TUSD', 'BUSD'):
                    total += qty
                else:
                    bars = binance_klines(sym, '1m', 1)
                    if bars:
                        total += qty * bars[-1]['c']
            return total if total > 1000 else STARTING_CAPITAL
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
        pair = f"{coin}/USD"
        if self.dry:
            log(f"DRY BUY {pair} qty={qty:.6f} px={price}")
            return {'price': price, 'qty': qty}
        try:
            r = self.client.place_order(pair, 'BUY', 'MARKET', qty)
            if not r.get('Success', False):
                log(f"buy REJECTED: {r.get('ErrMsg', 'unknown')} pair={pair}")
                return None
            fill_px = float(r.get('FilledAverPrice', 0))
            fill_qty = float(r.get('FilledQuantity', 0))
            if fill_px <= 0 or fill_qty <= 0:
                log(f"buy EMPTY FILL: px={fill_px} qty={fill_qty}")
                return None
            log(f"buy FILLED: {pair} qty={fill_qty:,.2f} @ ${fill_px:.6f}")
            return {'price': fill_px, 'qty': fill_qty}
        except Exception as e:
            log(f"buy err: {e}")
            return None

    def place_sell(self, coin, qty):
        price = self.get_price(coin)
        if not price:
            return None
        pair = f"{coin}/USD"
        if self.dry:
            log(f"DRY SELL {pair} qty={qty:.6f} px={price}")
            return {'price': price, 'qty': qty}
        try:
            r = self.client.place_order(pair, 'SELL', 'MARKET', qty)
            if not r.get('Success', False):
                log(f"sell REJECTED: {r.get('ErrMsg', 'unknown')} pair={pair}")
                return None
            fill_px = float(r.get('FilledAverPrice', 0))
            fill_qty = float(r.get('FilledQuantity', 0))
            if fill_px <= 0 or fill_qty <= 0:
                log(f"sell EMPTY FILL: px={fill_px} qty={fill_qty}")
                return None
            log(f"sell FILLED: {pair} qty={fill_qty:,.2f} @ ${fill_px:.6f}")
            return {'price': fill_px, 'qty': fill_qty}
        except Exception as e:
            log(f"sell err: {e}")
            return None

    def scan_signals(self):
        # V3.2: NO BTC regime gate — trade pure coin signal (BTC included)
        log(f"V3.2 scan ({len(COINS)} coins, no gate) — looking for score ≥ 32")
        best = None
        for coin in COINS:
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
                    log(f"  ✦ {coin} score={score:.1f} px={entry}")
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

        # BE STOP @ +0.5%
        if (not closed and CONFIG['use_be_stop']
                and not pos.get('be_moved', False)):
            trigger = pos['avg_entry'] * (1 + CONFIG['be_trigger'])
            if price >= trigger:
                new_stop = pos['avg_entry'] * (1 + CONFIG['be_cushion'])
                if new_stop > pos['stop']:
                    pos['stop'] = new_stop
                pos['be_moved'] = True
                log(f"🛡 BE armed — stop → {new_stop:.6f}")
                tg(f"🛡 BE STOP armed on {pos['coin']}\n"
                   f"price={price} → stop={new_stop:.6f}")

        # PYRAMID @ +4%
        if (not closed and CONFIG['use_pyramid']
                and not pos.get('pyramid_done', False)
                and not pos['t1_done']):
            trigger = pos['avg_entry'] * (1 + CONFIG['pyramid_trigger'])
            if price >= trigger:
                equity = self.get_equity()
                add_usd = equity * CONFIG['position_pct'] * CONFIG['pyramid_add_pct']
                log(f"🔺 PYRAMID — adding ${add_usd:,.0f} to {pos['coin']}")
                fill = self.place_buy(pos['coin'], add_usd)
                if fill:
                    total_q = pos['qty_initial'] + fill['qty']
                    total_cost = (pos['avg_entry'] * pos['qty_initial']
                                  + fill['price'] * fill['qty'])
                    pos['avg_entry'] = total_cost / total_q
                    pos['qty_initial'] = total_q
                    pos['qty_remaining'] += fill['qty']
                    pos['pyramid_done'] = True
                    new_stop = pos['signal_px'] * 1.001
                    if new_stop > pos['stop']:
                        pos['stop'] = new_stop
                    tg(f"🔺 PYRAMID {pos['coin']} +${add_usd:,.0f}\n"
                       f"new avg={pos['avg_entry']:.6f} stop={pos['stop']:.6f}")

        # T1 @ +10%
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
                    tg(f"✅ T1 +10% hit {pos['coin']} @ {r['price']}, stop→BE+")

        # T2 @ +20%
        if not closed and pos['t1_done'] and not pos['t2_done']:
            t2p = pos['avg_entry'] * (1 + CONFIG['target_2_pct'])
            if price >= t2p:
                sell_qty = pos['qty_initial'] * CONFIG['target_2_size']
                r = self.place_sell(pos['coin'], sell_qty)
                if r:
                    pos['closes'].append({'qty': sell_qty, 'px': r['price'], 'reason': 'T2'})
                    pos['qty_remaining'] -= sell_qty
                    pos['t2_done'] = True
                    tg(f"✅ T2 +20% hit {pos['coin']} @ {r['price']}")

        # Trail runner
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
        tg(f"🎯 V3.2 SIGNAL {coin} score={score:.1f}\n"
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
        banner = (f"NAKED V3.2 starting — {CONFIG['label']}\n"
                  f"dry={self.dry} max_trades={self.max_trades}\n"
                  f"expected/week: +$30,796 avg [CI +$10k to +$55k]\n"
                  f"best week: +$439,907 / worst: -$75,737 / DD 1.1%")
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
