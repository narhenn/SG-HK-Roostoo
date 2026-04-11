"""
MILK MODE SWEEP — extract max possible P&L
════════════════════════════════════════════════════════════════════
User has 4 days left, wants maximum profit extraction, accepts high DD.

Sweeping:
  - risk per trade: 3%, 4%, 5%, 6%, 8%, 10%
  - notional cap: 100%, 200%, 300%, 400%, 500%, 1000%
  - max concurrent positions: 5, 7, 10
  - min signal quality: 6, 7, 8
  - max open positions: 5, 7, 10
  - session filter: on/off
  - runner target: 5R, 8R, 10R

Only robust configs (positive on BOTH D1 and D2) are considered.
The goal is to find the absolute ceiling, not a conservative default.
"""
import json
import time
from collections import defaultdict, Counter
from statistics import mean

import backtest_pro_v2 as bp
from pro_patterns import scan_all as pp_scan_all


STARTING_CAPITAL = 1_000_000.0


def run_milk(data, risk, cap_pct, max_pos, min_q, use_session, runner_r, trail_mult,
             g1_r=1.0, g1_size=0.5, g2_r=2.0, g2_size=0.35):
    """Parameterized engine. Uses pro_v2 entry_combo but with adjustable min quality."""

    # Monkey patch bp entries quality filter
    def entry_pat_q(cl):
        fired, entry, stop, name, quality, _ = pp_scan_all(cl)
        if fired and quality >= min_q:
            return (entry, stop, name, quality)
        return None

    def combo(cl):
        opts = []
        for fn in (entry_pat_q, bp.entry_donchian, bp.entry_engulfing):
            r = fn(cl)
            if r:
                opts.append(r)
        if not opts:
            return None
        return max(opts, key=lambda x: x[3])

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

        if coin in positions:
            pos = positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            ca = False
            if cur['l'] <= pos.stop:
                bp.close_portion(pos, pos.remaining_pct, pos.stop, 'STOP')
                ca = True
            else:
                if not pos.gunner1_done:
                    g1p = pos.entry + g1_r * pos.R
                    if cur['h'] >= g1p:
                        bp.close_portion(pos, g1_size, g1p, 'G1')
                        pos.gunner1_done = True
                        pos.stop = pos.entry  # BE bump
                if not pos.gunner2_done and pos.gunner1_done:
                    g2p = pos.entry + g2_r * pos.R
                    if cur['h'] >= g2p:
                        bp.close_portion(pos, g2_size, g2p, 'G2')
                        pos.gunner2_done = True
                if pos.remaining_pct > 0 and pos.gunner2_done:
                    hard = pos.entry + runner_r * pos.R
                    if cur['h'] >= hard:
                        bp.close_portion(pos, pos.remaining_pct, hard, 'RUN_TP')
                        ca = True
                    else:
                        trail = pos.peak - trail_mult * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            bp.close_portion(pos, pos.remaining_pct, pos.stop, 'TRAIL')
                            ca = True
                if pos.remaining_pct <= 1e-6:
                    ca = True

            if ca:
                pnl = bp.realized(pos)
                equity += pnl
                trades.append({'pnl': pnl, 'win': pnl > 0, 'pattern': pos.pattern})
                del positions[coin]
                if pnl <= 0:
                    cd[coin] = bp.COOLDOWN_CANDLES
            if equity > peak_eq:
                peak_eq = equity
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        if idx < bp.WARMUP or coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= max_pos:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue
        sig = combo(cl)
        if not sig:
            continue
        entry, stop, name, quality = sig
        if entry <= 0 or stop <= 0 or entry <= stop:
            continue
        R = entry - stop
        if R / entry > 0.04:
            continue
        risk_d = equity * risk
        qty = risk_d / R
        notional = qty * entry
        cap = equity * cap_pct
        if notional > cap:
            qty = cap / entry
        atr_ref = bp.avg_range(cl, 14)
        pos = bp.Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        bp.close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = bp.realized(pos)
        equity += pnl
        trades.append({'pnl': pnl, 'win': pnl > 0, 'pattern': pos.pattern})

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0}
    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)
    return {'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl, 'dd': max_dd}


def main():
    datasets = {
        'D1': bp.load_data('data/binance_1m_7d.json', 60),
        'D2': bp.load_data('data/1min_7days.json', 60),
    }

    configs = []
    # Extreme sweep
    for risk in [0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15]:
        for cap in [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]:
            for max_pos in [5, 7, 10]:
                for min_q in [8]:  # keep high quality only — 6/7 was tested earlier and hurt
                    for runner_r in [5.0, 8.0, 10.0]:
                        for trail in [2.0, 3.0]:
                            configs.append({
                                'risk': risk, 'cap_pct': cap, 'max_pos': max_pos,
                                'min_q': min_q, 'use_session': True,
                                'runner_r': runner_r, 'trail_mult': trail,
                            })
    print(f"Running {len(configs)} milk configs...")

    results = []
    for i, cfg in enumerate(configs):
        row = {lbl: run_milk(data, **cfg) for lbl, data in datasets.items()}
        total = sum(r['pnl'] for r in row.values())
        worst = min(r['pnl'] for r in row.values())
        tr = sum(r['trades'] for r in row.values())
        wrs = [r['wr'] for r in row.values() if r['trades'] > 0]
        wr = mean(wrs) if wrs else 0
        dd = max(r['dd'] for r in row.values())
        results.append({
            **cfg, 'trades': tr, 'wr': wr, 'total': total, 'worst': worst, 'dd': dd,
            'D1_pnl': row['D1']['pnl'], 'D2_pnl': row['D2']['pnl'],
        })
        if (i + 1) % 30 == 0:
            print(f"  {i+1}/{len(configs)} done")

    # Filter robust configs
    robust = [r for r in results if r['worst'] > 0]
    robust.sort(key=lambda x: -x['total'])

    print(f"\n══════════ TOP 20 MILK CONFIGS (both datasets positive) ══════════")
    print(f"{'risk':>5} {'cap':>5} {'mxP':>3} {'runR':>4} {'trail':>5} "
          f"{'tr':>4} {'wr':>5}  {'D1':>11}  {'D2':>11}  {'total':>12}  {'dd':>5}")
    for r in robust[:20]:
        print(f"{r['risk']*100:4.1f}% {r['cap_pct']*100:4.0f}% {r['max_pos']:3}"
              f" {r['runner_r']:4.1f} {r['trail_mult']:5.1f}"
              f" {r['trades']:4} {r['wr']*100:4.1f}%"
              f"  ${r['D1_pnl']:+10,.0f}  ${r['D2_pnl']:+10,.0f}"
              f"  ${r['total']:+11,.0f}  {r['dd']*100:4.1f}%")

    # Best sharpe-ish (highest total / sqrt(dd))
    print(f"\n══════════ BEST RISK-ADJUSTED (total / dd) ══════════")
    robust_by_ratio = sorted(robust, key=lambda x: -x['total'] / max(x['dd'], 0.01))
    for r in robust_by_ratio[:10]:
        ratio = r['total'] / max(r['dd'], 0.01)
        print(f"{r['risk']*100:4.1f}% {r['cap_pct']*100:4.0f}% {r['max_pos']:3}"
              f" {r['runner_r']:4.1f} {r['trail_mult']:5.1f}"
              f" tot=${r['total']:+11,.0f} dd={r['dd']*100:4.1f}% ratio={ratio:,.0f}")

    with open('data/milk_results.json', 'w') as fp:
        json.dump(results, fp, indent=2, default=str)
    print(f"\nsaved → data/milk_results.json ({len(results)} configs, {len(robust)} robust)")


if __name__ == '__main__':
    main()
