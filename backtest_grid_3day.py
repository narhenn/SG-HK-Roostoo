"""
3-DAY GRID BACKTEST — matches user's remaining competition window.
════════════════════════════════════════════════════════════════════
Tests:
  - BTC grid ±3% over last 72 hours (not 168)
  - BTC grid ±2% (tighter chop capture)
  - BTC grid ±5% (wider range protection)
  - Multi-coin grid: BTC + ETH simultaneously
  - Multi-coin grid: BTC + ETH + SOL
  - Grid + passive basket hybrid (40% grid, 40% hold, 20% cash)
  - 24-hour window (stress test short window)

Also computes: **variance** of composite across different 3-day subwindows
of the full 7-day data. This tells us how lucky we'd need to get.
"""
import json
import math
from statistics import mean, pstdev, stdev

import backtest_pro_v2 as bp


STARTING_CAPITAL = 1_000_000.0
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
STABLES = {'USD1','RLUSD','U','XUSD','USDC','USDT','TUSD','BUSD','FDUSD',
           'USDP','DAI','EUR','EURI','GBPT'}


def load(path):
    d = bp.load_data(path, 60)
    return {k: v for k, v in d.items() if k not in STABLES}


def fill(price, side):
    if side == 'buy':
        return price * (1 + SLIPPAGE) * (1 + TAKER_FEE)
    return price * (1 - SLIPPAGE) * (1 - TAKER_FEE)


def trim_to_window(data, hours):
    """Return data restricted to the last N hours of 1H candles."""
    out = {}
    for coin, candles in data.items():
        if len(candles) >= hours:
            out[coin] = candles[-hours:]
        else:
            out[coin] = candles
    return out


def trim_to_sub(data, start_idx, hours):
    """Return data slice [start_idx, start_idx+hours)."""
    out = {}
    for coin, candles in data.items():
        end = start_idx + hours
        if end <= len(candles) and start_idx >= 0:
            out[coin] = candles[start_idx:end]
        else:
            out[coin] = candles
    return out


def compute_metrics(equity_curve):
    if len(equity_curve) < 2:
        return dict(sharpe=0, sortino=0, calmar=0, composite=0, max_dd=0,
                    total_return=0, final=STARTING_CAPITAL, n_bars=0)

    rets = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            rets.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        else:
            rets.append(0)

    if not rets:
        return dict(sharpe=0, sortino=0, calmar=0, composite=0, max_dd=0,
                    total_return=0, final=STARTING_CAPITAL, n_bars=0)

    mean_r = mean(rets)
    std_r = pstdev(rets) if len(rets) > 1 else 0
    neg_rets = [r for r in rets if r < 0]
    neg_std = pstdev(neg_rets) if len(neg_rets) > 1 else 0

    sharpe = (mean_r / std_r) * math.sqrt(8760) if std_r > 0 else 0
    sortino = (mean_r / neg_std) * math.sqrt(8760) if neg_std > 0 else 0

    peak = equity_curve[0]
    max_dd = 0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    final = equity_curve[-1]
    total_return = (final - STARTING_CAPITAL) / STARTING_CAPITAL
    hours = len(equity_curve)
    annualized = total_return * (8760 / hours) if hours > 0 else 0
    calmar = annualized / max_dd if max_dd > 0 else 0

    composite = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar

    return dict(
        sharpe=sharpe, sortino=sortino, calmar=calmar, composite=composite,
        max_dd=max_dd, total_return=total_return, final=final,
        n_bars=len(equity_curve),
    )


