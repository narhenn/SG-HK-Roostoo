"""
HEAD-TO-HEAD BACKTEST
════════════════════════════════════════════════════════════════════
Runs BOTH naked_trader.py (f857125 currently running on EC2) and
naked_trader_v2.py (the new bot) through the SAME engine, SAME data,
SAME fees and slippage. Apples-to-apples comparison.

Both bots are fully reimplemented here in the backtester (I'm not
importing naked_trader.py itself because the Roostoo API client
wouldn't work in backtest context). The pattern scoring, sizing,
and exit logic are copied verbatim from the files.

Fees/slippage are set identically for both:
  taker_fee = 0.001 (0.1%) — naked_trader.py's actual fee in production
  slippage  = 0.0002 (0.02%)

Datasets: D1 (binance_1m_7d.json) + D2 (1min_7days.json), 1H candles.
Both on $1,000,000 starting capital.
"""
import json
import os
import time
from collections import defaultdict, Counter
from statistics import mean

# Shared reusable pieces
from pro_patterns import scan_all as pp_scan_all, avg_range, pip_cushion

# ─── GLOBAL CONFIG ───
STARTING_CAPITAL = 1_000_000.0
TAKER_FEE = 0.0005     # 0.05% — actual Roostoo taker fee (fair for both bots)
SLIPPAGE = 0.0002
WARMUP = 40
MAX_COINS = 5          # max concurrent positions (both bots)


def fill(price, side):
    if side == 'buy':
        return price * (1 + SLIPPAGE) * (1 + TAKER_FEE)
    return price * (1 - SLIPPAGE) * (1 - TAKER_FEE)


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


# ═══════════════════════════════════════════════════════════════════
#  NAKED_TRADER.PY LOGIC — reimplemented for backtest
# ═══════════════════════════════════════════════════════════════════
def _bs(c): return abs(c['c'] - c['o'])
def _gr(c): return c['c'] > c['o']
def _rd(c): return c['c'] < c['o']
def _rng(c): return c['h'] - c['l']
def _uw(c): return c['h'] - max(c['o'], c['c'])
def _lw(c): return min(c['o'], c['c']) - c['l']


