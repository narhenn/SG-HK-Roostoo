#!/usr/bin/env python3
"""
NAKED TRADER BRICKS — Walter Peters' Profit Bricks overlay on MILK MAX
════════════════════════════════════════════════════════════════════
The nuclear option for the final 4 days.

Adds Walter Peters' Profit Bricks (fxjake.com, Naked Forex lineage —
same source as the Karthik plan) trade-level compounding on top of
the MILK engine. Backtest sweep of 46+ configurations identified
the ceiling:

  base_risk=6%, cap=2000%, pool_factor=2.0, max_risk=20%, trail=3xATR
  → +$1,032,052 total (D1 +$737k, D2 +$295k), DD 42.4%
  → 2.2x MILK MAX, both datasets meaningfully positive

═══ HOW PROFIT BRICKS WORKS ═══
Tracks a "pool" of accumulated winnings. Each trade's risk =
base_risk_dollars + (pool × pool_factor). On wins, pool grows. On
losses, pool drains to zero (never below). The pool compounds into
each subsequent trade's position size, amplifying winning streaks.

Peters' dartboard test: 7x profit improvement on random entries from
the compounding alone (same trades, different sizing).

Our crypto backtest: 2.2x improvement over MILK MAX in D1+D2.

═══ WHAT CHANGED FROM naked_trader_milk.py ═══
1. base_risk:      6% → 6%            (unchanged)
2. cap_pct:       500% → 2000%         (the cap that was strangling)
3. max_risk_pct:  none → 20%           (hard safety on single trades)
4. pool_factor:   n/a → 2.0            (aggressive compound rate)
5. trail:         3xATR → 3xATR        (unchanged)
6. + Profit Bricks pool tracking

═══ WARNING ═══
A single loss after a hot streak can hit 20% of current equity. At
$1M account, that's a -$200k trade. At $1.5M, -$300k. The
backtest DD of 42.4% reflects this asymmetry.

═══ THREE MODES ═══
  python3 naked_trader_bricks.py              # MAX (pool_f=2.0)
  python3 naked_trader_bricks.py --balanced   # BALANCED (pool_f=1.0)
  python3 naked_trader_bricks.py --mild       # MILD (pool_f=0.5)
  python3 naked_trader_bricks.py --dry        # paper mode
"""
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    STARTING_CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient
from pro_patterns import scan_all as pp_scan_all, avg_range, pip_cushion
from profit_bricks import ProfitBricks


# ════════════════════════════════════════
# MODE CONFIGS
# ════════════════════════════════════════
MODES = {
    'max': {
        'base_risk': 0.06,
        'cap': 20.0,          # 2000%
        'pool_factor': 2.0,
        'max_risk_pct': 0.20, # max 20% of equity per trade
        'trail_atr_mult': 3.0,
        'backtest_pnl': 1_032_052,
        'backtest_d1': 736_563,
        'backtest_d2': 295_488,
        'backtest_dd': 42.4,
        'label': 'BRICKS MAX',
    },
    'balanced': {
        'base_risk': 0.06,
        'cap': 20.0,
        'pool_factor': 1.0,
        'max_risk_pct': 0.20,
        'trail_atr_mult': 3.0,
        'backtest_pnl': 933_000,
        'backtest_d1': 752_000,
        'backtest_d2': 181_000,
        'backtest_dd': 42.4,
        'label': 'BRICKS BALANCED',
    },
    'mild': {
        'base_risk': 0.06,
        'cap': 20.0,
        'pool_factor': 0.5,
        'max_risk_pct': 0.20,
        'trail_atr_mult': 3.0,
        'backtest_pnl': 927_000,
        'backtest_d1': 598_000,
        'backtest_d2': 329_000,
        'backtest_dd': 42.4,
        'label': 'BRICKS MILD',
    },
}

# Fixed configs
MAX_OPEN_POSITIONS = 5
MIN_QUALITY = 8
TIMEFRAME_MIN = 60
WARMUP_CANDLES = 60

