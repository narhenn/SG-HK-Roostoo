"""
CRYPTO-NATIVE BACKTEST
════════════════════════════════════════════════════════════════════
Rebuilt from backtest_pro_v2.py with all forex-specific rules stripped
and crypto-native rules added.

Differences from backtest_pro_v2.py:
  - NO session filter (00-07 UTC was forex nonsense in crypto)
  - Volatility regime filter (skip chop, trade expansion)
  - Crypto-native patterns only (failed breakdown, bull flag, etc.)
  - Rebalanced scale-out: 30/30/40 with wider runner trail (3x ATR)
  - Higher runner target: +6R (crypto can run further than forex)
  - Multi-timeframe option: 15m / 30m / 1H / 4H sweeps
  - ATR-based entry/stop cushions (not fixed pip %)

Every config is tested against BOTH D1 and D2 datasets. Configs that
lose money on either dataset are rejected.
"""
import json
import math
import os
import time
from collections import defaultdict, Counter
from statistics import mean, pstdev

from crypto_patterns import (
    scan_all as cp_scan_all,
    atr,
    ema,
    avg_range,
    avg_volume,
    is_uptrend,
    vol_regime_ok,
)


# ────────────── CONFIG (defaults; tuning sweep will override) ──────────────
STARTING_CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.02
MAX_NOTIONAL_PCT = 0.50
MAX_OPEN_POSITIONS = 5
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002

# Rebalanced scale-out (crypto-tuned)
GUNNER_1_R = 1.0
GUNNER_1_SIZE = 0.30
GUNNER_2_R = 3.0
GUNNER_2_SIZE = 0.30
RUNNER_TARGET_R = 6.0
RUNNER_SIZE = 0.40
RUNNER_TRAIL_ATR_MULT = 3.0  # wider trail — let pumps run

COOLDOWN_CANDLES = 2
WARMUP = 60
USE_VOL_REGIME = True
MIN_ATR_RATIO = 0.7


# ────────────── DATA ──────────────
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


# ────────────── ENTRY STRATEGIES ──────────────
def entry_crypto_patterns(cl):
    """E1: crypto-native pattern scan."""
    fired, entry, stop, name, quality, _ = cp_scan_all(cl)
    if fired and quality >= 7:
        return (entry, stop, name, quality)
    return None


def entry_donchian_strict(cl):
    """E2: Donchian with strong trend filter + volume."""
    if len(cl) < 60:
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
    # Volume filter
    avg_v = avg_volume(cl[:-1], 20)
    if c.get('v', 0) < avg_v * 1.2:
        return None
    a = atr(cl, 14)
    entry = c['h'] + a * 0.1
    stop = c['l'] - a * 0.5
    if (entry - stop) / entry > 0.05:
        return None
    return (entry, stop, 'DONCHIAN', 8)


def entry_trend_pullback(cl):
    """E3: strong uptrend + pullback to EMA20 + bounce."""
    if len(cl) < 50:
        return None
    c = cl[-1]
    if c['c'] <= c['o']:
        return None
    if not is_uptrend(cl):
        return None
    closes = [x['c'] for x in cl]
    e20 = ema(closes, 20)
    if e20 is None:
        return None
    # Recent pullback to EMA20
    touched_e20 = False
    for x in cl[-6:-1]:
        if x['l'] <= e20 * 1.005:
            touched_e20 = True
            break
    if not touched_e20:
        return None
    # Current candle bounces (green + closes above prev high)
    prev = cl[-2]
    if c['c'] <= prev['h']:
        return None
    a = atr(cl, 14)
    entry = c['h'] + a * 0.1
    stop = min(x['l'] for x in cl[-6:]) - a * 0.2
    if (entry - stop) / entry > 0.04:
        return None
    return (entry, stop, 'TREND_PB', 8)


def entry_combo(cl):
    """Best of all crypto strategies."""
    options = []
    for fn in (entry_crypto_patterns, entry_donchian_strict, entry_trend_pullback):
        try:
            r = fn(cl)
            if r:
                options.append(r)
        except Exception:
            pass
    if not options:
        return None
    return max(options, key=lambda x: x[3])


