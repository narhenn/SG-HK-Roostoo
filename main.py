"""
Main Bot Loop — Wires all 7 layers together.
Owner: Narhen

Flow every 60 seconds:
1. Fetch price → store tick
2. Layer 1: Regime Detection
3. Layer 2: Signal Generation
4. Layer 3: Reversal Blocker
5. Layer 4: Multi-Timeframe Filter (cheap filter first)
6. Layer 5: XGBoost Confirmation (expensive filter last)
7. Layer 6: Position Sizing
8. Layer 7: Execute
9. Save state, log, repeat
"""

import time
import logging
import os
import statistics
from datetime import datetime

from config import (
    TRADING_PAIR, TRADE_INTERVAL_SECONDS, LOG_FILE, LOG_LEVEL,
    XGBOOST_MIN_PROBABILITY, LOGS_DIR, STARTING_CAPITAL,
    PROTECT_DAYS_BEFORE_END, RSI_OVERSOLD,
    ADX_TREND_THRESHOLD, ADX_NOTREND_THRESHOLD,
    CONSERVATIVE_DAYS,
    ENABLE_MULTICOIN, ALT_CAPITAL_PCT,
)
from roostoo_client import RoostooClient
from data.candle_builder import CandleBuilder
from data.state import save_state, load_state, default_state
from data.fetchers import fetch_fear_greed, fetch_funding_rate, fetch_market_breadth, get_order_precision
from strategy.regime import detect_regime, calculate_atr
from strategy.signals import generate_signal
from strategy.reversal_blocker import check_reversal_block
from strategy.timeframe import check_timeframe
from strategy.ml_model import xgboost_confirm, engineer_features

# Try to load Pranati's live predictor (log not yet defined, use print)
try:
    from live_predictor import get_xgboost_signal
    _USE_PRANATI_MODEL = True
    print("[INFO] Pranati's XGBoost model loaded successfully")
except Exception as e:
    _USE_PRANATI_MODEL = False
    print(f"[WARN] Pranati's model not available, using stub: {e}")

from risk.position_sizer import compute_position_size
from execution.executor import TradeExecutor
from execution.alerts import (
    alert_trade, alert_stop_loss, alert_startup,
    alert_drawdown, alert_kill_switch, alert_error, alert_daily_summary,
    send_alert,
)

# ── Setup Logging ──
os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("TradingBot")


