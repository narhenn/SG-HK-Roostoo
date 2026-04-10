"""
PRO TRADER BACKTEST
Tests the pro strategy from pro_patterns.py with R-based position sizing and
3-tier scale-out (Gunner 1 at +1R with breakeven bump, Gunner 2 at +2R, Runner
trailing by ATR).

Core rules (from Karthik Forex Business Plan 2015 + Naked Forex):
  - 1% of capital risked per trade (R = entry - stop)
  - 50% exits at +1R → stop moves to entry (breakeven). Fixes the "WLFI bug"
    where a partial-at-profit + percent-trail ended up net-negative.
  - 35% exits at +2R
  - 15% Runner trails 2x ATR below peak. Exits on trail or +5R hard target.
  - Session filter: skip 00-07 UTC (Asian session = crypto low-liquidity).
  - Zone filter: entry must occur near a support zone.
  - Min risk/reward 1:1 (implicit via R structure).
  - Cooldown of 2 candles after a stop-loss.

Outputs metrics: Win Rate, Avg R, Profit Factor, Sharpe, Sortino, Max DD,
Expectancy, Total Return, cumulative $ P&L against a $1,000,000 base.
"""
import json
import math
import os
import sys
import time
from collections import defaultdict, Counter
from statistics import mean, pstdev

from pro_patterns import (
    scan_all,
    find_zones,
    is_in_zone,
    avg_range,
)

# ══════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════
STARTING_CAPITAL = 1_000_000.0
RISK_PER_TRADE = 0.01          # 1% of current equity per trade
MAX_OPEN_POSITIONS = 5          # hard cap on concurrent positions
MAKER_FEE = 0.0005              # 0.05% per side
SLIPPAGE_PCT = 0.0003           # 0.03% slippage on each fill

GUNNER_1_R = 1.0                # first partial at +1R
GUNNER_1_SIZE = 0.50            # 50% off
GUNNER_2_R = 2.0                # second partial at +2R
GUNNER_2_SIZE = 0.35            # 35% off
RUNNER_TARGET_R = 5.0           # hard take-profit for runner
RUNNER_TRAIL_ATR_MULT = 2.0     # trailing stop = peak - 2*ATR

MIN_QUALITY = 6                 # only take patterns with quality >= 6
USE_ZONE_FILTER = True          # require catalyst in a support zone
USE_SESSION_FILTER = True       # skip 00-07 UTC (Asian session)
COOLDOWN_CANDLES_AFTER_STOP = 2

# Resample granularity: 60 = 1H candles (from 1-min data)
RESAMPLE_MINUTES = 60


def log(msg):
    print(msg, flush=True)


