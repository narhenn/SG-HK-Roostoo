"""
Tuning sweep for the pro strategy. Tries multiple configurations against both
datasets and prints the best-performing combination. This is meant to find
settings that generalize across D1 and D2 before wiring the live bot.
"""
import json
import os
import importlib

import backtest_pro as bp

DATA_PATHS = [
    ('data/binance_1m_7d.json', 'D1'),
    ('data/1min_7days.json', 'D2'),
]

# Configurations to sweep
# Each entry is a dict of overrides applied to backtest_pro module globals
CONFIGS = [
    # baseline (what we already ran)
    {'name': 'baseline', 'RESAMPLE_MINUTES': 60, 'MIN_QUALITY': 6},

    # Higher quality threshold
    {'name': 'Q8',       'RESAMPLE_MINUTES': 60, 'MIN_QUALITY': 8},
    {'name': 'Q9',       'RESAMPLE_MINUTES': 60, 'MIN_QUALITY': 9},

    # 2H timeframe
    {'name': '2H-Q6',    'RESAMPLE_MINUTES': 120, 'MIN_QUALITY': 6},
    {'name': '2H-Q8',    'RESAMPLE_MINUTES': 120, 'MIN_QUALITY': 8},

    # 4H timeframe
    {'name': '4H-Q6',    'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 6},
    {'name': '4H-Q8',    'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 8},

    # No breakeven bump (let Gunner 2 have full runway)
    {'name': 'noBE-Q6',  'RESAMPLE_MINUTES': 60,  'MIN_QUALITY': 6, 'DISABLE_BE': True},
    {'name': 'noBE-Q8',  'RESAMPLE_MINUTES': 60,  'MIN_QUALITY': 8, 'DISABLE_BE': True},
    {'name': '4H-Q6-noBE', 'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 6, 'DISABLE_BE': True},
    {'name': '4H-Q8-noBE', 'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 8, 'DISABLE_BE': True},

    # Only top 5 profitable patterns
    {'name': '4H-whitelist', 'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 6, 'WHITELIST': {'KT', 'BS', 'POGO', 'DROP', 'RHINO', 'ACAPULCO'}},
    {'name': '1H-whitelist', 'RESAMPLE_MINUTES': 60,  'MIN_QUALITY': 6, 'WHITELIST': {'KT', 'BS', 'POGO', 'DROP', 'RHINO', 'ACAPULCO'}},
    {'name': '1H-whitelist-noBE', 'RESAMPLE_MINUTES': 60, 'MIN_QUALITY': 6, 'WHITELIST': {'KT', 'BS', 'POGO', 'DROP', 'RHINO', 'ACAPULCO'}, 'DISABLE_BE': True},

    # No zone filter
    {'name': 'noZone-4H', 'RESAMPLE_MINUTES': 240, 'MIN_QUALITY': 6, 'USE_ZONE_FILTER': False},
    {'name': 'noZone-1H', 'RESAMPLE_MINUTES': 60, 'MIN_QUALITY': 8, 'USE_ZONE_FILTER': False},
]


# We need to patch the engine to honor WHITELIST + DISABLE_BE overrides.
# The cleanest way is to monkey-patch the scan_all wrapper and the
# close_portion/breakeven block, but that requires touching the engine.
# Instead we'll run each configuration by fiddling module globals and
# shadowing scan_all with a whitelist filter at the import site.

from pro_patterns import scan_all as _scan_all_orig


def make_scan_with_whitelist(whitelist):
    def scan(cl):
        fired, entry, stop, name, quality, all_fired = _scan_all_orig(cl)
        if not fired:
            return False, 0, 0, '', 0, all_fired
        if whitelist is None:
            return fired, entry, stop, name, quality, all_fired
        # pick best signal in whitelist
        kept = [x for x in all_fired if x[0] in whitelist]
        if not kept:
            return False, 0, 0, '', 0, all_fired
        best = max(kept, key=lambda x: x[1])
        return True, best[2], best[3], best[0], best[1], all_fired
    return scan


def patch_engine(disable_be):
    """Monkey-patch backtest_pro.run_backtest behaviour by rewriting the BE flag."""
    # Swap the GUNNER_1_SIZE / or override the stop-move via a module flag
    bp._DISABLE_BE = disable_be


def _patched_run_backtest(data, label=''):
    """A modified copy of bp.run_backtest that consults bp._DISABLE_BE."""
    import time as _time
    from statistics import mean as _mean
    from collections import defaultdict as _defaultdict

    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()
    if not events:
        return None

    equity = bp.STARTING_CAPITAL
    peak_equity = equity
    max_dd_pct = 0.0
    max_dd_dollar = 0.0
    open_positions = {}
    closed_trades = []
    cooldown = _defaultdict(int)
    equity_curve = []

    WARMUP = 40
    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        if coin in open_positions:
            pos = open_positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            closed_all = False

            if cur['l'] <= pos.stop:
                bp.close_portion(pos, pos.remaining_pct, pos.stop, 'STOP')
                closed_all = True
            else:
                if not pos.gunner1_done:
                    g1_price = pos.entry + bp.GUNNER_1_R * pos.R
                    if cur['h'] >= g1_price:
                        bp.close_portion(pos, bp.GUNNER_1_SIZE, g1_price, 'G1')
                        pos.gunner1_done = True
                        if not getattr(bp, '_DISABLE_BE', False):
                            pos.stop = pos.entry
                            pos.be_moved = True
                if not pos.gunner2_done and pos.gunner1_done:
                    g2_price = pos.entry + bp.GUNNER_2_R * pos.R
                    if cur['h'] >= g2_price:
                        bp.close_portion(pos, bp.GUNNER_2_SIZE, g2_price, 'G2')
                        pos.gunner2_done = True
                if pos.remaining_pct > 0 and pos.gunner2_done:
                    hard = pos.entry + bp.RUNNER_TARGET_R * pos.R
                    if cur['h'] >= hard:
                        bp.close_portion(pos, pos.remaining_pct, hard, 'RUN_TP')
                        closed_all = True
                    else:
                        trail = pos.peak - bp.RUNNER_TRAIL_ATR_MULT * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            bp.close_portion(pos, pos.remaining_pct, pos.stop, 'TRAIL')
                            closed_all = True
                if pos.remaining_pct <= 1e-6:
                    closed_all = True

            if closed_all:
                pnl = bp.realized_pnl(pos)
                equity += pnl
                r_real = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                was_winner = pnl > 0
                closed_trades.append({
                    'coin': pos.coin,
                    'entry_t': pos.entry_t,
                    'exit_t': t,
                    'pattern': pos.pattern,
                    'quality': pos.quality,
                    'pnl': pnl,
                    'r_multiple': r_real,
                    'win': was_winner,
                })
                del open_positions[coin]
                if not was_winner:
                    cooldown[coin] = bp.COOLDOWN_CANDLES_AFTER_STOP

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_dd_pct:
                max_dd_pct = dd
                max_dd_dollar = peak_equity - equity

        if idx < WARMUP or coin in open_positions:
            equity_curve.append((t, equity))
            continue
        if cooldown[coin] > 0:
            cooldown[coin] -= 1
            equity_curve.append((t, equity))
            continue
        if len(open_positions) >= bp.MAX_OPEN_POSITIONS:
            equity_curve.append((t, equity))
            continue
        if bp.USE_SESSION_FILTER:
            hour_utc = _time.gmtime(t).tm_hour
            if 0 <= hour_utc < 7:
                equity_curve.append((t, equity))
                continue

        fired, entry, stop, name, quality, _all = bp.scan_all(cl)
        if not fired or quality < bp.MIN_QUALITY:
            equity_curve.append((t, equity))
            continue
        if entry <= 0 or stop <= 0 or entry <= stop:
            equity_curve.append((t, equity))
            continue
        if bp.USE_ZONE_FILTER:
            zones = bp.find_zones(cl, lookback=50)
            in_z, _ = bp.is_in_zone(cur['c'], zones)
            zone_ok = in_z or any(
                z['type'] == 'support' and 0 < (entry - z['level']) / entry < 0.02
                for z in zones
            )
            if not zone_ok and quality < 9:
                equity_curve.append((t, equity))
                continue
        R = entry - stop
        if R / entry > 0.04:
            equity_curve.append((t, equity))
            continue
        risk_dollars = equity * bp.RISK_PER_TRADE
        qty = risk_dollars / R
        notional = qty * entry
        max_notional = equity * 0.20
        if notional > max_notional:
            qty = max_notional / entry
        atr_ref = bp.avg_range(cl, n=14)
        pos = bp.Position(coin, t, entry, stop, qty, atr_ref, name, quality)
        open_positions[coin] = pos
        equity_curve.append((t, equity))

    last_t = events[-1][0]
    for coin, pos in list(open_positions.items()):
        last_c = data[coin][-1]
        bp.close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = bp.realized_pnl(pos)
        equity += pnl
        closed_trades.append({
            'coin': pos.coin,
            'entry_t': pos.entry_t,
            'exit_t': last_t,
            'pattern': pos.pattern,
            'quality': pos.quality,
            'pnl': pnl,
            'r_multiple': pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0,
            'win': pnl > 0,
        })
    open_positions.clear()

    if not closed_trades:
        return {'label': label, 'trades': 0, 'wr': 0, 'pf': 0, 'pnl': 0, 'avg_r': 0, 'max_dd_pct': 0}

    wins = [t for t in closed_trades if t['win']]
    losses = [t for t in closed_trades if not t['win']]
    wr = len(wins) / len(closed_trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    avg_r = _mean(t['r_multiple'] for t in closed_trades)
    pnl = sum(t['pnl'] for t in closed_trades)
    return {'label': label, 'trades': len(closed_trades), 'wr': wr, 'pf': pf,
            'pnl': pnl, 'avg_r': avg_r, 'max_dd_pct': max_dd_pct}


def test_config(cfg):
    bp.RESAMPLE_MINUTES = cfg.get('RESAMPLE_MINUTES', 60)
    bp.MIN_QUALITY = cfg.get('MIN_QUALITY', 6)
    bp.USE_ZONE_FILTER = cfg.get('USE_ZONE_FILTER', True)
    patch_engine(cfg.get('DISABLE_BE', False))
    # patch scan_all for whitelist
    bp.scan_all = make_scan_with_whitelist(cfg.get('WHITELIST'))

    results = []
    for path, label in DATA_PATHS:
        if not os.path.exists(path):
            continue
        data = bp.load_data(path)
        r = _patched_run_backtest(data, label=label)
        if r:
            results.append(r)
    return results


def main():
    best = None
    summary = []
    for cfg in CONFIGS:
        results = test_config(cfg)
        if not results:
            continue
        total_pnl = sum(r['pnl'] for r in results)
        all_pos = all(r['pnl'] > 0 for r in results)
        worst = min(r['pnl'] for r in results)
        total_trades = sum(r['trades'] for r in results)
        avg_wr = sum(r['wr'] for r in results) / len(results)
        avg_pf = sum(r['pf'] if r['pf'] != float('inf') else 10 for r in results) / len(results)
        max_dd = max(r['max_dd_pct'] for r in results)
        entry = {
            'name': cfg['name'],
            'total_pnl': total_pnl,
            'worst': worst,
            'all_pos': all_pos,
            'trades': total_trades,
            'avg_wr': avg_wr,
            'avg_pf': avg_pf,
            'max_dd': max_dd,
        }
        summary.append(entry)
        print(f"{cfg['name']:20} trades={total_trades:3}  wr={avg_wr*100:5.1f}%  "
              f"pf={avg_pf:4.2f}  total=${total_pnl:+,.0f}  worst=${worst:+,.0f}  "
              f"dd={max_dd*100:4.1f}%  {'✓' if all_pos else '✗'}")
        if best is None or (all_pos and total_pnl > best['total_pnl']) or \
           (not best.get('all_pos') and total_pnl > best['total_pnl']):
            best = entry

    print("\n══════════ BEST ══════════")
    if best:
        print(json.dumps(best, indent=2))

    # sort by total_pnl descending
    summary.sort(key=lambda x: -x['total_pnl'])
    print("\n══════════ TOP 5 ══════════")
    for s in summary[:5]:
        print(f"{s['name']:20} total=${s['total_pnl']:+,.0f}  worst=${s['worst']:+,.0f}  wr={s['avg_wr']*100:5.1f}%")

    with open('data/pro_tune_results.json', 'w') as fp:
        json.dump(summary, fp, indent=2, default=str)


if __name__ == '__main__':
    main()
