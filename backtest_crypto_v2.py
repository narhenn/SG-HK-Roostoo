"""
CRYPTO BACKTEST V2 — HYBRID APPROACH
════════════════════════════════════════════════════════════════════
Lessons from v1:
  - Pure crypto_patterns were too restrictive, < 100 trades total
  - Pure 30m was too noisy, lost heavily
  - 120m had good PF but only 8-12 trades → high variance
  - We need ENOUGH samples AND a diverse ensemble

This v2 does a HYBRID approach:
  - Combines crypto_patterns + proven ensemble (Donchian + engulfing)
  - Tests with and without the forex-borrowed session filter
  - Tests different scale-out splits (50/35/15 vs 30/30/40 vs 25/25/25/25)
  - Tests different runner targets (5R, 6R, 8R, 10R) and trail (2/3/4 ATR)
  - Baseline to beat: pro bot E6-combo 1H = +$22,307 (D1 +$9k, D2 +$13k), both positive

Goal: find a config that is POSITIVE ON BOTH DATASETS and beats +$22k.
"""
import json
import os
import time
from collections import defaultdict, Counter
from statistics import mean

from crypto_patterns import (
    scan_all as cp_scan_all,
    atr as cp_atr,
    ema,
    is_uptrend,
    vol_regime_ok,
    avg_volume,
    avg_range,
)


# ─── CONFIG ───
STARTING_CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.02
MAX_OPEN_POSITIONS = 5
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
COOLDOWN_CANDLES = 2
WARMUP = 60


# ─── DATA ───
def resample(candles_1m, minutes):
    if not candles_1m:
        return []
    out, bucket, bstart = [], [], None
    for c in candles_1m:
        t = int(c['t'])
        b = (t // (minutes * 60)) * (minutes * 60)
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


def load_data(path, minutes=60):
    with open(path) as fp:
        raw = json.load(fp)
    return {coin: resample(sorted(cs, key=lambda x: x['t']), minutes)
            for coin, cs in raw.items()}


# ─── ENTRY STRATEGIES ───
def entry_crypto(cl, min_q=7):
    fired, entry, stop, name, quality, _ = cp_scan_all(cl)
    if fired and quality >= min_q:
        return (entry, stop, name, quality)
    return None


def entry_donchian(cl):
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
    avg_v = avg_volume(cl[:-1], 20)
    if c.get('v', 0) < avg_v * 1.1:
        return None
    a = cp_atr(cl, 14)
    entry = c['h'] + a * 0.1
    stop = c['l'] - a * 0.5
    if (entry - stop) / entry > 0.05:
        return None
    return (entry, stop, 'DONCHIAN', 8)


def entry_engulf(cl):
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
    a = cp_atr(cl, 14)
    entry = c['h'] + a * 0.1
    stop = min(c['l'], p['l']) - a * 0.3
    if (entry - stop) / entry > 0.04:
        return None
    return (entry, stop, 'ENGULF', 7)


def entry_hybrid(cl):
    """Crypto patterns + Donchian + Engulfing — pick best quality."""
    opts = []
    for fn in (entry_crypto, entry_donchian, entry_engulf):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception:
            pass
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


# ─── POSITION ───
class Position:
    __slots__ = ('coin', 'entry_t', 'entry', 'stop', 'R', 'qty', 'remaining_pct',
                 'peak', 'atr_ref', 'pattern', 'g1_done', 'g2_done', 'g3_done', 'closes')

    def __init__(self, coin, t, entry, stop, qty, atr_ref, pattern):
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
        self.g3_done = False
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
    return sum((px - ep) * pos.qty * portion for portion, px, _ in pos.closes)


# ─── ENGINE ───
def run(data, strategy_fn, g1=0.5, g2=0.35, g3=0.0, runner=None,
        g1_r=1.0, g2_r=2.0, g3_r=4.0, runner_r=5.0, trail_mult=2.0,
        enable_be=True, cap_pct=0.5, use_session=False, use_vol_regime=True):
    """Scale-out can be 3-tier (g1,g2,runner) or 4-tier (g1,g2,g3,runner).
    runner = 1 - g1 - g2 - g3 if not specified."""
    if runner is None:
        runner = 1 - g1 - g2 - g3
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
            closed = False

            if cur['l'] <= pos.stop:
                close_portion(pos, pos.remaining_pct, pos.stop, 'STOP')
                closed = True
            else:
                if not pos.g1_done:
                    g1p = pos.entry + g1_r * pos.R
                    if cur['h'] >= g1p:
                        close_portion(pos, g1, g1p, 'G1')
                        pos.g1_done = True
                        if enable_be:
                            pos.stop = pos.entry
                if pos.g1_done and not pos.g2_done:
                    g2p = pos.entry + g2_r * pos.R
                    if cur['h'] >= g2p:
                        close_portion(pos, g2, g2p, 'G2')
                        pos.g2_done = True
                if g3 > 0 and pos.g2_done and not pos.g3_done:
                    g3p = pos.entry + g3_r * pos.R
                    if cur['h'] >= g3p:
                        close_portion(pos, g3, g3p, 'G3')
                        pos.g3_done = True
                if pos.g2_done and (g3 == 0 or pos.g3_done) and pos.remaining_pct > 0:
                    hard = pos.entry + runner_r * pos.R
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
                if pnl <= 0:
                    cd[coin] = COOLDOWN_CANDLES

            if equity > peak_eq:
                peak_eq = equity
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        if idx < WARMUP or coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= MAX_OPEN_POSITIONS:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue
        if use_vol_regime and not vol_regime_ok(cl, 0.7):
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
        atr_ref = cp_atr(cl, 14)
        pos = Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    # EOD close
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
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'avg_r': 0, 'dd': 0, 'by_pat': {}}
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