# ══════════════════════════════════════════════════════
#  DATA HELPERS
# ══════════════════════════════════════════════════════
def resample(candles_1m, minutes):
    """Resample 1-minute candles to larger timeframe."""
    if not candles_1m:
        return []
    out = []
    bucket = []
    bucket_start = None
    for c in candles_1m:
        t = int(c['t'])
        bstart = (t // (minutes * 60)) * (minutes * 60)
        if bucket_start is None:
            bucket_start = bstart
        if bstart != bucket_start:
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
            bucket_start = bstart
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


def load_data(path):
    with open(path) as fp:
        raw = json.load(fp)
    out = {}
    for coin, candles in raw.items():
        # sort by timestamp, dedupe, resample
        candles = sorted(candles, key=lambda x: x['t'])
        out[coin] = resample(candles, RESAMPLE_MINUTES)
    return out


# ══════════════════════════════════════════════════════
#  POSITION / TRADE
# ══════════════════════════════════════════════════════
class Position:
    __slots__ = (
        'coin', 'entry_t', 'entry', 'stop', 'initial_stop', 'R',
        'qty', 'remaining_pct', 'peak', 'atr_ref', 'pattern', 'quality',
        'gunner1_done', 'gunner2_done', 'be_moved', 'closes',
    )

    def __init__(self, coin, entry_t, entry, stop, qty, atr_ref, pattern, quality):
        self.coin = coin
        self.entry_t = entry_t
        self.entry = entry
        self.stop = stop
        self.initial_stop = stop
        self.R = entry - stop
        self.qty = qty
        self.remaining_pct = 1.0
        self.peak = entry
        self.atr_ref = atr_ref
        self.pattern = pattern
        self.quality = quality
        self.gunner1_done = False
        self.gunner2_done = False
        self.be_moved = False
        # list of (portion_of_initial_qty, exit_price, reason)
        self.closes = []


def fill_with_fees(price, side):
    """Apply slippage and fees; side is 'buy' or 'sell'."""
    slip = SLIPPAGE_PCT
    if side == 'buy':
        p = price * (1 + slip)
        p = p * (1 + MAKER_FEE)
    else:
        p = price * (1 - slip)
        p = p * (1 - MAKER_FEE)
    return p


def close_portion(pos, portion_of_initial, price, reason):
    """Close `portion_of_initial` of the original position at `price`."""
    pos.closes.append((portion_of_initial, fill_with_fees(price, 'sell'), reason))
    pos.remaining_pct -= portion_of_initial


def realized_pnl(pos):
    """Dollars realized so far (entry already had fees applied)."""
    entry_effective = fill_with_fees(pos.entry, 'buy')
    dollars = 0.0
    for portion, px, _ in pos.closes:
        dollars += (px - entry_effective) * pos.qty * portion
    return dollars


def r_multiple(pos, price):
    """Compute R multiple of a price vs entry & R."""
    if pos.R <= 0:
        return 0
    return (price - pos.entry) / pos.R


# ══════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════
def run_backtest(data, label=''):
    """
    data: dict[coin] -> list of candles
    Iterates candle-by-candle in global time order.
    """
    log(f"\n══════════ {label} ══════════")

    # Build a global time index: list of (t, coin, idx)
    events = []
    for coin, candles in data.items():
        for i, c in enumerate(candles):
            events.append((c['t'], coin, i))
    events.sort()

    if not events:
        log("no events")
        return None

    equity = STARTING_CAPITAL
    peak_equity = equity
    max_dd_pct = 0.0
    max_dd_dollar = 0.0

    open_positions = {}  # coin -> Position
    closed_trades = []   # list of dicts
    cooldown = defaultdict(int)  # coin -> candles left in cooldown
    equity_curve = []  # list of (t, equity)
    daily_returns = {}  # day_idx -> return

    # warmup: don't scan until we have enough history
    WARMUP = 40

    for t, coin, idx in events:
        candles = data[coin]
        cl = candles[:idx + 1]  # history up to and including current candle
        cur = candles[idx]

        # ─── 1) Manage open position on this coin (exits BEFORE new entries) ───
        if coin in open_positions:
            pos = open_positions[coin]
            # Update peak
            if cur['h'] > pos.peak:
                pos.peak = cur['h']

            closed_all = False

            # --- Check intrabar stop hit FIRST (conservative: low before high) ---
            if cur['l'] <= pos.stop:
                # stopped out
                stop_price = pos.stop
                close_portion(pos, pos.remaining_pct, stop_price, 'STOP')
                closed_all = True

            else:
                # --- Gunner 1 @ +1R ---
                if not pos.gunner1_done:
                    g1_price = pos.entry + GUNNER_1_R * pos.R
                    if cur['h'] >= g1_price:
                        close_portion(pos, GUNNER_1_SIZE, g1_price, 'G1')
                        pos.gunner1_done = True
                        # Move to breakeven (fixes the WLFI bug)
                        pos.stop = pos.entry
                        pos.be_moved = True

                # --- Gunner 2 @ +2R ---
                if not pos.gunner2_done and pos.gunner1_done:
                    g2_price = pos.entry + GUNNER_2_R * pos.R
                    if cur['h'] >= g2_price:
                        close_portion(pos, GUNNER_2_SIZE, g2_price, 'G2')
                        pos.gunner2_done = True

                # --- Runner: hard target @ +5R OR ATR trail ---
                if pos.remaining_pct > 0 and pos.gunner2_done:
                    hard = pos.entry + RUNNER_TARGET_R * pos.R
                    if cur['h'] >= hard:
                        close_portion(pos, pos.remaining_pct, hard, 'RUN_TP')
                        closed_all = True
                    else:
                        # ATR trail for the runner
                        trail = pos.peak - RUNNER_TRAIL_ATR_MULT * pos.atr_ref
                        if trail > pos.stop:
                            pos.stop = trail
                        if cur['l'] <= pos.stop and pos.remaining_pct > 0:
                            close_portion(pos, pos.remaining_pct, pos.stop, 'TRAIL')
                            closed_all = True

                if pos.remaining_pct <= 1e-6:
                    closed_all = True

            if closed_all:
                # Finalize trade
                pnl = realized_pnl(pos)
                equity += pnl
                r_real = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
                was_winner = pnl > 0
                reasons = '+'.join(r for _, _, r in pos.closes)
                closed_trades.append({
                    'coin': pos.coin,
                    'entry_t': pos.entry_t,
                    'exit_t': t,
                    'pattern': pos.pattern,
                    'quality': pos.quality,
                    'entry': pos.entry,
                    'stop': pos.initial_stop,
                    'R_dollars_per_unit': pos.R,
                    'pnl': pnl,
                    'r_multiple': r_real,
                    'reasons': reasons,
                    'win': was_winner,
                })
                del open_positions[coin]
                # cooldown on losing trades
                if not was_winner:
                    cooldown[coin] = COOLDOWN_CANDLES_AFTER_STOP

            # Peak equity + drawdown
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity
            if dd > max_dd_pct:
                max_dd_pct = dd
                max_dd_dollar = peak_equity - equity

        # ─── 2) Look for a new entry on this coin ───
        if idx < WARMUP:
            equity_curve.append((t, equity))
            continue

        if coin in open_positions:
            equity_curve.append((t, equity))
            continue

        if cooldown[coin] > 0:
            cooldown[coin] -= 1
            equity_curve.append((t, equity))
            continue

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            equity_curve.append((t, equity))
            continue

        # Session filter: skip 00-07 UTC
        if USE_SESSION_FILTER:
            hour_utc = time.gmtime(t).tm_hour
            if 0 <= hour_utc < 7:
                equity_curve.append((t, equity))
                continue

        fired, entry, stop, name, quality, all_fired = scan_all(cl)
        if not fired or quality < MIN_QUALITY:
            equity_curve.append((t, equity))
            continue

        if entry <= 0 or stop <= 0 or entry <= stop:
            equity_curve.append((t, equity))
            continue

        # Zone filter: entry should be near a support zone (or zone must exist)
        if USE_ZONE_FILTER:
            zones = find_zones(cl, lookback=50)
            in_z, _ = is_in_zone(cur['c'], zones)
            # Soft rule: require a support zone anywhere within 2% below entry
            zone_ok = in_z or any(
                z['type'] == 'support' and 0 < (entry - z['level']) / entry < 0.02
                for z in zones
            )
            if not zone_ok and quality < 9:
                # allow quality >=9 patterns to bypass zone filter (picture-perfect KT)
                equity_curve.append((t, equity))
                continue

        # Sanity: risk per unit
        R = entry - stop
        if R / entry > 0.04:
            # Risk too wide — reject
            equity_curve.append((t, equity))
            continue

        # Position sizing: risk = equity * RISK_PER_TRADE
        risk_dollars = equity * RISK_PER_TRADE
        qty = risk_dollars / R
        # Cap notional to 20% of equity
        notional = qty * entry
        max_notional = equity * 0.20
        if notional > max_notional:
            qty = max_notional / entry

        # ATR reference for the runner trail
        atr_ref = avg_range(cl, n=14)

        pos = Position(
            coin=coin,
            entry_t=t,
            entry=entry,
            stop=stop,
            qty=qty,
            atr_ref=atr_ref,
            pattern=name,
            quality=quality,
        )
        open_positions[coin] = pos
        equity_curve.append((t, equity))

    # Force-close anything still open at end
    last_t = events[-1][0]
    for coin, pos in list(open_positions.items()):
        last_c = data[coin][-1]
        close_portion(pos, pos.remaining_pct, last_c['c'], 'EOD')
        pnl = realized_pnl(pos)
        equity += pnl
        r_real = pnl / (pos.qty * pos.R) if pos.qty * pos.R > 0 else 0
        closed_trades.append({
            'coin': pos.coin,
            'entry_t': pos.entry_t,
            'exit_t': last_t,
            'pattern': pos.pattern,
            'quality': pos.quality,
            'entry': pos.entry,
            'stop': pos.initial_stop,
            'R_dollars_per_unit': pos.R,
            'pnl': pnl,
            'r_multiple': r_real,
            'reasons': '+'.join(r for _, _, r in pos.closes),
            'win': pnl > 0,
        })
    open_positions.clear()

    # ─── Metrics ───
    return compute_metrics(label, closed_trades, equity_curve, max_dd_pct, max_dd_dollar)


def compute_metrics(label, trades, equity_curve, max_dd_pct, max_dd_dollar):
    if not trades:
        log(f"  {label}: NO TRADES")
        return {
            'label': label, 'trades': 0, 'wr': 0, 'avg_r': 0,
            'pf': 0, 'pnl': 0, 'sharpe': 0, 'sortino': 0,
            'max_dd_pct': 0, 'max_dd_dollar': 0, 'expectancy': 0, 'trades_list': [],
        }

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    wr = len(wins) / len(trades)

    gross_profit = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    avg_r = mean(t['r_multiple'] for t in trades)
    total_pnl = sum(t['pnl'] for t in trades)
    avg_win_r = mean(t['r_multiple'] for t in wins) if wins else 0
    avg_loss_r = mean(t['r_multiple'] for t in losses) if losses else 0
    expectancy_r = wr * avg_win_r + (1 - wr) * avg_loss_r

    # Sharpe / Sortino from equity curve (per-event returns)
    equity_values = [e for _, e in equity_curve]
    if len(equity_values) > 2:
        rets = []
        prev = equity_values[0]
        for v in equity_values[1:]:
            if prev > 0:
                rets.append((v - prev) / prev)
            prev = v
        if rets and pstdev(rets) > 0:
            sharpe = (mean(rets) / pstdev(rets)) * math.sqrt(len(rets))
        else:
            sharpe = 0
        neg_rets = [r for r in rets if r < 0]
        if neg_rets and pstdev(neg_rets) > 0:
            sortino = (mean(rets) / pstdev(neg_rets)) * math.sqrt(len(rets))
        else:
            sortino = 0
    else:
        sharpe = sortino = 0

    # Pattern distribution
    by_pat = Counter(t['pattern'] for t in trades)
    by_pat_pnl = defaultdict(float)
    for t in trades:
        by_pat_pnl[t['pattern']] += t['pnl']

    log(f"  {label}")
    log(f"    trades: {len(trades)}  wr: {wr*100:.1f}%  avg R: {avg_r:+.2f}  PF: {pf:.2f}")
    log(f"    P&L:    ${total_pnl:+,.0f}  expectancy: {expectancy_r:+.3f}R  max DD: {max_dd_pct*100:.1f}% (${max_dd_dollar:,.0f})")
    log(f"    Sharpe: {sharpe:+.2f}  Sortino: {sortino:+.2f}")
    log(f"    patterns:")
    for pat, n in by_pat.most_common():
        log(f"      {pat:12} {n:3}tr  ${by_pat_pnl[pat]:+,.0f}")

    return {
        'label': label,
        'trades': len(trades),
        'wr': wr,
        'avg_r': avg_r,
        'pf': pf,
        'pnl': total_pnl,
        'sharpe': sharpe,
        'sortino': sortino,
        'max_dd_pct': max_dd_pct,
        'max_dd_dollar': max_dd_dollar,
        'expectancy': expectancy_r,
        'by_pattern': dict(by_pat),
        'by_pattern_pnl': dict(by_pat_pnl),
        'trades_list': trades,
    }


# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    results = []

    for path, label in [
        ('data/binance_1m_7d.json', 'D1 (binance_1m_7d)'),
        ('data/1min_7days.json', 'D2 (1min_7days)'),
    ]:
        if not os.path.exists(path):
            log(f"SKIP {path} — not found")
            continue
        data = load_data(path)
        n_candles = sum(len(v) for v in data.values())
        log(f"loaded {path}: {len(data)} coins, {n_candles} {RESAMPLE_MINUTES}m candles")
        r = run_backtest(data, label=label)
        if r:
            results.append(r)

    # Walk-forward on D1: split into halves
    if results:
        log("\n══════════ WALK-FORWARD (D1 split into halves) ══════════")
        try:
            data = load_data('data/binance_1m_7d.json')
            # split by time: first half, second half
            all_ts = sorted({c['t'] for v in data.values() for c in v})
            if len(all_ts) >= 4:
                mid_t = all_ts[len(all_ts) // 2]
                first = {k: [c for c in v if c['t'] < mid_t] for k, v in data.items()}
                second = {k: [c for c in v if c['t'] >= mid_t] for k, v in data.items()}
                r1 = run_backtest(first, label='D1-first-half')
                r2 = run_backtest(second, label='D1-second-half')
                if r1:
                    results.append(r1)
                if r2:
                    results.append(r2)
        except Exception as e:
            log(f"walk-forward failed: {e}")

    # ─── Summary ───
    log("\n══════════ SUMMARY ══════════")
    for r in results:
        log(f"  {r['label']:30} trades={r['trades']:4}  wr={r['wr']*100:5.1f}%  "
            f"avgR={r['avg_r']:+5.2f}  PF={r['pf']:5.2f}  pnl=${r['pnl']:+,.0f}  "
            f"DD={r['max_dd_pct']*100:4.1f}%")

    if results:
        avg_pnl = mean(r['pnl'] for r in results)
        all_pos = all(r['pnl'] > 0 for r in results)
        log(f"\n  Average P&L across runs: ${avg_pnl:+,.0f}")
        log(f"  All scenarios profitable: {all_pos}")

    # Save JSON report
    out = {
        'timestamp': time.time(),
        'config': {
            'starting_capital': STARTING_CAPITAL,
            'risk_per_trade': RISK_PER_TRADE,
            'max_open_positions': MAX_OPEN_POSITIONS,
            'resample_minutes': RESAMPLE_MINUTES,
            'min_quality': MIN_QUALITY,
            'use_zone_filter': USE_ZONE_FILTER,
            'use_session_filter': USE_SESSION_FILTER,
            'maker_fee': MAKER_FEE,
            'slippage_pct': SLIPPAGE_PCT,
        },
        'results': [
            {k: v for k, v in r.items() if k != 'trades_list'}
            for r in results
        ],
    }
    with open('data/pro_backtest_results.json', 'w') as fp:
        json.dump(out, fp, indent=2, default=str)
    log("\nResults saved → data/pro_backtest_results.json")
    return results


if __name__ == '__main__':
    main()
