"""
Candle Builder: Stores tick data and aggregates into 1H, 4H, Daily candles.
Owner: Narhen

Also handles cold start bootstrap from Binance CSV.
Ticks are persisted to disk so restarts don't wipe candle history.
"""

import json
import os
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config import HISTORICAL_DATA_FILE

log = logging.getLogger(__name__)

# Flag: True while bootstrap (Binance) candles dominate the BB/z-score window.
# Set to False after 20+ live Roostoo 1H candles are appended.
BOOTSTRAP_DOMINANT = True

TICK_CACHE_FILE = "data/ticks_cache.jsonl"
TICK_CACHE_MAX = 2000


def _load_tick_cache() -> list:
    """Load cached ticks from disk."""
    if not os.path.exists(TICK_CACHE_FILE):
        return []
    ticks = []
    try:
        with open(TICK_CACHE_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    t = json.loads(line)
                    t['timestamp'] = datetime.fromisoformat(t['timestamp'])
                    ticks.append(t)
    except Exception:
        log.exception("Failed to load tick cache")
        return []
    return ticks


def _append_tick_to_cache(tick: dict):
    """Append one tick to the cache file."""
    try:
        record = {
            'timestamp': tick['timestamp'].isoformat(),
            'price': tick['price'],
            'volume': tick.get('volume', 0),
            'bid': tick.get('bid', 0),
            'ask': tick.get('ask', 0),
            'spread': tick.get('spread', 0),
        }
        with open(TICK_CACHE_FILE, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        log.exception("Failed to append tick to cache")


def _truncate_tick_cache():
    """Keep only the last TICK_CACHE_MAX ticks in the cache file."""
    if not os.path.exists(TICK_CACHE_FILE):
        return
    try:
        with open(TICK_CACHE_FILE, 'r') as f:
            lines = f.readlines()
        if len(lines) > TICK_CACHE_MAX:
            with open(TICK_CACHE_FILE, 'w') as f:
                f.writelines(lines[-TICK_CACHE_MAX:])
    except Exception:
        log.exception("Failed to truncate tick cache")


class CandleBuilder:
    """
    Stores 60-second price ticks and aggregates into higher timeframes.
    Also bootstraps with historical Binance data for cold start.
    """

    def __init__(self):
        self.ticks = []
        self.df_1h = pd.DataFrame()
        self.df_4h = pd.DataFrame()
        self.df_daily = pd.DataFrame()
        self._live_candle_count = 0

        # Load cached ticks from disk (survives restarts)
        cached = _load_tick_cache()
        if cached:
            self.ticks = cached[-1440:]  # Keep last 24h
            hours_covered = len(cached) / 60
            log.info(f"Loaded {len(cached)} cached ticks from disk (covers ~{hours_covered:.1f} live hours)")
        else:
            log.info("No tick cache found — starting fresh")

    def bootstrap(self):
        """
        Cold start: Load historical data from Binance CSV.
        This gives us 90 days of 1H candles so indicators work immediately.
        """
        if os.path.exists(HISTORICAL_DATA_FILE):
            df = pd.read_csv(HISTORICAL_DATA_FILE, parse_dates=['timestamp'])
            df = df.sort_values('timestamp').reset_index(drop=True)
            self.df_1h = df.copy()
            self._build_higher_timeframes()

            # If we have cached ticks, rebuild candles from them immediately
            if len(self.ticks) >= 60:
                log.info(f"Rebuilding candles from {len(self.ticks)} cached ticks")
                self._rebuild_from_ticks()

            return True
        return False

    def add_tick(self, price: float, volume: float = 0.0,
                 bid: float = 0.0, ask: float = 0.0):
        """Store a new 60-second tick."""
        tick = {
            'timestamp': datetime.utcnow(),
            'price': price,
            'volume': volume,
            'bid': bid,
            'ask': ask,
            'spread': (ask - bid) / price if price > 0 and ask > 0 and bid > 0 else 0,
        }
        self.ticks.append(tick)

        # Persist to disk
        _append_tick_to_cache(tick)

        # Keep last 24 hours of ticks (1440 ticks at 60s intervals)
        if len(self.ticks) > 1500:
            self.ticks = self.ticks[-1440:]
            _truncate_tick_cache()

        # Rebuild candles every 60 ticks (every hour)
        if len(self.ticks) % 60 == 0:
            self._rebuild_from_ticks()

    def _rebuild_from_ticks(self):
        """Aggregate ticks into 1H candles and append to existing history."""
        if len(self.ticks) < 60:
            return

        tick_df = pd.DataFrame(self.ticks)
        tick_df.set_index('timestamp', inplace=True)

        # Resample to 1H candles
        new_1h = tick_df['price'].resample('1h').agg(
            open='first', high='max', low='min', close='last'
        ).dropna()

        if 'volume' in tick_df.columns:
            vol = tick_df['volume'].resample('1h').sum()
            new_1h['volume'] = vol

        new_1h = new_1h.reset_index()
        new_1h.rename(columns={'timestamp': 'timestamp'}, inplace=True)

        # Append to existing 1H data (from bootstrap)
        if not self.df_1h.empty:
            # Only add candles newer than what we have
            last_ts = self.df_1h['timestamp'].max() if 'timestamp' in self.df_1h.columns else None
            if last_ts is not None:
                new_1h = new_1h[new_1h['timestamp'] > last_ts]

            # Data integrity check: warn if live ticks diverge from bootstrap
            if not new_1h.empty and 'close' in self.df_1h.columns:
                bootstrap_end = float(self.df_1h['close'].iloc[-1])
                live_close = float(new_1h['close'].iloc[0])
                if bootstrap_end > 0:
                    divergence = abs(live_close - bootstrap_end) / bootstrap_end
                    if divergence > 0.05:
                        log.warning(
                            f"Binance/Roostoo price divergence: {divergence:.1%} "
                            f"(bootstrap=${bootstrap_end:,.0f} vs live=${live_close:,.0f}). "
                            f"BB/z-score may be stale until live candles dominate."
                        )

            self.df_1h = pd.concat([self.df_1h, new_1h], ignore_index=True)

            # Track live candle count; clear bootstrap flag after 20
            global BOOTSTRAP_DOMINANT
            self._live_candle_count += len(new_1h)
            if self._live_candle_count >= 20 and BOOTSTRAP_DOMINANT:
                BOOTSTRAP_DOMINANT = False
                log.info(f"Bootstrap dominant cleared: {self._live_candle_count} live candles appended")
        else:
            self.df_1h = new_1h

        self._build_higher_timeframes()

    def _build_higher_timeframes(self):
        """Build 4H and Daily from 1H candles."""
        if self.df_1h.empty or 'timestamp' not in self.df_1h.columns:
            return

        df = self.df_1h.set_index('timestamp')

        # 4H candles
        self.df_4h = df.resample('4h').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
        }).dropna().reset_index()

        if 'volume' in df.columns:
            vol_4h = df['volume'].resample('4h').sum()
            self.df_4h['volume'] = vol_4h.values[:len(self.df_4h)]

        # Daily candles
        self.df_daily = df.resample('1D').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
        }).dropna().reset_index()

        if 'volume' in df.columns:
            vol_d = df['volume'].resample('1D').sum()
            self.df_daily['volume'] = vol_d.values[:len(self.df_daily)]

    def get_df(self, timeframe: str = '1h') -> pd.DataFrame:
        """Get candle DataFrame for a timeframe."""
        if timeframe == '1h':
            return self.df_1h
        elif timeframe == '4h':
            return self.df_4h
        elif timeframe == '1d' or timeframe == 'daily':
            return self.df_daily
        return self.df_1h

    def get_current_price(self) -> float:
        """Get most recent price."""
        if self.ticks:
            return self.ticks[-1]['price']
        if not self.df_1h.empty:
            return self.df_1h['close'].iloc[-1]
        return 0.0

    def get_spread_series(self) -> pd.Series:
        """Get spread history from ticks (for reversal blocker)."""
        if not self.ticks:
            return pd.Series(dtype=float)
        return pd.Series([t['spread'] for t in self.ticks])