GUNNER_1_R = 1.0
GUNNER_1_SIZE = 0.50
GUNNER_2_R = 2.0
GUNNER_2_SIZE = 0.35
RUNNER_TARGET_R = 5.0

COOLDOWN_MIN = 120
SESSION_SKIP_HOURS = set(range(0, 7))

USE_MTF_FILTER = True
USE_BTC_REGIME = True

BINANCE_REST = "https://api.binance.com/api/v3/klines"
BOOTSTRAP_CANDLES = 250

TOP_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "DOGE", "ADA", "TON", "AVAX", "LINK",
    "DOT", "TRX", "MATIC", "SHIB", "LTC",
    "NEAR", "ATOM", "ICP", "APT", "ARB",
]

CYCLE_SECONDS = 60
STATE_PATH = "data/bricks_trader_state.json"
LOG_PATH = "data/bricks_trader.log"


# ════════════════════════════════════════
# LOG / TELEGRAM
# ════════════════════════════════════════
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg, tg=False):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as fp:
            fp.write(line + "\n")
    except Exception:
        pass
    if tg and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": line},
                timeout=5,
            )
        except Exception:
            pass


# ════════════════════════════════════════
# BINANCE BOOTSTRAP
# ════════════════════════════════════════
def fetch_binance(symbol, interval="1h", limit=250):
    pair = symbol + "USDT"
    try:
        r = requests.get(
            BINANCE_REST,
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        return [
            {
                't': int(k[0]) // 1000,
                'o': float(k[1]),
                'h': float(k[2]),
                'l': float(k[3]),
                'c': float(k[4]),
                'v': float(k[5]),
            }
            for k in raw
        ]
    except Exception as e:
        log(f"Binance fetch failed {symbol}: {e}")
        return []


def bootstrap_candles():
    log(f"Bootstrapping {len(TOP_COINS)} coins ({BOOTSTRAP_CANDLES} 1H candles)...")
    out = {}
    for coin in TOP_COINS:
        candles = fetch_binance(coin, "1h", BOOTSTRAP_CANDLES)
        if len(candles) >= WARMUP_CANDLES:
            out[coin] = candles
            log(f"  {coin}: {len(candles)} candles, last ${candles[-1]['c']:,.4f}")
        else:
            log(f"  {coin}: SKIP ({len(candles)} candles)")
        time.sleep(0.3)
    return out


# ════════════════════════════════════════
# INDICATORS
# ════════════════════════════════════════
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def resample_to_4h(candles_1h):
    if not candles_1h:
        return []
    out, bucket, bstart = [], [], None
    for c in candles_1h:
        t = int(c['t'])
        b = (t // (4 * 3600)) * (4 * 3600)
        if bstart is None:
            bstart = b
        if b != bstart:
            if bucket:
                out.append({
                    't': bstart, 'o': bucket[0]['o'],
                    'h': max(x['h'] for x in bucket),
                    'l': min(x['l'] for x in bucket),
                    'c': bucket[-1]['c'],
                    'v': sum(x.get('v', 0) for x in bucket),
                })
            bucket = [c]
            bstart = b
        else:
            bucket.append(c)
    if bucket:
        out.append({
            't': bstart, 'o': bucket[0]['o'],
            'h': max(x['h'] for x in bucket),
            'l': min(x['l'] for x in bucket),
            'c': bucket[-1]['c'],
            'v': sum(x.get('v', 0) for x in bucket),
        })
    return out


def htf_uptrend_ok(cl_1h):
    cl_4h = resample_to_4h(cl_1h)
    if len(cl_4h) < 50:
        return True
    closes = [x['c'] for x in cl_4h]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return True
    return cl_4h[-1]['c'] > e20 > e50


def btc_regime_ok(btc_cl):
    if not btc_cl or len(btc_cl) < 200:
        return True
    closes = [x['c'] for x in btc_cl]
    e200 = ema(closes, 200)
    if e200 is None:
        return True
    return btc_cl[-1]['c'] > e200


# ════════════════════════════════════════
# ENTRIES
# ════════════════════════════════════════
def entry_pro_pat(cl):
    fired, entry, stop, name, quality, _ = pp_scan_all(cl)
    if fired and quality >= MIN_QUALITY:
        return (entry, stop, name, quality)
    return None


def entry_donchian(cl):
    if len(cl) < 50:
        return None
    c = cl[-1]
    if c['c'] <= c['o']:
        return None
    prior_high = max(x['h'] for x in cl[-21:-1])
    if c['c'] <= prior_high:
        return None
    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if not e20 or not e50 or e20 <= e50:
        return None
    atr = avg_range(cl, 14)
    entry = c['h'] + pip_cushion(c)
    stop = c['l'] - atr * 0.5
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'DONCHIAN', 8)


def entry_engulfing(cl):
    if len(cl) < 60:
        return None
    c = cl[-1]
    p = cl[-2]
    if not (p['c'] < p['o'] and c['c'] > c['o']):
        return None
    if c['o'] > p['c'] or c['c'] < p['o']:
        return None
    if c['c'] - c['o'] < (p['o'] - p['c']) * 1.1:
        return None
    closes = [x['c'] for x in cl]
    e50 = ema(closes, 50)
    if not e50 or c['c'] < e50:
        return None
    atr = avg_range(cl, 14)
    entry = c['h'] + pip_cushion(c)
    stop = min(c['l'], p['l']) - atr * 0.3
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'ENGULF', 7)


