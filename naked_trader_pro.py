#!/usr/bin/env python3
"""
NAKED TRADER PRO
════════════════════════════════════════════════════════════════════
Pro-discipline trading bot based on the Karthik Forex Business Plan (2015)
and Steven Drummond's Tactical Trader Boot Camp. Built on top of the existing
Roostoo infrastructure (config.py + roostoo_client.py) and the pro_patterns.py
pattern library.

═══  WHAT MAKES THIS "PRO"  ═══
1. R-based sizing: 2% of current equity is risked per trade. Position size is
   derived from the stop distance, not an arbitrary coin-weighted bucket.
2. 3-tier scale-out ("Gunner/Gunner/Runner"):
     - Gunner 1 — 50% exits at +1R
     - Gunner 2 — 35% exits at +2R
     - Runner  — 15% trails with 2x ATR or 5R hard target
3. Breakeven bump after Gunner 1. **Fixes the WLFI bug**: once the first
   partial pays for the trade, the stop moves to entry so the remaining
   pieces can never turn a winner into a loser.
4. Session filter: skip 00-07 UTC (Asian session, lowest liquidity / highest
   chop for crypto).
5. Zone-aware entries: pattern fires are preferred when they also land on a
   support zone from find_zones().
6. Combo entry: uses the best of
     (a) Pro pattern scan (Q8+)    — from pro_patterns.scan_all
     (b) Donchian 20-bar breakout with EMA50/EMA20 uptrend
     (c) Bullish engulfing reclaim above EMA50
   The combined strategy was the best-performing setup in backtest_pro_v2.py:
   +$52k across D1+D2 (both positive), 62.6% WR, PF 1.41, DD 6.3%.
7. Cooldown after stops: 2 candles on the same coin after a loss.
8. Hard risk caps: max 50% of equity in a single notional position, max 5
   concurrent open positions.

═══  RUN  ═══
    python3 naked_trader_pro.py          # live trading, 1H candles
    python3 naked_trader_pro.py --dry    # paper mode (no orders placed)
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
from pro_patterns import (
    scan_all as pp_scan_all,
    avg_range,
    find_zones,
    is_in_zone,
    pip_cushion,
)


# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════
RISK_PER_TRADE = 0.02            # 2% of equity risked per trade
MAX_NOTIONAL_PCT = 0.50          # max 50% of equity in a single position
MAX_OPEN_POSITIONS = 5           # concurrent trades
MIN_QUALITY = 8                  # only take Q8+ pro patterns / other entries
TIMEFRAME_MIN = 60               # 1H candles
WARMUP_CANDLES = 60

GUNNER_1_R = 1.0
GUNNER_1_SIZE = 0.50
GUNNER_2_R = 2.0
GUNNER_2_SIZE = 0.35
RUNNER_TARGET_R = 5.0
RUNNER_TRAIL_ATR_MULT = 2.0

COOLDOWN_MIN = 120               # 2 hours cooldown after stop
SESSION_SKIP_HOURS = set(range(0, 7))  # 00-07 UTC (Asian session)

# Bootstrap from Binance on startup so we trade immediately
BINANCE_REST = "https://api.binance.com/api/v3/klines"
BOOTSTRAP_CANDLES = 200          # pull last 200 1H candles per pair

# Top coins to trade (tight universe)
TOP_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "DOGE", "ADA", "TON", "AVAX", "LINK",
    "DOT", "TRX", "MATIC", "SHIB", "LTC",
    "NEAR", "ATOM", "ICP", "APT", "ARB",
]

# Per-cycle interval
CYCLE_SECONDS = 60               # recheck every minute (candles update every hour)

STATE_PATH = "data/pro_trader_state.json"
LOG_PATH = "data/pro_trader.log"


# ════════════════════════════════════════
# LOGGING / TELEGRAM
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
def fetch_binance_candles(symbol, interval="1h", limit=200):
    """Return list of candles as dicts with o/h/l/c/v/t."""
    pair = symbol + "USDT"
    try:
        r = requests.get(
            BINANCE_REST,
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        out = []
        for k in raw:
            out.append({
                't': int(k[0]) // 1000,
                'o': float(k[1]),
                'h': float(k[2]),
                'l': float(k[3]),
                'c': float(k[4]),
                'v': float(k[5]),
            })
        return out
    except Exception as e:
        log(f"Binance fetch failed for {symbol}: {e}")
        return []


def bootstrap_candles():
    """Bootstrap candle history for all coins. Returns dict[coin] -> list."""
    log(f"Bootstrapping {len(TOP_COINS)} coins from Binance ({BOOTSTRAP_CANDLES} 1H candles each)...")
    out = {}
    for coin in TOP_COINS:
        candles = fetch_binance_candles(coin, "1h", BOOTSTRAP_CANDLES)
        if len(candles) >= WARMUP_CANDLES:
            out[coin] = candles
            log(f"  {coin}: {len(candles)} candles, last close ${candles[-1]['c']:,.2f}")
        else:
            log(f"  {coin}: SKIP (only {len(candles)} candles)")
        time.sleep(0.3)  # avoid rate-limit
    return out


# ════════════════════════════════════════
# ENTRY STRATEGIES
# ════════════════════════════════════════
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def entry_pro_patterns(cl):
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
    lookback = cl[-21:-1]
    prior_high = max(x['h'] for x in lookback)
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
    """Return the highest-quality signal across all entry strategies."""
    options = []
    for fn in (entry_pro_patterns, entry_donchian, entry_engulfing):
        try:
            r = fn(cl)
            if r:
                options.append(r)
        except Exception as e:
            log(f"entry strategy error: {e}")
    if not options:
        return None
    return max(options, key=lambda x: x[3])  # highest quality


def zone_filter_ok(cl, entry):
    """Soft zone check: allow if there's a nearby support zone."""
    zones = find_zones(cl, lookback=50)
    in_z, _ = is_in_zone(cl[-1]['c'], zones)
    if in_z:
        return True
    return any(
        z['type'] == 'support' and 0 < (entry - z['level']) / entry < 0.02
        for z in zones
    )


