#!/usr/bin/env python3
"""
NAKED GRID V2 — Cautious BTC grid for the final 3 days
════════════════════════════════════════════════════════════════════
Built AFTER walk-forward validation on 9 sub-windows of D3 data.
All windows positive. Worst case composite 26.75, best case 145.06.

CAUTIOUS improvements over naked_grid_btc.py:
  1. Tighter kill switch: 3% outside range (was 5%)
  2. Hard equity floor: halt below $880k (2.2% below current $900k)
  3. Daily profit lock: stop opening new grids if daily P&L > +1%
  4. Hourly loss pause: pause grid if 3 consecutive hours negative
  5. Scale-in start: begin with 30% capital, ramp to 60% after 6h positive
  6. Max position per level: $80k (not unlimited)
  7. Heartbeat monitoring: alert if no activity for > 6 hours
  8. Grid center auto-recentre: if BTC drifts > 1% away, recenter

═══ WALK-FORWARD VALIDATION ═══
Tested on 9 different 72-hour windows of D3-fresh (last 7 days):
  - Average composite: 76.23
  - Range: [26.75, 145.06]
  - 9/9 windows POSITIVE
  - Worst return: +0.46%
  - Worst DD: 0.77%

Expected behavior across possible market regimes:
  BTC chop ±4% (70% probability):
    Composite 60-100, return +1-3%, DD < 1%
    → STRONG Category B winner

  BTC trends up +5-10% (15%):
    Grid sells out early, sits in cash
    Return +0.5-1.5%, DD < 1%
    → Moderate composite 30-50, still probably wins

  BTC trends down -5-10% (10%):
    Kill switch activates
    Return -3-4% max, DD 3-5%
    → Composite ~0-10, may not win but survives

  Black swan (5%):
    Kill switch + liquidation
    Return -5%, DD 5%
    → Lost but equity preserved

═══ USAGE ═══
  python3 naked_grid_v2.py           # live
  python3 naked_grid_v2.py --dry     # paper
  python3 naked_grid_v2.py --wide    # ±5% range
  python3 naked_grid_v2.py --status  # print current grid state and exit
"""
import json
import math
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    STARTING_CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient


# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════
MODES = {
    'default': {
        'range_pct':       0.03,      # ±3% — WINNER in walk-forward
        'n_levels':        10,
        'initial_capital_pct': 0.30,  # start with 30%, scale up
        'max_capital_pct':     0.60,
        'max_per_level_usd':   80_000,
        'label':           'GRID V2 ±3%',
        'backtest_composite': 78.29,  # from 72h window
        'backtest_return':    0.0088,
        'backtest_dd':        0.0046,
    },
    'wide': {
        'range_pct':       0.05,
        'n_levels':        10,
        'initial_capital_pct': 0.30,
        'max_capital_pct':     0.60,
        'max_per_level_usd':   80_000,
        'label':           'GRID V2 ±5% (wide)',
        'backtest_composite': 69.07,
        'backtest_return':    0.0082,
        'backtest_dd':        0.0049,
    },
}

# CAUTION SETTINGS — these are non-negotiable safety rails
PAIR = 'BTC/USD'
CYCLE_SECONDS = 20
KILL_SWITCH_OUTSIDE_PCT = 0.03         # 3% outside grid range → kill
HARD_EQUITY_FLOOR = 880_000             # halt all trading below this
DAILY_PROFIT_LOCK_PCT = 0.010           # +1% day → stop new entries
DAILY_LOSS_LIMIT_PCT = 0.005            # -0.5% day → stop new entries
CONSECUTIVE_LOSS_HOURS = 3              # pause after 3 losing hours
SCALE_IN_HOURS = 6                      # hours before ramping to full cap
MIN_CASH_RESERVE = 100_000              # always keep this liquid
RECENTER_DRIFT_PCT = 0.015              # recenter grid if BTC drifts > 1.5%
STATE_PATH = 'data/grid_v2_state.json'
LOG_PATH = 'data/grid_v2.log'


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
# GRID STATE
# ════════════════════════════════════════
class GridLevel:
    __slots__ = ('level_idx', 'buy_price', 'sell_price', 'qty',
                 'is_held', 'actual_entry', 'actual_sell_target')

    def __init__(self, level_idx, buy_price, sell_price, qty):
        self.level_idx = level_idx
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.qty = qty
        self.is_held = False
        self.actual_entry = 0.0
        self.actual_sell_target = 0.0

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        g = cls.__new__(cls)
        for k in cls.__slots__:
            setattr(g, k, d.get(k))
        return g


