"""
BACKTEST PROFIT BRICKS OVERLAY
════════════════════════════════════════════════════════════════════
Adds Walter Peters' Profit Bricks pool compounding on top of the
MILK MAX config (risk=6%, cap=500%, trail=3xATR).

Sweeping:
  - base_risk: 3%, 6% (normal & milk)
  - pool_factor: 0.25, 0.50, 0.75, 1.00
  - max_risk_pct: none, 10%, 20%, 30% (safety cap on total risk)
  - cap_pct: 200%, 500% (notional cap — tighter vs looser)

Goal: does the Profit Bricks overlay actually improve on MILK MAX
+$423k baseline, or does it just add variance without alpha?

Baselines for comparison:
  v2 default:  +$141k (risk=3%, cap=200%, trail=2xATR)
  MILK MILD:   +$258k (risk=3%, cap=300%, trail=3xATR)
  MILK MAX:    +$423k (risk=6%, cap=500%, trail=3xATR)
"""
import json
import time
from collections import defaultdict, Counter
from statistics import mean

import backtest_pro_v2 as bp
from pro_patterns import scan_all as pp_scan_all
from profit_bricks import ProfitBricks


STARTING_CAPITAL = 1_000_000.0


def run_with_bricks(data, base_risk, cap_pct, pool_factor, max_risk_pct,
                    trail_mult=3.0, runner_r=5.0, min_q=8,
                    g1_r=1.0, g1_size=0.5, g2_r=2.0, g2_size=0.35):
    """Run the milk engine with a Profit Bricks sizing overlay."""
    bricks = ProfitBricks(
        starting_equity=STARTING_CAPITAL,
        base_risk_pct=base_risk,
        pool_factor=pool_factor,
        max_risk_pct=max_risk_pct,
    )

    # Entry fn
    def entry_pat(cl):
        fired, entry, stop, name, quality, _ = pp_scan_all(cl)
        if fired and quality >= min_q:
            return (entry, stop, name, quality)
        return None

    def combo(cl):
        opts = []
        for fn in (entry_pat, bp.entry_donchian, bp.entry_engulfing):
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
                        pos.stop = pos.entry
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
                bricks.on_close(pnl)
                trades.append({
                    'pnl': pnl, 'win': pnl > 0, 'pattern': pos.pattern,
                })
                del positions[coin]
                if pnl <= 0:
                    cd[coin] = bp.COOLDOWN_CANDLES
            if equity > peak_eq:
                peak_eq = equity
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Entry
        if idx < bp.WARMUP or coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= bp.MAX_OPEN_POSITIONS:
            continue
        hour = time.gmtime(t).tm_hour
        if 0 <= hour < 7:
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

        # ═══ PROFIT BRICKS SIZING ═══
        risk_d = bricks.next_risk_dollars(equity)
        qty = risk_d / R
        notional = qty * entry
        cap = equity * cap_pct
        if notional > cap:
            qty = cap / entry

        atr_ref = bp.avg_range(cl, 14)
        pos = bp.Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    # EOD
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        bp.close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = bp.realized(pos)
        equity += pnl
        bricks.on_close(pnl)
        trades.append({
            'pnl': pnl, 'win': pnl > 0, 'pattern': pos.pattern,
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0,
                'peak_pool': 0, 'final_pool': 0}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)

    return {
        'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl,
        'dd': max_dd, 'peak_pool': bricks.peak_pool,
        'final_pool': bricks.pool,
        'bricks_wins': bricks.wins, 'bricks_losses': bricks.losses,
    }