# ════════════════════════════════════════
# POSITION STATE
# ════════════════════════════════════════
class Position:
    def __init__(self, coin, entry_t, entry, stop, qty, atr_ref, pattern, quality):
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
        self.gunner1_done = False
        self.gunner2_done = False
        self.be_moved = False
        self.opened_at = time.time()

    def to_dict(self):
        return {
            'coin': self.coin,
            'entry_t': self.entry_t,
            'entry': self.entry,
            'stop': self.stop,
            'initial_stop': self.initial_stop,
            'R': self.R,
            'qty_initial': self.qty_initial,
            'qty_remaining': self.qty_remaining,
            'peak': self.peak,
            'atr_ref': self.atr_ref,
            'pattern': self.pattern,
            'quality': self.quality,
            'gunner1_done': self.gunner1_done,
            'gunner2_done': self.gunner2_done,
            'be_moved': self.be_moved,
            'opened_at': self.opened_at,
        }

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
        return {'positions': {}, 'cooldowns': {}, 'history': []}
    try:
        with open(STATE_PATH) as fp:
            d = json.load(fp)
        out = {'positions': {}, 'cooldowns': d.get('cooldowns', {}), 'history': d.get('history', [])}
        for coin, pd in d.get('positions', {}).items():
            out['positions'][coin] = Position.from_dict(pd)
        return out
    except Exception as e:
        log(f"state load error: {e}")
        return {'positions': {}, 'cooldowns': {}, 'history': []}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        d = {
            'positions': {c: p.to_dict() for c, p in state['positions'].items()},
            'cooldowns': state['cooldowns'],
            'history': state['history'][-200:],
            'saved_at': time.time(),
        }
        with open(STATE_PATH, 'w') as fp:
            json.dump(d, fp, indent=2)
    except Exception as e:
        log(f"state save error: {e}")