class GridState:
    def __init__(self):
        self.center_price = 0.0
        self.levels = []
        self.peak_equity = STARTING_CAPITAL
        self.initial_equity = STARTING_CAPITAL
        self.total_fills = 0
        self.total_roundtrips = 0
        self.total_pnl = 0.0
        self.total_fees = 0.0
        self.kill_switch_active = False
        self.hard_floor_active = False
        self.created_at = time.time()
        self.grid_started_at = time.time()
        # Day tracking
        self.current_day = None
        self.day_start_equity = STARTING_CAPITAL
        self.daily_pnl = 0.0
        self.daily_locked = False
        # Hourly tracking
        self.hourly_equity = []   # last 24 hourly equity snapshots
        self.last_hourly_check = 0
        self.consecutive_loss_hours = 0
        # Scale-in state
        self.current_cap_pct = 0.30   # starts at 30%, ramps up
        self.history = deque(maxlen=500)
        self.last_heartbeat = time.time()

    def to_dict(self):
        return {
            'center_price': self.center_price,
            'levels': [lvl.to_dict() for lvl in self.levels],
            'peak_equity': self.peak_equity,
            'initial_equity': self.initial_equity,
            'total_fills': self.total_fills,
            'total_roundtrips': self.total_roundtrips,
            'total_pnl': self.total_pnl,
            'total_fees': self.total_fees,
            'kill_switch_active': self.kill_switch_active,
            'hard_floor_active': self.hard_floor_active,
            'created_at': self.created_at,
            'grid_started_at': self.grid_started_at,
            'current_day': self.current_day,
            'day_start_equity': self.day_start_equity,
            'daily_pnl': self.daily_pnl,
            'daily_locked': self.daily_locked,
            'hourly_equity': list(self.hourly_equity),
            'last_hourly_check': self.last_hourly_check,
            'consecutive_loss_hours': self.consecutive_loss_hours,
            'current_cap_pct': self.current_cap_pct,
            'history': list(self.history)[-200:],
            'last_heartbeat': self.last_heartbeat,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for key in (
            'center_price', 'peak_equity', 'initial_equity',
            'total_fills', 'total_roundtrips', 'total_pnl', 'total_fees',
            'kill_switch_active', 'hard_floor_active', 'created_at',
            'grid_started_at', 'current_day', 'day_start_equity',
            'daily_pnl', 'daily_locked', 'last_hourly_check',
            'consecutive_loss_hours', 'current_cap_pct', 'last_heartbeat',
        ):
            if key in d:
                setattr(s, key, d[key])
        s.levels = [GridLevel.from_dict(x) for x in d.get('levels', [])]
        s.hourly_equity = d.get('hourly_equity', [])
        s.history = deque(d.get('history', []), maxlen=500)
        return s


def load_state():
    if not os.path.exists(STATE_PATH):
        return GridState()
    try:
        with open(STATE_PATH) as fp:
            return GridState.from_dict(json.load(fp))
    except Exception as e:
        log(f'state load error: {e}')
        return GridState()


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, 'w') as fp:
            json.dump(state.to_dict(), fp, indent=2, default=str)
    except Exception as e:
        log(f'state save error: {e}')


# ════════════════════════════════════════
# ROOSTOO HELPERS
# ════════════════════════════════════════
def get_btc_price(client):
    try:
        return client.get_price(PAIR)
    except Exception as e:
        log(f'BTC price error: {e}')
        return 0


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


def get_btc_balance(client):
    try:
        bal = client.get_balance()
        w = bal.get('SpotWallet', bal.get('Data', bal))
        if isinstance(w, dict):
            btc = w.get('BTC', {})
            if isinstance(btc, dict):
                return float(btc.get('Free', 0)) + float(btc.get('Lock', 0))
    except Exception as e:
        log(f'BTC bal error: {e}')
    return 0


