"""
Backtester — Simulates the full 7-layer pipeline on historical data.
Outputs: total return, Sharpe, Sortino, Calmar, max drawdown, win rate, equity curve.

Uses the same Layer 1-4 code that the live bot uses.
Layer 5 (XGBoost) and Layer 6 (Kelly) use defaults since stubs aren't trained yet.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    MAKER_FEE, ATR_STOP_MULTIPLIER, ATR_PERIOD,
    DONCHIAN_UPPER_PERIOD, EMA_SLOW, FLAT_THRESHOLD,
)
from strategy.regime import detect_regime, calculate_atr
from strategy.signals import generate_signal
from strategy.reversal_blocker import check_reversal_block, reset_cooldown
from strategy.timeframe import check_timeframe


class BacktestResult:
    """Holds backtest results and prints summary."""

    def __init__(self, trades, equity_curve, initial_capital):
        self.trades = trades
        self.equity_curve = equity_curve
        self.initial_capital = initial_capital

    @property
    def final_equity(self):
        return self.equity_curve[-1] if self.equity_curve else self.initial_capital

    @property
    def total_return_pct(self):
        return (self.final_equity - self.initial_capital) / self.initial_capital * 100

    @property
    def num_trades(self):
        return len(self.trades)

    @property
    def winners(self):
        return [t for t in self.trades if t['pnl_pct'] > 0]

    @property
    def losers(self):
        return [t for t in self.trades if t['pnl_pct'] <= 0]

    @property
    def win_rate(self):
        if not self.trades:
            return 0.0
        return len(self.winners) / len(self.trades)

    @property
    def avg_win(self):
        if not self.winners:
            return 0.0
        return np.mean([t['pnl_pct'] for t in self.winners])

    @property
    def avg_loss(self):
        if not self.losers:
            return 0.0
        return np.mean([t['pnl_pct'] for t in self.losers])

    @property
    def max_drawdown(self):
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def daily_returns(self):
        """Calculate daily returns from hourly equity curve."""
        if len(self.equity_curve) < 24:
            return []
        # Sample every 24 hours
        daily_eq = self.equity_curve[::24]
        returns = []
        for i in range(1, len(daily_eq)):
            r = (daily_eq[i] - daily_eq[i-1]) / daily_eq[i-1]
            returns.append(r)
        return returns

    @property
    def sharpe_ratio(self):
        dr = self.daily_returns
        if not dr or np.std(dr) == 0:
            return 0.0
        return (np.mean(dr) / np.std(dr)) * np.sqrt(365)

    @property
    def sortino_ratio(self):
        dr = self.daily_returns
        if not dr:
            return 0.0
        downside = [r for r in dr if r < 0]
        if not downside or np.std(downside) == 0:
            return float('inf') if np.mean(dr) > 0 else 0.0
        return (np.mean(dr) / np.std(downside)) * np.sqrt(365)

    @property
    def calmar_ratio(self):
        if self.max_drawdown == 0:
            return float('inf') if self.total_return_pct > 0 else 0.0
        # Annualize the return
        days = len(self.equity_curve) / 24
        annual_return = (self.final_equity / self.initial_capital) ** (365 / max(days, 1)) - 1
        return annual_return / self.max_drawdown

    @property
    def composite_score(self):
        """0.4 * Sortino + 0.3 * Sharpe + 0.3 * Calmar"""
        s = min(self.sortino_ratio, 10)  # Cap infinities
        sh = self.sharpe_ratio
        c = min(self.calmar_ratio, 10)
        return 0.4 * s + 0.3 * sh + 0.3 * c

    def print_summary(self):
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"Initial Capital:    ${self.initial_capital:,.2f}")
        print(f"Final Equity:       ${self.final_equity:,.2f}")
        print(f"Total Return:       {self.total_return_pct:+.2f}%")
        print(f"Max Drawdown:       {self.max_drawdown:.2%}")
        print(f"")
        print(f"Total Trades:       {self.num_trades}")
        print(f"Winners:            {len(self.winners)}")
        print(f"Losers:             {len(self.losers)}")
        print(f"Win Rate:           {self.win_rate:.1%}")
        print(f"Avg Win:            {self.avg_win:+.2%}")
        print(f"Avg Loss:           {self.avg_loss:+.2%}")
        print(f"")
        print(f"Sharpe Ratio:       {self.sharpe_ratio:.2f}")
        print(f"Sortino Ratio:      {self.sortino_ratio:.2f}")
        print(f"Calmar Ratio:       {self.calmar_ratio:.2f}")
        print(f"Composite Score:    {self.composite_score:.2f}")
        print(f"  (0.4*Sortino + 0.3*Sharpe + 0.3*Calmar)")
        print("=" * 60)

        # Pass/Fail check
        print("\nDEPLOY GATE:")
        checks = [
            ("Sharpe > 1.0", self.sharpe_ratio > 1.0),
            ("Max DD < 10%", self.max_drawdown < 0.10),
            ("Win Rate > 50%", self.win_rate > 0.50),
            ("Net Profit > 0", self.total_return_pct > 0),
        ]
        all_pass = True
        for name, passed in checks:
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {name}")
            if not passed:
                all_pass = False

        if all_pass:
            print("\n  >>> ALL CHECKS PASSED — READY TO DEPLOY <<<")
        else:
            print("\n  >>> SOME CHECKS FAILED — TUNE PARAMETERS <<<")

    def plot_equity_curve(self, save_path="backtest/equity_curve.png"):
        """Plot and save equity curve."""
        fig, axes = plt.subplots(3, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [3, 1, 1]})

        # Equity curve
        axes[0].plot(self.equity_curve, color='#2196F3', linewidth=1.5)
        axes[0].axhline(y=self.initial_capital, color='gray', linestyle='--', alpha=0.5)
        axes[0].set_title(f'Equity Curve | Return: {self.total_return_pct:+.2f}% | Sharpe: {self.sharpe_ratio:.2f} | Max DD: {self.max_drawdown:.1%}')
        axes[0].set_ylabel('Portfolio Value ($)')
        axes[0].grid(True, alpha=0.3)

        # Mark trades
        for t in self.trades:
            color = '#4CAF50' if t['pnl_pct'] > 0 else '#F44336'
            axes[0].axvline(x=t['entry_bar'], color=color, alpha=0.3, linewidth=0.5)

        # Drawdown
        peak = self.equity_curve[0]
        drawdowns = []
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            drawdowns.append((eq - peak) / peak)
        axes[1].fill_between(range(len(drawdowns)), drawdowns, 0, color='#F44336', alpha=0.4)
        axes[1].set_ylabel('Drawdown')
        axes[1].set_title('Drawdown')
        axes[1].grid(True, alpha=0.3)

        # Trade P&L
        if self.trades:
            pnls = [t['pnl_pct'] * 100 for t in self.trades]
            colors = ['#4CAF50' if p > 0 else '#F44336' for p in pnls]
            axes[2].bar(range(len(pnls)), pnls, color=colors, width=0.8)
            axes[2].axhline(y=0, color='gray', linewidth=0.5)
            axes[2].set_ylabel('Trade P&L (%)')
            axes[2].set_title(f'Individual Trades | Win Rate: {self.win_rate:.0%} | {self.num_trades} trades')
            axes[2].grid(True, alpha=0.3)

        plt.tight_layout()
        save_dir = os.path.dirname(save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
        plt.savefig(save_path, dpi=150)
        plt.close()
        print(f"\nEquity curve saved to {save_path}")


def build_multi_timeframe(df_1h):
    """Build 4H and Daily candles from 1H data."""
    df = df_1h.copy()
    if 'timestamp' in df.columns:
        df = df.set_index('timestamp')

    df_4h = df.resample('4h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna().reset_index()

    df_daily = df.resample('1D').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    }).dropna().reset_index()

    if 'volume' in df.columns:
        vol_4h = df['volume'].resample('4h').sum()
        df_4h['volume'] = vol_4h.values[:len(df_4h)]
        vol_d = df['volume'].resample('1D').sum()
        df_daily['volume'] = vol_d.values[:len(df_daily)]

    return df_4h, df_daily


def run_backtest(csv_path="data/btc_1h_90days.csv", initial_capital=50000,
                 position_pct=0.08, verbose=True):
    """
    Run backtest on historical data.

    Args:
        csv_path: Path to 1H candle CSV
        initial_capital: Starting capital ($)
        position_pct: Fraction of capital per trade (default 8% = quarter-Kelly estimate)
        verbose: Print trade-by-trade details
    """
    # Load data
    df = pd.read_csv(csv_path, parse_dates=['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)

    # Build higher timeframes
    df_ts = df.set_index('timestamp')
    df_4h, df_daily = build_multi_timeframe(df)

    print(f"Backtesting on {len(df)} hourly candles ({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")
    print(f"Initial capital: ${initial_capital:,.2f}")
    print(f"Position size: {position_pct:.0%} of capital per trade")
    print(f"Fees: {MAKER_FEE:.2%} per trade (limit orders)")
    print()

    # State
    equity = initial_capital
    peak_equity = initial_capital
    position = None  # {'entry_price', 'quantity', 'entry_bar', 'stop_level', 'peak_price'}
    trades = []
    equity_curve = []
    cooldown_until = 0

    # Minimum bars needed before trading
    min_bars = max(EMA_SLOW, DONCHIAN_UPPER_PERIOD, ATR_PERIOD) + 10

    for i in range(len(df)):
        current_price = df['close'].iloc[i]

        # Update position value for equity curve
        if position:
            unrealized = (current_price - position['entry_price']) * position['quantity']
            current_equity = equity + position['quantity'] * current_price
        else:
            current_equity = equity

        equity_curve.append(current_equity)

        # Track peak for drawdown
        if current_equity > peak_equity:
            peak_equity = current_equity

        # Not enough data yet
        if i < min_bars:
            continue

        # Get the slice of data up to current bar
        df_slice = df.iloc[:i+1].copy()

        # ── Check trailing stop on open position ──
        if position:
            # Update trailing stop
            if current_price > position['peak_price']:
                position['peak_price'] = current_price
                atr_series = calculate_atr(df_slice)
                current_atr = atr_series.iloc[-1] if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else current_price * 0.015
                position['stop_level'] = position['peak_price'] - ATR_STOP_MULTIPLIER * current_atr

            # Check stop-loss
            if current_price <= position['stop_level']:
                sell_price = current_price * (1 - MAKER_FEE)
                pnl = (sell_price - position['entry_price']) * position['quantity']
                pnl_pct = (sell_price - position['entry_price']) / position['entry_price']
                equity += position['quantity'] * sell_price
                trades.append({
                    'entry_bar': position['entry_bar'],
                    'exit_bar': i,
                    'entry_price': position['entry_price'],
                    'exit_price': sell_price,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'exit_reason': 'stop_loss',
                    'hold_bars': i - position['entry_bar'],
                })
                if verbose:
                    print(f"  [{i}] STOP-LOSS @ ${current_price:.0f} | P&L: {pnl_pct:+.2%} (${pnl:+,.0f})")
                position = None
                cooldown_until = i + 1  # 1-hour cooldown (1 bar on 1H data)
                continue

            # Check time-based exit (hold > 8 bars on 1H = 8 hours)
            if i - position['entry_bar'] > 8:
                current_pnl_pct = (current_price - position['entry_price']) / position['entry_price']
                if abs(current_pnl_pct) < FLAT_THRESHOLD:
                    sell_price = current_price * (1 - MAKER_FEE)
                    pnl = (sell_price - position['entry_price']) * position['quantity']
                    pnl_pct = (sell_price - position['entry_price']) / position['entry_price']
                    equity += position['quantity'] * sell_price
                    trades.append({
                        'entry_bar': position['entry_bar'],
                        'exit_bar': i,
                        'entry_price': position['entry_price'],
                        'exit_price': sell_price,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'exit_reason': 'time_exit',
                        'hold_bars': i - position['entry_bar'],
                    })
                    if verbose:
                        print(f"  [{i}] TIME-EXIT @ ${current_price:.0f} | P&L: {pnl_pct:+.2%} (flat)")
                    position = None
                    continue

        # ── Skip if in cooldown ──
        if i < cooldown_until:
            continue

        # ── Skip if already in position (S2: one position at a time) ──
        if position:
            continue

        # ══════════════════════════════════════
        # LAYER 1: REGIME DETECTION
        # ══════════════════════════════════════
        regime = detect_regime(df_slice)

        # ══════════════════════════════════════
        # LAYER 2: SIGNAL GENERATION
        # ══════════════════════════════════════
        signal = generate_signal(df_slice, regime)
        direction = signal['direction']

        if direction != 'BUY':
            continue  # We can only go long (S1)

        # ══════════════════════════════════════
        # LAYER 3: REVERSAL BLOCKER
        # ══════════════════════════════════════
        reset_cooldown()  # Reset time-based cooldown for backtesting (time.time() doesn't advance)
        prices_list = df_slice['close'].tolist()[-20:]
        volumes_list = df_slice['volume'].tolist()[-20:] if 'volume' in df_slice.columns else [0] * 20
        blocker_result = check_reversal_block(prices_list, volumes_list, 0.0001, 'BUY')
        if blocker_result.get('decision') == 'BLOCK':
            continue

        # ══════════════════════════════════════
        # LAYER 4: MULTI-TIMEFRAME FILTER
        # ══════════════════════════════════════
        # Find the 4H and daily bars up to current time
        current_time = df['timestamp'].iloc[i]
        df_4h_slice = df_4h[df_4h['timestamp'] <= current_time]
        df_daily_slice = df_daily[df_daily['timestamp'] <= current_time]

        if len(df_4h_slice) < 30 or len(df_daily_slice) < 5:
            continue

        tf_result = check_timeframe(df_slice, df_4h_slice, df_daily_slice, regime=regime)
        if not tf_result['pass']:
            continue

        # ══════════════════════════════════════
        # LAYER 5: XGBOOST (stub — passthrough)
        # ══════════════════════════════════════
        # Will use real model once Pranati delivers

        # ══════════════════════════════════════
        # LAYER 6: POSITION SIZING
        # ══════════════════════════════════════
        # Drawdown throttle
        drawdown = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0
        if drawdown > 0.10:
            continue  # Emergency mode — skip
        elif drawdown > 0.08:
            continue  # Halt
        elif drawdown > 0.05:
            size_mult = 0.5
        elif drawdown > 0.02:
            size_mult = 0.75
        else:
            size_mult = 1.0

        # Regime multiplier
        regime_mult = {'TRENDING': 1.0, 'SIDEWAYS': 0.5, 'VOLATILE': 0.1}.get(regime, 0.5)

        # Timeframe multiplier
        tf_mult = tf_result['multiplier']

        # Final position size
        trade_pct = position_pct * regime_mult * tf_mult * size_mult
        trade_pct = min(trade_pct, 0.35)  # Hard cap 35%
        trade_value = equity * trade_pct

        if trade_value < 100:  # Minimum trade size
            continue

        # ══════════════════════════════════════
        # LAYER 7: EXECUTE
        # ══════════════════════════════════════
        buy_price = current_price * (1 + MAKER_FEE)  # Include fee
        quantity = trade_value / buy_price

        # Set initial trailing stop
        atr_series = calculate_atr(df_slice)
        current_atr = atr_series.iloc[-1] if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else current_price * 0.015
        stop_level = current_price - ATR_STOP_MULTIPLIER * current_atr

        position = {
            'entry_price': buy_price,
            'quantity': quantity,
            'entry_bar': i,
            'stop_level': stop_level,
            'peak_price': current_price,
        }
        equity -= trade_value  # Cash decreases

        if verbose:
            print(f"  [{i}] BUY @ ${current_price:.0f} | Regime={regime} | TF={tf_result['score']} | Size=${trade_value:.0f} ({trade_pct:.1%}) | Stop=${stop_level:.0f} | Source={signal['source']}")

    # Close any open position at end
    if position:
        final_price = df['close'].iloc[-1] * (1 - MAKER_FEE)
        pnl = (final_price - position['entry_price']) * position['quantity']
        pnl_pct = (final_price - position['entry_price']) / position['entry_price']
        equity += position['quantity'] * final_price
        trades.append({
            'entry_bar': position['entry_bar'],
            'exit_bar': len(df) - 1,
            'entry_price': position['entry_price'],
            'exit_price': final_price,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'exit_reason': 'end_of_data',
            'hold_bars': len(df) - 1 - position['entry_bar'],
        })
        equity_curve[-1] = equity

    result = BacktestResult(trades, equity_curve, initial_capital)
    return result


if __name__ == "__main__":
    result = run_backtest(verbose=True)
    result.print_summary()
    result.plot_equity_curve()
