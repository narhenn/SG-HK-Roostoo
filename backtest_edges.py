"""
BACKTEST EDGES — Option C
════════════════════════════════════════════════════════════════════
Real new-edge exploration (not just repackaging patterns).

Edges to test on top of the proven baseline E6-combo:

  EDGE 1 — MULTI-TIMEFRAME CONFIRMATION
    Compute 4H candles in parallel with 1H. Only take a 1H signal if the
    containing 4H candle (or recent 4H closes) show an uptrend:
    4H EMA20 > 4H EMA50.

  EDGE 2 — BTC REGIME FILTER
    Compute a 1H EMA200 on BTC. Only take ANY long signal (on any coin)
    when BTC close > BTC EMA200. Stand aside when BTC is in a bear regime.

  EDGE 3 — MARKET BREADTH FILTER
    Count how many of the 20 coins closed green over the last N candles.
    Only take entries when breadth > 55%.

  EDGE 4 — VOLATILITY-SCALED POSITION SIZING
    When current ATR > 120% of 50-bar avg ATR → risk 2.5% of equity (push).
    When current ATR between 80%-120% → risk 2% (normal).
    When current ATR < 80% → risk 1% (defend, chop market).

  EDGE 5 — DYNAMIC R TARGETS
    In high-vol regime, extend runner target to +8R. In normal vol, 5R.
    In low vol, 3R. Matches market potential.

  EDGE 6 — COIN ROTATION
    Rank coins by 24h momentum; only consider entries on the top 10.

Each is tested individually and in combination against the baseline.
Baseline: +$52,845 total (D1 +$20,643, D2 +$32,202) at risk=2%, cap=50%.
"""
import json
import os
import time
from collections import defaultdict, Counter
from statistics import mean

import backtest_pro_v2 as bp
from pro_patterns import scan_all as pp_scan_all


STARTING_CAPITAL = 1_000_000.0