class TradingBot:
    def __init__(self):
        self.client = RoostooClient()
        self.candles = CandleBuilder()
        self.state = load_state() or default_state()
        self.running = False

        precision = get_order_precision(self.client)
        self.executor = TradeExecutor(
            self.client,
            price_precision=precision.get('price_precision', 2),
            amount_precision=precision.get('amount_precision', 5),
            state=self.state,
            save_state_fn=save_state,
        )

        # External data (refreshed periodically)
        self.fear_greed = 50
        self.funding_rate = 0.0
        self.breadth = 0.5
        self.last_regime_check = 0
        self.last_external_fetch = 0
        self.last_daily_summary = 0
        self.last_heartbeat = 0

    def bootstrap(self):
        """Cold start: load historical data."""
        loaded = self.candles.bootstrap()
        if loaded:
            log.info(f"Bootstrapped with historical data: {len(self.candles.df_1h)} 1H candles")
        else:
            log.warning("No historical data found. Bot will build history from live ticks.")
            log.warning(f"Expected file: {os.path.abspath('data/btc_1h_90days.csv')}")

    def fetch_external_data(self):
        """Fetch Fear & Greed, funding rate, breadth (every hour)."""
        now = time.time()
        if now - self.last_external_fetch < 3600:
            return

        try:
            self.fear_greed = fetch_fear_greed()
            self.funding_rate = fetch_funding_rate()
            self.breadth = fetch_market_breadth()
            self.last_external_fetch = now
            log.info(f"External data: F&G={self.fear_greed}, Funding={self.funding_rate:.6f}, Breadth={self.breadth:.2f}")
        except Exception as e:
            log.error(f"Error fetching external data: {e}")

    def is_halted(self) -> bool:
        """Check if bot is in halt/cooldown mode."""
        halt_until = self.state.get('halt_until')
        if halt_until:
            if datetime.utcnow().isoformat() < halt_until:
                return True
            else:
                self.state['halt_until'] = None
                self.state['_kill_switch_fired'] = False
        return False

    def is_cooled_down(self) -> bool:
        """Check if post-stop-loss cooldown is active."""
        cooldown_until = self.state.get('cooldown_until')
        if cooldown_until:
            if datetime.utcnow().isoformat() < cooldown_until:
                return True
            else:
                self.state['cooldown_until'] = None
        return False

    def send_heartbeat(self):
        """Send a heartbeat to Telegram every 6 hours so team knows bot is alive."""
        now = time.time()
        if now - self.last_heartbeat < 21600:
            return

        equity = self.state.get('current_equity', STARTING_CAPITAL)
        cycle = self.state.get('cycle_count', 0)
        positions = self.state.get('positions', [])
        pos_status = "IN POSITION" if positions else "CASH"

        df_1h = self.candles.get_df('1h')
        regime = detect_regime(self.candles.df_1h, self.fear_greed, self.funding_rate, self.breadth) if len(df_1h) > 55 else 'UNKNOWN'

        try:
            raw_t = self.client.get_ticker(TRADING_PAIR)
            if isinstance(raw_t, dict) and 'Data' in raw_t:
                hb_price = float(raw_t['Data'].get(TRADING_PAIR, {}).get('LastPrice', 0))
            else:
                hb_price = self.candles.get_current_price()
        except Exception:
            hb_price = self.candles.get_current_price()

        send_alert(
            f"<b>HEARTBEAT</b>\n"
            f"Bot is alive and running\n"
            f"Cycle: #{cycle}\n"
            f"BTC: ${hb_price:,.2f}\n"
            f"Equity: ${equity:,.0f}\n"
            f"Status: {pos_status}\n"
            f"Regime: {regime}\n"
            f"F&G: {self.fear_greed}\n"
            f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
        )
        self.last_heartbeat = now
        log.info("Heartbeat sent to Telegram")

    def send_daily_summary(self):
        """Send daily summary to Telegram (once every 24 hours)."""
        now = time.time()
        if now - self.last_daily_summary < 86400:
            return

        equity = self.state.get('current_equity', STARTING_CAPITAL)
        peak = self.state.get('peak_equity', STARTING_CAPITAL)
        history = self.state.get('trade_history', [])

        today = datetime.utcnow().strftime('%Y-%m-%d')
        today_trades = [t for t in history if t.get('exit_time', '').startswith(today)]
        wins = len([t for t in today_trades if t.get('pnl', 0) > 0])
        losses = len(today_trades) - wins
        pnl_today = sum(t.get('pnl', 0) for t in today_trades)

        alert_daily_summary(equity, peak, len(today_trades), wins, losses, pnl_today)
        self.last_daily_summary = now
        log.info("Daily summary sent to Telegram")

    def has_position(self) -> bool:
        """Check if we currently hold BTC."""
        return bool(self.state.get('exec_position_open')) or len(self.state.get('positions', [])) > 0

    def run_cycle(self):
        """Run one complete trading cycle through all 7 layers."""
        self.state['cycle_count'] = self.state.get('cycle_count', 0) + 1
        cycle = self.state['cycle_count']

        # ── Fetch current price ──
        try:
            raw_ticker = self.client.get_ticker(TRADING_PAIR)
            if isinstance(raw_ticker, dict) and 'Data' in raw_ticker:
                ticker = raw_ticker['Data'].get(TRADING_PAIR, {})
            else:
                ticker = raw_ticker
            price = float(ticker.get('LastPrice', 0))
            bid = float(ticker.get('MaxBid', 0))
            ask = float(ticker.get('MinAsk', 0))
            volume = float(ticker.get('CoinTradeValue', 0))
        except Exception as e:
            log.error(f"Cycle {cycle}: Error fetching price: {e}")
            return

        if price <= 0:
            log.warning(f"Cycle {cycle}: Invalid price {price}, skipping")
            return

        # ── CHANGE 1: Guard against zero bid/ask ──
        if ask <= 0 or bid <= 0:
            log.warning(f"Cycle {cycle}: Invalid bid/ask (bid={bid:.2f}, ask={ask:.2f}), skipping")
            return

        # Store tick
        self.candles.add_tick(price, volume, bid, ask)

        # ── Competition end-date protection ──
        competition_end = datetime(2026, 3, 31, 23, 59)
        days_left = (competition_end - datetime.utcnow()).total_seconds() / 86400
        if days_left <= 1 and self.has_position():
            log.info(f"Cycle {cycle}: FINAL DAY — closing position to lock score")
            self.executor.execute_sell(bid, reason="COMPETITION_END")
            return
        if days_left <= PROTECT_DAYS_BEFORE_END and not self.has_position():
            log.info(f"Cycle {cycle}: {days_left:.1f} days left — no new positions")
            self.candles.add_tick(price, volume, bid, ask)
            return

        # ── Check halts ──
        if self.is_halted():
            log.info(f"Cycle {cycle}: Bot is HALTED. Price=${price:.2f}. Collecting data only.")
            return

        # ── Fetch external data (hourly) ──
        self.fetch_external_data()

        df_1h = self.candles.get_df('1h')
        df_1h_base = self.candles.df_1h

        if df_1h.empty or len(df_1h) < 55:
            log.info(f"Cycle {cycle}: Waiting for data ({len(df_1h)} 1H candles, need 55+). Price=${price:.2f}")
            return

        # ══════════════════════════════════════════
        # LAYER 1: REGIME DETECTION
        # ══════════════════════════════════════════
        regime = detect_regime(df_1h_base, self.fear_greed, self.funding_rate, self.breadth)

        from strategy.regime import calculate_adx, calculate_bb_width
        _adx = calculate_adx(df_1h_base)
        _atr_s = calculate_atr(df_1h_base)
        _atr_val = float(_atr_s.iloc[-1]) if not _atr_s.empty else 0
        _atr_pct = float((_atr_s.dropna() < _atr_val).mean()) if len(_atr_s.dropna()) > 50 else 0.5
        _bb_w = calculate_bb_width(df_1h_base)
        log.info(
            f"Cycle {cycle}: L1 REGIME={regime} | ADX={_adx:.1f}(trend>{ADX_TREND_THRESHOLD}/side<{ADX_NOTREND_THRESHOLD}) "
            f"ATR=${_atr_val:.0f} ATR_pct={_atr_pct:.2f}(vol>0.85) "
            f"F&G={self.fear_greed} Breadth={self.breadth:.2f} | Price=${price:.2f}"
        )

        # ══════════════════════════════════════════
        # LAYER 2: SIGNAL GENERATION
        # ══════════════════════════════════════════
        signal = generate_signal(df_1h, regime)
        direction = signal['direction']
        source = signal['source']

        close = df_1h['close']
        _rsi_delta = close.diff()
        _rsi_gain = _rsi_delta.where(_rsi_delta > 0, 0.0).rolling(14).mean()
        _rsi_loss = (-_rsi_delta.where(_rsi_delta < 0, 0.0)).rolling(14).mean()
        _rsi_rs = _rsi_gain / _rsi_loss.replace(0, 1e-10)
        _rsi_val = float(100 - (100 / (1 + _rsi_rs)).iloc[-1])
        _mean20 = float(close.rolling(20).mean().iloc[-1])
        _std20 = float(close.rolling(20).std().iloc[-1])
        _zscore = (float(close.iloc[-1]) - _mean20) / _std20 if _std20 > 0 else 0
        _lower_bb = _mean20 - 2 * _std20
        log.info(
            f"Cycle {cycle}: L2 Signal={direction}({source}) | RSI={_rsi_val:.1f}(thr<{RSI_OVERSOLD}) "
            f"Z={_zscore:.2f}(thr<-1.5) LowerBB=${_lower_bb:.0f} Price=${price:.0f}"
        )

        if direction == 'HOLD':
            return

        trade_history = self.state.get('trade_history', [])
        total_pnl = sum(t.get('pnl', 0) for t in trade_history)
        equity = self.state.get('current_equity', STARTING_CAPITAL)
        pnl_pct = total_pnl / equity if equity > 0 else 0
        if pnl_pct > 0.005 and len(trade_history) >= 3:
            self.state['_protect_mode'] = True
            log.info(f"Cycle {cycle}: PROTECT MODE active (P&L: {pnl_pct:.2%})")
        else:
            self.state['_protect_mode'] = False

        if direction == 'SELL' and not self.has_position():
            log.info(f"Cycle {cycle}: SELL signal but no position. Ignoring.")
            return

        if direction == 'SELL' and self.has_position():
            log.info(
                f"Cycle {cycle}: EXECUTING SELL | "
                f"Regime={regime} | Source={source} | Price=${price:.2f}"
            )
            self.executor.execute_sell(bid, reason=f"L2_{source}")
            return

        # S2: One position at a time for BTC
        if direction == 'BUY' and self.has_position():
            log.info(f"Cycle {cycle}: BUY signal but already in position. Skipping.")
            return

        # Check cooldown
        if self.is_cooled_down():
            log.info(f"Cycle {cycle}: In cooldown after stop-loss. Skipping.")
            return

        # ══════════════════════════════════════════
        # LAYER 3: REVERSAL BLOCKER
        # ══════════════════════════════════════════
        prices_list = df_1h['close'].tolist()[-20:]
        volumes_list = df_1h['volume'].tolist()[-20:] if 'volume' in df_1h.columns else [0] * 20
        current_spread = (ask - bid) / price if price > 0 and ask > 0 and bid > 0 else 0
        blocker_result = check_reversal_block(prices_list, volumes_list, current_spread, direction)
        log.info(
            f"Cycle {cycle}: L3 {blocker_result.get('decision')} | "
            f"spike={blocker_result.get('check1_extreme_move')} "
            f"spread={blocker_result.get('check2_spread')} "
            f"volume={blocker_result.get('check3_volume')} "
            f"cooldown={blocker_result.get('cooldown_remaining')}s"
        )
        if blocker_result.get('decision') == 'BLOCK':
            return

        # ══════════════════════════════════════════
        # LAYER 4: MULTI-TIMEFRAME FILTER
        # ── CHANGE 2: pass signal source for oversold override ──
        # ══════════════════════════════════════════
        df_4h = self.candles.get_df('4h')
        df_daily = self.candles.get_df('daily')

        tf_result = check_timeframe(df_1h, df_4h, df_daily, regime=regime,
                                    signal_source=signal.get('source', ''))
        log.info(
            f"Cycle {cycle}: L4 {'PASS' if tf_result['pass'] else 'BLOCKED'} | "
            f"1H={tf_result['scores']['1h']} 4H={tf_result['scores']['4h']} "
            f"Daily={tf_result['scores']['daily']} Total={tf_result['score']} "
            f"Mult={tf_result['multiplier']} MinNeeded={'1' if regime=='SIDEWAYS' else '2'}"
        )
        if not tf_result['pass']:
            return

        # ══════════════════════════════════════════
        # LAYER 5: XGBOOST CONFIRMATION
        # ══════════════════════════════════════════
        if _USE_PRANATI_MODEL:
            price_history = df_1h.to_dict('records')
            current_spread = (ask - bid) / price if price > 0 and ask > 0 and bid > 0 else 0.001
            xgb_decision, xgb_prob = get_xgboost_signal(
                price_history, breadth=self.breadth,
                spread_proxy=current_spread, threshold=XGBOOST_MIN_PROBABILITY
            )
        else:
            features = engineer_features(df_1h)
            xgb_prob = xgboost_confirm(features)

        min_prob = 0.70 if self.state.get('_protect_mode') else XGBOOST_MIN_PROBABILITY
        log.info(
            f"Cycle {cycle}: L5 {'PASS' if xgb_prob >= min_prob else 'BLOCKED'} | "
            f"XGB_prob={xgb_prob:.3f} threshold={min_prob} "
            f"{'PROTECT_MODE' if self.state.get('_protect_mode') else 'NORMAL'}"
        )
        if xgb_prob < min_prob:
            return

        # ══════════════════════════════════════════
        # LAYER 6: POSITION SIZING
        # ══════════════════════════════════════════
        equity = self.state.get('current_equity', STARTING_CAPITAL)
        trade_history = self.state.get('trade_history', [])

        est_stop_distance = price * 0.03
        atr_series = calculate_atr(df_1h_base)
        atr_14 = float(atr_series.iloc[-1]) if not atr_series.empty else est_stop_distance

        rolling_sharpe = 0.0
        recent_trades = trade_history[-20:] if trade_history else []
        if len(recent_trades) >= 5:
            returns = [t.get('pnl_pct', 0) for t in recent_trades]
            mean_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns) if len(returns) > 1 else 1.0
            rolling_sharpe = mean_ret / std_ret if std_ret > 0 else 0.0

        size_usd = compute_position_size(
            current_capital=equity,
            peak_capital=self.state.get('peak_equity', equity),
            trade_history=trade_history,
            regime=regime,
            timeframe_score=tf_result['score'],
            signal_score=xgb_prob * 100,
            atr_usd=atr_14,
            btc_price=price,
            current_position_open=self.has_position(),
            rolling_sharpe_3day=rolling_sharpe,
            timeframe_4h_bullish=tf_result['scores'].get('4h') == 1,
            state=self.state,
            save_state_fn=save_state,
        )

        # Conservative mode: first 2 days halve position size
        competition_start = datetime(2026, 3, 21)
        days_in = (datetime.utcnow() - competition_start).total_seconds() / 86400
        if 0 < days_in <= CONSERVATIVE_DAYS:
            size_usd *= 0.5
            log.info(f"Cycle {cycle}: CONSERVATIVE MODE — day {days_in:.1f}, size halved to ${size_usd:.0f}")

        log.info(
            f"Cycle {cycle}: L6 {'PASS' if size_usd > 0 else 'BLOCKED'} | "
            f"size=${size_usd:.0f} equity=${equity:.0f} regime={regime} "
            f"tf_score={tf_result['score']} sharpe={rolling_sharpe:.2f}"
        )
        if size_usd <= 0:
            return

        # ══════════════════════════════════════════
        # LAYER 7: EXECUTE BTC
        # ══════════════════════════════════════════
        log.info(
            f"Cycle {cycle}: EXECUTING BUY | "
            f"Regime={regime} | TF_score={tf_result['score']} | "
            f"XGB={xgb_prob:.3f} | Size=${size_usd:.0f} | "
            f"Source={source} | Price=${price:.2f}"
        )

        signal_source = "DONCHIAN_BREAKOUT" if "donchian" in source or "breakout" in source else "MEAN_REVERSION"
        entry_context = {
            "reversal_blocker_result": "PASSED",
            "xgboost_probability": xgb_prob,
            "timeframe_scores": {"1H": tf_result['scores'].get('1h'), "4H": tf_result['scores'].get('4h'), "Daily": tf_result['scores'].get('daily')},
            "timeframe_total_score": tf_result['score'],
        }

        self.executor.execute_trade(
            final_position_size_usd=size_usd,
            current_btc_price=price,
            current_bid=bid,
            current_ask=ask,
            atr_14=atr_14,
            regime=regime,
            signal_source=signal_source,
            entry_context=entry_context,
        )

        # ══════════════════════════════════════════
        # CHANGE 3: MULTICOIN — buy top momentum alts if enabled
        # ══════════════════════════════════════════
        if ENABLE_MULTICOIN:
            from strategy.multicoin import rank_coins, get_alt_position_size
            try:
                top_alts = rank_coins(self.client)
                for alt in top_alts[:2]:
                    if alt['bid'] <= 0 or alt['ask'] <= 0:
                        continue
                    alt_size = get_alt_position_size(
                        equity, ALT_CAPITAL_PCT, len(top_alts[:2])
                    )
                    log.info(
                        f"Cycle {cycle}: ALT {alt['pair']} "
                        f"change={alt['change']:.2%} size=${alt_size:.0f}"
                    )
                    self.executor.execute_trade(
                        final_position_size_usd=alt_size,
                        current_btc_price=alt['price'],
                        current_bid=alt['bid'],
                        current_ask=alt['ask'],
                        atr_14=atr_14,
                        regime=regime,
                        signal_source='MULTICOIN_MOMENTUM',
                        entry_context={
                            'xgboost_probability': 0.7,
                            'timeframe_total_score': 1,
                            'reversal_blocker_result': 'PASSED',
                            'timeframe_scores': {'1H': 0, '4H': 0, 'Daily': 0},
                        },
                    )
            except Exception as e:
                log.error(f"Cycle {cycle}: Multicoin error: {e}")

    def run(self):
        """Main loop. Runs until stopped."""
        self.running = True
        log.info("=" * 60)
        log.info("TRADING BOT STARTED")
        log.info(f"Pair: {TRADING_PAIR}")
        log.info(f"Interval: {TRADE_INTERVAL_SECONDS}s")
        log.info(f"Strategy: Adaptive Regime Momentum (ARM v2) — 7 Layer Pipeline")
        log.info("=" * 60)

        self.bootstrap()

        try:
            server_time = self.client.get_server_time()
            log.info(f"Connected to Roostoo. Server time: {server_time}")
        except Exception as e:
            log.error(f"Cannot connect to Roostoo API: {e}")
            alert_error(f"Cannot connect to Roostoo API: {e}")
            return

        try:
            balance = self.client.get_balance()
            wallet = balance.get('SpotWallet', balance.get('Data', {}))
            usd_free = 0.0
            if isinstance(wallet, dict) and 'USD' in wallet:
                usd_free = float(wallet['USD'].get('Free', 0))
            if usd_free > 0:
                self.state['current_equity'] = usd_free
                self.state['peak_equity'] = max(self.state.get('peak_equity', 0), usd_free)
                log.info(f"Synced equity from API: ${usd_free:,.0f}")
                save_state(self.state)
        except Exception as e:
            log.warning(f"Could not fetch balance (geo-blocked?): {e}")
            log.info(f"Using equity from state: ${self.state.get('current_equity', STARTING_CAPITAL):,.0f}")

        df_1h = self.candles.get_df('1h')
        regime = detect_regime(df_1h) if len(df_1h) > 55 else 'UNKNOWN'
        alert_startup(self.state.get('current_equity', STARTING_CAPITAL), regime, len(df_1h))

        if self.state.get('exec_position_open'):
            atr_series = calculate_atr(df_1h)
            atr_14 = float(atr_series.iloc[-1]) if len(df_1h) > 20 and not atr_series.empty else 1000.0
            entry_regime = self.state.get('exec_regime', regime)
            log.info(f"Recovering open position: restarting stop monitor (regime={entry_regime})")
            send_alert(f"<b>POSITION RECOVERED</b>\nRestarting stop monitor for open position")
            self.executor.start_stop_monitor(atr_14, entry_regime)

        while self.running:
            try:
                self.run_cycle()
                self.send_heartbeat()
                self.send_daily_summary()
                save_state(self.state)
                time.sleep(TRADE_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                log.info("\nBot stopped by user (Ctrl+C)")
                send_alert("<b>BOT STOPPED</b>\nManually stopped by user (Ctrl+C)")
                self.running = False
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                alert_error(f"Main loop error: {str(e)[:200]}")
                time.sleep(TRADE_INTERVAL_SECONDS)

        log.info("BOT STOPPED")
        save_state(self.state)
        send_alert("<b>BOT STOPPED</b>\nBot has shut down. Check EC2.")


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
