"""
Backtest SMC Trader on real Binance data.
Tests 6 periods: 2 bull, 2 bear, 2 sideways.
Uses the same 16-technique scoring from smc_trader.py.

Usage: python3 backtest_smc.py
"""

import requests
import time
import math
from datetime import datetime, timedelta

from smc_trader import (
    score_coin, find_swings, analyze_structure,
    HARD_STOP_PCT, TRAIL_STOP_PCT, PROFIT_TRAIL_PCT, MIN_SCORE,
    MAX_HOLD_CANDLES, FEE_PCT, BREADTH_MIN, MAX_POSITIONS,
    EXCLUDED,
)

POSITION_SIZE = 150000
STARTING_CAPITAL = 950000

# ALL 67 Roostoo coins mapped to Binance
COINS = {
    '1000CHEEMS/USD': '1000CHEEMSUSDT', 'AAVE/USD': 'AAVEUSDT', 'ADA/USD': 'ADAUSDT',
    'APT/USD': 'APTUSDT', 'ARB/USD': 'ARBUSDT', 'ASTER/USD': 'ASTERUSDT',
    'AVAX/USD': 'AVAXUSDT', 'AVNT/USD': 'AVNTUSDT', 'BIO/USD': 'BIOUSDT',
    'BMT/USD': 'BMTUSDT', 'BNB/USD': 'BNBUSDT', 'BONK/USD': 'BONKUSDT',
    'BTC/USD': 'BTCUSDT', 'CAKE/USD': 'CAKEUSDT', 'CFX/USD': 'CFXUSDT',
    'CRV/USD': 'CRVUSDT', 'DOGE/USD': 'DOGEUSDT', 'DOT/USD': 'DOTUSDT',
    'EDEN/USD': 'EDENUSDT', 'EIGEN/USD': 'EIGENUSDT', 'ENA/USD': 'ENAUSDT',
    'ETH/USD': 'ETHUSDT', 'FET/USD': 'FETUSDT', 'FIL/USD': 'FILUSDT',
    'FLOKI/USD': 'FLOKIUSDT', 'FORM/USD': 'FORMUSDT', 'HBAR/USD': 'HBARUSDT',
    'HEMI/USD': 'HEMIUSDT', 'ICP/USD': 'ICPUSDT', 'LINEA/USD': 'LINEAUSDT',
    'LINK/USD': 'LINKUSDT', 'LISTA/USD': 'LISTAUSDT', 'LTC/USD': 'LTCUSDT',
    'MIRA/USD': 'MIRAUSDT', 'NEAR/USD': 'NEARUSDT', 'OMNI/USD': 'OMNIUSDT',
    'ONDO/USD': 'ONDOUSDT', 'OPEN/USD': 'OPENUSDT', 'PAXG/USD': 'PAXGUSDT',
    'PENDLE/USD': 'PENDLEUSDT', 'PENGU/USD': 'PENGUUSDT', 'PEPE/USD': 'PEPEUSDT',
    'PLUME/USD': 'PLUMEUSDT', 'POL/USD': 'POLUSDT', 'PUMP/USD': 'PUMPUSDT',
    'S/USD': 'SUSDT', 'SEI/USD': 'SEIUSDT', 'SHIB/USD': 'SHIBUSDT',
    'SOL/USD': 'SOLUSDT', 'SOMI/USD': 'SOMIUSDT', 'STO/USD': 'STOUSDT',
    'SUI/USD': 'SUIUSDT', 'TAO/USD': 'TAOUSDT', 'TON/USD': 'TONUSDT',
    'TRUMP/USD': 'TRUMPUSDT', 'TRX/USD': 'TRXUSDT', 'TUT/USD': 'TUTUSDT',
    'UNI/USD': 'UNIUSDT', 'VIRTUAL/USD': 'VIRTUALUSDT', 'WIF/USD': 'WIFUSDT',
    'WLD/USD': 'WLDUSDT', 'WLFI/USD': 'WLFIUSDT', 'XLM/USD': 'XLMUSDT',
    'XPL/USD': 'XPLUSDT', 'XRP/USD': 'XRPUSDT', 'ZEC/USD': 'ZECUSDT',
    'ZEN/USD': 'ZENUSDT',
}