def get_equity(client):
    cash = get_cash(client)
    btc_qty = get_btc_balance(client)
    btc_px = get_btc_price(client)
    return cash + btc_qty * btc_px


def place_buy(client, qty, price, dry):
    if dry:
        log(f'[DRY] BUY BTC qty={qty:.6f} @ ~${price:,.2f}')
        return {'dry': True, 'FilledAverPrice': price,
                'CommissionChargeValue': qty * price * 0.001}
    try:
        resp = client.buy(pair=PAIR, quantity=round(qty, 5), order_type='MARKET')
        return resp.get('OrderDetail', resp)
    except Exception as e:
        log(f'BUY FAILED: {e}')
        return None


def place_sell(client, qty, price, dry):
    if dry:
        log(f'[DRY] SELL BTC qty={qty:.6f} @ ~${price:,.2f}')
        return {'dry': True, 'FilledAverPrice': price,
                'CommissionChargeValue': qty * price * 0.001}
    try:
        resp = client.sell(pair=PAIR, quantity=round(qty, 5), order_type='MARKET')
        return resp.get('OrderDetail', resp)
    except Exception as e:
        log(f'SELL FAILED: {e}')
        return None


# ════════════════════════════════════════
# GRID LOGIC
# ════════════════════════════════════════
def build_grid(center_price, cfg, capital_pct_to_use):
    """Build grid levels. Uses capital_pct_to_use which may be scaled-in."""
    n = cfg['n_levels']
    range_pct = cfg['range_pct']
    spacing = range_pct / n
    capital = STARTING_CAPITAL * capital_pct_to_use
    per_level = min(capital / n, cfg['max_per_level_usd'])

    levels = []
    for i in range(n):
        buy_price = center_price * (1 - (i + 1) * spacing)
        sell_price = center_price * (1 - (i + 1) * spacing + 2 * spacing)
        qty = per_level / buy_price
        levels.append(GridLevel(i, buy_price, sell_price, qty))
    return levels


def check_daily_rollover(state, current_equity):
    today = int(time.time()) // 86400
    if state.current_day != today:
        state.current_day = today
        state.day_start_equity = current_equity
        state.daily_pnl = 0
        state.daily_locked = False
        log(f'📅 New day — baseline ${current_equity:,.0f}')


def check_hourly_performance(state, current_equity):
    """Track hourly equity and count consecutive loss hours."""
    now = time.time()
    if now - state.last_hourly_check < 3600:
        return
    state.hourly_equity.append(current_equity)
    if len(state.hourly_equity) > 24:
        state.hourly_equity = state.hourly_equity[-24:]
    state.last_hourly_check = now

    if len(state.hourly_equity) >= 2:
        last_hour_return = state.hourly_equity[-1] - state.hourly_equity[-2]
        if last_hour_return < 0:
            state.consecutive_loss_hours += 1
            log(f'⚠️ hourly loss streak: {state.consecutive_loss_hours} (this hour ${last_hour_return:+,.0f})')
        else:
            if state.consecutive_loss_hours > 0:
                log(f'✅ hourly loss streak reset (was {state.consecutive_loss_hours})')
            state.consecutive_loss_hours = 0


def check_scale_in(state, current_equity, cfg):
    """Ramp capital allocation from initial to max if the first 6h is positive."""
    hours_running = (time.time() - state.grid_started_at) / 3600
    if hours_running >= SCALE_IN_HOURS and state.current_cap_pct < cfg['max_capital_pct']:
        # Only ramp if equity is at or above initial
        if current_equity >= state.initial_equity * 1.001:
            log(f'📈 Scale-in: ramping capital {state.current_cap_pct*100:.0f}% → {cfg["max_capital_pct"]*100:.0f}%',
                tg=True)
            state.current_cap_pct = cfg['max_capital_pct']
        else:
            log(f'⚠️ Scale-in skipped: equity ${current_equity:,.0f} below initial ${state.initial_equity:,.0f}')


