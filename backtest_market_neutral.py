"""
MARKET-NEUTRAL / LOW-VOL STRATEGY BACKTEST
════════════════════════════════════════════════════════════════════
Research-backed Category B strategies tested on D3-fresh out-of-sample:

  1. BUY-AND-HOLD equal-weighted basket (BTC/ETH/SOL/BNB/LINK)
     Expected: smooth upward drift, low turnover, zero fee drag
     Winning Sharpe in research: 1.0-1.5

  2. BTC GRID TRADING (long-only grid, buy dips, sell rips)
     Range: ±2.5% from starting price, 10 levels
     Expected: excels in chop, harvests 0.3%/trip
     Winning Sharpe in research: 1.5-2.0

  3. BTC/ETH PAIR ROTATION (long-only)
     Compute z-score of BTC/ETH ratio. When z > 1.5, rotate 100% to ETH
     (BTC expensive, ETH underpriced). When z < -1.5, rotate to BTC.
     When |z| < 0.5, hold cash.
     Mimics market-neutral without needing shorts.
     Expected: captures mean reversion in BTC-ETH ratio
     Winning Sharpe in research: 2.0-2.5 (with shorts), lower without

  4. BTC-ETH 50/50 HOLD (reference)
     Simplest, zero effort
     Expected: whatever the market does
"""
import json
import math
import time
from statistics import mean, pstdev, stdev
from collections import Counter

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


def compute_metrics(equity_curve, trades=None):
    """Compute Sharpe/Sortino/Calmar from hourly equity curve."""
    if len(equity_curve) < 2:
        return dict(sharpe=0, sortino=0, calmar=0, composite=0, max_dd=0,
                    total_return=0, final=STARTING_CAPITAL)

    # Hourly returns
    rets = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            rets.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        else:
            rets.append(0)

    if not rets:
        return dict(sharpe=0, sortino=0, calmar=0, composite=0, max_dd=0,
                    total_return=0, final=STARTING_CAPITAL)

    mean_r = mean(rets)
    std_r = pstdev(rets) if len(rets) > 1 else 0
    neg_rets = [r for r in rets if r < 0]
    neg_std = pstdev(neg_rets) if len(neg_rets) > 1 else 0

    # Annualize (8760 hours/year)
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
    # Annualize period return
    hours = len(equity_curve)
    annualized = total_return * (8760 / hours) if hours > 0 else 0
    calmar = annualized / max_dd if max_dd > 0 else 0

    composite = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar

    return dict(
        sharpe=sharpe, sortino=sortino, calmar=calmar, composite=composite,
        max_dd=max_dd, total_return=total_return, final=final,
        n_bars=len(equity_curve),
    )


# ═════════════════════════════════════════════════════════════
# STRATEGY 1: Equal-weighted basket buy-and-hold
# ═════════════════════════════════════════════════════════════
def strat_basket_hold(data, coins, equity_curve_only=False):
    """Buy equal weights of each coin at bar 50 (warmup), hold to end."""
    coins = [c for c in coins if c in data]
    if len(coins) < 2:
        return None

    # Find common time range
    all_times = set(c['t'] for c in data[coins[0]])
    for coin in coins[1:]:
        all_times = all_times & set(c['t'] for c in data[coin])
    sorted_times = sorted(all_times)
    if len(sorted_times) < 60:
        return None

    WARMUP = 50
    start_t = sorted_times[WARMUP]

    # Build price arrays aligned to these times
    def price_at(coin, t):
        for c in data[coin]:
            if c['t'] == t:
                return c['c']
        return None

    # Buy equal USD amounts at start_t
    per_coin = STARTING_CAPITAL / len(coins)
    entries = {}
    for coin in coins:
        p = price_at(coin, start_t)
        if p is None or p <= 0:
            continue
        entry_p = fill(p, 'buy')
        qty = per_coin / entry_p
        entries[coin] = {'qty': qty, 'entry': entry_p}

    equity_curve = []
    for t in sorted_times[WARMUP:]:
        total = 0
        for coin, pos in entries.items():
            p = price_at(coin, t)
            if p is None:
                continue
            total += pos['qty'] * p
        equity_curve.append(total)

    return compute_metrics(equity_curve)