# ─── MULTI-TIMEFRAME HELPERS ───
def resample_to_4h(candles_1h):
    """Fold 1H candles into 4H candles."""
    if not candles_1h:
        return []
    out = []
    bucket = []
    bucket_start = None
    for c in candles_1h:
        t = int(c['t'])
        b = (t // (4 * 3600)) * (4 * 3600)
        if bucket_start is None:
            bucket_start = b
        if b != bucket_start:
            if bucket:
                out.append({
                    't': bucket_start,
                    'o': bucket[0]['o'],
                    'h': max(x['h'] for x in bucket),
                    'l': min(x['l'] for x in bucket),
                    'c': bucket[-1]['c'],
                    'v': sum(x.get('v', 0) for x in bucket),
                })
            bucket = [c]
            bucket_start = b
        else:
            bucket.append(c)
    if bucket:
        out.append({
            't': bucket_start,
            'o': bucket[0]['o'],
            'h': max(x['h'] for x in bucket),
            'l': min(x['l'] for x in bucket),
            'c': bucket[-1]['c'],
            'v': sum(x.get('v', 0) for x in bucket),
        })
    return out


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def higher_tf_uptrend(cl_1h):
    """Check if 4H (derived from 1H) is in uptrend: 4H EMA20 > 4H EMA50."""
    cl_4h = resample_to_4h(cl_1h)
    if len(cl_4h) < 50:
        return True  # not enough history — allow
    closes_4h = [x['c'] for x in cl_4h]
    e20_4h = ema(closes_4h, 20)
    e50_4h = ema(closes_4h, 50)
    if e20_4h is None or e50_4h is None:
        return True
    return cl_4h[-1]['c'] > e20_4h > e50_4h


def btc_regime_ok(btc_cl, min_ema=200):
    """BTC > 200 EMA = bull regime = ok to go long anywhere."""
    if not btc_cl or len(btc_cl) < min_ema:
        return True  # not enough history — allow
    closes = [x['c'] for x in btc_cl]
    e200 = ema(closes, min_ema)
    if e200 is None:
        return True
    return btc_cl[-1]['c'] > e200


def market_breadth(all_coins_data, at_time, lookback=5, threshold=0.55):
    """Count coins that closed higher than N candles ago."""
    green = 0
    total = 0
    for coin, candles in all_coins_data.items():
        # Find the candle at or just before at_time
        idx = None
        for i in range(len(candles) - 1, -1, -1):
            if candles[i]['t'] <= at_time:
                idx = i
                break
        if idx is None or idx < lookback:
            continue
        total += 1
        if candles[idx]['c'] > candles[idx - lookback]['c']:
            green += 1
    if total == 0:
        return True
    return (green / total) >= threshold


def vol_regime(cl, atr_period=14, lookback=50):
    """Return 'high', 'normal', or 'low' based on current ATR vs historical."""
    if len(cl) < atr_period + lookback:
        return 'normal'
    # current ATR
    cur_atr = sum((x['h'] - x['l']) for x in cl[-atr_period:]) / atr_period
    # historical avg of ATR over last `lookback` bars
    historical = []
    for i in range(1, lookback + 1):
        if len(cl) > i + atr_period:
            historical.append(sum((x['h'] - x['l']) for x in cl[-i - atr_period:-i]) / atr_period)
    if not historical:
        return 'normal'
    avg = mean(historical)
    if avg == 0:
        return 'normal'
    ratio = cur_atr / avg
    if ratio > 1.2:
        return 'high'
    if ratio < 0.8:
        return 'low'
    return 'normal'


def coin_momentum_rank(all_coins_data, at_time, top_n=10, lookback=20):
    """Return set of top-N coins by 20-candle momentum at at_time."""
    scores = []
    for coin, candles in all_coins_data.items():
        idx = None
        for i in range(len(candles) - 1, -1, -1):
            if candles[i]['t'] <= at_time:
                idx = i
                break
        if idx is None or idx < lookback:
            continue
        ret = (candles[idx]['c'] - candles[idx - lookback]['c']) / candles[idx - lookback]['c']
        scores.append((coin, ret))
    scores.sort(key=lambda x: -x[1])
    return {coin for coin, _ in scores[:top_n]}


# ─── ENGINE WITH EDGES ───
class Position:
    __slots__ = ('coin', 'entry_t', 'entry', 'stop', 'R', 'qty',
                 'remaining_pct', 'peak', 'atr_ref', 'pattern',
                 'g1_done', 'g2_done', 'closes', 'risk_used')

    def __init__(self, coin, t, entry, stop, qty, atr_ref, pattern, risk_used):
        self.coin = coin
        self.entry_t = t
        self.entry = entry
        self.stop = stop
        self.R = entry - stop
        self.qty = qty
        self.remaining_pct = 1.0
        self.peak = entry
        self.atr_ref = atr_ref
        self.pattern = pattern
        self.g1_done = False
        self.g2_done = False
        self.closes = []
        self.risk_used = risk_used


def run_with_edges(data, strategy_fn, *,
                   risk=0.02, cap_pct=0.5, enable_be=True, use_session=True,
                   # edges
                   use_mtf=False,
                   use_btc_regime=False,
                   use_breadth=False, breadth_threshold=0.55, breadth_lookback=5,
                   use_vol_sizing=False,
                   use_vol_targets=False,
                   use_coin_rotation=False, top_n_coins=10):
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

    # pre-extract BTC for regime filter
    btc_candles = data.get('BTC', [])

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # ── manage open ──
        if coin in positions:
            pos = positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            closed = False

            if cur['l'] <= pos.stop:
                pos.closes.append((pos.remaining_pct, bp.fill(pos.stop, 'sell'), 'STOP'))
                pos.remaining_pct = 0
                closed = True
            else:
                # Dynamic R targets based on vol regime at entry
                if use_vol_targets:
                    vr = vol_regime(candles[:idx + 1])
                    if vr == 'high':
                        g1_r, g2_r, run_r = 1.0, 2.5, 8.0
                    elif vr == 'low':
                        g1_r, g2_r, run_r = 0.8, 1.5, 3.0
                    else:
                        g1_r, g2_r, run_r = 1.0, 2.0, 5.0
                else:
                    g1_r, g2_r, run_r = 1.0, 2.0, 5.0

                if not pos.g1_done:
                    g1p = pos.entry + g1_r * pos.R
                    if cur['h'] >= g1p:
                        pos.closes.append((0.5, bp.fill(g1p, 'sell'), 'G1'))
                        pos.remaining_pct -= 0.5
                        pos.g1_done = True
                        if enable_be:
                            pos.stop = pos.entry

                if pos.g1_done and not pos.g2_done:
                    g2p = pos.entry + g2_r * pos.R
                    if cur['h'] >= g2p:
                        pos.closes.append((0.35, bp.fill(g2p, 'sell'), 'G2'))
                        pos.remaining_pct -= 0.35
                        pos.g2_done = True

                if pos.g2_done and pos.remaining_pct > 0:
                    hard = pos.entry + run_r * pos.R
                    if cur['h'] >= hard:
                        pos.closes.append((pos.remaining_pct, bp.fill(hard, 'sell'), 'RUN_TP'))
                        pos.remaining_pct = 0
                        closed = True
                    else:
                        trail = pos.peak - 2.0 * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            pos.closes.append((pos.remaining_pct, bp.fill(pos.stop, 'sell'), 'TRAIL'))
                            pos.remaining_pct = 0
                            closed = True

                if pos.remaining_pct <= 1e-6:
                    closed = True

            if closed:
                ep = bp.fill(pos.entry, 'buy')
                pnl = sum((px - ep) * pos.qty * portion for portion, px, _ in pos.closes)
                equity += pnl
                r_mult = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                trades.append({
                    'coin': pos.coin, 'pattern': pos.pattern,
                    'pnl': pnl, 'r': r_mult, 'win': pnl > 0,
                })
                del positions[coin]
                if pnl <= 0:
                    cd[coin] = bp.COOLDOWN_CANDLES_AFTER_STOP if hasattr(bp, 'COOLDOWN_CANDLES_AFTER_STOP') else 2

            if equity > peak_eq:
                peak_eq = equity
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # ── entry ──
        if idx < bp.WARMUP if hasattr(bp, 'WARMUP') else 50:
            continue
        if coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= 5:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue

        # EDGE 1: Multi-timeframe confirmation
        if use_mtf and not higher_tf_uptrend(cl):
            continue

        # EDGE 2: BTC regime filter
        if use_btc_regime:
            # Get BTC candles up to this time
            btc_idx = None
            for i in range(len(btc_candles) - 1, -1, -1):
                if btc_candles[i]['t'] <= t:
                    btc_idx = i
                    break
            if btc_idx is not None:
                btc_slice = btc_candles[:btc_idx + 1]
                if not btc_regime_ok(btc_slice, min_ema=200):
                    continue

        # EDGE 3: Market breadth filter
        if use_breadth:
            if not market_breadth(data, t, breadth_lookback, breadth_threshold):
                continue

        # EDGE 6: Coin rotation
        if use_coin_rotation:
            top_coins = coin_momentum_rank(data, t, top_n=top_n_coins)
            if coin not in top_coins:
                continue

        sig = strategy_fn(cl)
        if not sig:
            continue
        entry, stop, name, quality = sig
        if entry <= 0 or stop <= 0 or entry <= stop:
            continue
        R = entry - stop
        if R / entry > 0.04:
            continue

        # EDGE 4: Volatility-scaled sizing
        if use_vol_sizing:
            vr = vol_regime(cl)
            if vr == 'high':
                risk_used = risk * 1.25
            elif vr == 'low':
                risk_used = risk * 0.5
            else:
                risk_used = risk
        else:
            risk_used = risk

        risk_dollars = equity * risk_used
        qty = risk_dollars / R
        notional = qty * entry
        cap = equity * cap_pct
        if notional > cap:
            qty = cap / entry
        atr_ref = bp.avg_range(cl, 14)
        pos = Position(coin, t, entry, stop, qty, atr_ref, name, risk_used)
        positions[coin] = pos

    # EOD close
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        pos.closes.append((pos.remaining_pct, bp.fill(last_c['c'], 'sell'), 'EOD'))
        ep = bp.fill(pos.entry, 'buy')
        pnl = sum((px - ep) * pos.qty * portion for portion, px, _ in pos.closes)
        equity += pnl
        trades.append({
            'coin': pos.coin, 'pattern': pos.pattern,
            'pnl': pnl,
            'r': pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0,
            'win': pnl > 0,
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0, 'by_pat': {}}
    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)
    return {'trades': len(trades), 'wr': wr, 'pf': pf,
            'pnl': pnl, 'dd': max_dd,
            'by_pat': Counter(t['pattern'] for t in trades)}


# ─── MAIN ───
def main():
    datasets = {
        'D1': bp.load_data('data/binance_1m_7d.json', 60),
        'D2': bp.load_data('data/1min_7days.json', 60),
    }

    BASELINE = '+$52,845 (D1 +$20,643 D2 +$32,202), 48 trades, 62.6% WR'

    # Every edge as a switch. Each config is a dict of switches.
    configs = [
        ('baseline', {}),
        ('mtf',      {'use_mtf': True}),
        ('btc',      {'use_btc_regime': True}),
        ('breadth55',{'use_breadth': True, 'breadth_threshold': 0.55}),
        ('breadth60',{'use_breadth': True, 'breadth_threshold': 0.60}),
        ('breadth50',{'use_breadth': True, 'breadth_threshold': 0.50}),
        ('volSize',  {'use_vol_sizing': True}),
        ('volTgt',   {'use_vol_targets': True}),
        ('rotation10',{'use_coin_rotation': True, 'top_n_coins': 10}),
        ('rotation5', {'use_coin_rotation': True, 'top_n_coins': 5}),
        # combos
        ('mtf+btc',              {'use_mtf': True, 'use_btc_regime': True}),
        ('mtf+btc+breadth',      {'use_mtf': True, 'use_btc_regime': True, 'use_breadth': True}),
        ('mtf+volSize',          {'use_mtf': True, 'use_vol_sizing': True}),
        ('btc+rotation10',       {'use_btc_regime': True, 'use_coin_rotation': True, 'top_n_coins': 10}),
        ('mtf+btc+rotation10',   {'use_mtf': True, 'use_btc_regime': True, 'use_coin_rotation': True, 'top_n_coins': 10}),
        ('mtf+volTgt',           {'use_mtf': True, 'use_vol_targets': True}),
        ('all',                  {'use_mtf': True, 'use_btc_regime': True, 'use_breadth': True, 'use_vol_sizing': True, 'use_vol_targets': True, 'use_coin_rotation': True, 'top_n_coins': 10}),
    ]

    # run each config with cap=50% baseline
    results = []
    print(f"baseline target:  {BASELINE}\n")
    print(f"{'cfg':28} {'tr':>4}  {'wr':>5}  {'pf':>5}  {'D1':>12}  {'D2':>12}  {'total':>12}  {'dd':>5}")
    for name, kwargs in configs:
        row = {lbl: run_with_edges(data, bp.entry_combo, risk=0.02, cap_pct=0.5, **kwargs)
               for lbl, data in datasets.items()}
        total = sum(r['pnl'] for r in row.values())
        worst = min(r['pnl'] for r in row.values())
        tr = sum(r['trades'] for r in row.values())
        wrs = [r['wr'] for r in row.values() if r['trades'] > 0]
        wr = mean(wrs) if wrs else 0
        pfs = [r['pf'] for r in row.values() if r['pf'] != float('inf') and r['trades'] > 0]
        pf = mean(pfs) if pfs else 0
        dd = max(r['dd'] for r in row.values())
        results.append({
            'name': name, 'kwargs': kwargs,
            'trades': tr, 'wr': wr, 'pf': pf, 'total': total, 'worst': worst, 'dd': dd,
            'D1': row['D1']['pnl'], 'D2': row['D2']['pnl'],
        })
        flag = '✓' if worst > 0 else '✗'
        print(f"{name:28} {tr:4}  {wr*100:4.1f}%  {pf:4.2f}  ${row['D1']['pnl']:+11,.0f}  "
              f"${row['D2']['pnl']:+11,.0f}  ${total:+11,.0f}  {dd*100:4.1f}%  {flag}")

    # rank robust configs
    robust = [r for r in results if r['worst'] > 0]
    robust.sort(key=lambda x: -x['total'])
    print(f"\n══════════ TOP ROBUST ({len(robust)}) ══════════")
    for r in robust[:15]:
        improvement = r['total'] - 52845
        marker = '🏆' if improvement > 0 else ' '
        print(f"  {marker} {r['name']:28} tot=${r['total']:+,.0f}  Δ vs baseline=${improvement:+,.0f}  "
              f"wr={r['wr']*100:4.1f}%  dd={r['dd']*100:.1f}%")

    # save
    out = [{k: v for k, v in r.items() if k != 'kwargs'} for r in results]
    for o, r in zip(out, results):
        o['switches'] = r['kwargs']
    with open('data/edges_results.json', 'w') as fp:
        json.dump(out, fp, indent=2, default=str)
    print(f"\nsaved → data/edges_results.json")


if __name__ == '__main__':
    main()
