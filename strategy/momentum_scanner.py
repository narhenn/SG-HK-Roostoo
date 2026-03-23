"""
Multi-coin momentum scanner.
Scans Roostoo for top performing coins and manages positions.
Runs alongside the main BTC strategy.
"""
import logging
import time
from datetime import datetime

log = logging.getLogger("TradingBot")

# Coins to exclude (too low price, precision issues)
EXCLUDED_COINS = {'BONK/USD', 'DOGE/USD'}  # AmountPrecision=0 means whole units only

# Scanner config
MIN_MOMENTUM = 0.01      # 1% minimum 24h change to consider
MAX_ALT_POSITIONS = 2    # Maximum simultaneous alt positions
ALT_POSITION_SIZE = 15000  # $15k per alt position
ALT_TRAIL_PCT = 0.03     # 3% trailing stop for alts
SCAN_INTERVAL = 300      # Scan every 5 minutes (seconds)
MIN_PRICE = 0.005        # Minimum coin price to trade


class MomentumScanner:
    def __init__(self, client, state, save_state_fn=None):
        self.client = client
        self.state = state
        self.save_state_fn = save_state_fn
        self.last_scan = 0

        # Initialize alt state in shared state dict
        self.state.setdefault('alt_positions', {})
        self.state.setdefault('alt_trade_history', [])

    def _save(self):
        if self.save_state_fn:
            self.save_state_fn(self.state)

    def _get_exchange_info(self):
        """Get precision info for all pairs."""
        try:
            info = self.client.get_exchange_info()
            return info.get('TradePairs', {})
        except Exception as e:
            log.error(f"[AltScanner] Exchange info failed: {e}")
            return {}

    def _scan_momentum(self):
        """Scan all coins and return top movers above MIN_MOMENTUM."""
        try:
            ticker = self.client.get_ticker()
            data = ticker.get('Data', {})
        except Exception as e:
            log.error(f"[AltScanner] Ticker scan failed: {e}")
            return []

        coins = []
        for pair, info in data.items():
            if pair == 'BTC/USD' or pair in EXCLUDED_COINS:
                continue
            try:
                price = float(info.get('LastPrice', 0))
                change = float(info.get('Change', 0))
                bid = float(info.get('MaxBid', 0))
                ask = float(info.get('MinAsk', 0))

                if price < MIN_PRICE or bid <= 0 or ask <= 0:
                    continue
                if change < MIN_MOMENTUM:
                    continue

                coins.append({
                    'pair': pair,
                    'price': price,
                    'change': change,
                    'bid': bid,
                    'ask': ask,
                })
            except (ValueError, TypeError):
                continue

        # Sort by momentum descending
        coins.sort(key=lambda x: x['change'], reverse=True)
        return coins

    def _get_precision(self, pair, exchange_info):
        """Get price and amount precision for a pair."""
        pair_info = exchange_info.get(pair, {})
        return {
            'price_precision': pair_info.get('PricePrecision', 4),
            'amount_precision': pair_info.get('AmountPrecision', 2),
        }

    def _open_alt_position(self, coin, exchange_info):
        """Open a position in an alt coin."""
        pair = coin['pair']
        price = coin['price']
        ask = coin['ask']

        prec = self._get_precision(pair, exchange_info)
        qty = round(ALT_POSITION_SIZE / ask, prec['amount_precision'])
        limit_price = round(ask, prec['price_precision'])

        if qty <= 0:
            log.warning(f"[AltScanner] {pair}: qty rounds to 0, skipping")
            return False

        try:
            order = self.client.place_order(pair, "BUY", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            filled_qty = float(detail.get("FilledQuantity", 0) or 0)
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)
            status = (detail.get("Status") or "").upper()

            if not order_id:
                log.error(f"[AltScanner] {pair}: BUY returned no OrderID. Response: {order}")
                return False

            if status in ("FILLED", "COMPLETED") or filled_qty > 0:
                fill_price = avg_price or limit_price
                fill_qty = filled_qty or qty

                # Record position
                self.state['alt_positions'][pair] = {
                    'entry_price': fill_price,
                    'qty': fill_qty,
                    'peak_price': fill_price,
                    'stop': fill_price * (1 - ALT_TRAIL_PCT),
                    'entry_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
                    'order_id': order_id,
                    'price_precision': prec['price_precision'],
                    'amount_precision': prec['amount_precision'],
                }
                self._save()

                log.info(f"[AltScanner] BOUGHT {pair}: qty={fill_qty} price=${fill_price:.4f} size=${fill_qty*fill_price:.0f}")

                # Telegram alert
                try:
                    from execution.alerts import send_alert
                    send_alert(
                        f"<b>ALT BUY {pair}</b>\n"
                        f"Price: ${fill_price:.4f}\n"
                        f"Size: ${fill_qty * fill_price:,.0f}\n"
                        f"Momentum: {coin['change']*100:+.1f}%\n"
                        f"Stop: ${fill_price * (1 - ALT_TRAIL_PCT):,.4f}"
                    )
                except Exception:
                    pass

                return True
            else:
                log.warning(f"[AltScanner] {pair}: BUY not filled immediately. Status={status}")
                # Cancel unfilled order
                try:
                    self.client.cancel_order(str(order_id))
                except Exception:
                    pass
                return False

        except Exception as e:
            log.error(f"[AltScanner] {pair}: BUY failed: {e}")
            return False

    def _check_alt_exits(self):
        """Check trailing stops and momentum reversal for alt positions."""
        positions = self.state.get('alt_positions', {})
        to_close = []

        for pair, pos in positions.items():
            try:
                raw_ticker = self.client.get_ticker(pair)
                if isinstance(raw_ticker, dict) and 'Data' in raw_ticker:
                    ticker = raw_ticker['Data'].get(pair, {})
                else:
                    ticker = raw_ticker

                price = float(ticker.get('LastPrice', 0))
                bid = float(ticker.get('MaxBid', 0))
                change = float(ticker.get('Change', 0))

                if price <= 0 or bid <= 0:
                    continue

                entry = pos['entry_price']
                peak = pos.get('peak_price', entry)

                # Update peak
                if price > peak:
                    pos['peak_price'] = price
                    pos['stop'] = price * (1 - ALT_TRAIL_PCT)
                    log.info(f"[AltScanner] {pair}: new peak ${price:.4f}, stop ${pos['stop']:.4f}")

                # Check trailing stop
                if price <= pos['stop']:
                    to_close.append((pair, bid, 'TRAILING_STOP'))
                    continue

                # Check momentum reversal (24h change went negative)
                if change < -0.005:  # -0.5% change = momentum lost
                    pnl_pct = (price - entry) / entry
                    if pnl_pct > 0.003:  # Only exit on reversal if in profit (above fees)
                        to_close.append((pair, bid, 'MOMENTUM_REVERSAL'))

            except Exception as e:
                log.error(f"[AltScanner] {pair}: check failed: {e}")

        # Execute exits
        for pair, bid, reason in to_close:
            self._close_alt_position(pair, bid, reason)

        self._save()

    def _close_alt_position(self, pair, current_bid, reason):
        """Close an alt position."""
        pos = self.state['alt_positions'].get(pair)
        if not pos:
            return

        qty = pos['qty']
        price_prec = pos.get('price_precision', 4)
        limit_price = round(current_bid, price_prec)

        try:
            order = self.client.place_order(pair, "SELL", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            filled_qty = float(detail.get("FilledQuantity", 0) or 0)
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)
            status = (detail.get("Status") or "").upper()

            if not order_id:
                log.error(f"[AltScanner] {pair}: SELL returned no OrderID. Response: {order}")
                # Don't retry infinitely -- mark for manual review
                pos['sell_failed'] = True
                self._save()
                try:
                    from execution.alerts import send_alert
                    send_alert(f"<b>ALT SELL FAILED {pair}</b>\nReason: {reason}\nManual review needed.")
                except Exception:
                    pass
                return

            exit_price = avg_price or limit_price
            entry_price = pos['entry_price']

            # Calculate P&L with fees
            gross_pnl = (exit_price - entry_price) * qty
            fee_entry = entry_price * qty * 0.001
            fee_exit = exit_price * qty * 0.001
            net_pnl = gross_pnl - fee_entry - fee_exit
            pnl_pct = net_pnl / (entry_price * qty) if entry_price * qty > 0 else 0

            log.info(f"[AltScanner] SOLD {pair}: exit=${exit_price:.4f} P&L=${net_pnl:+.2f} ({pnl_pct:+.2%}) reason={reason}")

            # Record in trade history
            self.state['alt_trade_history'].append({
                'pair': pair,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'qty': qty,
                'pnl': net_pnl,
                'pnl_pct': pnl_pct,
                'reason': reason,
                'exit_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
            })

            # Remove position
            del self.state['alt_positions'][pair]
            self._save()

            # Telegram alert
            try:
                from execution.alerts import send_alert
                send_alert(
                    f"<b>ALT SELL {pair}</b>\n"
                    f"Exit: ${exit_price:.4f}\n"
                    f"P&L: ${net_pnl:+,.2f} ({pnl_pct:+.2%})\n"
                    f"Reason: {reason}"
                )
            except Exception:
                pass

        except Exception as e:
            log.error(f"[AltScanner] {pair}: SELL failed: {e}")

    def run_cycle(self):
        """Run one scanner cycle. Call from main loop."""
        now = time.time()

        # Always check exits on every cycle (every 60s)
        if self.state.get('alt_positions'):
            self._check_alt_exits()

        # Scan for new entries every SCAN_INTERVAL
        if now - self.last_scan < SCAN_INTERVAL:
            return
        self.last_scan = now

        # Don't open new positions if already at max
        current_alts = len(self.state.get('alt_positions', {}))
        if current_alts >= MAX_ALT_POSITIONS:
            return

        # Scan for momentum
        top_movers = self._scan_momentum()
        if not top_movers:
            return

        exchange_info = self._get_exchange_info()
        if not exchange_info:
            return

        # Try to open position in best mover we don't already hold
        slots = MAX_ALT_POSITIONS - current_alts
        opened = 0
        for coin in top_movers:
            if opened >= slots:
                break
            if coin['pair'] in self.state.get('alt_positions', {}):
                continue
            # Skip coins with failed sells (need manual intervention)
            existing = self.state.get('alt_positions', {}).get(coin['pair'], {})
            if existing and existing.get('sell_failed'):
                continue

            log.info(f"[AltScanner] Top mover: {coin['pair']} {coin['change']*100:+.1f}% ${coin['price']:.4f}")
            if self._open_alt_position(coin, exchange_info):
                opened += 1
                time.sleep(2)  # Small delay between orders