def single_grid(candles, range_pct, n_levels, capital, start_price):
    """Run a single coin grid on candles. Returns hourly equity list."""
    if not candles:
        return []
    cash = capital / 2
    coin_qty = (capital / 2) / fill(start_price, 'buy')

    level_spacing = range_pct / n_levels
    per_level = capital / n_levels / 2  # half for buy orders
    buy_levels = [start_price * (1 - (i + 1) * level_spacing) for i in range(n_levels)]
    buy_hit = [False] * n_levels
    grid_holdings = []

    equity_curve = []
    for bar in candles:
        low = bar['l']
        high = bar['h']
        close = bar['c']

        # Buy fills
        for j, bp_ in enumerate(buy_levels):
            if not buy_hit[j] and low <= bp_ and cash >= per_level:
                qty = per_level / fill(bp_, 'buy')
                coin_qty += qty
                cash -= per_level
                grid_holdings.append({'price': bp_, 'qty': qty, 'level': j})
                buy_hit[j] = True

        # Sell fills
        sold_idx = []
        for idx, pos in enumerate(grid_holdings):
            target = pos['price'] * (1 + 2 * level_spacing)
            if high >= target:
                sold = pos['qty'] * fill(target, 'sell')
                cash += sold
                coin_qty -= pos['qty']
                sold_idx.append(idx)
                buy_hit[pos['level']] = False
        for idx in reversed(sold_idx):
            grid_holdings.pop(idx)

        equity_curve.append(cash + coin_qty * close)

    return equity_curve


def strat_btc_grid(data, range_pct=0.03, n_levels=10, capital_pct=0.6):
    btc = data.get('BTC', [])
    if len(btc) < 20:
        return None
    capital = STARTING_CAPITAL * capital_pct
    cash_reserve = STARTING_CAPITAL - capital
    start = btc[0]['c']
    curve = single_grid(btc, range_pct, n_levels, capital, start)
    # Add cash reserve to each equity point
    curve = [c + cash_reserve for c in curve]
    return compute_metrics(curve)


def strat_multi_grid(data, coins, range_pct=0.03, n_levels=10, capital_pct=0.6):
    """Run simultaneous grids on multiple coins, each with 1/n of allocated capital."""
    valid = [c for c in coins if c in data and len(data[c]) >= 20]
    if not valid:
        return None

    capital_per_coin = (STARTING_CAPITAL * capital_pct) / len(valid)
    cash_reserve = STARTING_CAPITAL - STARTING_CAPITAL * capital_pct

    # Each grid returns its own equity curve
    curves = []
    for coin in valid:
        start = data[coin][0]['c']
        curve = single_grid(data[coin], range_pct, n_levels, capital_per_coin, start)
        curves.append(curve)

    # Sum equity curves (time-aligned)
    max_len = max(len(c) for c in curves)
    total_curve = []
    for i in range(max_len):
        total = cash_reserve
        for curve in curves:
            if i < len(curve):
                total += curve[i]
            else:
                total += curve[-1] if curve else 0
        total_curve.append(total)

    return compute_metrics(total_curve)