def scan_combo(cl):
    opts = []
    for fn in (entry_pro_pat, entry_donchian, entry_engulfing):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception as e:
            log(f"entry strategy error: {e}")
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


# ════════════════════════════════════════
# POSITION STATE
# ════════════════════════════════════════
class Position:
    def __init__(self, coin, entry_t, entry, stop, qty, atr_ref, pattern,
                 quality, pool_used, risk_used):
        self.coin = coin
        self.entry_t = entry_t
        self.entry = entry
        self.stop = stop
        self.initial_stop = stop
        self.R = entry - stop
        self.qty_initial = qty
        self.qty_remaining = qty
        self.peak = entry
        self.atr_ref = atr_ref
        self.pattern = pattern
        self.quality = quality
        self.pool_used = pool_used   # pool value at entry
        self.risk_used = risk_used    # risk_dollars at entry
        self.gunner1_done = False
        self.gunner2_done = False
        self.be_moved = False
        self.opened_at = time.time()

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            'coin', 'entry_t', 'entry', 'stop', 'initial_stop', 'R',
            'qty_initial', 'qty_remaining', 'peak', 'atr_ref',
            'pattern', 'quality', 'pool_used', 'risk_used',
            'gunner1_done', 'gunner2_done', 'be_moved', 'opened_at',
        )}

    @classmethod
    def from_dict(cls, d):
        p = cls.__new__(cls)
        for k, v in d.items():
            setattr(p, k, v)
        return p


# ════════════════════════════════════════
# STATE PERSISTENCE
# ════════════════════════════════════════
def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            'positions': {}, 'cooldowns': {}, 'history': [],
            'peak_equity': STARTING_CAPITAL,
            'pool': 0.0, 'peak_pool': 0.0,
        }
    try:
        with open(STATE_PATH) as fp:
            d = json.load(fp)
        out = {
            'positions': {},
            'cooldowns': d.get('cooldowns', {}),
            'history': d.get('history', []),
            'peak_equity': d.get('peak_equity', STARTING_CAPITAL),
            'pool': d.get('pool', 0.0),
            'peak_pool': d.get('peak_pool', 0.0),
        }
        for coin, pd in d.get('positions', {}).items():
            out['positions'][coin] = Position.from_dict(pd)
        return out
    except Exception as e:
        log(f"state load error: {e}")
        return {
            'positions': {}, 'cooldowns': {}, 'history': [],
            'peak_equity': STARTING_CAPITAL,
            'pool': 0.0, 'peak_pool': 0.0,
        }