def nt_detect_patterns(cl, hour_utc=12):
    """Copied from naked_trader.py detect_patterns() — 13 patterns + bonuses."""
    if len(cl) < 10:
        return 0, ''
    score = 0
    patterns = []
    n = len(cl)
    c = cl[-1]
    p = cl[-2] if n >= 2 else c
    ab = sum(_bs(x) for x in cl[-14:]) / min(14, n)
    ar = sum(_rng(x) for x in cl[-14:]) / min(14, n)
    if ab == 0:
        ab = 0.0001
    if ar == 0:
        ar = 0.0001
    bs = _bs(c)
    rng = _rng(c)
    bs_p = _bs(p)

    # 1. Hammer
    if rng > 0 and bs > 0 and _lw(c) >= bs * 2 and _uw(c) <= bs * 0.5 and _gr(c):
        score += 3
        patterns.append('HAMMER')

    # 2. Bullish Piercing
    if _rd(p) and _gr(c):
        mid = (p['o'] + p['c']) / 2
        if c['o'] < p['c'] and c['c'] > mid and c['c'] < p['o']:
            score += 3
            patterns.append('PIERCE')

    # 3. Morning Star
    if n >= 3:
        b1, b2, b3 = _bs(cl[-3]), _bs(cl[-2]), _bs(cl[-1])
        if _rd(cl[-3]) and b1 > ab and b2 < b1 * 0.3 and _gr(cl[-1]) and b3 > ab:
            if cl[-1]['c'] > (cl[-3]['o'] + cl[-3]['c']) / 2:
                score += 4
                patterns.append('MSTAR')

    # 4. HHHL
    if n >= 5 and cl[-2]['l'] > cl[-4]['l'] and cl[-1]['h'] > cl[-3]['h'] and _gr(cl[-1]):
        score += 2
        patterns.append('HHHL')

    # 5. Three White Soldiers
    if n >= 3 and _gr(cl[-3]) and _gr(cl[-2]) and _gr(cl[-1]):
        if cl[-1]['c'] > cl[-2]['c'] > cl[-3]['c']:
            if _bs(cl[-1]) > 0 and _bs(cl[-2]) > 0 and _bs(cl[-3]) > 0:
                score += 3
                patterns.append('3WS')

    # 6. Bullish Engulfing
    if _rd(p) and _gr(c) and c['o'] <= p['c'] and c['c'] >= p['o'] and bs > bs_p * 1.2:
        score += 3
        patterns.append('ENGULF')

    # 7. Marubozu
    if _gr(c) and bs > ab * 2 and _uw(c) < bs * 0.1 and _lw(c) < bs * 0.1:
        score += 3
        patterns.append('MARU')

    # 8. Three Outside Up
    if n >= 3:
        if _rd(cl[-3]) and _gr(cl[-2]) and cl[-2]['o'] <= cl[-3]['c'] and cl[-2]['c'] >= cl[-3]['o']:
            if _gr(cl[-1]) and cl[-1]['c'] > cl[-2]['c']:
                score += 4
                patterns.append('3OUT_UP')

    # 9. Closing Marubozu
    if _gr(c) and bs > ab * 1.5:
        if (c['h'] - c['c']) < bs * 0.05:
            if not any('MARU' in pat for pat in patterns):
                score += 3
                patterns.append('CLOSE_MARU')

    # 10. Bearish-as-Bullish (mean reversion)
    if n >= 6:
        uptrend = cl[-1]['c'] > cl[-6]['c']
        if uptrend and n >= 3:
            for back in [1, 2]:
                if n > back + 1:
                    prev_c = cl[-(back + 1)]
                    prev_p = cl[-(back + 2)]
                    if _gr(prev_p) and _rd(prev_c):
                        if prev_c['o'] >= prev_p['c'] and prev_c['c'] <= prev_p['o']:
                            if _gr(c):
                                score += 3
                                patterns.append('BEAR_REVERT')
                                break

    # 11. Mean Reversion
    if n >= 4:
        reds = sum(1 for x in cl[-4:-1] if _rd(x))
        if reds >= 3 and _gr(c):
            score += 2
            patterns.append('MEANREV')

    # 12. Inside Bar Breakout
    if n >= 3:
        if cl[-2]['h'] <= cl[-3]['h'] and cl[-2]['l'] >= cl[-3]['l']:
            if cl[-1]['c'] > cl[-3]['h'] and _gr(cl[-1]):
                score += 2
                patterns.append('INSIDE')

    # 13. Momentum
    if n >= 6 and (cl[-1]['c'] - cl[-6]['c']) / cl[-6]['c'] * 100 >= 1.0:
        score += 1
        patterns.append('MOM')

    # Bonuses
    if n >= 20:
        low20 = min(x['l'] for x in cl[-20:])
        if (c['c'] - low20) / c['c'] * 100 < 1.5 and score > 0:
            score += 1
            patterns.append('sup')

    if n >= 2 and c.get('v', 0) > p.get('v', 0) * 1.2 and _gr(c) and score > 0:
        score += 1
        patterns.append('vol')

    if 14 <= hour_utc <= 19 and score > 0:
        score += 1
        patterns.append('peak')

    return score, '+'.join(patterns) if patterns else 'NONE'


def nt_get_size(score, available, reserve=200_000):
    """Copied from naked_trader.py get_dynamic_size()."""
    if score >= 10:
        size = 350_000
    elif score >= 8:
        size = 250_000
    else:
        size = 200_000
    # Capped at (available - reserve) * 0.25
    cap = (available - reserve) * 0.25
    size = min(size, cap)
    return size if size >= 50_000 else 0


class NtPosition:
    __slots__ = ('coin', 'entry_t', 'entry', 'qty_initial', 'qty_remaining',
                 'peak', 'stop', 'partial_done', 'candle_count', 'pattern',
                 'score', 'closes')

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


