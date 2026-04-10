"""
PRO TRADER BACKTEST V2
==========================================
V1 (pro_patterns only) lost money because the 21 forex patterns don't fire
often enough on short 7-day 1H crypto data and the BE-after-G1 rule cuts off
runners too early.

V2 separates ENTRIES from RISK MANAGEMENT. The risk management is the real
edge from the Karthik plan:
  - 1R-based position sizing (fixed % risk per trade)
  - 3-tier scale-out (50%/35%/15%)
  - Breakeven bump after Gunner 1 (fixes the WLFI bug)
  - ATR-based runner trail
  - Cooldown after stop-loss

This file runs multiple entry strategies under the SAME pro risk management
so we can measure which entry works best with pro exits.

Entry strategies tested:
  E1 = Pro patterns (pro_patterns.scan_all)
  E2 = Donchian breakout (close > 20-bar high)
  E3 = EMA pullback (price > EMA50, candle = green, prev candle = red)
  E4 = Breakout + volume confirmation
  E5 = Simple candlestick gate (engulfing, hammer) — mirrors the proven
       naked_trader.py approach
"""
import json
import math
import os
import time
from collections import defaultdict, Counter
from statistics import mean, pstdev

from pro_patterns import scan_all as pp_scan_all, avg_range, find_zones, is_in_zone

# ──────────── CONFIG ────────────
STARTING_CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.01
MAX_OPEN_POSITIONS = 5
TAKER_FEE = 0.0005          # Roostoo: 0.05%
SLIPPAGE = 0.0002

GUNNER_1_R = 1.0
GUNNER_1_SIZE = 0.50
GUNNER_2_R = 2.0
GUNNER_2_SIZE = 0.35
RUNNER_TARGET_R = 5.0
RUNNER_TRAIL_ATR_MULT = 2.0

COOLDOWN_CANDLES = 2
WARMUP = 50


# ──────────── DATA ────────────
def resample(candles_1m, minutes):
    if not candles_1m:
        return []
    out = []
    bucket = []
    bstart = None
    for c in candles_1m:
        t = int(c['t'])
        b = (t // (minutes * 60)) * (minutes * 60)
        if bstart is None:
            bstart = b
        if b != bstart:
            if bucket:
                out.append({
                    't': bstart,
                    'o': bucket[0]['o'],
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
            't': bstart,
            'o': bucket[0]['o'],
            'h': max(x['h'] for x in bucket),
            'l': min(x['l'] for x in bucket),
            'c': bucket[-1]['c'],
            'v': sum(x.get('v', 0) for x in bucket),
        })
    return out


def load_data(path, minutes=60):
    with open(path) as fp:
        raw = json.load(fp)
    out = {}
    for coin, candles in raw.items():
        candles = sorted(candles, key=lambda x: x['t'])
        out[coin] = resample(candles, minutes)
    return out


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


# ──────────── ENTRY STRATEGIES ────────────
def entry_patterns(cl):
    """E1: pro patterns (scan_all)."""
    fired, entry, stop, name, quality, _ = pp_scan_all(cl)
    if fired and quality >= 8:
        return (entry, stop, name, quality)
    return None


def entry_donchian(cl):
    """E2: 20-candle Donchian breakout with uptrend filter."""
    if len(cl) < 50:
        return None
    c = cl[-1]
    prev = cl[-2]
    if c['c'] <= c['o']:
        return None  # must be green
    lookback20 = cl[-21:-1]
    prior_high = max(x['h'] for x in lookback20)
    if c['c'] <= prior_high:
        return None  # no breakout
    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if not e20 or not e50 or e20 <= e50:
        return None  # must be uptrend
    # entry, stop
    atr = avg_range(cl, 14)
    entry = c['h'] * 1.0005
    stop = c['l'] - atr * 0.5
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'DONCHIAN', 8)


def entry_ema_pullback(cl):
    """E3: EMA pullback. Price > EMA50, prev candle red, current candle green
    reclaiming prev's high."""
    if len(cl) < 60:
        return None
    c = cl[-1]
    p = cl[-2]
    closes = [x['c'] for x in cl]
    e50 = ema(closes, 50)
    if not e50 or c['c'] < e50:
        return None
    if p['c'] >= p['o']:
        return None  # prev must be red
    if c['c'] <= c['o']:
        return None  # current must be green
    if c['c'] <= p['h']:
        return None  # must reclaim prev high
    atr = avg_range(cl, 14)
    entry = c['h'] * 1.0005
    stop = min(c['l'], p['l']) - atr * 0.3
    if (entry - stop) / entry > 0.025:
        return None
    return (entry, stop, 'EMA_PULLBACK', 7)


def entry_volume_breakout(cl):
    """E4: Breakout + volume > 2x 20-bar avg volume."""
    if len(cl) < 25:
        return None
    c = cl[-1]
    if c['c'] <= c['o']:
        return None
    avg_vol = mean(x.get('v', 0) for x in cl[-21:-1])
    if avg_vol == 0 or c.get('v', 0) < avg_vol * 1.8:
        return None
    prior_high = max(x['h'] for x in cl[-21:-1])
    if c['c'] <= prior_high:
        return None
    atr = avg_range(cl, 14)
    entry = c['h'] * 1.0005
    stop = c['l'] - atr * 0.3
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'VOL_BRK', 8)


