"""
Layer 7: Execution + Trade Management
Owner: Kireeti (refactored for integration)

Implements entry execution, trailing exits, time exits, cooldowns, logging,
monitoring, and state persistence for BTC spot trading on Roostoo.

Uses the shared bot state dict (from data/state.py) — no separate state file.
"""

import json
import logging
import os
import sys
import time
import threading
from typing import Optional
from datetime import datetime, timedelta, timezone

log = logging.getLogger("TradingBot")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import (
    TRADING_PAIR,
    LIMIT_ORDER_TIMEOUT,
    BREAKOUT_AGGRESSIVE_OFFSET,
    PARTIAL_FILL_THRESHOLD,
    ATR_STOP_MULTIPLIER,
    TIME_EXIT_HOURS,
    FLAT_THRESHOLD,
    COOLDOWN_AFTER_STOP,
    MAKER_FEE,
    STARTING_CAPITAL,
)

TRADES_LOG_FILE = "trades_log.json"
EVENTS_LOG_FILE = "events_log.json"


def _utc_now():
    return datetime.now(timezone.utc)


def _iso_now():
    # Use format without +00:00 suffix for Python 3.9 compatibility
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f")


def _parse_iso(s: str) -> datetime:
    """Parse ISO timestamp compatible with Python 3.9 (no timezone suffix support)."""
    if not s:
        return _utc_now()
    # Strip timezone suffix if present (+00:00 or Z)
    clean = s.replace("+00:00", "").replace("Z", "")
    try:
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        try:
            return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return _utc_now()