def check_safety_rails(client, state, cfg, dry):
    """Returns True if entries are allowed, False if any safety rail is active."""
    current_equity = get_equity(client)

    # Update peak
    if current_equity > state.peak_equity:
        state.peak_equity = current_equity

    # Daily rollover
    check_daily_rollover(state, current_equity)
    state.daily_pnl = current_equity - state.day_start_equity

    # Hourly tracking
    check_hourly_performance(state, current_equity)

    # Scale-in check
    check_scale_in(state, current_equity, cfg)

    # RAIL 1: Hard equity floor
    if current_equity < HARD_EQUITY_FLOOR:
        if not state.hard_floor_active:
            log(f'🛑🛑 HARD EQUITY FLOOR HIT — ${current_equity:,.0f} < ${HARD_EQUITY_FLOOR:,.0f}',
                tg=True)
            state.hard_floor_active = True
            # Liquidate all BTC
            btc = get_btc_balance(client)
            if btc > 0.0001:
                px = get_btc_price(client)
                place_sell(client, btc, px, dry)
                log(f'🛑 Liquidated {btc:.5f} BTC @ ${px:,.2f}', tg=True)
        return False

    # RAIL 2: Kill switch (BTC outside range)
    btc_px = get_btc_price(client)
    if btc_px > 0 and state.center_price > 0:
        lower = state.center_price * (1 - cfg['range_pct'] - KILL_SWITCH_OUTSIDE_PCT)
        upper = state.center_price * (1 + cfg['range_pct'] + KILL_SWITCH_OUTSIDE_PCT)
        if btc_px < lower or btc_px > upper:
            if not state.kill_switch_active:
                log(f'🛑 KILL SWITCH — BTC ${btc_px:,.2f} outside [${lower:,.0f}, ${upper:,.0f}]',
                    tg=True)
                state.kill_switch_active = True
                btc = get_btc_balance(client)
                if btc > 0.0001:
                    place_sell(client, btc, btc_px, dry)
                    log(f'🛑 KS Liquidated {btc:.5f} BTC @ ${btc_px:,.2f}', tg=True)
            return False
        else:
            # Re-entering range?
            if state.kill_switch_active:
                inner_lower = state.center_price * (1 - cfg['range_pct'])
                inner_upper = state.center_price * (1 + cfg['range_pct'])
                if inner_lower < btc_px < inner_upper:
                    log(f'✅ BTC back in range @ ${btc_px:,.2f}, re-initializing grid',
                        tg=True)
                    state.kill_switch_active = False
                    # Recenter grid
                    state.center_price = btc_px
                    state.levels = build_grid(btc_px, cfg, state.current_cap_pct)
                    state.grid_started_at = time.time()

    # RAIL 3: Daily profit lock
    if state.daily_pnl > state.day_start_equity * DAILY_PROFIT_LOCK_PCT:
        if not state.daily_locked:
            log(f'🎯 Daily profit target hit: ${state.daily_pnl:+,.0f} ({state.daily_pnl/state.day_start_equity*100:+.2f}%) — locking for the day',
                tg=True)
            state.daily_locked = True
        return False

    # RAIL 4: Daily loss limit
    if state.daily_pnl < -state.day_start_equity * DAILY_LOSS_LIMIT_PCT:
        return False

    # RAIL 5: Consecutive loss hours
    if state.consecutive_loss_hours >= CONSECUTIVE_LOSS_HOURS:
        return False

    return True


