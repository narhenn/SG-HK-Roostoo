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
MAX_ALT_POSITIONS = 4    # Maximum simultaneous alt positions
MAX_ALT_EXPOSURE = 0.10  # Max 10% of portfolio in alts total
ALT_TRAIL_MIN = 0.02     # Minimum trailing stop: 2%
ALT_TRAIL_MAX = 0.07     # Maximum trailing stop: 7%
SCAN_INTERVAL = 300      # Scan every 5 minutes (seconds)
MIN_PRICE = 0.005        # Minimum coin price to trade
COIN_COOLDOWN = 1800     # 30 min cooldown per coin after selling (seconds)
MOMENTUM_REVERSAL = -0.02  # -2% 24h change = real reversal (was -0.5%)

# Hybrid position sizing — stronger momentum = bigger position
MOMENTUM_TIERS = [
    (0.10, 60000),   # +10%+ → $60k (very high conviction)
    (0.06, 40000),   # +6-10% → $40k (high conviction)
    (0.03, 25000),   # +3-6% → $25k (medium conviction)
    (0.01, 10000),   # +1-3% → $10k (low conviction)
]


def _hybrid_size(change_24h: float, alt_trade_history: list) -> float:
    """Calculate position size based on momentum strength + anti-martingale."""
    # Base size from momentum tier
    base = 10000  # default
    for threshold, size in MOMENTUM_TIERS:
        if abs(change_24h) >= threshold:
            base = size
            break

    # Anti-martingale: adjust based on recent alt win/loss streak
    if alt_trade_history:
        consec_losses = 0
        consec_wins = 0
        for t in reversed(alt_trade_history[-10:]):
            if t.get('pnl', 0) < 0:
                if consec_wins > 0:
                    break
                consec_losses += 1
            elif t.get('pnl', 0) > 0:
                if consec_losses > 0:
                    break
                consec_wins += 1
            else:
                break

        if consec_losses >= 3:
            base *= 0.3
        elif consec_losses == 2:
            base *= 0.5
        elif consec_losses == 1:
            base *= 0.7
        elif consec_wins >= 2:
            base *= 1.3

    return base


def _adaptive_trail(change_24h: float) -> float:
    """Calculate trailing stop % based on coin's 24h volatility.
    Trail at half the 24h move, clamped between MIN and MAX."""
    trail = abs(change_24h) * 0.5
    return max(ALT_TRAIL_MIN, min(trail, ALT_TRAIL_MAX))


