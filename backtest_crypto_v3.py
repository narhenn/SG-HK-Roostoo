"""
CRYPTO BACKTEST V3 — AUGMENT THE PROVEN BASELINE
════════════════════════════════════════════════════════════════════
Previous findings:
  - v1 crypto_patterns alone: lost money — over-filtered, too few fires
  - v2 crypto+donchian+engulf: still lost on D2 (bearish dataset)
  - **OLD pro bot E6-combo (forex patterns + donchian + engulf) was BOTH
    positive**: D1=+$9,209, D2=+$13,098, total=+$22,307
  - Pattern breakdown showed ACAPULCO, LK, BS were profitable on D2 — the
    forex patterns actually work for crypto (they fire liberally across
    regimes)

v3 strategy: don't fight the baseline, AUGMENT it.
  - Start with the proven E6-combo ensemble (pro_patterns Q≥8 + Donchian +
    Engulfing)
  - ADD crypto_patterns as a 4th source
  - Test with/without session filter, vol regime, scale-out variants
  - Also test crypto-tuned trail (3x ATR, wider) with proven baseline

Baseline to beat: +$22,307 (D1 +$9,209, D2 +$13,098), both positive.
"""
import json
import os
import time
from collections import defaultdict, Counter
from statistics import mean

from pro_patterns import scan_all as pp_scan_all
from crypto_patterns import (
    scan_all as cp_scan_all,
    atr as cp_atr,
    ema,
    is_uptrend,
    vol_regime_ok,
    avg_volume,
)


# ─── CONFIG ───
STARTING_CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.02
MAX_OPEN_POSITIONS = 5
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
COOLDOWN_CANDLES = 2
WARMUP = 60


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


# ─── ENTRIES ───
def entry_pro_pat(cl, min_q=8):
    fired, entry, stop, name, quality, _ = pp_scan_all(cl)
    if fired and quality >= min_q:
        return (entry, stop, name, quality)
    return None


def entry_crypto_pat(cl, min_q=7):
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


def entry_baseline(cl):
    """E6-combo from pro bot: pro_patterns Q>=8 + Donchian + Engulfing."""
    opts = []
    for fn in (lambda c: entry_pro_pat(c, 8), entry_donchian, entry_engulf):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception:
            pass
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


def entry_augmented(cl):
    """Baseline + crypto_patterns. All 4 sources."""
    opts = []
    for fn in (lambda c: entry_pro_pat(c, 8), lambda c: entry_crypto_pat(c, 7),
               entry_donchian, entry_engulf):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception:
            pass
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


def entry_augmented_loose(cl):
    """Baseline (Q7) + crypto_patterns (Q6) — more fires."""
    opts = []
    for fn in (lambda c: entry_pro_pat(c, 7), lambda c: entry_crypto_pat(c, 6),
               entry_donchian, entry_engulf):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception:
            pass
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


# ─── ENGINE ───
class Position:
    __slots__ = ('coin', 'entry_t', 'entry', 'stop', 'R', 'qty', 'remaining_pct',
                 'peak', 'atr_ref', 'pattern', 'g1_done', 'g2_done', 'closes')

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


def run(data, strategy_fn, g1_r=1.0, g1=0.5, g2_r=2.0, g2=0.35,
        runner_r=5.0, trail_mult=2.0, enable_be=True,
        cap_pct=0.5, use_session=True, use_vol_regime=False):
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
                if pos.g2_done and pos.remaining_pct > 0:
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
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0, 'by_pat': {}}
    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)
    by_pat = Counter(t['pattern'] for t in trades)
    return {'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl,
            'dd': max_dd, 'by_pat': dict(by_pat)}


