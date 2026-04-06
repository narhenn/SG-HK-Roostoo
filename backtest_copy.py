"""
Backtest copy trader on collected gap data.
Usage: python3 backtest_copy.py gap_data_*.csv
"""

import csv
import sys
from collections import defaultdict, deque

if len(sys.argv) < 2:
    print("Usage: python3 backtest_copy.py <gap_data.csv>")
    sys.exit(1)

# ── Parameters to sweep ──
CONFIGS = [
    {"name": "conservative", "gap_buy": 0.30, "gap_sell": -0.05, "stop": 0.005, "trail": 0.01, "min_ticks": 3, "max_hold": 600},
    {"name": "default",      "gap_buy": 0.20, "gap_sell": -0.10, "stop": 0.005, "trail": 0.01, "min_ticks": 2, "max_hold": 1800},
    {"name": "aggressive",   "gap_buy": 0.15, "gap_sell": -0.05, "stop": 0.003, "trail": 0.008, "min_ticks": 1, "max_hold": 900},
    {"name": "tight_trail",  "gap_buy": 0.20, "gap_sell": -0.05, "stop": 0.004, "trail": 0.005, "min_ticks": 2, "max_hold": 600},
    {"name": "wide",         "gap_buy": 0.25, "gap_sell": -0.15, "stop": 0.008, "trail": 0.015, "min_ticks": 2, "max_hold": 1800},
]

POSITION_SIZE = 100000
MAX_POSITIONS = 3
COOLDOWN_TICKS = 200  # ~10 min at 3s intervals
FEE_PCT = 0.001  # 0.1% round-trip fee (0.05% each way)

# ── Load data ──
print(f"Loading {sys.argv[1]}...")
data = []  # list of (ts, pair, roostoo_px, binance_px, gap_pct, vol)
with open(sys.argv[1]) as f:
    reader = csv.DictReader(f)
    for row in reader:
        data.append({
            'ts': row['ts'],
            'pair': row['pair'],
            'r_px': float(row['roostoo_px']),
            'b_px': float(row['binance_px']),
            'gap': float(row['gap_pct']),
            'vol': float(row['roostoo_vol']),
        })

# Group by timestamp
snapshots = []
current_ts = None
current_snap = {}
for d in data:
    if d['ts'] != current_ts:
        if current_snap:
            snapshots.append((current_ts, current_snap))
        current_ts = d['ts']
        current_snap = {}
    current_snap[d['pair']] = d
if current_snap:
    snapshots.append((current_ts, current_snap))

print(f"Loaded {len(data)} rows, {len(snapshots)} snapshots, {len(set(d['pair'] for d in data))} coins")
print(f"Time range: {snapshots[0][0]} -> {snapshots[-1][0]}")
print()

# ── Run backtest for each config ──
for cfg in CONFIGS:
    positions = {}     # pair -> {entry, qty, peak, tick, gap_at_entry}
    cooldowns = {}     # pair -> cooldown_until_tick
    gap_streak = {}
    total_pnl = 0
    trades = []
    tick = 0

    for ts, snap in snapshots:
        tick += 1

        # ── Check exits ──
        to_close = []
        for pair, pos in positions.items():
            if pair not in snap:
                continue
            d = snap[pair]
            current = d['r_px']
            entry = pos['entry']
            pnl_pct = (current - entry) / entry
            gap = d['gap']

            if current > pos['peak']:
                pos['peak'] = current

            reason = None
            if pnl_pct <= -cfg['stop']:
                reason = "STOP"
            elif pos['peak'] > 0 and current <= pos['peak'] * (1 - cfg['trail']):
                reason = "TRAIL"
            elif gap < cfg['gap_sell']:
                reason = "GAP_CLOSE"
            elif tick - pos['tick'] > cfg['max_hold'] // 3:  # convert seconds to ticks (~3s each)
                reason = "TIME"

            if reason:
                pnl = (current - entry) / entry * POSITION_SIZE
                fee = POSITION_SIZE * FEE_PCT
                pnl -= fee
                total_pnl += pnl
                trades.append({
                    'pair': pair, 'reason': reason, 'pnl': pnl,
                    'pnl_pct': pnl_pct, 'gap_entry': pos['gap_at_entry'],
                    'gap_exit': gap, 'held_ticks': tick - pos['tick'],
                    'ts': ts,
                })
                to_close.append(pair)
                cooldowns[pair] = tick + COOLDOWN_TICKS

        for p in to_close:
            positions.pop(p, None)

        # ── Check entries ──
        if len(positions) < MAX_POSITIONS:
            for pair, d in snap.items():
                if pair in positions or len(positions) >= MAX_POSITIONS:
                    continue
                if pair in cooldowns and tick < cooldowns[pair]:
                    continue

                gap = d['gap']
                if gap >= cfg['gap_buy']:
                    gap_streak[pair] = gap_streak.get(pair, 0) + 1
                else:
                    gap_streak[pair] = 0

                if gap_streak.get(pair, 0) >= cfg['min_ticks']:
                    positions[pair] = {
                        'entry': d['r_px'],
                        'qty': POSITION_SIZE / d['r_px'],
                        'peak': d['r_px'],
                        'tick': tick,
                        'gap_at_entry': gap,
                    }
                    gap_streak[pair] = 0

    # Close remaining positions at last price
    for pair, pos in positions.items():
        last_snap = snapshots[-1][1]
        if pair in last_snap:
            current = last_snap[pair]['r_px']
            pnl = (current - pos['entry']) / pos['entry'] * POSITION_SIZE
            fee = POSITION_SIZE * FEE_PCT
            pnl -= fee
            total_pnl += pnl
            trades.append({'pair': pair, 'reason': 'END', 'pnl': pnl,
                           'pnl_pct': (current-pos['entry'])/pos['entry'],
                           'gap_entry': pos['gap_at_entry'], 'gap_exit': 0,
                           'held_ticks': tick - pos['tick'], 'ts': snapshots[-1][0]})

    # ── Results ──
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    print(f"{'='*60}")
    print(f"CONFIG: {cfg['name']}")
    print(f"  Gap buy: {cfg['gap_buy']}% | Gap sell: {cfg['gap_sell']}% | Stop: {cfg['stop']*100}%")
    print(f"  Trail: {cfg['trail']*100}% | Min ticks: {cfg['min_ticks']} | Max hold: {cfg['max_hold']}s")
    print(f"  ---")
    print(f"  Trades: {len(trades)} | Win rate: {wr:.0f}%")
    print(f"  Total P&L: ${total_pnl:+,.0f}")
    print(f"  Avg win: ${avg_win:+,.0f} | Avg loss: ${avg_loss:+,.0f}")
    if trades:
        print(f"  Best: ${max(t['pnl'] for t in trades):+,.0f} | Worst: ${min(t['pnl'] for t in trades):+,.0f}")

        # By exit reason
        reasons = defaultdict(list)
        for t in trades:
            reasons[t['reason']].append(t['pnl'])
        print(f"  Exit reasons:")
        for r, pnls in sorted(reasons.items()):
            print(f"    {r}: {len(pnls)} trades, ${sum(pnls):+,.0f}")

        # Top coins
        coins = defaultdict(list)
        for t in trades:
            coins[t['pair']].append(t['pnl'])
        print(f"  Top coins:")
        for pair, pnls in sorted(coins.items(), key=lambda x: sum(x[1]), reverse=True)[:5]:
            print(f"    {pair}: {len(pnls)} trades, ${sum(pnls):+,.0f}")
    print()