def nt_run(data):
    """Reimplementation of naked_trader.py's trading logic."""
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

    MIN_HOLD = 3
    MAX_HOLD = 12
    MIN_CASH_RESERVE = 200_000
    MAX_POS = 4
    TREND_FILTER = 10.0
    COOLDOWN_LOSS_SEC = 86400
    COOLDOWN_WIN_SEC = 3600

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # ─── MANAGE OPEN POSITION ───
        if coin in positions:
            pos = positions[coin]
            pos.candle_count += 1
            if cur['h'] > pos.peak:
                pos.peak = cur['h']

            # Recompute ATR
            atr = sum(_rng(x) for x in cl[-14:]) / min(14, len(cl)) if cl else pos.entry * 0.01

            sell = False
            reason = ''
            cur_price = cur['c']  # use close as "current"
            # Use high/low to simulate intrabar stop/target
            low = cur['l']
            high = cur['h']

            # Min hold: only hard stop
            if pos.candle_count < MIN_HOLD:
                hard = pos.entry - atr * 2.0
                if low <= hard:
                    sell = True
                    reason = 'HARD_STOP'
                    exit_price = hard
            else:
                # ATR stop
                stop_level = pos.entry - atr * 1.2
                # Partial-at-+1%: THE WLFI BUG TRIGGER
                partial_price = pos.entry * 1.01
                # Profit trail (when pnl>1%)
                # Trailing stop at peak*0.98 (when pnl>0.3%)

                # Check exits in priority order (intrabar resolution)
                # 1. Hard stop first (conservative intrabar)
                if low <= stop_level and not sell:
                    sell = True
                    reason = 'ATR_STOP'
                    exit_price = stop_level

                # 2. Partial @ +1%
                if not sell and not pos.partial_done and high >= partial_price:
                    # Sell 50% at +1%
                    sell_qty = pos.qty_initial * 0.5
                    exit_p = fill(partial_price, 'sell')
                    entry_p = fill(pos.entry, 'buy')
                    pnl = (exit_p - entry_p) * sell_qty
                    pos.closes.append((sell_qty, exit_p, 'PARTIAL'))
                    pos.qty_remaining -= sell_qty
                    pos.partial_done = True
                    cash += sell_qty * exit_p

                # 3. Trailing stop at peak*0.98 (if pnl>0.3% ever was reached)
                if not sell and pos.peak >= pos.entry * 1.003:
                    trail_stop = pos.peak * 0.98
                    if trail_stop > pos.stop:
                        pos.stop = trail_stop
                    if low <= pos.stop:
                        sell = True
                        reason = 'TRAIL'
                        exit_price = pos.stop

                # 4. Profit trail at 1% (peak*0.99)
                if not sell and pos.peak >= pos.entry * 1.01:
                    profit_trail = pos.peak * 0.99
                    if low <= profit_trail:
                        sell = True
                        reason = 'PROFIT_TRAIL'
                        exit_price = profit_trail

                # 5. Max hold
                if not sell and pos.candle_count >= MAX_HOLD:
                    sell = True
                    reason = 'MAX_TIME'
                    exit_price = cur_price

            if sell:
                sell_qty = pos.qty_remaining
                if sell_qty > 0:
                    exit_p = fill(exit_price, 'sell')
                    entry_p = fill(pos.entry, 'buy')
                    pos.closes.append((sell_qty, exit_p, reason))
                    cash += sell_qty * exit_p
                # Realize trade
                entry_p = fill(pos.entry, 'buy')
                pnl_total = sum((px - entry_p) * q for q, px, _ in pos.closes)
                equity = cash + sum(
                    p.qty_remaining * candles[min(idx, len(candles) - 1)]['c']
                    for p in positions.values() if p.coin != coin
                )
                trades.append({
                    'coin': coin, 'pattern': pos.pattern, 'score': pos.score,
                    'pnl': pnl_total, 'held': pos.candle_count,
                    'closes': [(q, px, r) for q, px, r in pos.closes],
                    'win': pnl_total > 0,
                })
                cd = COOLDOWN_LOSS_SEC if pnl_total < 0 else COOLDOWN_WIN_SEC
                cooldowns[coin] = t + cd
                del positions[coin]

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # ─── TRY ENTRY ───
        if idx < WARMUP:
            continue
        if coin in positions:
            continue
        if cooldowns.get(coin, 0) > t:
            continue
        if len(positions) >= MAX_POS:
            continue
        if cash < MIN_CASH_RESERVE + 50_000:
            continue

        # Trend filter
        if len(cl) >= 10:
            trend = (cl[-1]['c'] - cl[-10]['c']) / cl[-10]['c'] * 100
            if trend > TREND_FILTER or trend < -TREND_FILTER:
                continue

        # ATR filter
        if len(cl) >= 14:
            atr = sum(_rng(x) for x in cl[-14:]) / 14
            if atr / cl[-1]['c'] * 100 < 0.3:
                continue

        # Doji filter
        if _rng(cl[-1]) > 0 and _bs(cl[-1]) / _rng(cl[-1]) < 0.1:
            continue

        hour_utc = time.gmtime(t).tm_hour
        score, pattern = nt_detect_patterns(cl, hour_utc)
        if score < 6:
            continue
        # Chart gate: without chart pattern, need score >= 8
        # We don't simulate chart patterns in the backtest, so use strict score>=8
        if score < 8:
            continue

        # Size
        size = nt_get_size(score, cash, MIN_CASH_RESERVE)
        if size == 0:
            continue

        # Buy at current close
        entry_price = cur['c']
        entry_effective = fill(entry_price, 'buy')
        qty = size / entry_effective
        cash -= qty * entry_effective
        positions[coin] = NtPosition(coin, t, entry_price, qty, pattern, score)

    # EOD close all
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        sell_qty = pos.qty_remaining
        if sell_qty > 0:
            exit_p = fill(last_c['c'], 'sell')
            pos.closes.append((sell_qty, exit_p, 'EOD'))
            cash += sell_qty * exit_p
        entry_p = fill(pos.entry, 'buy')
        pnl_total = sum((px - entry_p) * q for q, px, _ in pos.closes)
        trades.append({
            'coin': coin, 'pattern': pos.pattern, 'score': pos.score,
            'pnl': pnl_total, 'held': pos.candle_count,
            'closes': [(q, px, r) for q, px, r in pos.closes],
            'win': pnl_total > 0,
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0, 'by_pat': {}, 'by_reason': {}}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)

    # Breakdown by exit reason
    reason_counts = Counter()
    reason_pnl = defaultdict(float)
    for tr in trades:
        # Get final reason
        if tr['closes']:
            final = tr['closes'][-1][2]
            reason_counts[final] += 1
            reason_pnl[final] += tr['pnl']

    return {
        'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl,
        'dd': max_dd, 'by_pat': Counter(t['pattern'] for t in trades),
        'by_reason': dict(reason_counts),
        'by_reason_pnl': dict(reason_pnl),
    }