# ═════════════════════════════════════════════════════════════
# STRATEGY 2: BTC grid trading (long-only)
# ═════════════════════════════════════════════════════════════
def strat_btc_grid(data, n_levels=10, range_pct=0.03, capital_pct=0.6):
    """Place a grid of buy/sell orders ±range_pct around starting BTC price."""
    if 'BTC' not in data or len(data['BTC']) < 60:
        return None

    btc = data['BTC']
    WARMUP = 50
    start_price = btc[WARMUP]['c']
    capital = STARTING_CAPITAL * capital_pct
    per_level = capital / n_levels

    # Grid levels: symmetric around start_price
    # Each level below = buy limit; each level above = sell limit
    # We START neutral: 50% cash, 50% BTC at start_price
    initial_btc_qty = (capital / 2) / fill(start_price, 'buy')
    cash_remaining = STARTING_CAPITAL - (initial_btc_qty * fill(start_price, 'buy'))

    # Track grid state: list of "filled" buy orders waiting to be sold higher
    # Each entry: {'price': buy_price, 'qty': qty}
    grid_holdings = []  # positions bought but not yet sold
    current_btc_qty = initial_btc_qty  # base position

    level_spacing = range_pct / n_levels

    # Precompute buy prices (below start) and sell triggers (above)
    buy_levels = [start_price * (1 - (i + 1) * level_spacing) for i in range(n_levels)]
    sell_levels = [start_price * (1 + (i + 1) * level_spacing) for i in range(n_levels)]
    buy_hit = [False] * n_levels

    equity_curve = []
    for i in range(WARMUP, len(btc)):
        bar = btc[i]
        low = bar['l']
        high = bar['h']
        close = bar['c']

        # Check buy fills (any buy_level touched by low)
        for j, bp_ in enumerate(buy_levels):
            if not buy_hit[j] and low <= bp_:
                # Buy per_level worth at bp_
                buy_qty = per_level / fill(bp_, 'buy')
                if cash_remaining >= per_level:
                    current_btc_qty += buy_qty
                    cash_remaining -= per_level
                    grid_holdings.append({'price': bp_, 'qty': buy_qty, 'level': j})
                    buy_hit[j] = True

        # Check sell fills (sell any holdings whose +1 level is hit)
        sold_idx = []
        for idx, pos in enumerate(grid_holdings):
            target = pos['price'] * (1 + 2 * level_spacing)  # sell at 2 levels higher
            if high >= target:
                sold_amount = pos['qty'] * fill(target, 'sell')
                cash_remaining += sold_amount
                current_btc_qty -= pos['qty']
                sold_idx.append(idx)
                buy_hit[pos['level']] = False  # reset so we can buy again

        for idx in reversed(sold_idx):
            grid_holdings.pop(idx)

        total_equity = cash_remaining + current_btc_qty * close
        equity_curve.append(total_equity)

    return compute_metrics(equity_curve)


# ═════════════════════════════════════════════════════════════
# STRATEGY 3: BTC/ETH pair rotation (long-only)
# ═════════════════════════════════════════════════════════════
def strat_pair_rotation(data, lookback=20, entry_z=1.5, exit_z=0.3,
                         capital_pct=0.8):
    """Rotate between BTC and ETH based on BTC/ETH price ratio z-score."""
    if 'BTC' not in data or 'ETH' not in data:
        return None

    btc = data['BTC']
    eth = data['ETH']

    # Align by timestamp
    btc_map = {c['t']: c for c in btc}
    eth_map = {c['t']: c for c in eth}
    common_ts = sorted(set(btc_map.keys()) & set(eth_map.keys()))
    if len(common_ts) < lookback + 10:
        return None

    capital = STARTING_CAPITAL * capital_pct
    cash_side = STARTING_CAPITAL - capital  # held as cash always

    position = None   # 'BTC', 'ETH', or None (in cash)
    qty = 0
    entry_price = 0

    equity_curve = []
    ratios = []

    for i, t in enumerate(common_ts):
        btc_c = btc_map[t]
        eth_c = eth_map[t]
        ratio = btc_c['c'] / eth_c['c'] if eth_c['c'] > 0 else 0
        ratios.append(ratio)

        if i < lookback:
            # Warmup — hold cash
            equity_curve.append(STARTING_CAPITAL)
            continue

        # Z-score of current ratio vs lookback window
        window = ratios[-lookback:]
        m = mean(window)
        s = stdev(window) if len(window) > 1 else 0
        z = (ratio - m) / s if s > 0 else 0

        # Decide action
        # z > entry_z → BTC expensive, switch to ETH
        # z < -entry_z → ETH expensive, switch to BTC
        # |z| < exit_z → go to cash
        target = None
        if z > entry_z:
            target = 'ETH'
        elif z < -entry_z:
            target = 'BTC'
        elif abs(z) < exit_z:
            target = None

        # Execute rotation if target changes
        if target != position:
            # Close current position
            if position == 'BTC' and qty > 0:
                cash_side += qty * fill(btc_c['c'], 'sell') + capital * 0  # just add to cash
                sell_proceeds = qty * fill(btc_c['c'], 'sell')
                capital_now = sell_proceeds
                capital = capital_now
                qty = 0
            elif position == 'ETH' and qty > 0:
                sell_proceeds = qty * fill(eth_c['c'], 'sell')
                capital = sell_proceeds
                qty = 0

            # Open new position
            if target == 'BTC':
                entry = fill(btc_c['c'], 'buy')
                qty = capital / entry
                entry_price = entry
                position = 'BTC'
            elif target == 'ETH':
                entry = fill(eth_c['c'], 'buy')
                qty = capital / entry
                entry_price = entry
                position = 'ETH'
            else:
                # cash
                position = None
                qty = 0

        # Compute current equity
        if position == 'BTC':
            total = cash_side + qty * btc_c['c']
        elif position == 'ETH':
            total = cash_side + qty * eth_c['c']
        else:
            total = cash_side + capital

        equity_curve.append(total)

    return compute_metrics(equity_curve)


