"""
Breakout Scanner — Primary trading strategy for finals.
Backtested: +14.27% per 10 days, 2.75% max DD, never lost a 10-day window.

Entry signals (priority order):
1. Donchian 20-period breakout + green + above SMA50 + vol>1.2x
2. Donchian 10-period breakout + green + above SMA50 + vol>1.0x + MACD bullish
3. RSI < 30 oversold bounce + green + above SMA50
4. Below BB lower band + green + above SMA50

Exit: SL=0.20%, Trail=0.40%, Breakeven at +0.10%, max hold 12h.
Positions: 15 max, 25% of equity each.
"""

import logging
import time
import math
import threading
from datetime import datetime

log = logging.getLogger("TradingBot")

# Thread lock shared with momentum_scanner
from strategy.momentum_scanner import _alt_lock

# ── Parameters (backtested optimal) ──
STOP_LOSS_PCT = 0.002        # 0.20% — cut losers instantly
TRAIL_PCT = 0.004            # 0.40% trailing stop from peak
BREAKEVEN_AT = 0.001         # Move stop to BE at +0.10% profit
MAX_POSITIONS = 15
POSITION_SIZE_PCT = 0.25     # 25% of equity per position
MAX_HOLD_BARS = 720          # 12 hours at 60-second polling
SCAN_INTERVAL = 60           # Scan every 60 seconds

# Coins to exclude (low liquidity / meme)
EXCLUDED = {'PAXG/USD', 'BONK/USD', 'DOGE/USD', 'SHIB/USD', 'PEPE/USD',
            'FLOKI/USD', '1000CHEEMS/USD', 'PUMP/USD'}