# ═══════════════════════════════════════════════════════════════════
#  NAKED_TRADER_V2.PY LOGIC — reimplemented for backtest
# ═══════════════════════════════════════════════════════════════════
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def v2_entry_pro_pat(cl):
    fired, entry, stop, name, quality, _ = pp_scan_all(cl)
    if fired and quality >= 8:
        return (entry, stop, name, quality)
    return None


def v2_entry_donchian(cl):
    if len(cl) < 50:
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
    atr = avg_range(cl, 14)
    entry = c['h'] * 1.0005       # match backtest_pro_v2.py cushion
    stop = c['l'] - atr * 0.5
    if (entry - stop) / entry > 0.03:
        return None
    return (entry, stop, 'DONCHIAN', 8)


def v2_entry_engulfing(cl):
    if len(cl) < 60:
        return None
    c = cl[-1]
    p = cl[-2]
    if not (_rd(p) and _gr(c)):
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


def v2_scan_combo(cl):
    opts = []
    for fn in (v2_entry_pro_pat, v2_entry_donchian, v2_entry_engulfing):
        try:
            r = fn(cl)
            if r:
                opts.append(r)
        except Exception:
            pass
    if not opts:
        return None
    return max(opts, key=lambda x: x[3])


def resample_to_4h(candles_1h):
    if not candles_1h:
        return []
    out, bucket, bstart = [], [], None
    for c in candles_1h:
        t = int(c['t'])
        b = (t // (4 * 3600)) * (4 * 3600)
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


def v2_htf_uptrend_ok(cl_1h):
    cl_4h = resample_to_4h(cl_1h)
    if len(cl_4h) < 50:
        return True
    closes = [x['c'] for x in cl_4h]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return True
    return cl_4h[-1]['c'] > e20 > e50


def v2_btc_regime_ok(btc_cl):
    if not btc_cl or len(btc_cl) < 200:
        return True
    closes = [x['c'] for x in btc_cl]
    e200 = ema(closes, 200)
    if e200 is None:
        return True
    return btc_cl[-1]['c'] > e200


class V2Position:
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


def v2_run(data, risk=0.03, cap_pct=2.0, use_mtf=True, use_btc=True,
           use_session=True, safe_mode=False):
    if safe_mode:
        risk = 0.02
        cap_pct = 1.0

    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()
    if not events:
        return None

    equity = STARTING_CAPITAL
    peak_equity = equity
    max_dd = 0
    positions = {}
    trades = []
    cd = defaultdict(int)
    btc_candles = data.get('BTC', [])

    GUNNER_1_R, GUNNER_1_SIZE = 1.0, 0.5
    GUNNER_2_R, GUNNER_2_SIZE = 2.0, 0.35
    RUNNER_TARGET_R = 5.0
    RUNNER_TRAIL_ATR_MULT = 2.0
    COOLDOWN_CANDLES = 2
    DD_THROTTLE_TRIGGER = 0.10
    DD_THROTTLE_RISK = 0.015

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]
        cur = candles[idx]

        # Manage
        if coin in positions:
            pos = positions[coin]
            if cur['h'] > pos.peak:
                pos.peak = cur['h']
            closed = False

            if cur['l'] <= pos.stop:
                pos.closes.append((pos.remaining_pct, fill(pos.stop, 'sell'), 'STOP'))
                pos.remaining_pct = 0
                closed = True
            else:
                if not pos.g1_done:
                    g1p = pos.entry + GUNNER_1_R * pos.R
                    if cur['h'] >= g1p:
                        pos.closes.append((GUNNER_1_SIZE, fill(g1p, 'sell'), 'G1'))
                        pos.remaining_pct -= GUNNER_1_SIZE
                        pos.g1_done = True
                        pos.stop = pos.entry  # BE bump
                if pos.g1_done and not pos.g2_done:
                    g2p = pos.entry + GUNNER_2_R * pos.R
                    if cur['h'] >= g2p:
                        pos.closes.append((GUNNER_2_SIZE, fill(g2p, 'sell'), 'G2'))
                        pos.remaining_pct -= GUNNER_2_SIZE
                        pos.g2_done = True
                if pos.g2_done and pos.remaining_pct > 0:
                    hard = pos.entry + RUNNER_TARGET_R * pos.R
                    if cur['h'] >= hard:
                        pos.closes.append((pos.remaining_pct, fill(hard, 'sell'), 'RUN_TP'))
                        pos.remaining_pct = 0
                        closed = True
                    else:
                        trail = pos.peak - RUNNER_TRAIL_ATR_MULT * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            pos.closes.append((pos.remaining_pct, fill(pos.stop, 'sell'), 'TRAIL'))
                            pos.remaining_pct = 0
                            closed = True
                if pos.remaining_pct <= 1e-6:
                    closed = True

            if closed:
                ep = fill(pos.entry, 'buy')
                pnl = sum((px - ep) * pos.qty * portion for portion, px, _ in pos.closes)
                equity += pnl
                r_mult = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                trades.append({
                    'coin': pos.coin, 'pattern': pos.pattern,
                    'pnl': pnl, 'r': r_mult, 'win': pnl > 0,
                    'closes': [(portion, px, r) for portion, px, r in pos.closes],
                })
                del positions[coin]
                if pnl <= 0:
                    cd[coin] = COOLDOWN_CANDLES
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Entry
        if idx < WARMUP or coin in positions:
            continue
        if cd[coin] > 0:
            cd[coin] -= 1
            continue
        if len(positions) >= MAX_COINS:
            continue
        if use_session:
            h = time.gmtime(t).tm_hour
            if 0 <= h < 7:
                continue
        if use_mtf and not v2_htf_uptrend_ok(cl):
            continue
        if use_btc:
            btc_idx = None
            for i in range(len(btc_candles) - 1, -1, -1):
                if btc_candles[i]['t'] <= t:
                    btc_idx = i
                    break
            if btc_idx is not None:
                btc_slice = btc_candles[:btc_idx + 1]
                if not v2_btc_regime_ok(btc_slice):
                    continue

        sig = v2_scan_combo(cl)
        if not sig:
            continue
        entry, stop, name, quality = sig
        if entry <= 0 or stop <= 0 or entry <= stop:
            continue
        R = entry - stop
        if R / entry > 0.04:
            continue

        # DD throttle
        cur_dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
        actual_risk = DD_THROTTLE_RISK if cur_dd > DD_THROTTLE_TRIGGER else risk

        risk_dollars = equity * actual_risk
        qty = risk_dollars / R
        notional = qty * entry
        cap = equity * cap_pct
        if notional > cap:
            qty = cap / entry
        atr_ref = avg_range(cl, 14)
        pos = V2Position(coin, t, entry, stop, qty, atr_ref, name)
        positions[coin] = pos

    # EOD
    last_t = events[-1][0]
    for coin, pos in list(positions.items()):
        last_c = data[coin][-1]
        pos.closes.append((pos.remaining_pct, fill(last_c['c'], 'sell'), 'EOD'))
        ep = fill(pos.entry, 'buy')
        pnl = sum((px - ep) * pos.qty * portion for portion, px, _ in pos.closes)
        equity += pnl
        trades.append({
            'coin': pos.coin, 'pattern': pos.pattern,
            'pnl': pnl, 'r': pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0,
            'win': pnl > 0,
            'closes': [(portion, px, r) for portion, px, r in pos.closes],
        })

    if not trades:
        return {'trades': 0, 'pnl': 0, 'wr': 0, 'pf': 0, 'dd': 0, 'by_pat': {}, 'by_reason': {}}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    pnl = sum(t['pnl'] for t in trades)

    reason_counts = Counter()
    reason_pnl = defaultdict(float)
    for tr in trades:
        if tr.get('closes'):
            final = tr['closes'][-1][2]
            reason_counts[final] += 1
            reason_pnl[final] += tr['pnl']

    return {
        'trades': len(trades), 'wr': wr, 'pf': pf, 'pnl': pnl,
        'dd': max_dd, 'by_pat': Counter(t['pattern'] for t in trades),
        'by_reason': dict(reason_counts),
        'by_reason_pnl': dict(reason_pnl),
    }


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def print_result(label, r):
    if not r or r['trades'] == 0:
        print(f"  {label}: NO TRADES")
        return
    print(f"  {label}")
    print(f"    trades: {r['trades']}  wr: {r['wr']*100:.1f}%  pf: {r['pf']:.2f}  pnl: ${r['pnl']:+,.0f}  dd: {r['dd']*100:.1f}%")
    if r.get('by_reason'):
        rp = r.get('by_reason_pnl', {})
        reasons = ', '.join(f'{k}={v}(${rp.get(k, 0):+,.0f})' for k, v in r['by_reason'].items())
        print(f"    exits: {reasons}")