def fetch_candles(symbol, interval='1h', start_ts=None, end_ts=None, limit=500):
    """Fetch historical candles from Binance."""
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    if start_ts:
        params['startTime'] = int(start_ts * 1000)
    if end_ts:
        params['endTime'] = int(end_ts * 1000)
    try:
        r = requests.get('https://api.binance.com/api/v3/klines', params=params, timeout=10)
        data = r.json()
        if not isinstance(data, list):
            return []
        return [{'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                 'c': float(k[4]), 'v': float(k[5]), 't': int(k[0]) / 1000} for k in data]
    except:
        return []


def calc_breadth_from_candles(all_candles, idx):
    """Calculate breadth from candle data at a given index."""
    total = 0
    green = 0
    for pair, cl in all_candles.items():
        if idx >= len(cl) or idx < 1:
            continue
        total += 1
        if cl[idx]['c'] > cl[idx-1]['c']:
            green += 1
    return green / total if total > 0 else 0


def run_backtest(period_name, start_date, days=3):
    """Run backtest for a specific period."""
    start_ts = start_date.timestamp()
    end_ts = (start_date + timedelta(days=days)).timestamp()

    print(f"\n{'='*70}")
    print(f"BACKTEST: {period_name}")
    print(f"Period: {start_date.strftime('%Y-%m-%d')} to {(start_date + timedelta(days=days)).strftime('%Y-%m-%d')} ({days} days)")
    print(f"{'='*70}")

    # Fetch candles for all coins
    # We need extra history before start for analysis (100 candles = ~4 days buffer)
    buffer_ts = start_ts - 100 * 3600
    all_candles = {}
    print("Fetching data...", end='', flush=True)
    # Always fetch BTC first for regime detection
    btc_cl = fetch_candles('BTCUSDT', '1h', buffer_ts, end_ts, 500)
    if btc_cl and len(btc_cl) > 50:
        all_candles['BTC/USD'] = btc_cl
        print('B', end='', flush=True)
    time.sleep(0.1)

    for pair, symbol in COINS.items():
        if pair in EXCLUDED or pair == 'BTC/USD':  # BTC already fetched
            continue
        cl = fetch_candles(symbol, '1h', buffer_ts, end_ts, 500)
        if len(cl) > 50:
            all_candles[pair] = cl
            print('.', end='', flush=True)
        time.sleep(0.1)
    print(f" {len(all_candles)} coins loaded")

    if len(all_candles) < 10:
        print("ERROR: Not enough data")
        return None

    # Find the index where our test period starts
    first_pair = list(all_candles.keys())[0]
    start_idx = 0
    for i, c in enumerate(all_candles[first_pair]):
        if c['t'] >= start_ts:
            start_idx = i
            break

    if start_idx < 20:
        print(f"ERROR: Not enough buffer (start_idx={start_idx})")
        return None

    total_candles = len(all_candles[first_pair])
    test_candles = total_candles - start_idx
    print(f"Buffer candles: {start_idx} | Test candles: {test_candles} ({test_candles}h = {test_candles/24:.1f} days)")

    # Calculate BTC change for period classification
    btc_cl = all_candles.get('BNB/USD', list(all_candles.values())[0])  # Use BNB as proxy if no BTC
    if start_idx < len(btc_cl) and total_candles - 1 < len(btc_cl):
        period_change = (btc_cl[-1]['c'] - btc_cl[start_idx]['c']) / btc_cl[start_idx]['c'] * 100
    else:
        period_change = 0
    print(f"Market change over period: {period_change:+.2f}%")

    # Simulate trading
    positions = {}
    trades = []
    cash = STARTING_CAPITAL
    peak_equity = cash
    max_drawdown = 0
    skipped_breadth = 0
    signals_seen = 0

    for idx in range(start_idx, total_candles):
        # Calculate breadth
        breadth = calc_breadth_from_candles(all_candles, idx)

        # Check exits
        to_close = []
        for pair, pos in positions.items():
            cl = all_candles.get(pair, [])
            if idx >= len(cl):
                continue

            px = cl[idx]['c']
            if px > pos['peak']:
                pos['peak'] = px

            pnl_pct = (px - pos['entry']) / pos['entry']
            pos['candles_held'] += 1

            reason = None

            # Hard stop
            if pnl_pct <= -HARD_STOP_PCT:
                reason = 'STOP'

            # Profit trail
            elif pnl_pct > 0.015 and px <= pos['peak'] * (1 - PROFIT_TRAIL_PCT):
                reason = 'PROFIT_TRAIL'

            # Dynamic trail
            elif pnl_pct > 0.003:
                new_stop = pos['peak'] * (1 - TRAIL_STOP_PCT)
                if new_stop > pos.get('stop', 0):
                    pos['stop'] = new_stop
                if pos['stop'] > 0 and px <= pos['stop']:
                    reason = 'TRAIL'

            # Time stop
            elif pos['candles_held'] >= MAX_HOLD_CANDLES:
                reason = 'TIME'

            if reason:
                pnl_usd = (px - pos['entry']) * pos['qty']
                fees = pos['entry'] * pos['qty'] * FEE_PCT + px * pos['qty'] * FEE_PCT
                pnl_usd -= fees
                cash += pos['qty'] * px - fees
                trades.append({
                    'pair': pair, 'pnl': pnl_usd, 'pnl_pct': pnl_pct * 100,
                    'reason': reason, 'score': pos['score'],
                    'signals': pos['signals'], 'candles': pos['candles_held'],
                })
                to_close.append(pair)

        for p in to_close:
            positions.pop(p, None)

        # Track drawdown
        equity = cash + sum(
            all_candles[p][idx]['c'] * pos['qty']
            for p, pos in positions.items()
            if idx < len(all_candles.get(p, []))
        )
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100
        if dd > max_drawdown:
            max_drawdown = dd

        # ── REGIME GATE: EMA50 slope + ADX on BTC ──
        btc_cl = all_candles.get('BTC/USD', all_candles.get('BNB/USD', list(all_candles.values())[0]))

        if idx >= 60:
            from smc_trader import calc_ema, calc_adx
            closes = [c['c'] for c in btc_cl[:idx+1]]
            ema50_now = calc_ema(closes, 50)
            ema50_prev = calc_ema(closes[:-5], 50)
            slope_pct = (ema50_now - ema50_prev) / ema50_prev * 100 if ema50_prev > 0 else 0
            adx = calc_adx(btc_cl[:idx+1], 14)

            if slope_pct > 0.1 and adx >= 20:
                regime = 'BULL_TREND'
            elif slope_pct < -0.1 and adx >= 20:
                regime = 'BEAR_TREND'
            else:
                regime = 'CHOP'

            if regime != 'BULL_TREND':
                skipped_breadth += 1
                continue

        if breadth < 0.25:
            skipped_breadth += 1
            continue

        if len(positions) >= MAX_POSITIONS:
            continue

        # Score all coins
        candidates = []
        for pair, cl in all_candles.items():
            if pair in positions or pair in EXCLUDED:
                continue
            if idx >= len(cl) or idx < 20:
                continue

            history = cl[:idx+1]
            if len(history) < 20:
                continue

            score, signals, details = score_coin(pair, history)
            signals_seen += 1

            if score >= MIN_SCORE:
                candidates.append((score, pair, signals))

        candidates.sort(key=lambda x: -x[0])

        for score, pair, signals in candidates[:1]:
            if len(positions) >= MAX_POSITIONS:
                break

            cl = all_candles[pair]
            ask = cl[idx]['c']
            actual_size = min(POSITION_SIZE, cash * 0.25)
            if actual_size < 10000 or cash < actual_size:
                break

            qty = actual_size / ask
            cash -= qty * ask

            positions[pair] = {
                'entry': ask, 'qty': qty, 'peak': ask,
                'stop': ask * (1 - HARD_STOP_PCT), 'score': score,
                'signals': signals, 'candles_held': 0,
            }

    # Close remaining positions at end
    for pair, pos in positions.items():
        cl = all_candles.get(pair, [])
        if not cl:
            continue
        px = cl[-1]['c']
        pnl_usd = (px - pos['entry']) * pos['qty']
        fees = pos['entry'] * pos['qty'] * FEE_PCT + px * pos['qty'] * FEE_PCT
        pnl_usd -= fees
        cash += pos['qty'] * px - fees
        trades.append({
            'pair': pair, 'pnl': pnl_usd, 'pnl_pct': (px - pos['entry']) / pos['entry'] * 100,
            'reason': 'END', 'score': pos['score'],
            'signals': pos['signals'], 'candles': pos['candles_held'],
        })

    # Results
    total_pnl = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['pnl'] for t in wins) / sum(t['pnl'] for t in losses)) if losses and sum(t['pnl'] for t in losses) != 0 else 999

    print(f"\n--- RESULTS ---")
    print(f"Trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {win_rate:.0f}%")
    print(f"Total P&L: ${total_pnl:+,.0f}")
    print(f"Avg win: ${avg_win:+,.0f} | Avg loss: ${avg_loss:+,.0f}")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Max drawdown: {max_drawdown:.2f}%")
    print(f"Breadth skipped: {skipped_breadth}/{test_candles} candles ({skipped_breadth/test_candles*100:.0f}%)")
    print(f"Return on capital: {total_pnl/STARTING_CAPITAL*100:+.2f}%")

    if trades:
        print(f"\nBest trade: ${max(t['pnl'] for t in trades):+,.0f}")
        print(f"Worst trade: ${min(t['pnl'] for t in trades):+,.0f}")

        # By exit reason
        from collections import defaultdict
        reasons = defaultdict(list)
        for t in trades:
            reasons[t['reason']].append(t['pnl'])
        print(f"\nExit reasons:")
        for r, pnls in sorted(reasons.items()):
            print(f"  {r}: {len(pnls)} trades, ${sum(pnls):+,.0f}, avg ${sum(pnls)/len(pnls):+,.0f}")

        # Top coins
        coins = defaultdict(list)
        for t in trades:
            coins[t['pair']].append(t['pnl'])
        print(f"\nTop coins:")
        for pair, pnls in sorted(coins.items(), key=lambda x: sum(x[1]), reverse=True)[:5]:
            print(f"  {pair}: {len(pnls)} trades, ${sum(pnls):+,.0f}")

        # Average score of winners vs losers
        win_scores = [t['score'] for t in wins] if wins else [0]
        loss_scores = [t['score'] for t in losses] if losses else [0]
        print(f"\nAvg score — Winners: {sum(win_scores)/len(win_scores):.1f} | Losers: {sum(loss_scores)/len(loss_scores):.1f}")

        # Most common winning signals
        from collections import Counter
        win_signals = Counter()
        loss_signals = Counter()
        for t in wins:
            for s in t['signals']:
                win_signals[s] += 1
        for t in losses:
            for s in t['signals']:
                loss_signals[s] += 1
        print(f"\nTop winning signals:")
        for sig, count in win_signals.most_common(8):
            print(f"  {sig}: {count} wins")
        print(f"Top losing signals:")
        for sig, count in loss_signals.most_common(5):
            print(f"  {sig}: {count} losses")

    return {
        'name': period_name,
        'trades': len(trades),
        'win_rate': win_rate,
        'pnl': total_pnl,
        'max_dd': max_drawdown,
        'profit_factor': profit_factor,
        'market_change': period_change,
    }