class MomentumScanner:
    def __init__(self, client, state, save_state_fn=None):
        self.client = client
        self.state = state
        self.save_state_fn = save_state_fn
        self.last_scan = 0
        self._exchange_info_cache = None

        # Initialize alt state in shared state dict
        self.state.setdefault('alt_positions', {})
        self.state.setdefault('alt_trade_history', [])
        self.state.setdefault('alt_cooldowns', {})  # {pair: sell_timestamp}

    def _save(self):
        if self.save_state_fn:
            self.save_state_fn(self.state)

    def _get_exchange_info(self):
        """Get precision info for all pairs (cached — never changes)."""
        if self._exchange_info_cache:
            return self._exchange_info_cache
        try:
            info = self.client.get_exchange_info()
            self._exchange_info_cache = info.get('TradePairs', {})
            return self._exchange_info_cache
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

    def _current_alt_exposure(self):
        """Total USD value of all alt positions."""
        total = 0
        for pair, pos in self.state.get('alt_positions', {}).items():
            total += pos.get('entry_price', 0) * pos.get('qty', 0)
        return total

    def _open_alt_position(self, coin, exchange_info):
        """Open a position in an alt coin with hybrid sizing."""
        pair = coin['pair']
        price = coin['price']
        ask = coin['ask']

        # Hybrid size based on momentum + anti-martingale
        position_size = _hybrid_size(coin['change'], self.state.get('alt_trade_history', []))

        # Cap by max exposure (20% of portfolio)
        current_exposure = self._current_alt_exposure()
        portfolio = self.state.get('current_equity', 1000000)
        max_remaining = (MAX_ALT_EXPOSURE * portfolio) - current_exposure
        if max_remaining <= 0:
            log.info(f"[AltScanner] Alt exposure cap reached (${current_exposure:,.0f}/{MAX_ALT_EXPOSURE*portfolio:,.0f})")
            return False
        position_size = min(position_size, max_remaining)

        prec = self._get_precision(pair, exchange_info)
        qty = round(position_size / ask, prec['amount_precision'])
        limit_price = round(ask, prec['price_precision'])
        log.info(f"[AltScanner] {pair}: hybrid size=${position_size:,.0f} (momentum={coin['change']*100:+.1f}%)")

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

                # Adaptive trailing stop based on coin's volatility
                trail_pct = _adaptive_trail(coin['change'])
                # Gunner TP at 1/3 of entry momentum (quick profit, runner rides the rest)
                tp_pct = max(abs(coin['change']) * 0.33, 0.01)  # min 1% TP
                tp_price = fill_price * (1 + tp_pct)

                # Record position
                self.state['alt_positions'][pair] = {
                    'entry_price': fill_price,
                    'qty': fill_qty,
                    'peak_price': fill_price,
                    'trail_pct': trail_pct,
                    'tp_price': tp_price,
                    'tp_pct': tp_pct,
                    'stop': fill_price * (1 - trail_pct),
                    'entry_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
                    'order_id': order_id,
                    'entry_change': coin['change'],
                    'price_precision': prec['price_precision'],
                    'amount_precision': prec['amount_precision'],
                }
                self._save()

                log.info(f"[AltScanner] BOUGHT {pair}: qty={fill_qty} price=${fill_price:.4f} size=${fill_qty*fill_price:.0f} trail={trail_pct:.1%}")

                # Telegram alert
                try:
                    from execution.alerts import send_alert
                    send_alert(
                        f"<b>ALT BUY {pair}</b>\n"
                        f"Price: ${fill_price:.4f}\n"
                        f"Size: ${fill_qty * fill_price:,.0f}\n"
                        f"Momentum: {coin['change']*100:+.1f}%\n"
                        f"TP: ${tp_price:.4f} (+{tp_pct*100:.1f}%)\n"
                        f"Stop: ${fill_price * (1 - trail_pct):,.4f} (-{trail_pct*100:.1f}%)"
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

        # Fetch ALL tickers once (not per-position)
        try:
            all_ticker_raw = self.client.get_ticker()
            all_ticker = all_ticker_raw.get('Data', {})
        except Exception as e:
            log.error(f"[AltScanner] Ticker fetch failed: {e}")
            return

        for pair, pos in list(positions.items()):  # list() for safe iteration
            try:
                ticker = all_ticker.get(pair, {})
                price = float(ticker.get('LastPrice', 0))
                bid = float(ticker.get('MaxBid', 0))
                change = float(ticker.get('Change', 0))

                if price <= 0 or bid <= 0:
                    continue

                entry = pos['entry_price']
                peak = pos.get('peak_price', entry)

                # Update peak and adaptive trail
                if price > peak:
                    pos['peak_price'] = price
                    new_trail = _adaptive_trail(pos.get('entry_change', change))
                    pos['trail_pct'] = new_trail
                    pos['stop'] = price * (1 - new_trail)
                    log.info(f"[AltScanner] {pair}: new peak ${price:.4f}, trail={new_trail:.1%}, stop ${pos['stop']:.4f}")

                # Runner-Gunner take profit
                tp_price = pos.get('tp_price', 0)
                if tp_price > 0 and price >= tp_price and not pos.get('gunner_fired'):
                    tp_pct = pos.get('tp_pct', 0)
                    log.info(f"[AltScanner] {pair}: GUNNER TP at ${price:.4f} (+{tp_pct*100:.1f}%). Selling 70%, runner stays.")
                    gunner_qty = round(pos['qty'] * 0.7, pos.get('amount_precision', 2))
                    if gunner_qty > 0:
                        self._close_partial(pair, bid, gunner_qty, 'GUNNER_TP')
                        pos['qty'] = round(pos['qty'] - gunner_qty, pos.get('amount_precision', 2))
                        pos['gunner_fired'] = True
                        pos['stop'] = entry * 1.002  # Breakeven INCLUDING entry fee
                        pos['tp_price'] = 0
                        log.info(f"[AltScanner] {pair}: RUNNER active. qty={pos['qty']} stop=breakeven+fee ${entry*1.001:.4f}")
                        try:
                            from execution.alerts import send_alert
                            send_alert(
                                f"<b>GUNNER FIRED {pair}</b>\n"
                                f"Sold 70% at ${bid:.4f}\n"
                                f"Runner: {pos['qty']} units\n"
                                f"Runner stop: ${entry*1.001:.4f}\n"
                                f"Free upside from here"
                            )
                        except Exception:
                            pass
                    continue

                # Check trailing stop
                if price <= pos['stop']:
                    to_close.append((pair, bid, 'TRAILING_STOP'))
                    continue

                # Check momentum reversal — real reversal, not tiny dip
                if change < MOMENTUM_REVERSAL:
                    pnl_pct = (price - entry) / entry
                    if pnl_pct > 0.003:
                        # In profit + momentum dead → take profit and move on
                        to_close.append((pair, bid, 'MOMENTUM_REVERSAL'))
                    elif pnl_pct < -0.01:
                        # Losing 1%+ AND momentum negative → cut the loss
                        to_close.append((pair, bid, 'MOMENTUM_LOSS_CUT'))

            except Exception as e:
                log.error(f"[AltScanner] {pair}: check failed: {e}")

        # Execute exits with delay between sells
        for i, (pair, bid, reason) in enumerate(to_close):
            if i > 0:
                time.sleep(2)  # Rate limit protection
            self._close_alt_position(pair, bid, reason)

        self._save()

    def _close_partial(self, pair, current_bid, qty, reason):
        """Sell a partial quantity of an alt position (gunner exit)."""
        pos = self.state['alt_positions'].get(pair)
        if not pos:
            return

        price_prec = pos.get('price_precision', 4)
        limit_price = round(current_bid, price_prec)

        try:
            order = self.client.place_order(pair, "SELL", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)

            if not order_id:
                log.error(f"[AltScanner] {pair}: PARTIAL SELL failed. Response: {order}")
                return

            exit_price = avg_price or limit_price
            entry_price = pos['entry_price']
            gross_pnl = (exit_price - entry_price) * qty
            fee_entry = entry_price * qty * 0.001
            fee_exit = exit_price * qty * 0.001
            net_pnl = gross_pnl - fee_entry - fee_exit

            log.info(f"[AltScanner] GUNNER SOLD {pair}: qty={qty} exit=${exit_price:.4f} P&L=${net_pnl:+.2f} reason={reason}")

            # Record partial exit in trade history
            self.state['alt_trade_history'].append({
                'pair': pair,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'qty': qty,
                'pnl': net_pnl,
                'pnl_pct': net_pnl / (entry_price * qty) if entry_price * qty > 0 else 0,
                'reason': reason,
                'exit_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
            })

            # Feed gunner P&L back to equity
            equity = self.state.get('current_equity', 1000000)
            self.state['current_equity'] = equity + net_pnl

            self._save()

        except Exception as e:
            log.error(f"[AltScanner] {pair}: PARTIAL SELL failed: {e}")

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
                log.error(f"[AltScanner] {pair}: SELL returned no OrderID. Retrying at lower price...")
                # Retry at slightly lower price
                try:
                    retry_price = round(current_bid * 0.999, price_prec)
                    order2 = self.client.place_order(pair, "SELL", "LIMIT", qty, retry_price)
                    detail2 = order2.get("OrderDetail", order2)
                    order_id = detail2.get("OrderID") or order2.get("OrderID")
                    if order_id:
                        avg_price = float(detail2.get("FilledAverPrice", 0) or 0)
                        log.info(f"[AltScanner] {pair}: Retry sell succeeded. id={order_id}")
                    else:
                        pos['sell_failed'] = True
                        self._save()
                        try:
                            from execution.alerts import send_alert
                            send_alert(f"<b>ALT SELL FAILED {pair}</b>\nReason: {reason}\nManual review needed.")
                        except Exception:
                            pass
                        return
                except Exception as e2:
                    log.error(f"[AltScanner] {pair}: Retry sell also failed: {e2}")
                    pos['sell_failed'] = True
                    self._save()
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

            # Feed alt P&L back to equity tracking
            equity = self.state.get('current_equity', 1000000)
            self.state['current_equity'] = equity + net_pnl

            # Remove position and set cooldown
            del self.state['alt_positions'][pair]
            self.state['alt_cooldowns'][pair] = time.time()
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
            # Skip coins in cooldown (recently sold — prevents churn)
            cooldown_time = self.state.get('alt_cooldowns', {}).get(coin['pair'], 0)
            if time.time() - cooldown_time < COIN_COOLDOWN:
                mins_left = (COIN_COOLDOWN - (time.time() - cooldown_time)) / 60
                log.info(f"[AltScanner] {coin['pair']}: in cooldown ({mins_left:.0f}min left), skipping")
                continue

            log.info(f"[AltScanner] Top mover: {coin['pair']} {coin['change']*100:+.1f}% ${coin['price']:.4f}")
            if self._open_alt_position(coin, exchange_info):
                opened += 1
                time.sleep(2)  # Small delay between orders