# ─── MAIN ───
def main():
    datasets_1h = {
        'D1': load_data('data/binance_1m_7d.json', 60),
        'D2': load_data('data/1min_7days.json', 60),
    }

    # Configs to test
    CONFIGS = [
        # name, kwargs
        ('3tier-5035-1R2R5R-2xATR', dict(g1=0.5, g2=0.35, runner=0.15,
                                          g1_r=1, g2_r=2, runner_r=5, trail_mult=2)),
        ('3tier-3030-1R3R6R-3xATR', dict(g1=0.3, g2=0.3, runner=0.4,
                                          g1_r=1, g2_r=3, runner_r=6, trail_mult=3)),
        ('3tier-3030-1R3R8R-3xATR', dict(g1=0.3, g2=0.3, runner=0.4,
                                          g1_r=1, g2_r=3, runner_r=8, trail_mult=3)),
        ('3tier-4030-1R2R5R-3xATR', dict(g1=0.4, g2=0.3, runner=0.3,
                                          g1_r=1, g2_r=2, runner_r=5, trail_mult=3)),
        ('4tier-25each-1R2R4R8R',   dict(g1=0.25, g2=0.25, g3=0.25, runner=0.25,
                                          g1_r=1, g2_r=2, g3_r=4, runner_r=8, trail_mult=3)),
        ('3tier-5030-1R2R5R-2xATR-wide', dict(g1=0.5, g2=0.3, runner=0.2,
                                                g1_r=1, g2_r=2, runner_r=5, trail_mult=2)),
    ]

    STRATS = {
        'crypto-only':  entry_crypto,
        'donchian':     entry_donchian,
        'engulf':       entry_engulf,
        'hybrid':       entry_hybrid,
    }

    all_results = []
    print(f"\n{'strat':12} {'config':35} {'sess':5} {'vol':4} {'tr':>4}  {'wr':>5}  {'pf':>5}  {'total':>12}  {'worst':>12}  {'dd':>5}")

    for strat_name, fn in STRATS.items():
        for cfg_name, cfg_kw in CONFIGS:
            for use_session in [False, True]:
                for use_vol_regime in [True, False]:
                    for cap_pct in [0.5, 1.0]:
                        row = {
                            lbl: run(data, fn, cap_pct=cap_pct,
                                     use_session=use_session,
                                     use_vol_regime=use_vol_regime,
                                     **cfg_kw)
                            for lbl, data in datasets_1h.items()
                        }
                        total = sum(r['pnl'] for r in row.values())
                        worst = min(r['pnl'] for r in row.values())
                        tr = sum(r['trades'] for r in row.values())
                        wrs = [r['wr'] for r in row.values() if r['trades'] > 0]
                        wr = mean(wrs) if wrs else 0
                        pfs = [r['pf'] for r in row.values() if r['pf'] != float('inf') and r['trades'] > 0]
                        pf = mean(pfs) if pfs else 0
                        dd = max(r['dd'] for r in row.values())
                        res = {
                            'strat': strat_name, 'cfg': cfg_name,
                            'session': use_session, 'vol_regime': use_vol_regime,
                            'cap_pct': cap_pct,
                            'trades': tr, 'wr': wr, 'pf': pf,
                            'total': total, 'worst': worst, 'dd': dd,
                            'rows': row,
                        }
                        all_results.append(res)

    # Filter robust configs
    robust = [r for r in all_results if r['worst'] > 0 and r['trades'] >= 20]
    robust.sort(key=lambda x: -x['total'])
    print("\n══════════ TOP 20 ROBUST (both positive, >=20 trades) ══════════")
    for r in robust[:20]:
        print(f"  {r['strat']:12} {r['cfg']:35} sess={str(r['session'])[0]} vol={str(r['vol_regime'])[0]} "
              f"cap={r['cap_pct']:.0%} tr={r['trades']:3}  wr={r['wr']*100:4.1f}%  pf={r['pf']:4.2f}  "
              f"tot=${r['total']:+,.0f}  worst=${r['worst']:+,.0f}  dd={r['dd']*100:4.1f}%")

    print("\n══════════ ALL ROBUST CONFIGS COUNT ══════════")
    print(f"  robust configs: {len(robust)} / {len(all_results)}")

    if robust:
        best = robust[0]
        print("\n══════════ BEST ROBUST ══════════")
        print(f"  strat:       {best['strat']}")
        print(f"  config:      {best['cfg']}")
        print(f"  session:     {best['session']}")
        print(f"  vol_regime:  {best['vol_regime']}")
        print(f"  cap_pct:     {best['cap_pct']:.0%}")
        print(f"  trades:      {best['trades']}")
        print(f"  wr:          {best['wr']*100:.1f}%")
        print(f"  pf:          {best['pf']:.2f}")
        print(f"  total:       ${best['total']:+,.0f}")
        print(f"  worst:       ${best['worst']:+,.0f}")
        print(f"  dd:          {best['dd']*100:.1f}%")
        print(f"  D1:          ${best['rows']['D1']['pnl']:+,.0f}")
        print(f"  D2:          ${best['rows']['D2']['pnl']:+,.0f}")

    # Save
    out = [{k: v for k, v in r.items() if k != 'rows'} for r in all_results]
    for o, r in zip(out, all_results):
        o['D1_pnl'] = r['rows']['D1']['pnl']
        o['D2_pnl'] = r['rows']['D2']['pnl']
        o['D1_trades'] = r['rows']['D1']['trades']
        o['D2_trades'] = r['rows']['D2']['trades']
    with open('data/crypto_v2_results.json', 'w') as fp:
        json.dump(out, fp, indent=2, default=str)
    print(f"\nsaved → data/crypto_v2_results.json ({len(out)} total configs)")


if __name__ == '__main__':
    main()