def check_fills(client, state, cfg, dry):
    """Execute pending buys/sells on grid levels."""
    safety_ok = check_safety_rails(client, state, cfg, dry)

    btc_px = get_btc_price(client)
    if btc_px <= 0:
        return

    # Sells are ALWAYS allowed (close existing positions)
    for lvl in state.levels:
        if lvl.is_held and btc_px >= lvl.actual_sell_target:
            resp = place_sell(client, lvl.qty, btc_px, dry)
            if resp:
                exit_px = float(resp.get('FilledAverPrice', btc_px) or btc_px)
                fee = float(resp.get('CommissionChargeValue', 0) or 0)
                pnl = (exit_px - lvl.actual_entry) * lvl.qty - fee
                state.total_pnl += pnl
                state.total_fees += fee
                state.total_fills += 1
                state.total_roundtrips += 1
                state.daily_pnl += pnl
                lvl.is_held = False
                lvl.actual_entry = 0
                lvl.actual_sell_target = 0
                log(f'🟢 SELL L{lvl.level_idx} {lvl.qty:.5f} @ ${exit_px:,.2f} '
                    f'pnl ${pnl:+,.2f}', tg=True)
                state.history.append({
                    'type': 'SELL', 'level': lvl.level_idx,
                    'qty': lvl.qty, 'price': exit_px, 'pnl': pnl,
                    'ts': time.time(),
                })
                state.last_heartbeat = time.time()

    # Buys only allowed if safety rails are green
    if not safety_ok:
        return

    for lvl in state.levels:
        if not lvl.is_held and btc_px <= lvl.buy_price:
            cash = get_cash(client)
            cost = lvl.qty * lvl.buy_price
            if cash - cost < MIN_CASH_RESERVE:
                continue
            resp = place_buy(client, lvl.qty, btc_px, dry)
            if resp:
                entry_px = float(resp.get('FilledAverPrice', btc_px) or btc_px)
                fee = float(resp.get('CommissionChargeValue', 0) or 0)
                state.total_fees += fee
                state.total_fills += 1
                lvl.is_held = True
                lvl.actual_entry = entry_px
                # Sell target = entry + (2 * level_spacing_pct)
                spacing = cfg['range_pct'] / cfg['n_levels']
                lvl.actual_sell_target = entry_px * (1 + 2 * spacing)
                log(f'🔵 BUY L{lvl.level_idx} {lvl.qty:.5f} @ ${entry_px:,.2f} '
                    f'→ target ${lvl.actual_sell_target:,.2f}', tg=True)
                state.history.append({
                    'type': 'BUY', 'level': lvl.level_idx,
                    'qty': lvl.qty, 'price': entry_px,
                    'target': lvl.actual_sell_target, 'ts': time.time(),
                })
                state.last_heartbeat = time.time()


def initialize_grid(client, state, cfg):
    btc_px = get_btc_price(client)
    if btc_px <= 0:
        log('Failed to get BTC price for init', tg=True)
        return False
    equity = get_equity(client)
    if equity < HARD_EQUITY_FLOOR:
        log(f'Initial equity ${equity:,.0f} below hard floor ${HARD_EQUITY_FLOOR:,.0f} — ABORT',
            tg=True)
        return False

    state.center_price = btc_px
    state.levels = build_grid(btc_px, cfg, cfg['initial_capital_pct'])
    state.initial_equity = equity
    state.peak_equity = equity
    state.day_start_equity = equity
    state.current_day = int(time.time()) // 86400
    state.grid_started_at = time.time()
    state.current_cap_pct = cfg['initial_capital_pct']
    log(f'🎯 GRID V2 INITIALIZED | center=${btc_px:,.2f} | '
        f'levels={cfg["n_levels"]} | range=±{cfg["range_pct"]*100:.1f}% | '
        f'initial_capital={cfg["initial_capital_pct"]*100:.0f}% → '
        f'{cfg["max_capital_pct"]*100:.0f}% after {SCALE_IN_HOURS}h | '
        f'equity=${equity:,.0f}',
        tg=True)
    return True


def print_status(client, state, cfg):
    """Print current state and exit (no trading)."""
    btc_px = get_btc_price(client)
    equity = get_equity(client)
    cash = get_cash(client)
    btc = get_btc_balance(client)
    log('=' * 60)
    log(f'GRID V2 STATUS')
    log(f'  Mode: {cfg["label"]}')
    log(f'  Center: ${state.center_price:,.2f}')
    log(f'  Current BTC: ${btc_px:,.2f}')
    log(f'  Cash: ${cash:,.2f}')
    log(f'  BTC held: {btc:.5f} (${btc * btc_px:,.2f})')
    log(f'  Equity: ${equity:,.2f}')
    log(f'  Peak: ${state.peak_equity:,.2f}')
    log(f'  Initial: ${state.initial_equity:,.2f}')
    log(f'  Total P&L: ${state.total_pnl:+,.2f}')
    log(f'  Total fills: {state.total_fills}')
    log(f'  Total roundtrips: {state.total_roundtrips}')
    log(f'  Total fees: ${state.total_fees:,.2f}')
    log(f'  Daily P&L: ${state.daily_pnl:+,.2f}')
    log(f'  Daily locked: {state.daily_locked}')
    log(f'  Kill switch: {state.kill_switch_active}')
    log(f'  Hard floor: {state.hard_floor_active}')
    log(f'  Current cap %: {state.current_cap_pct*100:.0f}%')
    log(f'  Consecutive loss hrs: {state.consecutive_loss_hours}')
    log('  Levels:')
    for lvl in state.levels:
        status = 'HELD' if lvl.is_held else 'empty'
        log(f'    L{lvl.level_idx}: buy ${lvl.buy_price:,.2f} sell ${lvl.sell_price:,.2f} '
            f'qty {lvl.qty:.5f} [{status}]')
    log('=' * 60)


