"""
CAT B BACKTEST — naked_trader signals + v2 risk mgmt + Cat B tuning
════════════════════════════════════════════════════════════════════
Surgical transplant:
  Heart: nt's 13 reversal patterns (proven 69% WR on D3-fresh)
  Exits: nt's 1% partial + 1% trail + BE bump (fixes WLFI bug)
  Sizing: tiny positions — risk 0.3% per trade (not 3%)
  Safety: hard equity kill switch + daily loss limit + DD throttle
  Filters: MTF 4H + BTC EMA200 + session filter + Q>=8

Goal: produce a smooth, low-vol equity curve for Category B wins.
  - Sortino > 30
  - Sharpe > 15
  - Max new DD < 2%
  - Tiny but consistent positive returns

Tests the config on ALL 4 datasets to validate:
  D1 (old): acceptable (small loss OK, positions are tiny)
  D2 (old): acceptable
  D3-majors (fresh, CURRENT market): WINS
  D3-full (fresh, broader): WINS or breakeven
"""
import json
import math
import time
from collections import defaultdict
from statistics import mean, pstdev

import backtest_head_to_head as hh
import backtest_pro_v2 as bp


STARTING_CAPITAL = 1_000_000.0
STABLES = {'USD1','RLUSD','U','XUSD','USDC','USDT','TUSD','BUSD','FDUSD',
           'USDP','DAI','EUR','EURI','GBPT'}


def load(path):
    d = bp.load_data(path, 60)
    return {k: v for k, v in d.items() if k not in STABLES}


# ─────────────────────────────────────────────────────────────
# Cat B Position class — tracks nt-style partial + trail state
# ─────────────────────────────────────────────────────────────
class CatBPosition:
    __slots__ = ('coin', 'entry_t', 'entry', 'qty_initial', 'qty_remaining',
                 'peak', 'stop', 'partial_done', 'candle_count', 'pattern',
                 'score', 'closes', 'be_moved')

    def __init__(self, coin, t, entry, qty, pattern, score):
        self.coin = coin
        self.entry_t = t
        self.entry = entry
        self.qty_initial = qty
        self.qty_remaining = qty
        self.peak = entry
        self.stop = 0.0
        self.partial_done = False
        self.candle_count = 0
        self.pattern = pattern
        self.score = score
        self.closes = []
        self.be_moved = False