def strat_grid_plus_hold(data, grid_pct=0.40, hold_pct=0.40):
    """Grid on BTC for grid_pct, buy-and-hold BTC+ETH 50/50 for hold_pct, rest cash."""
    if 'BTC' not in data or 'ETH' not in data:
        return None
    btc = data['BTC']
    eth = data['ETH']
    if len(btc) < 20 or len(eth) < 20:
        return None

    # Align by timestamp
    btc_map = {c['t']: c for c in btc}
    eth_map = {c['t']: c for c in eth}
    common = sorted(set(btc_map.keys()) & set(eth_map.keys()))
    if len(common) < 20:
        return None

    # Build hold positions
    start_btc = btc_map[common[0]]['c']
    start_eth = eth_map[common[0]]['c']
    hold_capital = STARTING_CAPITAL * hold_pct
    hold_btc_qty = (hold_capital / 2) / fill(start_btc, 'buy')
    hold_eth_qty = (hold_capital / 2) / fill(start_eth, 'buy')

    # Grid state on BTC only
    grid_capital = STARTING_CAPITAL * grid_pct
    cash_reserve = STARTING_CAPITAL * (1 - grid_pct - hold_pct)

    cash = grid_capital / 2
    grid_btc_qty = (grid_capital / 2) / fill(start_btc, 'buy')

    range_pct = 0.03
    n_levels = 10
    level_spacing = range_pct / n_levels
    per_level = grid_capital / n_levels / 2
    buy_levels = [start_btc * (1 - (i + 1) * level_spacing) for i in range(n_levels)]
    buy_hit = [False] * n_levels
    grid_holdings = []

    equity_curve = []
    for t in common:
        bar = btc_map[t]
        low = bar['l']
        high = bar['h']
        close = bar['c']
        eth_close = eth_map[t]['c']

        # Buy fills
        for j, bp_ in enumerate(buy_levels):
            if not buy_hit[j] and low <= bp_ and cash >= per_level:
                qty = per_level / fill(bp_, 'buy')
                grid_btc_qty += qty
                cash -= per_level
                grid_holdings.append({'price': bp_, 'qty': qty, 'level': j})
                buy_hit[j] = True

        # Sell fills
        sold_idx = []
        for idx, pos in enumerate(grid_holdings):
            target = pos['price'] * (1 + 2 * level_spacing)
            if high >= target:
                sold = pos['qty'] * fill(target, 'sell')
                cash += sold
                grid_btc_qty -= pos['qty']
                sold_idx.append(idx)
                buy_hit[pos['level']] = False
        for idx in reversed(sold_idx):
            grid_holdings.pop(idx)

        # Total equity
        total = cash_reserve + cash + grid_btc_qty * close
        total += hold_btc_qty * close
        total += hold_eth_qty * eth_close
        equity_curve.append(total)

    return compute_metrics(equity_curve)