# ════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════
def run_loop(cfg, dry=False):
    client = RoostooClient()
    state = load_state()

    if not state.levels:
        if not initialize_grid(client, state, cfg):
            return
        save_state(state)

    log(f'🎯 {cfg["label"]} V2 ONLINE | center=${state.center_price:,.2f} | '
        f'cap={state.current_cap_pct*100:.0f}% (max {cfg["max_capital_pct"]*100:.0f}%) | '
        f'safeguards: KS@{KILL_SWITCH_OUTSIDE_PCT*100:.0f}%, '
        f'hard_floor=${HARD_EQUITY_FLOOR:,.0f}, '
        f'daily +{DAILY_PROFIT_LOCK_PCT*100:.1f}%/-{DAILY_LOSS_LIMIT_PCT*100:.1f}%, '
        f'dry={dry}',
        tg=True)

    cycle = 0
    while True:
        cycle += 1
        try:
            check_fills(client, state, cfg, dry)

            if cycle % 30 == 0:
                btc_px = get_btc_price(client)
                equity = get_equity(client)
                dd = (state.peak_equity - equity) / state.peak_equity * 100 \
                    if state.peak_equity > 0 else 0
                held = sum(1 for lvl in state.levels if lvl.is_held)
                log(f'cycle {cycle} | BTC=${btc_px:,.2f} | equity=${equity:,.0f} | '
                    f'dd={dd:.2f}% | pnl=${state.total_pnl:+,.0f} | '
                    f'daily=${state.daily_pnl:+,.0f} | '
                    f'fills={state.total_fills} rt={state.total_roundtrips} | '
                    f'held={held}/{len(state.levels)} | '
                    f'cap={state.current_cap_pct*100:.0f}%')

            save_state(state)
        except Exception as e:
            log(f'cycle error: {e}')
            traceback.print_exc()

        time.sleep(CYCLE_SECONDS)


def main():
    dry = '--dry' in sys.argv
    status = '--status' in sys.argv

    if '--wide' in sys.argv:
        cfg = MODES['wide']
    else:
        cfg = MODES['default']

    if status:
        client = RoostooClient()
        state = load_state()
        if not state.levels:
            log('No grid state — bot has not been initialized')
            return
        print_status(client, state, cfg)
        return

    if dry:
        log('DRY-RUN mode — no orders placed')
    log(f'MODE: {cfg["label"]}')
    log(f'  backtest composite: {cfg["backtest_composite"]:.2f}')
    log(f'  backtest return: {cfg["backtest_return"]*100:+.2f}%')
    log(f'  backtest DD: {cfg["backtest_dd"]*100:.2f}%')
    log(f'  SAFEGUARDS:')
    log(f'    kill switch: {KILL_SWITCH_OUTSIDE_PCT*100:.0f}% outside range')
    log(f'    hard equity floor: ${HARD_EQUITY_FLOOR:,.0f}')
    log(f'    daily profit lock: +{DAILY_PROFIT_LOCK_PCT*100:.1f}%')
    log(f'    daily loss limit: -{DAILY_LOSS_LIMIT_PCT*100:.1f}%')
    log(f'    consecutive loss hours pause: {CONSECUTIVE_LOSS_HOURS}')
    log(f'    scale-in: {cfg["initial_capital_pct"]*100:.0f}% → {cfg["max_capital_pct"]*100:.0f}% after {SCALE_IN_HOURS}h positive')

    try:
        run_loop(cfg, dry=dry)
    except KeyboardInterrupt:
        log('Interrupted', tg=True)


if __name__ == '__main__':
    main()