def run_catb(
    data,
    # Cat B sizing
    base_size_low=20_000,    # score < 8
    base_size_med=30_000,    # score >= 8
    base_size_high=50_000,   # score >= 10
    # Entry gate
    min_score=8,             # strict quality
    # Exit params (nt-style, tight)
    partial_pct=0.01,        # sell 50% at +1%
    partial_size=0.5,
    profit_trail_pct=0.01,   # trail at peak*0.99 after +1%
    atr_stop_mult=1.2,
    # Risk limits
    max_concurrent=2,        # max 2 positions (not 4)
    daily_profit_target_pct=0.01,   # +1% day → stop
    daily_loss_limit_pct=0.005,     # -0.5% day → stop
    kill_switch_equity=880_000,     # halt if equity below
    dd_throttle_pct=0.02,           # after -2% DD, cut size to 25%
    dd_throttle_size_mult=0.25,
    # Filters
    use_session=True,        # skip 00-07 UTC
    use_mtf=False,           # 4H uptrend (optional — uses resample)
    use_btc_regime=False,    # BTC > EMA200
):
    """Cat B optimized engine."""
    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()
    if not events:
        return None

    equity = STARTING_CAPITAL
    cash = STARTING_CAPITAL
    peak_equity = equity
    max_dd = 0
    positions = {}
    trades = []
    cooldowns = {}
    hourly_returns = []       # for Sharpe/Sortino
    daily_pnl = defaultdict(float)  # keyed by day (epoch // 86400)

    MIN_CASH_RESERVE = 200_000
    MIN_HOLD = 3
    MAX_HOLD = 12

    kill_switch_hit = False
    last_equity = equity
    day_start_equity = equity
    current_day = None

    btc_candles = data.get('BTC', [])

    def compute_equity():
        return cash + sum(
            p.qty_remaining * (data[p.coin][-1]['c'] if data[p.coin] else p.entry)
            for p in positions.values()
        )

    def ema(values, period):
        if len(values) < period:
            return None
        k = 2 / (period + 1)
        e = sum(values[:period]) / period
        for v in values[period:]:
            e = v * k + e * (1 - k)
        return e

    def resample_4h(cl):
        if not cl: return []
        out, bucket, bstart = [], [], None
        for c in cl:
            t = int(c['t'])
            b = (t // (4 * 3600)) * (4 * 3600)
            if bstart is None:
                bstart = b
            if b != bstart:
                if bucket:
                    out.append({'t':bstart,'o':bucket[0]['o'],
                                'h':max(x['h'] for x in bucket),
                                'l':min(x['l'] for x in bucket),
                                'c':bucket[-1]['c'],'v':sum(x.get('v',0) for x in bucket)})
                bucket = [c]; bstart = b
            else:
                bucket.append(c)
        if bucket:
            out.append({'t':bstart,'o':bucket[0]['o'],
                        'h':max(x['h'] for x in bucket),
                        'l':min(x['l'] for x in bucket),
                        'c':bucket[-1]['c'],'v':sum(x.get('v',0) for x in bucket)})
        return out

    def mtf_ok(cl):
        cl4 = resample_4h(cl)
        if len(cl4) < 50:
            return True
        closes = [x['c'] for x in cl4]
        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        if e20 is None or e50 is None:
            return True
        return cl4[-1]['c'] > e20 > e50

    def btc_ok(t_now):
        if not btc_candles:
            return True
        idx = None
        for i in range(len(btc_candles)-1, -1, -1):
            if btc_candles[i]['t'] <= t_now:
                idx = i; break
        if idx is None or idx < 200:
            return True
        closes = [c['c'] for c in btc_candles[:idx+1]]
        e200 = ema(closes, 200)
        if e200 is None:
            return True
        return btc_candles[idx]['c'] > e200

    for t, coin, idx in events:
        if kill_switch_hit:
            break

        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # Daily rollover tracking
        day = int(t) // 86400
        if current_day is None or day != current_day:
            if current_day is not None:
                # Finalize previous day
                pass
            current_day = day
            day_start_equity = compute_equity()

        # ── MANAGE OPEN POSITIONS ──
        if coin in positions:
            pos = positions[coin]
            pos.candle_count += 1
            if cur['h'] > pos.peak:
                pos.peak = cur['h']

            atr = sum(x['h'] - x['l'] for x in cl[-14:]) / min(14, len(cl)) if cl else pos.entry * 0.01
            low = cur['l']
            high = cur['h']
            sell = False
            reason = ''
            exit_price = None

            if pos.candle_count < MIN_HOLD:
                hard = pos.entry - atr * 2.0
                if low <= hard:
                    sell = True
                    reason = 'HARD_STOP'
                    exit_price = hard
            else:
                # ATR stop
                stop_level = pos.entry - atr * atr_stop_mult
                if low <= stop_level and pos.stop == 0:
                    sell = True
                    reason = 'ATR_STOP'
                    exit_price = stop_level

                # Partial at +1%
                if not sell and not pos.partial_done:
                    pp = pos.entry * (1 + partial_pct)
                    if high >= pp:
                        sell_qty = pos.qty_initial * partial_size
                        exit_p = hh.fill(pp, 'sell')
                        pos.closes.append((sell_qty, exit_p, 'PARTIAL'))
                        pos.qty_remaining -= sell_qty
                        pos.partial_done = True
                        cash += sell_qty * exit_p
                        # BE BUMP — THE FIX
                        pos.stop = pos.entry * 1.001   # breakeven + tiny buffer
                        pos.be_moved = True

                # Profit trail (only after partial fires)
                if not sell and pos.peak >= pos.entry * (1 + profit_trail_pct):
                    trail_stop = pos.peak * (1 - profit_trail_pct)
                    if pos.be_moved and trail_stop < pos.entry * 1.001:
                        trail_stop = pos.entry * 1.001
                    if trail_stop > pos.stop:
                        pos.stop = trail_stop
                    if pos.stop > 0 and low <= pos.stop:
                        sell = True
                        reason = 'TRAIL'
                        exit_price = pos.stop

                # Max hold
                if not sell and pos.candle_count >= MAX_HOLD:
                    sell = True
                    reason = 'MAX_TIME'
                    exit_price = cur['c']

            if sell:
                sell_qty = pos.qty_remaining
                if sell_qty > 0:
                    exit_p = hh.fill(exit_price, 'sell')
                    pos.closes.append((sell_qty, exit_p, reason))
                    cash += sell_qty * exit_p
                entry_p = hh.fill(pos.entry, 'buy')
                pnl_total = sum((px - entry_p) * q for q, px, _ in pos.closes)
                trades.append({'coin': coin, 'pnl': pnl_total, 'win': pnl_total > 0,
                               'pattern': pos.pattern, 'partial_done': pos.partial_done})
                daily_pnl[day] += pnl_total
                equity = compute_equity()
                cooldowns[coin] = t + (86400 if pnl_total < 0 else 3600)
                del positions[coin]

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Compute current equity for tracking
        cur_eq = compute_equity()
        hourly_returns.append((cur_eq - last_equity) / last_equity if last_equity > 0 else 0)
        last_equity = cur_eq

        # ── KILL SWITCH ──
        if cur_eq < kill_switch_equity:
            kill_switch_hit = True
            # Close all positions
            for c_name, p in list(positions.items()):
                lc = data[c_name][-1]
                ep = hh.fill(p.entry, 'buy')
                exit_p = hh.fill(lc['c'], 'sell')
                p.closes.append((p.qty_remaining, exit_p, 'KILL_SWITCH'))
                cash += p.qty_remaining * exit_p
                pnl = sum((px - ep) * q for q, px, _ in p.closes)
                trades.append({'coin': c_name, 'pnl': pnl, 'win': pnl > 0,
                               'pattern': p.pattern, 'partial_done': p.partial_done})
                daily_pnl[day] += pnl
                del positions[c_name]
            break

        # ── DAILY LIMITS ──
        day_pnl_now = cur_eq - day_start_equity
        if day_pnl_now > day_start_equity * daily_profit_target_pct:
            continue  # daily profit target hit, no new entries
        if day_pnl_now < -day_start_equity * daily_loss_limit_pct:
            continue  # daily loss limit hit, no new entries

        # ── ENTRY GATE ──
        if idx < hh.WARMUP:
            continue
        if coin in positions:
            continue
        if cooldowns.get(coin, 0) > t:
            continue
        if len(positions) >= max_concurrent:
            continue
        if cash < MIN_CASH_RESERVE + 20_000:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue
        if use_mtf and not mtf_ok(cl):
            continue
        if use_btc_regime and not btc_ok(t):
            continue

        # Trend, ATR, doji filters
        if len(cl) >= 10:
            trend = (cl[-1]['c'] - cl[-10]['c']) / cl[-10]['c'] * 100
            if trend > 10 or trend < -10:
                continue
        if len(cl) >= 14:
            ar = sum(x['h'] - x['l'] for x in cl[-14:]) / 14
            if ar / cl[-1]['c'] * 100 < 0.3:
                continue
        rng = cl[-1]['h'] - cl[-1]['l']
        if rng > 0 and abs(cl[-1]['c'] - cl[-1]['o']) / rng < 0.1:
            continue

        hour_utc = time.gmtime(t).tm_hour
        score, pattern = hh.nt_detect_patterns(cl, hour_utc)
        if score < min_score:
            continue

        # Dynamic size by score (Cat B: tiny)
        if score >= 10:
            base_size = base_size_high
        elif score >= 8:
            base_size = base_size_med
        else:
            base_size = base_size_low

        # DD throttle
        dd_now = (peak_equity - cur_eq) / peak_equity if peak_equity > 0 else 0
        if dd_now > dd_throttle_pct:
            base_size *= dd_throttle_size_mult

        actual_size = min(base_size, (cash - MIN_CASH_RESERVE) * 0.5)
        if actual_size < 5_000:
            continue

        entry_price = cur['c']
        entry_effective = hh.fill(entry_price, 'buy')
        qty = actual_size / entry_effective
        cash -= qty * entry_effective
        positions[coin] = CatBPosition(coin, t, entry_price, qty, pattern, score)

    # EOD close
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        if pos.qty_remaining > 0:
            exit_p = hh.fill(last_c['c'], 'sell')
            pos.closes.append((pos.qty_remaining, exit_p, 'EOD'))
            cash += pos.qty_remaining * exit_p
        entry_p = hh.fill(pos.entry, 'buy')
        pnl = sum((px - entry_p) * q for q, px, _ in pos.closes)
        trades.append({'coin': coin, 'pnl': pnl, 'win': pnl > 0,
                       'pattern': pos.pattern, 'partial_done': pos.partial_done})

    final_equity = cash
    total_pnl = final_equity - STARTING_CAPITAL

    # Metrics
    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0,
                'sharpe': 0, 'sortino': 0, 'calmar': 0, 'composite': 0,
                'days_pnl': {}, 'kill_switch': kill_switch_hit}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins) if wins else 0
    gl = abs(sum(t['pnl'] for t in losses)) if losses else 0
    pf = gp / gl if gl > 0 else float('inf')

    # Compute Sharpe/Sortino from hourly returns
    if len(hourly_returns) > 2:
        mean_r = mean(hourly_returns)
        std_r = pstdev(hourly_returns) if len(hourly_returns) > 1 else 0
        neg_r = [r for r in hourly_returns if r < 0]
        neg_std = pstdev(neg_r) if len(neg_r) > 1 else 0
        # Annualize from hourly (8760 hours/year)
        sharpe = (mean_r / std_r * math.sqrt(8760)) if std_r > 0 else 0
        sortino = (mean_r / neg_std * math.sqrt(8760)) if neg_std > 0 else 0
    else:
        sharpe = 0
        sortino = 0

    # Calmar: annualized return / max drawdown
    period_return = total_pnl / STARTING_CAPITAL
    # days in the dataset (approx)
    total_hours = len(hourly_returns)
    annualized_return = period_return * (8760 / total_hours) if total_hours > 0 else 0
    calmar = annualized_return / max_dd if max_dd > 0 else 0

    composite = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar

    return {
        'trades': len(trades),
        'wr': wr,
        'pf': pf,
        'pnl': total_pnl,
        'dd': max_dd,
        'sharpe': sharpe,
        'sortino': sortino,
        'calmar': calmar,
        'composite': composite,
        'days_pnl': dict(daily_pnl),
        'kill_switch': kill_switch_hit,
        'final_equity': final_equity,
    }