def entry_engulfing(cl):
    """E5: Bullish engulfing + uptrend filter (proxies the proven 10-pattern set)."""
    if len(cl) < 50:
        return None
    c = cl[-1]
    p = cl[-2]
    # bullish engulfing
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
    entry = c['h'] * 1.0005
    stop = min(c['l'], p['l']) - atr * 0.3
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'ENGULF', 7)


def entry_combo(cl):
    """E6: Combo — use the best of pro patterns, donchian, engulfing."""
    options = []
    for fn in (entry_patterns, entry_donchian, entry_engulfing):
        r = fn(cl)
        if r:
            options.append(r)
    if not options:
        return None
    # prefer highest quality
    return max(options, key=lambda x: x[3])


STRATEGIES = {
    'E1-patterns': entry_patterns,
    'E2-donchian': entry_donchian,
    'E3-ema-pull': entry_ema_pullback,
    'E4-vol-brk':  entry_volume_breakout,
    'E5-engulf':   entry_engulfing,
    'E6-combo':    entry_combo,
}


# ──────────── RISK MANAGEMENT ENGINE ────────────
class Position:
    __slots__ = ('coin', 'entry_t', 'entry', 'stop', 'initial_stop', 'R',
                 'qty', 'remaining_pct', 'peak', 'atr_ref', 'pattern',
                 'gunner1_done', 'gunner2_done', 'closes')

    def __init__(self, coin, t, entry, stop, qty, atr_ref, pattern):
        self.coin = coin
        self.entry_t = t
        self.entry = entry
        self.stop = stop
        self.initial_stop = stop
        self.R = entry - stop
        self.qty = qty
        self.remaining_pct = 1.0
        self.peak = entry
        self.atr_ref = atr_ref
        self.pattern = pattern
        self.gunner1_done = False
        self.gunner2_done = False
        self.closes = []


def fill(price, side):
    if side == 'buy':
        return price * (1 + SLIPPAGE) * (1 + TAKER_FEE)
    return price * (1 - SLIPPAGE) * (1 - TAKER_FEE)


def close_portion(pos, portion, price, reason):
    pos.closes.append((portion, fill(price, 'sell'), reason))
    pos.remaining_pct -= portion


def realized(pos):
    ep = fill(pos.entry, 'buy')
    dollars = 0
    for portion, px, _ in pos.closes:
        dollars += (px - ep) * pos.qty * portion
    return dollars