STRATEGIES = {
    'C1-patterns': entry_crypto_patterns,
    'C2-donchian': entry_donchian_strict,
    'C3-pullback': entry_trend_pullback,
    'C4-combo':    entry_combo,
}


# ────────────── POSITION ──────────────
class Position:
    __slots__ = ('coin', 'entry_t', 'entry', 'stop', 'initial_stop', 'R',
                 'qty', 'remaining_pct', 'peak', 'atr_ref', 'pattern',
                 'g1_done', 'g2_done', 'closes')

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
        self.g1_done = False
        self.g2_done = False
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


# ────────────── ENGINE ──────────────
def run_single(data, strategy_fn, enable_be=True, use_vol_regime=True,
               use_cooldown=True, cap_pct=None, g1_size=None, g2_size=None,
               trail_mult=None, runner_target=None):
    cap_pct = cap_pct if cap_pct is not None else MAX_NOTIONAL_PCT
    g1_size = g1_size if g1_size is not None else GUNNER_1_SIZE
    g2_size = g2_size if g2_size is not None else GUNNER_2_SIZE
    trail_mult = trail_mult if trail_mult is not None else RUNNER_TRAIL_ATR_MULT
    runner_target = runner_target if runner_target is not None else RUNNER_TARGET_R

    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()
    if not events:
        return None

    equity = STARTING_CAPITAL
    peak_eq = equity
    max_dd = 0
    positions = {}
    trades = []
    cd = defaultdict(int)

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # ── manage open position ──
        if coin in positions:
            pos = positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            closed = False

            if cur['l'] <= pos.stop:
                close_portion(pos, pos.remaining_pct, pos.stop, 'STOP')
                closed = True
            else:
                if not pos.g1_done:
                    g1p = pos.entry + GUNNER_1_R * pos.R
                    if cur['h'] >= g1p:
                        close_portion(pos, g1_size, g1p, 'G1')
                        pos.g1_done = True
                        if enable_be:
                            pos.stop = pos.entry

                if pos.g1_done and not pos.g2_done:
                    g2p = pos.entry + GUNNER_2_R * pos.R
                    if cur['h'] >= g2p:
                        close_portion(pos, g2_size, g2p, 'G2')
                        pos.g2_done = True

                if pos.g2_done and pos.remaining_pct > 0:
                    hard = pos.entry + runner_target * pos.R
                    if cur['h'] >= hard:
                        close_portion(pos, pos.remaining_pct, hard, 'RUN_TP')
                        closed = True
                    else:
                        trail = pos.peak - trail_mult * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            close_portion(pos, pos.remaining_pct, pos.stop, 'TRAIL')
                            closed = True

                if pos.remaining_pct <= 1e-6:
                    closed = True

            if closed:
                pnl = realized(pos)
                equity += pnl
                r_mult = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                trades.append({
                    'coin': pos.coin, 'pattern': pos.pattern,
                    'pnl': pnl, 'r': r_mult, 'win': pnl > 0,
                })
                del positions[coin]
                if use_cooldown and pnl <= 0:
                    cd[coin] = COOLDOWN_CANDLES

            if equity > peak_eq:
                peak_eq = equity
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # ── try entry ──
        if idx < WARMUP or coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= MAX_OPEN_POSITIONS:
            continue
        if use_vol_regime and not vol_regime_ok(cl, MIN_ATR_RATIO):
            continue

        sig = strategy_fn(cl)
        if not sig:
            continue
        entry, stop, name, quality = sig
        if entry <= 0 or stop <= 0 or entry <= stop:
            continue
        R = entry - stop
        if R / entry > 0.06:
            continue
        risk = equity * RISK_PER_TRADE
        qty = risk / R
        notional = qty * entry
        cap = equity * cap_pct
        if notional > cap:
            qty = cap / entry
        atr_ref = atr(cl, 14)
        pos = Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    # force-close at end
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = realized(pos)
        equity += pnl
        trades.append({
            'coin': pos.coin, 'pattern': pos.pattern,
            'pnl': pnl, 'r': pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0,
            'win': pnl > 0,
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'avg_r': 0, 'dd': 0,
                'by_pat': {}}

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
        'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl,
        'avg_r': avg_r, 'dd': max_dd, 'by_pat': dict(by_pat),
    }