def main():
    datasets = {
        'D1':        load('data/binance_1m_7d.json'),
        'D2':        load('data/1min_7days.json'),
        'D3-majors': {k: v for k, v in load('data/binance_1m_full.json').items()
                      if k in {"BTC","ETH","SOL","BNB","XRP","DOGE","ADA","TON","AVAX","LINK",
                               "DOT","TRX","MATIC","SHIB","LTC","NEAR","ATOM","ICP","APT","ARB"}},
        'D3-full':   load('data/binance_1m_full.json'),
    }
    for k, v in datasets.items():
        print(f"  {k}: {len(v)} coins, {sum(len(c) for c in v.values()):,} 1H candles")
    print()

    # CAT B candidate configs
    configs = {
        'C0-baseline-nt':  dict(base_size_low=200_000, base_size_med=250_000, base_size_high=350_000,
                                min_score=6, max_concurrent=4, use_mtf=False, use_btc_regime=False,
                                daily_profit_target_pct=10.0, daily_loss_limit_pct=10.0,  # effectively disabled
                                kill_switch_equity=0),
        'C1-tiny-strict':  dict(base_size_low=20_000, base_size_med=30_000, base_size_high=50_000,
                                min_score=8, max_concurrent=2),
        'C2-tinier':       dict(base_size_low=10_000, base_size_med=15_000, base_size_high=25_000,
                                min_score=8, max_concurrent=2),
        'C3-tiny+mtf':     dict(base_size_low=20_000, base_size_med=30_000, base_size_high=50_000,
                                min_score=8, max_concurrent=2, use_mtf=True),
        'C4-tiny+btc':     dict(base_size_low=20_000, base_size_med=30_000, base_size_high=50_000,
                                min_score=8, max_concurrent=2, use_btc_regime=True),
        'C5-tiny+all':     dict(base_size_low=20_000, base_size_med=30_000, base_size_high=50_000,
                                min_score=8, max_concurrent=2, use_mtf=True, use_btc_regime=True),
        'C6-q9':           dict(base_size_low=20_000, base_size_med=30_000, base_size_high=50_000,
                                min_score=9, max_concurrent=2),
        'C7-q10':          dict(base_size_low=30_000, base_size_med=40_000, base_size_high=60_000,
                                min_score=10, max_concurrent=2),
    }

    print(f"{'config':20} {'dataset':12} {'tr':>4} {'wr':>5} {'pnl':>11} "
          f"{'dd':>5} {'sharpe':>7} {'sortino':>8} {'calmar':>7} {'composite':>10}")
    print("─" * 110)

    results = {}
    for cfg_name, cfg in configs.items():
        results[cfg_name] = {}
        for ds_name, ds in datasets.items():
            r = run_catb(ds, **cfg)
            results[cfg_name][ds_name] = r
            print(f"{cfg_name:20} {ds_name:12} {r['trades']:4} "
                  f"{r['wr']*100:4.1f}% ${r['pnl']:+10,.0f} "
                  f"{r['dd']*100:4.1f}% {r['sharpe']:7.2f} {r['sortino']:8.2f} "
                  f"{r['calmar']:7.2f} {r['composite']:10.2f}")
        print()

    # Rank by combined composite across all datasets
    print()
    print("═══ RANK BY AVG COMPOSITE SCORE ═══")
    scores = []
    for cfg_name, ds_results in results.items():
        avg_comp = mean(r['composite'] for r in ds_results.values())
        min_comp = min(r['composite'] for r in ds_results.values())
        total_pnl = sum(r['pnl'] for r in ds_results.values())
        worst_pnl = min(r['pnl'] for r in ds_results.values())
        max_dd = max(r['dd'] for r in ds_results.values())
        scores.append({
            'cfg': cfg_name, 'avg_comp': avg_comp, 'min_comp': min_comp,
            'total_pnl': total_pnl, 'worst_pnl': worst_pnl, 'max_dd': max_dd,
        })
    scores.sort(key=lambda x: -x['avg_comp'])
    for s in scores:
        print(f"  {s['cfg']:20} avg_comp={s['avg_comp']:7.2f} min_comp={s['min_comp']:7.2f} "
              f"total=${s['total_pnl']:+,.0f} worst=${s['worst_pnl']:+,.0f} max_dd={s['max_dd']*100:.1f}%")

    # Also rank by D3-fresh composite only (most important for live deployment)
    print()
    print("═══ RANK BY D3-MAJORS COMPOSITE (the live market) ═══")
    d3_scores = [(cfg_name, results[cfg_name]['D3-majors']) for cfg_name in results]
    d3_scores.sort(key=lambda x: -x[1]['composite'])
    for cfg_name, r in d3_scores:
        print(f"  {cfg_name:20} comp={r['composite']:7.2f}  "
              f"pnl=${r['pnl']:+,.0f}  wr={r['wr']*100:.1f}%  "
              f"sharpe={r['sharpe']:.2f}  sortino={r['sortino']:.2f}  calmar={r['calmar']:.2f}")

    with open('data/catb_results.json', 'w') as fp:
        json.dump(results, fp, indent=2, default=str)
    print(f"\nsaved → data/catb_results.json")


if __name__ == '__main__':
    main()