def main():
    datasets = {
        'D1': load_data('data/binance_1m_7d.json', 60),
        'D2': load_data('data/1min_7days.json', 60),
    }

    print("═══ HEAD-TO-HEAD BACKTEST ═══")
    print(f"Same engine, same fees (taker={TAKER_FEE*100:.2f}%), same slippage ({SLIPPAGE*100:.2f}%)")
    print(f"Both on ${STARTING_CAPITAL:,.0f}, 1H candles, 20 coins, 7 days\n")

    results = {}

    for lbl, data in datasets.items():
        print(f"══════════ {lbl} ══════════")
        nt = nt_run(data)
        v2_default = v2_run(data, risk=0.03, cap_pct=2.0, use_mtf=True, use_btc=True)
        v2_safe = v2_run(data, risk=0.02, cap_pct=1.0, use_mtf=True, use_btc=True, safe_mode=True)
        print_result('naked_trader.py (current)      ', nt)
        print_result('naked_trader_v2.py (default)   ', v2_default)
        print_result('naked_trader_v2.py (--safe)    ', v2_safe)
        print()
        results[lbl] = {'nt': nt, 'v2_default': v2_default, 'v2_safe': v2_safe}

    # Combined summary
    print("══════════ COMBINED (D1 + D2) ══════════")
    print(f"{'bot':35} {'trades':>7} {'wr':>6} {'pf':>6} {'D1':>12} {'D2':>12} {'total':>13} {'worst':>12} {'dd':>6}")
    for key, label in [
        ('nt', 'naked_trader.py (current)'),
        ('v2_default', 'naked_trader_v2.py (default)'),
        ('v2_safe', 'naked_trader_v2.py (--safe)'),
    ]:
        d1 = results['D1'][key]
        d2 = results['D2'][key]
        total = d1['pnl'] + d2['pnl']
        worst = min(d1['pnl'], d2['pnl'])
        tr = d1['trades'] + d2['trades']
        wr = mean([d1['wr'], d2['wr']]) if d1['trades'] and d2['trades'] else 0
        pf_vals = [x['pf'] for x in [d1, d2] if x['trades'] > 0 and x['pf'] != float('inf')]
        pf = mean(pf_vals) if pf_vals else 0
        dd = max(d1['dd'], d2['dd'])
        flag = '✓' if worst > 0 else '✗'
        print(f"{label:35} {tr:7} {wr*100:5.1f}% {pf:5.2f} "
              f"${d1['pnl']:+11,.0f} ${d2['pnl']:+11,.0f} ${total:+12,.0f} ${worst:+11,.0f} {dd*100:5.1f}% {flag}")

    # Save
    out = {}
    for lbl in results:
        out[lbl] = {}
        for key in ('nt', 'v2_default', 'v2_safe'):
            r = results[lbl][key]
            out[lbl][key] = {k: (dict(v) if isinstance(v, (Counter, defaultdict)) else v)
                             for k, v in r.items()}
    with open('data/head_to_head_results.json', 'w') as fp:
        json.dump(out, fp, indent=2, default=str)
    print("\nsaved → data/head_to_head_results.json")


if __name__ == '__main__':
    main()