# ─── MAIN ───
def main():
    datasets = {
        'D1': load_data('data/binance_1m_7d.json', 60),
        'D2': load_data('data/1min_7days.json', 60),
    }

    STRATS = {
        'baseline-E6':     entry_baseline,
        'augmented':       entry_augmented,
        'augmented-loose': entry_augmented_loose,
    }

    CFGS = [
        # name, kwargs
        ('default-5035-1R2R5R',  dict(g1=0.5, g2=0.35, g1_r=1, g2_r=2, runner_r=5, trail_mult=2)),
        ('wider-3030-1R3R6R-3x', dict(g1=0.3, g2=0.3, g1_r=1, g2_r=3, runner_r=6, trail_mult=3)),
        ('4tier-proxy-4030-1R2R5R-3x', dict(g1=0.4, g2=0.3, g1_r=1, g2_r=2, runner_r=5, trail_mult=3)),
        ('crypto-3030-1R2R8R-3x', dict(g1=0.3, g2=0.3, g1_r=1, g2_r=2, runner_r=8, trail_mult=3)),
    ]

    ENV_CONFIGS = [
        ('sess-cap50',  dict(use_session=True,  use_vol_regime=False, cap_pct=0.5)),
        ('sess-cap70',  dict(use_session=True,  use_vol_regime=False, cap_pct=0.7)),
        ('sess-cap100', dict(use_session=True,  use_vol_regime=False, cap_pct=1.0)),
        ('vol-cap50',   dict(use_session=False, use_vol_regime=True,  cap_pct=0.5)),
        ('vol-cap100',  dict(use_session=False, use_vol_regime=True,  cap_pct=1.0)),
        ('both-cap50',  dict(use_session=True,  use_vol_regime=True,  cap_pct=0.5)),
        ('both-cap100', dict(use_session=True,  use_vol_regime=True,  cap_pct=1.0)),
        ('none-cap50',  dict(use_session=False, use_vol_regime=False, cap_pct=0.5)),
    ]

    results = []
    print(f"{'strat':20} {'cfg':30} {'env':15} {'tr':>4}  {'wr':>5}  {'pf':>5}  {'total':>12}  {'worst':>12}")

    for strat_name, fn in STRATS.items():
        for cfg_name, cfg_kw in CFGS:
            for env_name, env_kw in ENV_CONFIGS:
                kw = {**cfg_kw, **env_kw}
                row = {lbl: run(data, fn, **kw) for lbl, data in datasets.items()}
                total = sum(r['pnl'] for r in row.values())
                worst = min(r['pnl'] for r in row.values())
                tr = sum(r['trades'] for r in row.values())
                wrs = [r['wr'] for r in row.values() if r['trades'] > 0]
                wr = mean(wrs) if wrs else 0
                pfs = [r['pf'] for r in row.values() if r['pf'] != float('inf') and r['trades'] > 0]
                pf = mean(pfs) if pfs else 0
                dd = max(r['dd'] for r in row.values())
                results.append({
                    'strat': strat_name, 'cfg': cfg_name, 'env': env_name,
                    'trades': tr, 'wr': wr, 'pf': pf, 'total': total,
                    'worst': worst, 'dd': dd,
                    'D1_pnl': row['D1']['pnl'], 'D2_pnl': row['D2']['pnl'],
                    'D1_trades': row['D1']['trades'], 'D2_trades': row['D2']['trades'],
                    'D1_patterns': row['D1']['by_pat'], 'D2_patterns': row['D2']['by_pat'],
                })
                flag = '✓' if worst > 0 else '✗'
                print(f"{strat_name:20} {cfg_name:30} {env_name:15} {tr:4}  {wr*100:4.1f}%  {pf:4.2f}  "
                      f"${total:+11,.0f}  ${worst:+11,.0f}  {flag}")

    robust = [r for r in results if r['worst'] > 0]
    robust.sort(key=lambda x: -x['total'])
    print(f"\n══════════ ROBUST CONFIGS ({len(robust)}) ══════════")
    for r in robust[:20]:
        print(f"  {r['strat']:20} {r['cfg']:30} {r['env']:15} "
              f"tot=${r['total']:+,.0f}  worst=${r['worst']:+,.0f}  "
              f"wr={r['wr']*100:4.1f}%  pf={r['pf']:.2f}  "
              f"D1=${r['D1_pnl']:+,.0f}  D2=${r['D2_pnl']:+,.0f}")

    if robust:
        best = robust[0]
        print("\n══════════ WINNER ══════════")
        for k, v in best.items():
            print(f"  {k}: {v}")

    with open('data/crypto_v3_results.json', 'w') as fp:
        json.dump(results, fp, indent=2, default=str)
    print(f"\nsaved → data/crypto_v3_results.json")


if __name__ == '__main__':
    main()