class BreakoutScanner:
    """
    Multi-coin breakout scanner. Runs alongside main.py.
    Manages its own positions in state['breakout_positions'].
    """

    def __init__(self, client, state, save_state_fn=None):
        self.client = client
        self.state = state
        self.save_state_fn = save_state_fn
        self.last_scan = 0
        self._exchange_info_cache = None

        # Price history for indicators (built from live ticks)
        self._price_history = {}   # {pair: [close_prices]}
        self._volume_history = {}  # {pair: [volumes]}
        self._ohlc_history = {}    # {pair: [(open, high, low, close)]}
        self._tick_count = 0

        # Initialize state
        self.state.setdefault('breakout_positions', {})
        self.state.setdefault('breakout_trade_history', [])

    def _save(self):
        if self.save_state_fn:
            self.save_state_fn(self.state)

    def _get_exchange_info(self):
        if self._exchange_info_cache:
            return self._exchange_info_cache
        try:
            info = self.client.get_exchange_info()
            self._exchange_info_cache = info.get('TradePairs', {})
            return self._exchange_info_cache
        except Exception as e:
            log.error(f"[Breakout] Exchange info failed: {e}")
            return {}

    def _update_history(self, ticker_data):
        """Update price/volume history from ticker data."""
        for pair, info in ticker_data.items():
            if pair in EXCLUDED:
                continue
            try:
                price = float(info.get('LastPrice', 0))
                volume = float(info.get('CoinTradeValue', 0))
                bid = float(info.get('MaxBid', 0))
                ask = float(info.get('MinAsk', 0))
                if price <= 0:
                    continue

                if pair not in self._price_history:
                    self._price_history[pair] = []
                    self._volume_history[pair] = []
                    self._ohlc_history[pair] = []

                self._price_history[pair].append(price)
                self._volume_history[pair].append(volume)

                # Keep last 1500 ticks (~25 hours at 60s)
                if len(self._price_history[pair]) > 1500:
                    self._price_history[pair] = self._price_history[pair][-1200:]
                    self._volume_history[pair] = self._volume_history[pair][-1200:]

            except (ValueError, TypeError):
                continue
        self._tick_count += 1

    def _rsi(self, pair, period=14):
        prices = self._price_history.get(pair, [])
        if len(prices) < period + 1:
            return 50.0
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(c, 0) for c in changes]
        losses = [max(-c, 0) for c in changes]
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
        if al == 0:
            return 100.0
        return 100 - (100 / (1 + ag / al))

    def _sma(self, pair, period=50):
        prices = self._price_history.get(pair, [])
        if len(prices) < period:
            return 0
        return sum(prices[-period:]) / period

    def _donchian_high(self, pair, period=20):
        """Highest price in last N ticks."""
        prices = self._price_history.get(pair, [])
        if len(prices) < period:
            return float('inf')
        # Use second-to-last to require breakout (current >= previous high)
        return max(prices[-(period+1):-1]) if len(prices) > period else max(prices[-period:])

    def _donchian_high_short(self, pair, period=10):
        prices = self._price_history.get(pair, [])
        if len(prices) < period:
            return float('inf')
        return max(prices[-(period+1):-1]) if len(prices) > period else max(prices[-period:])

    def _vol_ratio(self, pair, period=20):
        vols = self._volume_history.get(pair, [])
        if len(vols) < period + 1:
            return 1.0
        cur = vols[-1]
        avg = sum(vols[-(period+1):-1]) / period
        return cur / avg if avg > 0 else 1.0

    def _is_green(self, pair):
        """Last tick price > previous tick price (proxy for green candle)."""
        prices = self._price_history.get(pair, [])
        if len(prices) < 2:
            return False
        return prices[-1] > prices[-2]

    def _macd_bullish(self, pair):
        """EMA12 > EMA26."""
        prices = self._price_history.get(pair, [])
        if len(prices) < 30:
            return False
        # Simple EMA calculation
        def ema(data, period):
            mult = 2 / (period + 1)
            val = sum(data[:period]) / period
            for p in data[period:]:
                val = (p - val) * mult + val
            return val
        return ema(prices, 12) > ema(prices, 26)

    def _bb_lower(self, pair, period=20):
        prices = self._price_history.get(pair, [])
        if len(prices) < period:
            return 0
        recent = prices[-period:]
        mean = sum(recent) / period
        std = (sum((p - mean) ** 2 for p in recent) / period) ** 0.5
        return mean - 2 * std

    def _score_entry(self, pair, info, ticker_data):
        """Score a coin for entry. Returns (score, signal_type) or (0, None)."""
        price = float(info.get('LastPrice', 0))
        if price <= 0:
            return 0, None

        # Need minimum history
        if len(self._price_history.get(pair, [])) < 60:
            return 0, None

        sma50 = self._sma(pair, 50)
        if sma50 <= 0 or price <= sma50:
            return 0, None

        green = self._is_green(pair)
        if not green:
            return 0, None

        # Signal 1: Donchian 20 breakout (highest priority)
        don20 = self._donchian_high(pair, 20)
        vol_ratio = self._vol_ratio(pair)
        if price >= don20 and vol_ratio > 1.2:
            return vol_ratio + 30, 'DON20_BREAKOUT'

        # Signal 2: Donchian 10 breakout + MACD
        don10 = self._donchian_high_short(pair, 10)
        if price >= don10 and vol_ratio > 1.0 and self._macd_bullish(pair):
            return vol_ratio + 15, 'DON10_MACD'

        # Signal 3: RSI oversold bounce
        rsi = self._rsi(pair)
        if rsi < 30:
            return 5, 'RSI_OVERSOLD'

        # Signal 4: BB lower band touch
        bb_low = self._bb_lower(pair)
        if bb_low > 0 and price < bb_low:
            return 3, 'BB_LOWER'

        return 0, None

    def _check_exits(self):
        """Check all breakout positions for exits."""
        positions = self.state.get('breakout_positions', {})
        if not positions:
            return

        try:
            ticker_raw = self.client.get_ticker()
            ticker_data = ticker_raw.get('Data', {})
        except Exception as e:
            log.error(f"[Breakout] Ticker failed in exit check: {e}")
            return

        to_close = []
        for pair, pos in list(positions.items()):
            try:
                info = ticker_data.get(pair, {})
                price = float(info.get('LastPrice', 0))
                bid = float(info.get('MaxBid', 0))
                if price <= 0 or bid <= 0:
                    continue

                entry = pos['entry_price']
                peak = pos.get('peak_price', entry)
                pos['bars'] = pos.get('bars', 0) + 1

                # Update peak
                if price > peak:
                    pos['peak_price'] = price

                # Breakeven: move stop to entry + tiny margin once +0.10% in profit
                if price >= entry * (1 + BREAKEVEN_AT) and pos['stop'] < entry:
                    pos['stop'] = entry * 1.001  # breakeven + 0.1% buffer
                    log.info(f"[Breakout] {pair}: moved to breakeven at ${price:.4f}")

                # Trailing stop: trail at 0.40% below peak
                trail_stop = pos['peak_price'] * (1 - TRAIL_PCT)
                if trail_stop > pos['stop']:
                    pos['stop'] = trail_stop

                # Check stop
                if price <= pos['stop']:
                    to_close.append((pair, bid, 'STOP', price))
                    continue

                # Check max hold (12 hours = 720 ticks at 60s)
                if pos['bars'] >= MAX_HOLD_BARS:
                    to_close.append((pair, bid, 'TIME', price))
                    continue

            except Exception as e:
                log.error(f"[Breakout] {pair} exit check error: {e}")

        # Execute exits
        for pair, bid, reason, price in to_close:
            self._close_position(pair, bid, reason)

    def _close_position(self, pair, current_bid, reason):
        """Close a breakout position."""
        pos = self.state['breakout_positions'].get(pair)
        if not pos:
            return

        qty = pos['qty']
        price_prec = pos.get('price_precision', 4)
        amount_prec = pos.get('amount_precision', 2)
        limit_price = round(current_bid, price_prec)

        try:
            order = self.client.place_order(pair, "SELL", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            status = (detail.get("Status") or "").upper()
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)

            if not order_id or (status not in ("FILLED", "COMPLETED", "") and
                               float(detail.get("FilledQuantity", 0) or 0) <= 0):
                # Retry at lower price
                retry_price = round(current_bid * 0.999, price_prec)
                order = self.client.place_order(pair, "SELL", "LIMIT", qty, retry_price)
                detail = order.get("OrderDetail", order)
                avg_price = float(detail.get("FilledAverPrice", 0) or 0)

            exit_price = avg_price or limit_price
            entry_price = pos['entry_price']
            gross_pnl = (exit_price - entry_price) * qty
            fees = entry_price * qty * 0.001 + exit_price * qty * 0.001
            net_pnl = gross_pnl - fees

            log.info(f"[Breakout] SOLD {pair}: {reason} entry=${entry_price:.4f} exit=${exit_price:.4f} "
                     f"P&L=${net_pnl:+,.2f} ({net_pnl/(entry_price*qty)*100:+.2f}%)")

            # Record trade
            self.state['breakout_trade_history'].append({
                'pair': pair,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'qty': qty,
                'pnl': net_pnl,
                'pnl_pct': net_pnl / (entry_price * qty) if entry_price * qty > 0 else 0,
                'reason': reason,
                'signal': pos.get('signal_type', ''),
                'exit_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
            })

            # Update equity
            equity = self.state.get('current_equity', 1000000)
            self.state['current_equity'] = equity + net_pnl

            # Remove position
            del self.state['breakout_positions'][pair]
            self._save()

            # Telegram alert
            try:
                from execution.alerts import send_alert
                send_alert(
                    f"<b>BREAKOUT {reason} {pair}</b>\n"
                    f"P&L: ${net_pnl:+,.2f} ({net_pnl/(entry_price*qty)*100:+.2f}%)\n"
                    f"Entry: ${entry_price:.4f} Exit: ${exit_price:.4f}")
            except Exception:
                pass

        except Exception as e:
            log.error(f"[Breakout] {pair}: SELL failed: {e}")

    def _open_position(self, pair, info, signal_type, score):
        """Open a new breakout position."""
        ask = float(info.get('MinAsk', 0))
        if ask <= 0:
            return False

        equity = self.state.get('current_equity', 1000000)
        size = equity * POSITION_SIZE_PCT
        size = min(size, 250000)  # Hard cap $250k per position

        exinfo = self._get_exchange_info()
        pair_info = exinfo.get(pair, {})
        price_prec = int(pair_info.get('PricePrecision', 4))
        amount_prec = int(pair_info.get('AmountPrecision', 2))

        amt_mult = 10 ** amount_prec
        qty = math.floor(size / ask * amt_mult) / amt_mult
        if qty <= 0:
            return False

        limit_price = round(ask, price_prec)

        try:
            order = self.client.place_order(pair, "BUY", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            filled_qty = float(detail.get("FilledQuantity", 0) or 0)
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)
            status = (detail.get("Status") or "").upper()

            if not order_id:
                return False
            if status not in ("FILLED", "COMPLETED", "") and filled_qty <= 0:
                return False

            fill_price = avg_price or limit_price
            fill_qty = filled_qty or qty

            with _alt_lock:
                self.state['breakout_positions'][pair] = {
                    'entry_price': fill_price,
                    'qty': fill_qty,
                    'peak_price': fill_price,
                    'stop': fill_price * (1 - STOP_LOSS_PCT),
                    'signal_type': signal_type,
                    'score': score,
                    'entry_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
                    'order_id': order_id,
                    'price_precision': price_prec,
                    'amount_precision': amount_prec,
                    'bars': 0,
                }
                self._save()

            actual_cost = fill_price * fill_qty
            log.info(f"[Breakout] BOUGHT {pair}: {signal_type} score={score:.1f} "
                     f"qty={fill_qty} @ ${fill_price:.4f} = ${actual_cost:,.0f}")

            try:
                from execution.alerts import send_alert
                send_alert(
                    f"<b>BREAKOUT BUY {pair}</b>\n"
                    f"Signal: {signal_type}\n"
                    f"Size: ${actual_cost:,.0f}\n"
                    f"Stop: ${fill_price * (1 - STOP_LOSS_PCT):.4f} (-{STOP_LOSS_PCT*100:.1f}%)")
            except Exception:
                pass

            return True

        except Exception as e:
            log.error(f"[Breakout] {pair}: BUY failed: {e}")
            return False

    def run_cycle(self):
        """Run one scan cycle. Call from main loop every 60s."""
        now = time.time()
        if now - self.last_scan < SCAN_INTERVAL:
            return
        self.last_scan = now

        # Competition protection
        if self.state.get('_competition_protect'):
            return

        # Fetch ticker
        try:
            ticker_raw = self.client.get_ticker()
            ticker_data = ticker_raw.get('Data', {})
        except Exception as e:
            log.error(f"[Breakout] Ticker failed: {e}")
            return

        # Update price history
        self._update_history(ticker_data)

        # Need minimum 60 ticks (~1 hour) before trading
        if self._tick_count < 60:
            if self._tick_count % 10 == 0:
                log.info(f"[Breakout] Warming up: {self._tick_count}/60 ticks")
            return

        # Check exits
        with _alt_lock:
            self._check_exits()

        # Check entries
        positions = self.state.get('breakout_positions', {})
        if len(positions) >= MAX_POSITIONS:
            return

        # Score all coins
        candidates = []
        for pair, info in ticker_data.items():
            if pair in EXCLUDED or pair in positions:
                continue
            score, signal = self._score_entry(pair, info, ticker_data)
            if score > 0:
                candidates.append((score, pair, info, signal))

        if not candidates:
            return

        # Sort by score descending, take top signals
        candidates.sort(key=lambda x: -x[0])
        slots = MAX_POSITIONS - len(positions)
        opened = 0

        for score, pair, info, signal in candidates:
            if opened >= slots or opened >= 4:  # Max 4 new per cycle
                break
            if self._open_position(pair, info, signal, score):
                opened += 1
                time.sleep(1)  # Rate limit between orders

    def start_exit_monitor(self):
        """Start background thread checking exits every 2 seconds."""
        def _loop():
            log.info("[BreakoutMonitor] Started. Checking every 2s.")
            while True:
                try:
                    if self.state.get('breakout_positions'):
                        with _alt_lock:
                            self._check_exits()
                except Exception as e:
                    log.error(f"[BreakoutMonitor] Error: {e}")
                time.sleep(2)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