def run_single(data, strategy_fn, enable_be=True, use_session=True,
               use_zone_filter=False, use_cooldown=True):
    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()
    if not events:
        return None

    equity = STARTING_CAPITAL
    peak_equity = equity
    max_dd_pct = 0
    max_dd_dollar = 0
    positions = {}
    trades = []
    cooldown = defaultdict(int)

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # manage open position
        if coin in positions:
            pos = positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            closed_all = False

            if cur['l'] <= pos.stop:
                close_portion(pos, pos.remaining_pct, pos.stop, 'STOP')
                closed_all = True
            else:
                if not pos.gunner1_done:
                    g1 = pos.entry + GUNNER_1_R * pos.R
                    if cur['h'] >= g1:
                        close_portion(pos, GUNNER_1_SIZE, g1, 'G1')
                        pos.gunner1_done = True
                        if enable_be:
                            pos.stop = pos.entry
                if not pos.gunner2_done and pos.gunner1_done:
                    g2 = pos.entry + GUNNER_2_R * pos.R
                    if cur['h'] >= g2:
                        close_portion(pos, GUNNER_2_SIZE, g2, 'G2')
                        pos.gunner2_done = True
                if pos.remaining_pct > 0 and pos.gunner2_done:
                    hard = pos.entry + RUNNER_TARGET_R * pos.R
                    if cur['h'] >= hard:
                        close_portion(pos, pos.remaining_pct, hard, 'RUN_TP')
                        closed_all = True
                    else:
                        trail = pos.peak - RUNNER_TRAIL_ATR_MULT * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            close_portion(pos, pos.remaining_pct, pos.stop, 'TRAIL')
                            closed_all = True
                if pos.remaining_pct <= 1e-6:
                    closed_all = True

            if closed_all:
                pnl = realized(pos)
                equity += pnl
                r_mult = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                trades.append({
                    'coin': pos.coin, 'pattern': pos.pattern,
                    'entry_t': pos.entry_t, 'exit_t': t,
                    'pnl': pnl, 'r': r_mult, 'win': pnl > 0,
                })
                del positions[coin]
                if use_cooldown and pnl <= 0:
                    cooldown[coin] = COOLDOWN_CANDLES

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_dd_pct:
                max_dd_pct = dd
                max_dd_dollar = peak_equity - equity

        # entry
        if idx < WARMUP or coin in positions:
            continue
        if cooldown[coin] > 0:
            cooldown[coin] -= 1
            continue
        if len(positions) >= MAX_OPEN_POSITIONS:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue

        sig = strategy_fn(cl)
        if not sig:
            continue
        entry, stop, name, quality = sig
        if entry <= 0 or stop <= 0 or entry <= stop:
            continue

        if use_zone_filter:
            zones = find_zones(cl, lookback=50)
            in_z, _ = is_in_zone(cur['c'], zones)
            zone_ok = in_z or any(
                z['type'] == 'support' and 0 < (entry - z['level']) / entry < 0.02
                for z in zones
            )
            if not zone_ok:
                continue

        R = entry - stop
        if R / entry > 0.04:
            continue
        risk = equity * RISK_PER_TRADE
        qty = risk / R
        notional = qty * entry
        cap = equity * 0.20
        if notional > cap:
            qty = cap / entry
        atr_ref = avg_range(cl, 14)
        pos = Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    # EOD close
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = realized(pos)
        equity += pnl
        r_mult = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
        trades.append({
            'coin': pos.coin, 'pattern': pos.pattern,
            'entry_t': pos.entry_t, 'exit_t': last_t,
            'pnl': pnl, 'r': r_mult, 'win': pnl > 0,
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'avg_r': 0, 'dd': 0}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    avg_r = mean(t['r'] for t in trades)
    pnl = sum(t['pnl'] for t in trades)
    by_pat = Counter(t['pattern'] for t in trades)
    return {
        'trades': len(trades),
        'wr': wr,
        'pf': pf,
        'pnl': pnl,
        'avg_r': avg_r,
        'dd': max_dd_pct,
        'by_pat': dict(by_pat),
    }


# ──────────── MAIN ────────────
def main():
    paths = [
        ('data/binance_1m_7d.json', 'D1'),
        ('data/1min_7days.json', 'D2'),
    ]
    datasets = {lbl: load_data(p, 60) for p, lbl in paths if os.path.exists(p)}
    print(f"loaded {len(datasets)} datasets, 1H candles")

    results = defaultdict(dict)

    CONFIGS = [
        ('default', {'enable_be': True,  'use_session': True,  'use_zone_filter': False}),
        ('noBE',    {'enable_be': False, 'use_session': True,  'use_zone_filter': False}),
        ('noSes',   {'enable_be': True,  'use_session': False, 'use_zone_filter': False}),
        ('zone',    {'enable_be': True,  'use_session': True,  'use_zone_filter': True}),
    ]

    for strat_name, fn in STRATEGIES.items():
        for cfg_name, cfg in CONFIGS:
            row = {}
            for lbl, data in datasets.items():
                r = run_single(data, fn, **cfg)
                row[lbl] = r
            # summary
            total = sum(row[l]['pnl'] for l in row)
            worst = min(row[l]['pnl'] for l in row)
            tr = sum(row[l]['trades'] for l in row)
            wr = mean(row[l]['wr'] for l in row if row[l]['trades'] > 0) \
                if any(row[l]['trades'] for l in row) else 0
            pf_vals = [row[l]['pf'] for l in row if row[l]['pf'] != float('inf') and row[l]['trades'] > 0]
            pf = mean(pf_vals) if pf_vals else 0
            dd = max(row[l]['dd'] for l in row)
            results[strat_name][cfg_name] = {
                'total': total, 'worst': worst, 'trades': tr,
                'wr': wr, 'pf': pf, 'dd': dd, 'rows': row,
            }
            print(f"{strat_name:14} {cfg_name:8} tr={tr:4}  wr={wr*100:5.1f}%  pf={pf:4.2f}  "
                  f"tot=${total:+,.0f}  worst=${worst:+,.0f}  dd={dd*100:4.1f}%")

    # Find the best strategy/config pair
    print("\n══════════ TOP 10 COMBOS ══════════")
    flat = []
    for s, cfgs in results.items():
        for c, r in cfgs.items():
            flat.append((s, c, r))
    flat.sort(key=lambda x: -x[2]['total'])
    for s, c, r in flat[:10]:
        flag = 'ALL+' if r['worst'] > 0 else '✗'
        print(f"  {s:14} {c:8} total=${r['total']:+,.0f}  worst=${r['worst']:+,.0f}  "
              f"wr={r['wr']*100:5.1f}%  pf={r['pf']:4.2f}  dd={r['dd']*100:4.1f}%  {flag}")

    # Save
    serializable = {}
    for s, cfgs in results.items():
        serializable[s] = {}
        for c, r in cfgs.items():
            serializable[s][c] = {k: v for k, v in r.items() if k != 'rows'}
            serializable[s][c]['by_dataset'] = {lbl: {k: v for k, v in r['rows'][lbl].items() if k != 'by_pat'}
                                                for lbl in r['rows']}
    with open('data/pro_tune_v2_results.json', 'w') as fp:
        json.dump(serializable, fp, indent=2, default=str)
    print("\nsaved → data/pro_tune_v2_results.json")


if __name__ == '__main__':
    main()