def main():
    print("=" * 70)
    print("SMC TRADER BACKTEST — 6 PERIODS (Bull, Bear, Sideways)")
    print(f"Capital: ${STARTING_CAPITAL:,} | Position: ${POSITION_SIZE:,}")
    print(f"Min score: {MIN_SCORE} | Stop: {HARD_STOP_PCT*100}% | Trail: {TRAIL_STOP_PCT*100}%")
    print(f"Breadth min: {BREADTH_MIN*100}% | Max pos: {MAX_POSITIONS}")
    print("=" * 70)

    # 6 test periods — 3 days each
    # Pick diverse market conditions from recent months
    periods = [
        # BULL periods (market up 5%+)
        ("BULL 1 — Jan 2025 rally", datetime(2025, 1, 15)),
        ("BULL 2 — Nov 2024 Trump pump", datetime(2024, 11, 10)),

        # BEAR periods (market down 5%+)
        ("BEAR 1 — Feb 2025 crash", datetime(2025, 2, 24)),
        ("BEAR 2 — Apr 2025 tariff crash", datetime(2025, 4, 7)),

        # SIDEWAYS periods (market flat ±2%)
        ("SIDEWAYS 1 — Mar 2025 chop", datetime(2025, 3, 15)),
        ("SIDEWAYS 2 — Dec 2024 range", datetime(2024, 12, 20)),
    ]

    results = []
    for name, start in periods:
        r = run_backtest(name, start, days=3)
        if r:
            results.append(r)
        time.sleep(1)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY — ALL 6 PERIODS")
    print("=" * 70)
    print(f"{'Period':<35} {'Trades':>6} {'WR%':>5} {'P&L':>10} {'PF':>6} {'DD%':>6} {'Mkt':>7}")
    print("-" * 70)

    total_trades = 0
    total_pnl = 0
    total_wins = 0

    for r in results:
        print(f"{r['name']:<35} {r['trades']:>6} {r['win_rate']:>4.0f}% ${r['pnl']:>+9,.0f} {r['profit_factor']:>5.1f}x {r['max_dd']:>5.1f}% {r['market_change']:>+6.1f}%")
        total_trades += r['trades']
        total_pnl += r['pnl']

    print("-" * 70)
    print(f"{'TOTAL':<35} {total_trades:>6}       ${total_pnl:>+9,.0f}")
    print(f"\nTotal return: {total_pnl/STARTING_CAPITAL*100:+.2f}% on ${STARTING_CAPITAL:,}")

    if total_pnl > 0:
        print("\nVERDICT: PROFITABLE across mixed conditions")
    else:
        print("\nVERDICT: NOT PROFITABLE — needs parameter tuning")


if __name__ == '__main__':
    main()