def save_state(state, bricks):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        d = {
            'positions': {c: p.to_dict() for c, p in state['positions'].items()},
            'cooldowns': state['cooldowns'],
            'history': state['history'][-200:],
            'peak_equity': state.get('peak_equity', STARTING_CAPITAL),
            'pool': bricks.pool,
            'peak_pool': bricks.peak_pool,
            'saved_at': time.time(),
        }
        with open(STATE_PATH, 'w') as fp:
            json.dump(d, fp, indent=2)
    except Exception as e:
        log(f"state save error: {e}")


# ════════════════════════════════════════
# CANDLE UPDATE
# ════════════════════════════════════════
def update_candles(candles, ticker_price, now_ts, tf_seconds):
    if not candles:
        return candles
    last = candles[-1]
    cur_bucket = (now_ts // tf_seconds) * tf_seconds
    if cur_bucket > last['t']:
        candles.append({
            't': cur_bucket, 'o': ticker_price,
            'h': ticker_price, 'l': ticker_price, 'c': ticker_price, 'v': 0,
        })
    else:
        last['h'] = max(last['h'], ticker_price)
        last['l'] = min(last['l'], ticker_price)
        last['c'] = ticker_price
    return candles


# ════════════════════════════════════════
# ROOSTOO HELPERS
# ════════════════════════════════════════
def get_pair(coin):
    return f"{coin}/USD"


def get_price_safe(client, coin):
    try:
        return client.get_price(get_pair(coin))
    except Exception as e:
        log(f"price fetch {coin}: {e}")
        return 0.0


def get_equity(client):
    try:
        bal = client.get_balance()
        if not isinstance(bal, dict):
            return STARTING_CAPITAL
        data = bal.get('Wallet') or bal.get('SpotWallet') or bal.get('Data') or {}
        total = 0.0
        if isinstance(data, dict):
            for asset, info in data.items():
                if isinstance(info, dict):
                    free = float(info.get('Free', 0) or 0)
                    locked = float(info.get('Lock', 0) or info.get('Locked', 0) or 0)
                    total += free + locked
                    if asset not in ('USD', 'USDT'):
                        price = get_price_safe(client, asset) or 0
                        total += (free + locked) * price
        if total > 0:
            return total
    except Exception as e:
        log(f"equity fetch failed: {e}")
    return STARTING_CAPITAL


def place_market_buy(client, coin, qty, dry=False):
    if dry:
        log(f"[DRY] BUY {coin} qty={qty:.6f}")
        return {'dry': True}
    try:
        return client.buy(pair=get_pair(coin), quantity=round(qty, 5), order_type="MARKET")
    except Exception as e:
        log(f"BUY {coin} FAILED: {e}")
        return None


def place_market_sell(client, coin, qty, dry=False):
    if dry:
        log(f"[DRY] SELL {coin} qty={qty:.6f}")
        return {'dry': True}
    try:
        return client.sell(pair=get_pair(coin), quantity=round(qty, 5), order_type="MARKET")
    except Exception as e:
        log(f"SELL {coin} FAILED: {e}")
        return None


# ════════════════════════════════════════
# POSITION MANAGEMENT
# ════════════════════════════════════════
def manage_position(client, coin, candles, state, bricks, trail_mult, dry):
    pos = state['positions'].get(coin)
    if not pos:
        return
    last = candles[-1]
    price = last['c']
    if last['h'] > pos.peak:
        pos.peak = last['h']

    hit_stop = last['l'] <= pos.stop or price <= pos.stop

    if hit_stop and pos.qty_remaining > 0:
        sold_qty = pos.qty_remaining
        place_market_sell(client, coin, sold_qty, dry=dry)
        reason = 'BE_STOP' if pos.be_moved else 'STOP'
        # Estimate trade PnL for brick tracking
        entry_p = pos.entry
        exit_p = pos.stop
        # If partial already happened, count only remaining piece's PnL plus g1
        g1_pnl = (pos.entry + pos.R - entry_p) * pos.qty_initial * GUNNER_1_SIZE if pos.gunner1_done else 0
        g2_pnl = (pos.entry + 2*pos.R - entry_p) * pos.qty_initial * GUNNER_2_SIZE if pos.gunner2_done else 0
        remaining_pnl = (exit_p - entry_p) * sold_qty
        trade_pnl = g1_pnl + g2_pnl + remaining_pnl
        bricks.on_close(trade_pnl)
        state['history'].append({
            'coin': coin, 'exit_reason': reason, 'qty': sold_qty,
            'exit_price': pos.stop, 'entry_price': pos.entry,
            'R': pos.R, 'pattern': pos.pattern, 'pnl': trade_pnl,
            'pool_after': bricks.pool, 'ts': time.time(),
        })
        pos.qty_remaining = 0
        log(f"🛑 {coin} {reason} @ ${pos.stop:.4f} trade_pnl=${trade_pnl:+,.0f} pool=${bricks.pool:,.0f}", tg=True)
        state['cooldowns'][coin] = time.time() + COOLDOWN_MIN * 60
        del state['positions'][coin]
        return

    if not pos.gunner1_done:
        g1p = pos.entry + GUNNER_1_R * pos.R
        if last['h'] >= g1p:
            sell_qty = pos.qty_initial * GUNNER_1_SIZE
            place_market_sell(client, coin, sell_qty, dry=dry)
            pos.qty_remaining -= sell_qty
            pos.gunner1_done = True
            pos.stop = pos.entry
            pos.be_moved = True
            state['history'].append({
                'coin': coin, 'exit_reason': 'G1', 'qty': sell_qty,
                'exit_price': g1p, 'entry_price': pos.entry,
                'R': pos.R, 'pattern': pos.pattern, 'ts': time.time(),
            })
            log(f"🎯 {coin} G1 +1R @ ${g1p:.4f} sold {sell_qty:.5f}, stop→BE", tg=True)

    if pos.gunner1_done and not pos.gunner2_done:
        g2p = pos.entry + GUNNER_2_R * pos.R
        if last['h'] >= g2p:
            sell_qty = pos.qty_initial * GUNNER_2_SIZE
            place_market_sell(client, coin, sell_qty, dry=dry)
            pos.qty_remaining -= sell_qty
            pos.gunner2_done = True
            state['history'].append({
                'coin': coin, 'exit_reason': 'G2', 'qty': sell_qty,
                'exit_price': g2p, 'entry_price': pos.entry,
                'R': pos.R, 'pattern': pos.pattern, 'ts': time.time(),
            })
            log(f"🎯🎯 {coin} G2 +2R @ ${g2p:.4f}", tg=True)

    if pos.gunner2_done and pos.qty_remaining > 0:
        hard = pos.entry + RUNNER_TARGET_R * pos.R
        if last['h'] >= hard:
            sell_qty = pos.qty_remaining
            place_market_sell(client, coin, sell_qty, dry=dry)
            g1_pnl = pos.R * pos.qty_initial * GUNNER_1_SIZE
            g2_pnl = 2 * pos.R * pos.qty_initial * GUNNER_2_SIZE
            runner_pnl = RUNNER_TARGET_R * pos.R * sell_qty
            trade_pnl = g1_pnl + g2_pnl + runner_pnl
            bricks.on_close(trade_pnl)
            state['history'].append({
                'coin': coin, 'exit_reason': 'RUN_TP', 'qty': sell_qty,
                'exit_price': hard, 'entry_price': pos.entry,
                'R': pos.R, 'pattern': pos.pattern, 'pnl': trade_pnl,
                'pool_after': bricks.pool, 'ts': time.time(),
            })
            pos.qty_remaining = 0
            log(f"🏆 {coin} RUNNER +5R @ ${hard:.4f} trade_pnl=${trade_pnl:+,.0f} pool=${bricks.pool:,.0f}", tg=True)
            if coin in state['positions']:
                del state['positions'][coin]
        else:
            trail = pos.peak - trail_mult * pos.atr_ref
            if trail > pos.stop:
                pos.stop = trail
            if last['l'] <= pos.stop:
                sell_qty = pos.qty_remaining
                place_market_sell(client, coin, sell_qty, dry=dry)
                g1_pnl = pos.R * pos.qty_initial * GUNNER_1_SIZE
                g2_pnl = 2 * pos.R * pos.qty_initial * GUNNER_2_SIZE
                runner_pnl = (pos.stop - pos.entry) * sell_qty
                trade_pnl = g1_pnl + g2_pnl + runner_pnl
                r_est = (pos.stop - pos.entry) / pos.R if pos.R else 0
                bricks.on_close(trade_pnl)
                state['history'].append({
                    'coin': coin, 'exit_reason': 'TRAIL', 'qty': sell_qty,
                    'exit_price': pos.stop, 'entry_price': pos.entry,
                    'R': pos.R, 'r_mult': r_est, 'pattern': pos.pattern,
                    'pnl': trade_pnl, 'pool_after': bricks.pool,
                    'ts': time.time(),
                })
                pos.qty_remaining = 0
                log(f"📉 {coin} TRAIL @ ${pos.stop:.4f} (+{r_est:.1f}R) trade_pnl=${trade_pnl:+,.0f} pool=${bricks.pool:,.0f}", tg=True)
                if coin in state['positions']:
                    del state['positions'][coin]

    if pos.qty_remaining <= 1e-8 and coin in state['positions']:
        del state['positions'][coin]


# ════════════════════════════════════════
# ENTRY LOGIC
# ════════════════════════════════════════
def try_open(client, coin, candles, state, bricks, equity, cap_pct,
             btc_candles, dry):
    if coin in state['positions']:
        return
    if len(state['positions']) >= MAX_OPEN_POSITIONS:
        return

    now_ts = time.time()
    cd = state['cooldowns'].get(coin, 0)
    if cd and cd > now_ts:
        return

    hour_utc = datetime.now(timezone.utc).hour
    if hour_utc in SESSION_SKIP_HOURS:
        return

    if len(candles) < WARMUP_CANDLES:
        return

    if USE_MTF_FILTER and not htf_uptrend_ok(candles):
        return

    if USE_BTC_REGIME and not btc_regime_ok(btc_candles):
        return

    sig = scan_combo(candles)
    if not sig:
        return
    entry, stop, name, quality = sig
    if entry <= 0 or stop <= 0 or entry <= stop:
        return

    R = entry - stop
    if R / entry > 0.04:
        return

    # ═══ PROFIT BRICKS SIZING ═══
    risk_dollars = bricks.next_risk_dollars(equity)
    qty = risk_dollars / R
    notional = qty * entry
    cap = equity * cap_pct
    if notional > cap:
        qty = cap / entry
        notional = cap
    if qty * entry < 5:
        return

    cur_price = candles[-1]['c']
    if cur_price < entry * 0.999:
        return

    resp = place_market_buy(client, coin, qty, dry=dry)
    if not resp:
        return

    actual_entry = cur_price
    atr_ref = avg_range(candles, 14)
    pos = Position(
        coin=coin, entry_t=time.time(), entry=actual_entry, stop=stop,
        qty=qty, atr_ref=atr_ref, pattern=name, quality=quality,
        pool_used=bricks.pool, risk_used=risk_dollars,
    )
    state['positions'][coin] = pos
    log(
        f"🧱 BRICKS BUY {coin} @ ${actual_entry:.4f} stop ${stop:.4f} "
        f"R=${R:.4f} qty={qty:.5f} notional=${notional:,.0f} "
        f"risk=${risk_dollars:,.0f} pool=${bricks.pool:,.0f} "
        f"pattern={name} Q{quality}",
        tg=True,
    )


# ════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════
def run_loop(mode_cfg, dry=False):
    client = RoostooClient()
    state = load_state()

    # Initialize Profit Bricks from state
    bricks = ProfitBricks(
        starting_equity=STARTING_CAPITAL,
        base_risk_pct=mode_cfg['base_risk'],
        pool_factor=mode_cfg['pool_factor'],
        max_risk_pct=mode_cfg['max_risk_pct'],
    )
    bricks.pool = state.get('pool', 0.0)
    bricks.peak_pool = state.get('peak_pool', 0.0)

    cap = mode_cfg['cap']
    trail_mult = mode_cfg['trail_atr_mult']

    candles_by_coin = bootstrap_candles()
    if not candles_by_coin or 'BTC' not in candles_by_coin:
        log("No bootstrap candles or no BTC — ABORT", tg=True)
        return

    log(
        f"🧱 BRICKS TRADER {mode_cfg['label']} ONLINE — "
        f"base_risk={mode_cfg['base_risk']*100:.1f}%, cap={cap*100:.0f}%, "
        f"pool_factor={mode_cfg['pool_factor']}, max_risk={mode_cfg['max_risk_pct']*100:.0f}%, "
        f"trail={trail_mult}xATR, "
        f"backtest=${mode_cfg['backtest_pnl']:+,.0f} DD={mode_cfg['backtest_dd']}%, "
        f"starting_pool=${bricks.pool:,.0f}, dry={dry}",
        tg=True,
    )

    cycle = 0
    while True:
        cycle += 1
        try:
            for coin, candles in candles_by_coin.items():
                price = get_price_safe(client, coin)
                if price > 0:
                    update_candles(candles, price, int(time.time()), TIMEFRAME_MIN * 60)

            equity = get_equity(client)
            if equity > state.get('peak_equity', 0):
                state['peak_equity'] = equity

            if cycle % 5 == 0:
                dd = (state['peak_equity'] - equity) / state['peak_equity'] * 100 \
                    if state.get('peak_equity') else 0
                log(
                    f"cycle {cycle}: equity=${equity:,.0f} peak=${state['peak_equity']:,.0f} "
                    f"dd={dd:.1f}% pool=${bricks.pool:,.0f} peak_pool=${bricks.peak_pool:,.0f} "
                    f"positions={list(state['positions'].keys())}"
                )

            for coin in list(state['positions'].keys()):
                if coin in candles_by_coin:
                    manage_position(client, coin, candles_by_coin[coin],
                                    state, bricks, trail_mult, dry)

            btc_candles = candles_by_coin.get('BTC', [])
            for coin, candles in candles_by_coin.items():
                if coin in state['positions']:
                    continue
                try:
                    try_open(client, coin, candles, state, bricks, equity,
                             cap, btc_candles, dry)
                except Exception as e:
                    log(f"entry error {coin}: {e}")
                    traceback.print_exc()

            save_state(state, bricks)
        except Exception as e:
            log(f"cycle error: {e}")
            traceback.print_exc()

        time.sleep(CYCLE_SECONDS)


def main():
    dry = '--dry' in sys.argv

    if '--mild' in sys.argv:
        mode = 'mild'
    elif '--balanced' in sys.argv:
        mode = 'balanced'
    else:
        mode = 'max'

    cfg = MODES[mode]

    if dry:
        log("DRY-RUN mode: no orders placed")
    log(f"🧱 MODE: {cfg['label']}")
    log(f"  base_risk={cfg['base_risk']*100:.1f}% cap={cfg['cap']*100:.0f}% "
        f"pool_factor={cfg['pool_factor']} max_risk={cfg['max_risk_pct']*100:.0f}% "
        f"trail={cfg['trail_atr_mult']}xATR")
    log(f"  backtest: ${cfg['backtest_pnl']:+,.0f} "
        f"(D1 ${cfg['backtest_d1']:+,.0f}, D2 ${cfg['backtest_d2']:+,.0f}) "
        f"DD={cfg['backtest_dd']}%")

    try:
        run_loop(cfg, dry=dry)
    except KeyboardInterrupt:
        log("Interrupted — shutting down", tg=True)


if __name__ == '__main__':
    main()