# ────────────── MAIN ──────────────
def main():
    paths = [
        ('data/binance_1m_7d.json', 'D1'),
        ('data/1min_7days.json', 'D2'),
    ]

    # Sweep multiple timeframes
    all_results = []
    print(f"{'strat':14} {'tf':4} {'cfg':12} {'trades':>6}  {'wr':>5}  {'pf':>5}  {'total':>12}  {'worst':>12}  {'dd':>5}")
    for tf in [30, 60, 120, 240]:
        datasets = {lbl: load_data(p, tf) for p, lbl in paths if os.path.exists(p)}
        if not datasets:
            continue

        for strat_name, fn in STRATEGIES.items():
            for cfg_name, kw in [
                ('default', {}),
                ('noBE',    {'enable_be': False}),
                ('noVolR',  {'use_vol_regime': False}),
                ('cap100',  {'cap_pct': 1.0}),
                ('cap70',   {'cap_pct': 0.7}),
                ('run8R',   {'runner_target': 8.0}),
                ('trail4',  {'trail_mult': 4.0}),
                ('wide',    {'cap_pct': 1.0, 'runner_target': 8.0, 'trail_mult': 4.0}),
            ]:
                row = {lbl: run_single(data, fn, **kw) for lbl, data in datasets.items()}
                total = sum(r['pnl'] for r in row.values())
                worst = min(r['pnl'] for r in row.values())
                tr = sum(r['trades'] for r in row.values())
                wrs = [r['wr'] for r in row.values() if r['trades'] > 0]
                wr = mean(wrs) if wrs else 0
                pfs = [r['pf'] for r in row.values() if r['pf'] != float('inf') and r['trades'] > 0]
                pf = mean(pfs) if pfs else 0
                dd = max(r['dd'] for r in row.values())
                all_results.append({
                    'strat': strat_name, 'tf': tf, 'cfg': cfg_name,
                    'trades': tr, 'wr': wr, 'pf': pf, 'total': total,
                    'worst': worst, 'dd': dd, 'rows': row,
                })
                flag = '✓' if worst > 0 else '✗'
                print(f"{strat_name:14} {tf:3}m {cfg_name:12} {tr:6}  {wr*100:4.1f}%  {pf:5.2f}  "
                      f"${total:+11,.0f}  ${worst:+11,.0f}  {dd*100:4.1f}%  {flag}")

    # Filter and rank
    print("\n══════════ TOP 15 (both datasets positive) ══════════")
    positive = [r for r in all_results if r['worst'] > 0]
    positive.sort(key=lambda x: -x['total'])
    for r in positive[:15]:
        print(f"  {r['strat']:14} tf={r['tf']:3}m  cfg={r['cfg']:10}  total=${r['total']:+,.0f}  "
              f"worst=${r['worst']:+,.0f}  wr={r['wr']*100:4.1f}%  pf={r['pf']:.2f}  dd={r['dd']*100:.1f}%")

    print("\n══════════ TOP 10 OVERALL (by total P&L, may not be robust) ══════════")
    all_results.sort(key=lambda x: -x['total'])
    for r in all_results[:10]:
        flag = '✓' if r['worst'] > 0 else '✗'
        print(f"  {r['strat']:14} tf={r['tf']:3}m  cfg={r['cfg']:10}  total=${r['total']:+,.0f}  "
              f"worst=${r['worst']:+,.0f}  wr={r['wr']*100:4.1f}%  pf={r['pf']:.2f}  {flag}")

    # Save
    out = []
    for r in all_results:
        out.append({k: v for k, v in r.items() if k != 'rows'})
    with open('data/crypto_backtest_results.json', 'w') as fp:
        json.dump(out, fp, indent=2, default=str)
    print(f"\nsaved → data/crypto_backtest_results.json ({len(out)} results)")


if __name__ == '__main__':
    main()