def _write_json_line(path: str, payload: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _round_value(value: float, precision: int) -> float:
    return round(value, precision)


def _log_event(description: str, details: Optional[dict] = None):
    payload = {"timestamp": _iso_now(), "description": description}
    if details:
        payload.update(details)
    _write_json_line(EVENTS_LOG_FILE, payload)


def _parse_ticker(raw_ticker, pair: str = TRADING_PAIR) -> dict:
    """Parse Roostoo ticker handling nested Data format."""
    if isinstance(raw_ticker, dict) and 'Data' in raw_ticker:
        ticker = raw_ticker['Data'].get(pair, {})
    else:
        ticker = raw_ticker if isinstance(raw_ticker, dict) else {}
    return {
        'price': float(ticker.get('LastPrice', 0)),
        'bid': float(ticker.get('MaxBid', 0)),
        'ask': float(ticker.get('MinAsk', 0)),
    }


def _entry_price_for_signal(current_bid: float, current_ask: float,
                            signal_source: str, price_precision: int) -> float:
    # On mock exchange, MAKER orders at bid may never fill (no real counterparties)
    # Cross the spread by placing at ask to guarantee fill as taker
    if signal_source == "DONCHIAN_BREAKOUT":
        # Breakout: pay ask + small premium to ensure fill
        price = current_ask * (1 + BREAKOUT_AGGRESSIVE_OFFSET)
    else:
        # Mean reversion: place at ask (cross spread)
        price = current_ask
    return _round_value(price, price_precision)


def _calc_btc_qty(final_position_size_usd: float, current_btc_price: float, amount_precision: int) -> float:
    raw_qty = 0.0 if current_btc_price <= 0 else final_position_size_usd / current_btc_price
    return _round_value(raw_qty, amount_precision)


def _pnl_with_fees(entry_price: float, exit_price: float, qty: float) -> dict:
    gross_pnl = (exit_price - entry_price) * qty
    # Entry crosses spread (taker), exit tries limit first but may fall back to market
    from config import TAKER_FEE
    fee_entry = entry_price * qty * TAKER_FEE
    fee_exit = exit_price * qty * TAKER_FEE  # Worst case: taker on both sides
    total_fees = fee_entry + fee_exit
    net_pnl = gross_pnl - total_fees
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
    return {
        "pnl_usd_gross": gross_pnl,
        "pnl_usd_net": net_pnl,
        "pnl_pct": pnl_pct,
        "fees_paid_usd": total_fees,
    }


def _poll_order(client, order_id: str, timeout_seconds: int, sleep_seconds: int = 10) -> dict:
    deadline = time.time() + timeout_seconds
    last = {"status": "NEW", "filled_qty": 0.0, "avg_price": 0.0}
    while time.time() < deadline:
        try:
            resp = client.query_orders(pair=TRADING_PAIR)
            # Roostoo returns: {'OrderMatched': [{'OrderID': ..., 'Status': ..., ...}]}
            orders_list = resp.get('OrderMatched', []) if isinstance(resp, dict) else []
            if isinstance(orders_list, list):
                for row in orders_list:
                    if str(row.get("OrderID")) == str(order_id):
                        status_raw = (row.get("Status") or "").upper()
                        filled_qty = float(row.get("FilledQuantity", 0) or 0)
                        avg_price = float(row.get("FilledAverPrice", 0) or 0)
                        ordered_qty = float(row.get("Quantity", 0) or 0)
                        # Roostoo uses PENDING/FILLED/CANCELED
                        if status_raw == "FILLED" or status_raw == "COMPLETED" or (ordered_qty > 0 and filled_qty >= ordered_qty):
                            return {"status": "FILLED", "filled_qty": filled_qty, "avg_price": avg_price}
                        elif filled_qty > 0 and filled_qty < ordered_qty:
                            return {"status": "PARTIALLY_FILLED", "filled_qty": filled_qty, "avg_price": avg_price}
                        last = {"status": status_raw, "filled_qty": filled_qty, "avg_price": avg_price}
        except Exception:
            pass
        time.sleep(sleep_seconds)
    return last


def _cooldown_active(state: dict) -> bool:
    cooldown_until = state.get("cooldown_until")
    if not cooldown_until:
        return False
    try:
        return datetime.utcnow() < _parse_iso(cooldown_until)
    except (ValueError, TypeError):
        return False


def _set_cooldown(state: dict, seconds: int):
    state["cooldown_until"] = (_utc_now() + timedelta(seconds=seconds)).isoformat()


class TradeExecutor:
    def __init__(self, client, price_precision: int, amount_precision: int,
                 state: dict = None, save_state_fn=None):
        self.client = client
        self.price_precision = price_precision
        self.amount_precision = amount_precision
        # Use the shared bot state — no separate bot_state.json
        self.state = state if state is not None else {}
        self.save_state_fn = save_state_fn
        self.lock = threading.Lock()
        self.stop_thread = None

        # Ensure executor-specific fields exist in shared state
        self.state.setdefault("exec_position_open", False)
        self.state.setdefault("exec_btc_qty", 0.0)
        self.state.setdefault("exec_entry_price", 0.0)
        self.state.setdefault("exec_stop", 0.0)
        self.state.setdefault("exec_open_time", None)
        self.state.setdefault("exec_regime", None)
        self.state.setdefault("exec_signal_source", None)
        self.state.setdefault("exec_take_profit", None)
        self.state.setdefault("drawdown_alerts_fired", {"2pct": False, "5pct": False, "8pct": False})

    def _save(self):
        if self.save_state_fn:
            self.save_state_fn(self.state)

    def _log_trade(self, entry: dict):
        _write_json_line(TRADES_LOG_FILE, entry)
        # Also append to shared state trade_history
        if 'trade_history' not in self.state:
            self.state['trade_history'] = []
        self.state['trade_history'].append({
            'pnl': entry.get('pnl_usd_net', 0),
            'pnl_pct': entry.get('pnl_pct', 0),
            'exit_time': entry.get('timestamp', ''),
            'exit_reason': entry.get('exit_reason', ''),
            'entry_price': entry.get('entry_price', 0),
            'exit_price': entry.get('exit_price', 0),
        })

    def _log_event(self, description: str, details: Optional[dict] = None):
        _log_event(description, details)

    def has_position(self) -> bool:
        return bool(self.state.get("exec_position_open"))

    def _alert_drawdown_crossings(self):
        equity = self.state.get("current_equity", STARTING_CAPITAL)
        peak = self.state.get("peak_equity", STARTING_CAPITAL)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        alerts = self.state.get("drawdown_alerts_fired", {"2pct": False, "5pct": False, "8pct": False})

        from execution.alerts import alert_drawdown
        if dd >= 0.02 and not alerts.get("2pct"):
            alert_drawdown("LEVEL 1 (2%)", dd, equity)
            alerts["2pct"] = True
        if dd >= 0.05 and not alerts.get("5pct"):
            alert_drawdown("LEVEL 2 (5%)", dd, equity)
            alerts["5pct"] = True
        if dd >= 0.08 and not alerts.get("8pct"):
            alert_drawdown("LEVEL 3 (8%)", dd, equity)
            alerts["8pct"] = True
        self.state["drawdown_alerts_fired"] = alerts

    def _enter_position(self, final_position_size_usd: float, current_bid: float,
                        current_ask: float, current_btc_price: float, signal_source: str):
        if self.state.get("exec_position_open"):
            self._log_event("BUY signal ignored: position already open")
            return None

        qty = _calc_btc_qty(final_position_size_usd, current_btc_price, self.amount_precision)
        if qty == 0:
            self._log_event("Position too small after rounding, skipping")
            return None

        limit_price = _entry_price_for_signal(current_bid, current_ask, signal_source, self.price_precision)

        # Attempt 1: place limit order
        try:
            order = self.client.place_order(TRADING_PAIR, "BUY", "LIMIT", qty, limit_price)
        except Exception as e:
            self._log_event(f"Order placement failed: {e}")
            return None

        # Roostoo returns OrderDetail with fill info in place_order response
        detail = order.get("OrderDetail", order)
        order_id = detail.get("OrderID") or order.get("OrderID")
        filled_qty = float(detail.get("FilledQuantity", 0) or 0)
        avg_price = float(detail.get("FilledAverPrice", 0) or 0)
        order_status = (detail.get("Status") or "").upper()
        _log_event(f"BUY order placed", {
            "order_id": order_id, "status": order_status,
            "qty": qty, "price": limit_price,
            "filled": filled_qty, "fill_price": avg_price,
            "success": order.get("Success"),
        })

        # Check if immediately filled (Roostoo returns FILLED instantly for taker orders)
        if order_status in ("FILLED", "COMPLETED") or (filled_qty > 0 and filled_qty >= qty):
            return {"order_id": order_id, "qty": qty, "fill_price": avg_price or limit_price}

        # Poll for fill (60s max, not 120s — keeps main loop responsive)
        poll_timeout = min(LIMIT_ORDER_TIMEOUT, 60)
        status = _poll_order(self.client, order_id, poll_timeout, sleep_seconds=5)
        if status["status"] == "FILLED":
            return {"order_id": order_id, "qty": qty, "fill_price": status["avg_price"] or limit_price}

        # Handle partial fill
        if status["status"] == "PARTIALLY_FILLED":
            filled_pct = status["filled_qty"] / qty if qty > 0 else 0
            if filled_pct > PARTIAL_FILL_THRESHOLD:
                self._log_event("Partial fill ACCEPTED", {"filled_pct": filled_pct})
                return {"order_id": order_id, "qty": status["filled_qty"],
                        "fill_price": status["avg_price"] or limit_price}
            try:
                self.client.cancel_order(order_id)
                if status["filled_qty"] > 0:
                    self.client.place_order(TRADING_PAIR, "SELL", "MARKET", status["filled_qty"], 0)
            except Exception:
                pass
            _set_cooldown(self.state, 60)
            self._save()
            self._log_event("Partial fill REJECTED", {"filled_pct": filled_pct})
            return None

        # Attempt 2: cancel and retry with updated price
        try:
            self.client.cancel_order(order_id)
            raw_ticker = self.client.get_ticker(TRADING_PAIR)
            parsed = _parse_ticker(raw_ticker)
            bid = parsed['bid'] if parsed['bid'] > 0 else current_bid
            ask = parsed['ask'] if parsed['ask'] > 0 else current_ask
            limit_price = _entry_price_for_signal(bid, ask, signal_source, self.price_precision)
            order = self.client.place_order(TRADING_PAIR, "BUY", "LIMIT", qty, limit_price)

            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            filled_qty = float(detail.get("FilledQuantity", 0) or 0)
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)
            if (detail.get("Status") or "").upper() in ("FILLED", "COMPLETED") or filled_qty >= qty:
                return {"order_id": order_id, "qty": qty, "fill_price": avg_price or limit_price}

            status = _poll_order(self.client, order_id, poll_timeout, sleep_seconds=5)
            if status["status"] == "FILLED":
                return {"order_id": order_id, "qty": qty, "fill_price": status["avg_price"] or limit_price}
            self.client.cancel_order(order_id)
        except Exception as e:
            self._log_event(f"Retry failed: {e}")

        self._log_event("Signal abandoned after 2 attempts — price moved away.")
        return None

    def _exit_position(self, exit_reason: str, current_bid: float):
        if not self.state.get("exec_position_open"):
            self._log_event("SELL ignored: no position open")
            return None

        qty = self.state.get("exec_btc_qty", 0.0)
        if qty <= 0:
            self._log_event("SELL ignored: invalid position qty")
            return None

        # Sell at bid to cross spread (taker) — fills instantly on mock exchange
        limit_price = _round_value(current_bid, self.price_precision)
        try:
            order = self.client.place_order(TRADING_PAIR, "SELL", "LIMIT", qty, limit_price)
            detail = order.get("OrderDetail", order)
            order_id = detail.get("OrderID") or order.get("OrderID")
            filled_qty = float(detail.get("FilledQuantity", 0) or 0)
            avg_price = float(detail.get("FilledAverPrice", 0) or 0)
            order_status = (detail.get("Status") or "").upper()
            log.info(f"SELL order placed: id={order_id} status={order_status} filled={filled_qty} price={avg_price}")

            # Check if immediately filled (expected on mock exchange)
            if order_status in ("FILLED", "COMPLETED") or (filled_qty > 0 and filled_qty >= qty):
                exit_price = avg_price or limit_price
            else:
                # Poll for fill
                status = _poll_order(self.client, order_id, 60, sleep_seconds=5)
                if status["status"] == "FILLED":
                    exit_price = status.get("avg_price") or limit_price
                else:
                    # Cancel and retry at fresh bid
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                    raw_ticker = self.client.get_ticker(TRADING_PAIR)
                    parsed = _parse_ticker(raw_ticker)
                    retry_price = _round_value(parsed['bid'] if parsed['bid'] > 0 else current_bid, self.price_precision)
                    retry = self.client.place_order(TRADING_PAIR, "SELL", "LIMIT", qty, retry_price)
                    retry_detail = retry.get("OrderDetail", retry)
                    retry_status = (retry_detail.get("Status") or "").upper()
                    retry_avg = float(retry_detail.get("FilledAverPrice", 0) or 0)
                    retry_filled = float(retry_detail.get("FilledQuantity", 0) or 0)
                    retry_id = retry_detail.get("OrderID") or retry.get("OrderID")
                    log.info(f"SELL retry: id={retry_id} status={retry_status} filled={retry_filled} price={retry_avg}")

                    if retry_status in ("FILLED", "COMPLETED") or (retry_filled > 0 and retry_filled >= qty):
                        exit_price = retry_avg or retry_price
                    else:
                        # Poll retry order
                        retry_poll = _poll_order(self.client, retry_id, 30, sleep_seconds=5)
                        if retry_poll["status"] == "FILLED":
                            exit_price = retry_poll.get("avg_price") or retry_price
                        else:
                            log.error(f"SELL failed after 2 attempts. Order {retry_id} status={retry_poll['status']}")
                            from execution.alerts import send_alert
                            send_alert(f"<b>SELL FAILED</b>\n2 attempts failed. Manual intervention needed.")
                            return None
        except Exception as e:
            self._log_event(f"Exit order failed: {e}")
            log.error(f"EXIT ORDER FAILED: {e}", exc_info=True)
            return None
        entry_price = self.state.get("exec_entry_price", 0.0)
        pnl = _pnl_with_fees(entry_price, exit_price, qty)

        # Sync real equity from exchange after sell (avoids drift from mid-trade sync)
        try:
            balance = self.client.get_balance()
            wallet = balance.get('SpotWallet', balance.get('Data', {}))
            if isinstance(wallet, dict) and 'USD' in wallet:
                usd_free = float(wallet['USD'].get('Free', 0))
                if usd_free > 0:
                    equity = usd_free
                    log.info(f"Post-sell equity synced from API: ${equity:,.0f}")
                else:
                    equity = self.state.get("current_equity", STARTING_CAPITAL) + pnl["pnl_usd_net"]
            else:
                equity = self.state.get("current_equity", STARTING_CAPITAL) + pnl["pnl_usd_net"]
        except Exception:
            # Geo-blocked on EC2 — fall back to calculated equity
            equity = self.state.get("current_equity", STARTING_CAPITAL) + pnl["pnl_usd_net"]
        self.state["current_equity"] = equity
        peak = self.state.get("peak_equity", equity)
        if equity > peak:
            self.state["peak_equity"] = equity

        # Clear position
        self.state["exec_position_open"] = False
        self.state["exec_btc_qty"] = 0.0
        self.state["exec_entry_price"] = 0.0
        self.state["exec_stop"] = 0.0
        self.state["exec_open_time"] = None
        self.state["exec_take_profit"] = None
        self.state["positions"] = []

        # Cooldown after stop-loss
        if "STOP" in exit_reason:
            _set_cooldown(self.state, COOLDOWN_AFTER_STOP)

        # Build trade log record
        trade_entry = {
            "timestamp": _iso_now(),
            "regime": self.state.get("exec_regime"),
            "signal_source": self.state.get("exec_signal_source"),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "position_size_btc": qty,
            "pnl_usd_gross": pnl["pnl_usd_gross"],
            "pnl_usd_net": pnl["pnl_usd_net"],
            "pnl_pct": pnl["pnl_pct"],
            "fees_paid_usd": pnl["fees_paid_usd"],
            "portfolio_value_after": equity,
        }
        self._log_trade(trade_entry)
        self._save()

        # Send Telegram alerts
        from execution.alerts import alert_trade, alert_stop_loss
        if "STOP" in exit_reason:
            alert_stop_loss(entry_price, exit_price, pnl["pnl_pct"], pnl["pnl_usd_net"])
        else:
            alert_trade("SELL", exit_price, qty * exit_price, self.state.get("exec_regime", ""),
                       exit_reason, 0, 0)

        self._alert_drawdown_crossings()
        return trade_entry

    def _evaluate_trailing(self, entry_price: float, current_price: float,
                          atr_14: float, regime: str, current_stop: float):
        if regime == "SIDEWAYS":
            # R:R = 1:2+ for scoring formula (small losses, bigger wins)
            # SL = 0.7x ATR (tight — protects Calmar/Sortino)
            # TP = 1.5x ATR (lets winners run)
            take_profit = entry_price + 1.5 * atr_14
            initial_stop = entry_price - 0.7 * atr_14
            profit = current_price - entry_price

            # Trailing logic for mean reversion:
            # - Once 0.5x ATR in profit: move stop to breakeven
            # - Once 0.7x ATR in profit: trail at 0.5x ATR below price
            # This prevents winners from turning into losers
            if profit >= 0.7 * atr_14:
                # Trailing mode: stop follows price at 0.5x ATR distance
                trailing_stop = current_price - 0.5 * atr_14
                stop = max(current_stop, trailing_stop)
            elif profit >= 0.5 * atr_14:
                # Breakeven mode: don't lose money on a winning trade
                stop = max(current_stop, entry_price)
            else:
                # Initial stop
                stop = max(current_stop, initial_stop)

            if current_price >= take_profit:
                return "FIXED_TAKE_PROFIT", stop, take_profit
            if current_price <= stop:
                return "STOP_LOSS_SIDEWAYS", stop, take_profit
            return None, stop, take_profit

        # TRENDING: trailing stop at 1.5x ATR (no fixed TP — let trends run)
        candidate = current_price - ATR_STOP_MULTIPLIER * atr_14
        new_stop = max(current_stop, candidate)
        if current_price <= new_stop:
            return "TRAILING_STOP", new_stop, None
        return None, new_stop, None

    def _should_time_exit(self, entry_price: float, current_price: float) -> bool:
        open_time = self.state.get("exec_open_time")
        if not open_time:
            return False
        try:
            hours = (datetime.utcnow() - _parse_iso(open_time)).total_seconds() / 3600
        except (ValueError, TypeError):
            return False
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        # Only time-exit if position is negative. If positive (even slightly),
        # let it run — trailing stop or TP will handle the exit.
        # Mean reversion trades sometimes need >4hr to snap back.
        return hours >= TIME_EXIT_HOURS and pnl_pct < 0.0

    def start_stop_monitor(self, atr_14: float, regime: str):
        def _loop():
            log.info("[StopMonitor] Thread started. Checking every 10s.")
            while True:
                try:
                    # Check if position is still open (quick lock)
                    with self.lock:
                        if not self.state.get("exec_position_open"):
                            log.info("[StopMonitor] Position closed, thread exiting.")
                            return
                        entry = self.state.get("exec_entry_price", 0.0)
                        current_stop = self.state.get("exec_stop", 0.0)

                    # API call OUTSIDE lock — doesn't block main thread
                    try:
                        raw_ticker = self.client.get_ticker(TRADING_PAIR)
                        parsed = _parse_ticker(raw_ticker)
                        current_price = parsed['price']
                        current_bid = parsed['bid'] if parsed['bid'] > 0 else current_price
                    except Exception as e:
                        log.warning(f"[StopMonitor] Ticker fetch failed: {e}")
                        time.sleep(10)
                        continue

                    if current_price <= 0:
                        time.sleep(10)
                        continue

                    # Evaluate exit conditions (quick lock for state read)
                    with self.lock:
                        if not self.state.get("exec_position_open"):
                            return

                        reason, new_stop, tp = self._evaluate_trailing(
                            entry, current_price, atr_14, regime, current_stop
                        )
                        self.state["exec_stop"] = new_stop
                        self.state["exec_take_profit"] = tp

                        exit_reason = None
                        if reason:
                            exit_reason = reason
                        elif self._should_time_exit(entry, current_price):
                            exit_reason = "TIME_EXIT_FLAT"

                    # Execute exit OUTSIDE the evaluation lock section
                    # If sell fails, DON'T exit thread — retry next iteration
                    if exit_reason:
                        log.info(f"[StopMonitor] Exit triggered: {exit_reason} at ${current_price:.2f}")
                        with self.lock:
                            result = self._exit_position(exit_reason, current_bid)
                        if result:
                            log.info(f"[StopMonitor] Exit successful. Thread exiting.")
                            return
                        else:
                            log.error(f"[StopMonitor] Exit FAILED for {exit_reason}. Will retry in 10s.")
                            from execution.alerts import send_alert
                            send_alert(f"<b>EXIT FAILED</b>\nReason: {exit_reason}\nRetrying in 10s...")

                    time.sleep(10)
                except Exception as e:
                    log.error(f"[StopMonitor] Unexpected error: {e}", exc_info=True)
                    time.sleep(10)

        self.stop_thread = threading.Thread(target=_loop, daemon=True)
        self.stop_thread.start()

    def execute_trade(self, final_position_size_usd: float, current_btc_price: float,
                      current_bid: float, current_ask: float, atr_14: float, regime: str,
                      signal_source: str, entry_context: dict):
        # Quick lock check for cooldown/position, then release for API calls
        with self.lock:
            if _cooldown_active(self.state):
                self._log_event("In post-stop cooldown. Skipping signal.")
                return
            if self.state.get("exec_position_open"):
                self._log_event("BUY signal ignored: position already open")
                return

        # Order placement happens OUTSIDE lock — doesn't block stop monitor
        entry = self._enter_position(
            final_position_size_usd, current_bid, current_ask, current_btc_price, signal_source
        )
        if not entry:
            with self.lock:
                self._save()
            return

        with self.lock:
            # Save entry details in shared state
            self.state["exec_position_open"] = True
            self.state["exec_btc_qty"] = entry["qty"]
            self.state["exec_entry_price"] = entry["fill_price"]
            self.state["exec_open_time"] = _iso_now()
            self.state["exec_regime"] = regime
            self.state["exec_signal_source"] = signal_source
            # Set initial stop based on regime (SIDEWAYS=0.7x, TRENDING=1.5x)
            if regime == "SIDEWAYS":
                self.state["exec_stop"] = entry["fill_price"] - 0.7 * atr_14
            else:
                self.state["exec_stop"] = entry["fill_price"] - ATR_STOP_MULTIPLIER * atr_14
            self.state["exec_take_profit"] = None

            # Also update the positions list for compatibility with main.py
            self.state["positions"] = [{
                'order_id': entry['order_id'],
                'entry_price': entry['fill_price'],
                'quantity': entry['qty'],
                'entry_time': _iso_now(),
            }]

            self._save()

            from execution.alerts import alert_trade
            alert_trade("BUY", entry['fill_price'], final_position_size_usd, regime,
                       signal_source, entry_context.get("timeframe_total_score", 0),
                       entry_context.get("xgboost_probability", 0))

            # Start trailing stop monitor
            self.start_stop_monitor(atr_14, regime)
            self._alert_drawdown_crossings()

    def execute_sell(self, current_bid: float, reason: str = "L2_SIGNAL"):
        """Execute a SELL from main loop (L2 signal or manual exit)."""
        with self.lock:
            return self._exit_position(reason, current_bid)


__all__ = [
    "TradeExecutor",
    "_calc_btc_qty",
    "_entry_price_for_signal",
    "_pnl_with_fees",
    "_parse_ticker",
]