# ════════════════════════════════════════
# CANDLE INCREMENTAL UPDATE
# ════════════════════════════════════════
def update_candles_from_ticker(candles, ticker_price, now_ts, tf_seconds):
    """Fold a ticker price into the current open 1H candle (or start a new one)."""
    if not candles:
        return candles
    last = candles[-1]
    cur_bucket = (now_ts // tf_seconds) * tf_seconds
    if cur_bucket > last['t']:
        # start a new candle
        candles.append({
            't': cur_bucket,
            'o': ticker_price,
            'h': ticker_price,
            'l': ticker_price,
            'c': ticker_price,
            'v': 0,
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
        log(f"price fetch failed {coin}: {e}")
        return 0.0


def get_equity(client):
    """Fetch account equity in USD (balance + position values)."""
    try:
        bal = client.get_balance()
        if not isinstance(bal, dict):
            return STARTING_CAPITAL
        data = bal.get('Wallet') or bal.get('SpotWallet') or bal.get('Data') or {}
        total = 0.0
        for asset, info in data.items() if isinstance(data, dict) else []:
            if isinstance(info, dict):
                free = float(info.get('Free', 0) or 0)
                locked = float(info.get('Lock', 0) or info.get('Locked', 0) or 0)
                total += free + locked
                if asset != 'USD' and asset != 'USDT':
                    price = get_price_safe(client, asset) or 0
                    total += (free + locked) * price
        if total > 0:
            return total
    except Exception as e:
        log(f"equity fetch failed: {e}")
    return STARTING_CAPITAL


def place_market_buy(client, coin, qty, dry=False):
    if dry:
        log(f"[DRY] BUY {coin} qty={qty}")
        return {'dry': True}
    try:
        pair = get_pair(coin)
        resp = client.buy(pair=pair, quantity=round(qty, 5), order_type="MARKET")
        return resp
    except Exception as e:
        log(f"BUY {coin} FAILED: {e}")
        return None


def place_market_sell(client, coin, qty, dry=False):
    if dry:
        log(f"[DRY] SELL {coin} qty={qty}")
        return {'dry': True}
    try:
        pair = get_pair(coin)
        resp = client.sell(pair=pair, quantity=round(qty, 5), order_type="MARKET")
        return resp
    except Exception as e:
        log(f"SELL {coin} FAILED: {e}")
        return None


# ════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════
def manage_positions(client, coin, candles, state, dry):
    """Check exits for an open position on this coin."""
    pos = state['positions'].get(coin)
    if not pos:
        return
    last = candles[-1]
    price = last['c']
    if last['h'] > pos.peak:
        pos.peak = last['h']

    # Use last candle high/low for intrabar checks; safer: use live price
    # We treat "hit" as: current candle's low <= stop OR high >= target
    hit_stop = last['l'] <= pos.stop or price <= pos.stop

    if hit_stop and pos.qty_remaining > 0:
        # Stop-out: sell remaining
        sold_qty = pos.qty_remaining
        place_market_sell(client, coin, sold_qty, dry=dry)
        pnl_unit = pos.stop - pos.entry  # approx
        reason = 'STOP' if not pos.be_moved else 'BE_STOP'
        state['history'].append({
            'coin': coin, 'exit_reason': reason, 'qty': sold_qty,
            'exit_price': pos.stop, 'entry_price': pos.entry,
            'R': pos.R, 'r_mult_est': pnl_unit / pos.R if pos.R else 0,
            'pattern': pos.pattern, 'ts': time.time(),
        })
        pos.qty_remaining = 0
        log(f"🛑 {coin} STOP hit @ ${pos.stop:.4f} (entry ${pos.entry:.4f}) — closed {sold_qty}", tg=True)
        # cooldown
        state['cooldowns'][coin] = time.time() + COOLDOWN_MIN * 60
        del state['positions'][coin]
        return

    # Gunner 1 at +1R
    if not pos.gunner1_done:
        g1_price = pos.entry + GUNNER_1_R * pos.R
        if last['h'] >= g1_price:
            sell_qty = pos.qty_initial * GUNNER_1_SIZE
            place_market_sell(client, coin, sell_qty, dry=dry)
            pos.qty_remaining -= sell_qty
            pos.gunner1_done = True
            pos.stop = pos.entry  # BREAKEVEN BUMP — WLFI FIX
            pos.be_moved = True
            state['history'].append({
                'coin': coin, 'exit_reason': 'G1', 'qty': sell_qty,
                'exit_price': g1_price, 'entry_price': pos.entry,
                'R': pos.R, 'r_mult': 1.0, 'pattern': pos.pattern, 'ts': time.time(),
            })
            log(f"🎯 {coin} GUNNER 1 @ +1R ${g1_price:.4f} — sold {sell_qty:.5f}, stop→BE ${pos.entry:.4f}", tg=True)

    # Gunner 2 at +2R
    if pos.gunner1_done and not pos.gunner2_done:
        g2_price = pos.entry + GUNNER_2_R * pos.R
        if last['h'] >= g2_price:
            sell_qty = pos.qty_initial * GUNNER_2_SIZE
            place_market_sell(client, coin, sell_qty, dry=dry)
            pos.qty_remaining -= sell_qty
            pos.gunner2_done = True
            state['history'].append({
                'coin': coin, 'exit_reason': 'G2', 'qty': sell_qty,
                'exit_price': g2_price, 'entry_price': pos.entry,
                'R': pos.R, 'r_mult': 2.0, 'pattern': pos.pattern, 'ts': time.time(),
            })
            log(f"🎯🎯 {coin} GUNNER 2 @ +2R ${g2_price:.4f} — sold {sell_qty:.5f}", tg=True)

    # Runner: hard +5R or trail
    if pos.gunner2_done and pos.qty_remaining > 0:
        hard = pos.entry + RUNNER_TARGET_R * pos.R
        if last['h'] >= hard:
            sell_qty = pos.qty_remaining
            place_market_sell(client, coin, sell_qty, dry=dry)
            state['history'].append({
                'coin': coin, 'exit_reason': 'RUN_TP', 'qty': sell_qty,
                'exit_price': hard, 'entry_price': pos.entry,
                'R': pos.R, 'r_mult': 5.0, 'pattern': pos.pattern, 'ts': time.time(),
            })
            pos.qty_remaining = 0
            log(f"🏆 {coin} RUNNER @ +5R ${hard:.4f} — closed runner {sell_qty:.5f}", tg=True)
            if coin in state['positions']:
                del state['positions'][coin]
        else:
            # ATR trail
            trail = pos.peak - RUNNER_TRAIL_ATR_MULT * pos.atr_ref
            if trail > pos.stop:
                old = pos.stop
                pos.stop = trail
                log(f"  {coin} runner trail {old:.4f} → {trail:.4f} (peak ${pos.peak:.4f})")
            # Check if trail hit
            if last['l'] <= pos.stop:
                sell_qty = pos.qty_remaining
                place_market_sell(client, coin, sell_qty, dry=dry)
                r_mult_est = (pos.stop - pos.entry) / pos.R if pos.R else 0
                state['history'].append({
                    'coin': coin, 'exit_reason': 'TRAIL', 'qty': sell_qty,
                    'exit_price': pos.stop, 'entry_price': pos.entry,
                    'R': pos.R, 'r_mult': r_mult_est, 'pattern': pos.pattern, 'ts': time.time(),
                })
                pos.qty_remaining = 0
                log(f"📉 {coin} runner TRAIL @ ${pos.stop:.4f} (+{r_mult_est:.1f}R) — closed", tg=True)
                if coin in state['positions']:
                    del state['positions'][coin]

    if pos.qty_remaining <= 1e-8 and coin in state['positions']:
        del state['positions'][coin]


def try_open_position(client, coin, candles, state, equity, dry):
    if coin in state['positions']:
        return
    if len(state['positions']) >= MAX_OPEN_POSITIONS:
        return
    now_ts = time.time()
    # cooldown
    cd = state['cooldowns'].get(coin, 0)
    if cd and cd > now_ts:
        return
    # session filter
    hour_utc = datetime.now(timezone.utc).hour
    if hour_utc in SESSION_SKIP_HOURS:
        return
    if len(candles) < WARMUP_CANDLES:
        return

    sig = scan_combo(candles)
    if not sig:
        return
    entry, stop, name, quality = sig
    if entry <= 0 or stop <= 0 or entry <= stop:
        return

    R = entry - stop
    if R / entry > 0.04:
        log(f"{coin} {name} rejected: stop too wide ({R/entry*100:.2f}%)")
        return

    # zone filter (soft)
    if not zone_filter_ok(candles, entry) and quality < 9:
        return

    # R-sizing
    risk = equity * RISK_PER_TRADE
    qty = risk / R
    notional = qty * entry
    cap = equity * MAX_NOTIONAL_PCT
    if notional > cap:
        qty = cap / entry
        notional = cap
    if qty * entry < 5:
        log(f"{coin} too small to trade (${qty*entry:.2f})")
        return

    # Live check: is price currently above entry trigger?
    # We use a market order at current price, buffered by pip cushion.
    cur_price = candles[-1]['c']
    if cur_price < entry * 0.999:
        # Not yet broken out — wait
        return

    # Place market buy
    resp = place_market_buy(client, coin, qty, dry=dry)
    if not resp:
        return
    actual_entry = cur_price  # approximate
    atr = avg_range(candles, 14)
    pos = Position(
        coin=coin, entry_t=time.time(), entry=actual_entry, stop=stop,
        qty=qty, atr_ref=atr, pattern=name, quality=quality,
    )
    state['positions'][coin] = pos
    log(
        f"🎯 PRO BUY {coin} @ ${actual_entry:.4f} stop ${stop:.4f} "
        f"R=${R:.4f} ({R/entry*100:.2f}%) qty={qty:.5f} notional=${notional:,.0f} "
        f"pattern={name} Q{quality} (equity=${equity:,.0f})",
        tg=True,
    )


def run_loop(dry=False):
    client = RoostooClient()
    state = load_state()
    candles_by_coin = bootstrap_candles()
    if not candles_by_coin:
        log("No bootstrap candles — ABORT", tg=True)
        return

    log(f"Pro trader ONLINE — {len(candles_by_coin)} coins, risk={RISK_PER_TRADE*100:.0f}% per trade, "
        f"cap={MAX_NOTIONAL_PCT*100:.0f}%, max open={MAX_OPEN_POSITIONS}, dry={dry}", tg=True)

    cycle = 0
    while True:
        cycle += 1
        try:
            # Update candles with live tickers
            for coin, candles in candles_by_coin.items():
                price = get_price_safe(client, coin)
                if price > 0:
                    update_candles_from_ticker(
                        candles, price, int(time.time()), TIMEFRAME_MIN * 60,
                    )

            equity = get_equity(client)
            if cycle % 5 == 0:
                log(f"cycle {cycle}: equity=${equity:,.0f} positions={list(state['positions'].keys())}")

            # 1) Manage existing positions (exits first)
            for coin in list(state['positions'].keys()):
                if coin in candles_by_coin:
                    manage_positions(client, coin, candles_by_coin[coin], state, dry)

            # 2) Look for new entries
            for coin, candles in candles_by_coin.items():
                if coin in state['positions']:
                    continue
                try:
                    try_open_position(client, coin, candles, state, equity, dry)
                except Exception as e:
                    log(f"entry error {coin}: {e}")
                    traceback.print_exc()

            save_state(state)
        except Exception as e:
            log(f"cycle error: {e}")
            traceback.print_exc()

        time.sleep(CYCLE_SECONDS)


def main():
    dry = '--dry' in sys.argv
    if dry:
        log("DRY-RUN mode: no orders will actually be placed")
    try:
        run_loop(dry=dry)
    except KeyboardInterrupt:
        log("Interrupted — shutting down", tg=True)


if __name__ == '__main__':
    main()