def main():
    datasets = {
        'D1': bp.load_data('data/binance_1m_7d.json', 60),
        'D2': bp.load_data('data/1min_7days.json', 60),
    }

    print("═══ BASELINES (no Profit Bricks) ═══")
    for label, base_risk, cap in [
        ('v2 DEFAULT (3%/200%/trail=2)', 0.03, 2.0),
        ('MILK MILD (3%/300%)          ', 0.03, 3.0),
        ('MILK BAL  (5%/500%)          ', 0.05, 5.0),
        ('MILK MAX  (6%/500%)          ', 0.06, 5.0),
    ]:
        # Force pool_factor=0 to disable bricks
        trail = 2.0 if '2)' in label else 3.0
        row = {lbl: run_with_bricks(d, base_risk=base_risk, cap_pct=cap,
                                     pool_factor=0.0, max_risk_pct=None,
                                     trail_mult=trail)
               for lbl, d in datasets.items()}
        total = sum(r['pnl'] for r in row.values())
        worst = min(r['pnl'] for r in row.values())
        dd = max(r['dd'] for r in row.values())
        print(f"  {label}  D1=${row['D1']['pnl']:+11,.0f}  D2=${row['D2']['pnl']:+11,.0f}  "
              f"tot=${total:+11,.0f}  worst=${worst:+11,.0f}  dd={dd*100:4.1f}%")

    print("\n═══ PROFIT BRICKS SWEEP (on top of MILK MAX base 6%/500%) ═══")
    print(f"{'pool_f':>7} {'max_risk':>9} {'D1':>12} {'D2':>12} {'total':>13} {'worst':>12} {'dd':>6} {'peak_pool':>12}")
    print("─" * 90)
    results = []
    for pool_factor in [0.0, 0.25, 0.50, 0.75, 1.0]:
        for max_risk in [None, 0.10, 0.15, 0.20, 0.30, 0.50]:
            row = {lbl: run_with_bricks(d, base_risk=0.06, cap_pct=5.0,
                                         pool_factor=pool_factor,
                                         max_risk_pct=max_risk,
                                         trail_mult=3.0)
                   for lbl, d in datasets.items()}
            total = sum(r['pnl'] for r in row.values())
            worst = min(r['pnl'] for r in row.values())
            dd = max(r['dd'] for r in row.values())
            peak_pool = max(r['peak_pool'] for r in row.values())
            mr_str = 'none' if max_risk is None else f'{max_risk*100:.0f}%'
            flag = 'OK' if worst > 0 else ' X'
            print(f"{pool_factor:>7.2f} {mr_str:>9} "
                  f"${row['D1']['pnl']:+11,.0f} ${row['D2']['pnl']:+11,.0f} "
                  f"${total:+12,.0f} ${worst:+11,.0f} {dd*100:5.1f}% "
                  f"${peak_pool:>11,.0f} {flag}")
            results.append({
                'base_risk': 0.06, 'cap_pct': 5.0,
                'pool_factor': pool_factor, 'max_risk_pct': max_risk,
                'D1_pnl': row['D1']['pnl'], 'D2_pnl': row['D2']['pnl'],
                'total': total, 'worst': worst, 'dd': dd, 'peak_pool': peak_pool,
            })

    print("\n═══ PROFIT BRICKS on conservative base (3%/300% MILK MILD) ═══")
    print(f"{'pool_f':>7} {'max_risk':>9} {'D1':>12} {'D2':>12} {'total':>13} {'worst':>12} {'dd':>6}")
    print("─" * 90)
    for pool_factor in [0.25, 0.50, 0.75, 1.0]:
        for max_risk in [None, 0.15, 0.20, 0.30]:
            row = {lbl: run_with_bricks(d, base_risk=0.03, cap_pct=3.0,
                                         pool_factor=pool_factor,
                                         max_risk_pct=max_risk,
                                         trail_mult=3.0)
                   for lbl, d in datasets.items()}
            total = sum(r['pnl'] for r in row.values())
            worst = min(r['pnl'] for r in row.values())
            dd = max(r['dd'] for r in row.values())
            mr_str = 'none' if max_risk is None else f'{max_risk*100:.0f}%'
            flag = 'OK' if worst > 0 else ' X'
            print(f"{pool_factor:>7.2f} {mr_str:>9} "
                  f"${row['D1']['pnl']:+11,.0f} ${row['D2']['pnl']:+11,.0f} "
                  f"${total:+12,.0f} ${worst:+11,.0f} {dd*100:5.1f}% {flag}")
            results.append({
                'base_risk': 0.03, 'cap_pct': 3.0,
                'pool_factor': pool_factor, 'max_risk_pct': max_risk,
                'D1_pnl': row['D1']['pnl'], 'D2_pnl': row['D2']['pnl'],
                'total': total, 'worst': worst, 'dd': dd,
            })

    # Rank robust
    robust = [r for r in results if r['worst'] > 0]
    robust.sort(key=lambda x: -x['total'])
    print(f"\n═══ TOP 15 ROBUST CONFIGS (both datasets positive) ═══")
    print(f"{'base':>5} {'cap':>5} {'pool_f':>7} {'max_risk':>9} {'D1':>12} {'D2':>12} {'total':>13} {'dd':>6}")
    for r in robust[:15]:
        mr_str = 'none' if r['max_risk_pct'] is None else f"{r['max_risk_pct']*100:.0f}%"
        print(f"{r['base_risk']*100:4.0f}% {r['cap_pct']*100:4.0f}% {r['pool_factor']:>7.2f} {mr_str:>9} "
              f"${r['D1_pnl']:+11,.0f} ${r['D2_pnl']:+11,.0f} ${r['total']:+12,.0f} {r['dd']*100:5.1f}%")

    with open('data/bricks_results.json', 'w') as fp:
        json.dump(results, fp, indent=2, default=str)
    print(f"\nsaved → data/bricks_results.json ({len(results)} configs, {len(robust)} robust)")


if __name__ == '__main__':
    main()