def main():
    d3_full = load('data/binance_1m_full.json')
    MAJORS = {"BTC","ETH","SOL","BNB","XRP","DOGE","ADA","TON","AVAX","LINK",
              "DOT","TRX","MATIC","SHIB","LTC","NEAR","ATOM","ICP","APT","ARB"}
    d3_majors = {k: v for k, v in d3_full.items() if k in MAJORS}

    # Test 1: last 72 hours (3 days) — the actual remaining window
    print("═══ PRIMARY TEST: LAST 72 HOURS (3 days — matches user's remaining time) ═══")
    d3_72h = trim_to_window(d3_majors, 72)
    if 'BTC' in d3_72h:
        btc_first = d3_72h['BTC'][0]['c']
        btc_last = d3_72h['BTC'][-1]['c']
        print(f"BTC in this window: ${btc_first:,.2f} → ${btc_last:,.2f} ({(btc_last/btc_first-1)*100:+.2f}%)")
        btc_high = max(c['h'] for c in d3_72h['BTC'])
        btc_low = min(c['l'] for c in d3_72h['BTC'])
        print(f"BTC range: ${btc_low:,.2f}-${btc_high:,.2f} ({(btc_high/btc_low-1)*100:.2f}% swing)")
    print()

    print(f"{'strategy':30} {'return':>9} {'max_dd':>8} {'sharpe':>8} {'sortino':>9} "
          f"{'calmar':>8} {'composite':>10}")
    print("─" * 95)

    strats_72h = [
        ('BTC grid ±3% 10 levels', lambda d: strat_btc_grid(d, 0.03, 10, 0.6)),
        ('BTC grid ±2% 15 levels', lambda d: strat_btc_grid(d, 0.02, 15, 0.6)),
        ('BTC grid ±5% 10 levels', lambda d: strat_btc_grid(d, 0.05, 10, 0.6)),
        ('BTC grid ±3% (cap 40%)', lambda d: strat_btc_grid(d, 0.03, 10, 0.4)),
        ('BTC grid ±3% (cap 80%)', lambda d: strat_btc_grid(d, 0.03, 10, 0.8)),
        ('BTC+ETH multi-grid',     lambda d: strat_multi_grid(d, ['BTC','ETH'], 0.03, 10, 0.6)),
        ('BTC+ETH+SOL multi-grid', lambda d: strat_multi_grid(d, ['BTC','ETH','SOL'], 0.03, 10, 0.6)),
        ('BTC+ETH+BNB multi-grid', lambda d: strat_multi_grid(d, ['BTC','ETH','BNB'], 0.03, 10, 0.6)),
        ('Grid+hold hybrid 40/40', lambda d: strat_grid_plus_hold(d, 0.4, 0.4)),
        ('Grid+hold hybrid 30/50', lambda d: strat_grid_plus_hold(d, 0.3, 0.5)),
    ]

    results_72h = {}
    for name, fn in strats_72h:
        try:
            r = fn(d3_72h)
            if r is None:
                continue
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            continue
        results_72h[name] = r
        print(f"{name:30} {r['total_return']*100:+8.3f}% {r['max_dd']*100:7.3f}% "
              f"{r['sharpe']:8.2f} {r['sortino']:9.2f} {r['calmar']:8.2f} {r['composite']:10.2f}")

    # Test 2: ROBUSTNESS — run same strategies on every 72h window in the 7-day data
    print()
    print("═══ ROBUSTNESS: every 72h window through the 7-day data ═══")
    # Extract BTC candles length to know windowing
    if 'BTC' in d3_majors and len(d3_majors['BTC']) >= 100:
        total_hours = len(d3_majors['BTC'])
        window_starts = list(range(0, total_hours - 72, 12))  # every 12h
        print(f"Testing {len(window_starts)} sub-windows of 72h each...")
        print()

        for strat_name, fn in strats_72h[:6]:  # only test the simpler ones for speed
            composites = []
            returns = []
            dds = []
            for ws in window_starts:
                window = trim_to_sub(d3_majors, ws, 72)
                r = fn(window)
                if r:
                    composites.append(r['composite'])
                    returns.append(r['total_return'])
                    dds.append(r['max_dd'])
            if composites:
                avg_comp = mean(composites)
                std_comp = stdev(composites) if len(composites) > 1 else 0
                min_comp = min(composites)
                max_comp = max(composites)
                avg_ret = mean(returns)
                max_dd_across = max(dds)
                positive_count = sum(1 for c in composites if c > 0)
                print(f"  {strat_name:30} avg_comp={avg_comp:7.2f} std={std_comp:6.2f} "
                      f"range=[{min_comp:6.2f},{max_comp:7.2f}] "
                      f"avg_ret={avg_ret*100:+6.2f}% pos/{len(composites)}={positive_count}")

    # Test 3: Variance across sub-windows — how lucky do we need to be?
    print()
    print("═══ WORST-CASE SCENARIO: worst 72h window for BTC grid ±3% ═══")
    worst_comp = 999
    worst_window = None
    for ws in range(0, len(d3_majors['BTC']) - 72, 6):
        window = trim_to_sub(d3_majors, ws, 72)
        r = strat_btc_grid(window, 0.03, 10, 0.6)
        if r and r['composite'] < worst_comp:
            worst_comp = r['composite']
            worst_window = (ws, r)

    if worst_window:
        ws, r = worst_window
        btc_window = d3_majors['BTC'][ws:ws + 72]
        bstart = btc_window[0]['c']
        bend = btc_window[-1]['c']
        bmove = (bend - bstart) / bstart * 100
        print(f"  Worst window: hours {ws}-{ws+72}, BTC {bmove:+.2f}%")
        print(f"  Return: {r['total_return']*100:+.3f}%")
        print(f"  Max DD: {r['max_dd']*100:.3f}%")
        print(f"  Composite: {r['composite']:.2f}")

    with open('data/grid_3day_results.json', 'w') as fp:
        json.dump({
            'primary_72h': results_72h,
        }, fp, indent=2, default=str)
    print(f"\nsaved → data/grid_3day_results.json")


if __name__ == '__main__':
    main()
