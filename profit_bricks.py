"""
PROFIT BRICKS — Walter Peters' trade-level compounding engine
════════════════════════════════════════════════════════════════════
Core rule (from fxjake.com / Walter Peters' Small Account Big Profits):
  "If a trade is a winner, use those winnings as risk capital in
   the next trade."

Mechanics:
  - Track a "pool" of accumulated winnings since inception
  - Next trade's risk = base_risk + (pool * pool_factor)
  - On a winning close:  pool += pnl
  - On a losing close:   pool += pnl (pnl is negative)
                         pool = max(pool, 0)   # losses eat pool first
  - Optional max_risk_pct caps the per-trade risk as a % of current
    equity (safety net on the exponential growth)

Peters' dartboard test (random entries, 3:1 reward-to-risk, 389 trades):
  Without bricks:  +12.3R cumulative
  With bricks:     +99.3R  (7.07x improvement)
  Max DD:          -91.7R → -133.7R (1.46x worse)

This module is engine-agnostic — the backtest and live bot both
instantiate ProfitBricks() once and call it for sizing + trade-close
updates.
"""


class ProfitBricks:
    """
    Profit Bricks pool tracker.

    Parameters
    ----------
    starting_equity : float
        The account balance at the start of the run. Used as the base
        for the fixed-fraction risk (not the current equity, so the
        base doesn't compound on its own).
    base_risk_pct : float
        Fractional risk of starting_equity deployed every trade,
        regardless of pool state. E.g. 0.06 = 6%.
    pool_factor : float
        Fraction of the current pool to deploy on the next trade.
        0.0 disables Profit Bricks (normal fixed-fraction).
        1.0 deploys the entire pool every trade (most aggressive).
        0.5 is a middle-ground that splits the pool over trades.
    max_risk_pct : float | None
        Optional hard cap on per-trade risk as a fraction of current
        equity. Prevents the pool from driving risk to absurd levels
        (e.g. cap at 20% of equity means one trade can never risk
        more than 20% of the account even if the pool is huge).

    Usage
    -----
        bricks = ProfitBricks(1_000_000, 0.06, 0.5, max_risk_pct=0.20)
        risk = bricks.next_risk_dollars(current_equity)
        ...trade fills, exits...
        bricks.on_close(trade_pnl)
    """

    __slots__ = (
        'starting_equity', 'base_risk_pct', 'pool_factor', 'max_risk_pct',
        'pool', 'peak_pool', 'trades_counted', 'wins', 'losses',
        'total_pool_deployed', 'last_pool_used',
    )

    def __init__(self, starting_equity, base_risk_pct, pool_factor=0.5,
                 max_risk_pct=None):
        if starting_equity <= 0:
            raise ValueError(f"starting_equity must be positive, got {starting_equity}")
        if base_risk_pct < 0 or base_risk_pct > 1:
            raise ValueError(f"base_risk_pct must be in [0,1], got {base_risk_pct}")
        if pool_factor < 0:
            raise ValueError(f"pool_factor must be >= 0, got {pool_factor}")

        self.starting_equity = starting_equity
        self.base_risk_pct = base_risk_pct
        self.pool_factor = pool_factor
        self.max_risk_pct = max_risk_pct

        self.pool = 0.0
        self.peak_pool = 0.0
        self.trades_counted = 0
        self.wins = 0
        self.losses = 0
        self.total_pool_deployed = 0.0
        self.last_pool_used = 0.0

    def next_risk_dollars(self, current_equity):
        """Compute the risk for the next trade given the current pool."""
        base = self.starting_equity * self.base_risk_pct
        pool_contribution = self.pool * self.pool_factor
        total = base + pool_contribution

        if self.max_risk_pct is not None:
            cap = current_equity * self.max_risk_pct
            total = min(total, cap)

        # Sanity: never risk more than current equity (would imply infinite leverage)
        total = min(total, current_equity * 0.95)

        self.last_pool_used = pool_contribution
        self.total_pool_deployed += pool_contribution
        return total

    def on_close(self, pnl):
        """Update pool after a trade closes.

        Winners add to the pool. Losers drain the pool (floored at 0 —
        the base capital is never tracked here, it's just a reference).
        """
        self.trades_counted += 1
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        self.pool += pnl
        if self.pool < 0:
            self.pool = 0.0

        if self.pool > self.peak_pool:
            self.peak_pool = self.pool

    def reset(self):
        """Reset the pool to zero. Useful between backtest runs."""
        self.pool = 0.0
        self.peak_pool = 0.0
        self.trades_counted = 0
        self.wins = 0
        self.losses = 0
        self.total_pool_deployed = 0.0
        self.last_pool_used = 0.0

    def stats(self):
        return {
            'pool': self.pool,
            'peak_pool': self.peak_pool,
            'trades': self.trades_counted,
            'wins': self.wins,
            'losses': self.losses,
            'wr': self.wins / self.trades_counted if self.trades_counted else 0,
            'avg_pool_deployed': self.total_pool_deployed / self.trades_counted
                                 if self.trades_counted else 0,
        }

    def __repr__(self):
        return (f"ProfitBricks(base={self.base_risk_pct*100:.1f}%, "
                f"pool_factor={self.pool_factor}, "
                f"max_risk={self.max_risk_pct}, "
                f"pool=${self.pool:,.0f})")