# ═════════════════════════════════════════════════════════════
# STRATEGY 4: BTC-ETH 50/50 (reference)
# ═════════════════════════════════════════════════════════════
def strat_btc_eth_5050(data):
    return strat_basket_hold(data, ['BTC', 'ETH'])


# ═════════════════════════════════════════════════════════════
# STRATEGY 5: BTC alone buy-and-hold (reference)
# ═════════════════════════════════════════════════════════════
def strat_btc_hold(data):
    return strat_basket_hold(data, ['BTC'])


# ═════════════════════════════════════════════════════════════
# STRATEGY 6: Full 5-coin basket
# ═════════════════════════════════════════════════════════════
def strat_5coin_basket(data):
    return strat_basket_hold(data, ['BTC', 'ETH', 'SOL', 'BNB', 'LINK'])


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════
def main():
    datasets = {
        'D1':        load('data/binance_1m_7d.json'),
        'D2':        load('data/1min_7days.json'),
        'D3-majors': load('data/binance_1m_full.json'),
    }

    # For D3, filter to majors
    MAJORS = {"BTC","ETH","SOL","BNB","XRP","DOGE","ADA","TON","AVAX","LINK",
              "DOT","TRX","MATIC","SHIB","LTC","NEAR","ATOM","ICP","APT","ARB"}
    datasets['D3-majors'] = {k: v for k, v in datasets['D3-majors'].items() if k in MAJORS}

    strategies = {
        'BTC-hold':           lambda d: strat_btc_hold(d),
        'BTC-ETH 50/50':      lambda d: strat_btc_eth_5050(d),
        '5-coin basket':      lambda d: strat_5coin_basket(d),
        'BTC grid ±3%':       lambda d: strat_btc_grid(d, n_levels=10, range_pct=0.03),
        'BTC grid ±5%':       lambda d: strat_btc_grid(d, n_levels=10, range_pct=0.05),
        'BTC grid tight ±2%': lambda d: strat_btc_grid(d, n_levels=15, range_pct=0.02),
        'Pair rot z=1.5':     lambda d: strat_pair_rotation(d, lookback=20, entry_z=1.5),
        'Pair rot z=2.0':     lambda d: strat_pair_rotation(d, lookback=30, entry_z=2.0),
        'Pair rot z=1.0':     lambda d: strat_pair_rotation(d, lookback=20, entry_z=1.0),
    }

    results = {}
    print(f"{'strategy':20} {'dataset':12} {'return':>10} {'max_dd':>8} "
          f"{'sharpe':>8} {'sortino':>9} {'calmar':>8} {'composite':>10}")
    print("─" * 95)

    for strat_name, fn in strategies.items():
        results[strat_name] = {}
        for ds_name, ds in datasets.items():
            try:
                r = fn(ds)
                if r is None:
                    continue
            except Exception as e:
                print(f"  ERROR {strat_name} {ds_name}: {e}")
                continue
            results[strat_name][ds_name] = r
            print(f"{strat_name:20} {ds_name:12} {r['total_return']*100:+9.2f}% "
                  f"{r['max_dd']*100:7.2f}% {r['sharpe']:8.2f} {r['sortino']:9.2f} "
                  f"{r['calmar']:8.2f} {r['composite']:10.2f}")
        print()

    # Rank by D3-majors composite
    print("═══ RANK BY D3-MAJORS COMPOSITE (the live market) ═══")
    ranked = [(n, r.get('D3-majors', {})) for n, r in results.items() if 'D3-majors' in r]
    ranked.sort(key=lambda x: -x[1].get('composite', -999))
    for name, r in ranked:
        print(f"  {name:20} composite={r['composite']:7.2f}  "
              f"return={r['total_return']*100:+.2f}%  "
              f"dd={r['max_dd']*100:.2f}%  "
              f"sharpe={r['sharpe']:.2f}  sortino={r['sortino']:.2f}  calmar={r['calmar']:.2f}")

    # Rank by average composite across all datasets
    print()
    print("═══ RANK BY AVG COMPOSITE (robustness) ═══")
    avg_ranked = []
    for name, ds_res in results.items():
        if not ds_res:
            continue
        avg_comp = mean(r['composite'] for r in ds_res.values())
        min_comp = min(r['composite'] for r in ds_res.values())
        avg_ret = mean(r['total_return'] for r in ds_res.values())
        max_dd_across = max(r['max_dd'] for r in ds_res.values())
        avg_ranked.append((name, avg_comp, min_comp, avg_ret, max_dd_across))
    avg_ranked.sort(key=lambda x: -x[1])
    for name, avg_comp, min_comp, avg_ret, max_dd in avg_ranked:
        print(f"  {name:20} avg_comp={avg_comp:7.2f}  min_comp={min_comp:7.2f}  "
              f"avg_ret={avg_ret*100:+.2f}%  max_dd={max_dd*100:.2f}%")

    with open('data/market_neutral_results.json', 'w') as fp:
        json.dump(results, fp, indent=2, default=str)
    print(f"\nsaved → data/market_neutral_results.json")


if __name__ == '__main__':
    main()
