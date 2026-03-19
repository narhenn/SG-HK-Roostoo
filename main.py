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
from datetime import datetime

from config import (
    TRADING_PAIR, TRADE_INTERVAL_SECONDS, LOG_FILE, LOG_LEVEL,
    XGBOOST_MIN_PROBABILITY, LOGS_DIR, STARTING_CAPITAL,
)
from roostoo_client import RoostooClient
from data.candle_builder import CandleBuilder
from data.state import save_state, load_state, default_state
from data.fetchers import fetch_fear_greed, fetch_funding_rate, fetch_market_breadth
from strategy.regime import detect_regime
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
from risk.position_sizer import calculate_position
from execution.executor import execute_trade
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
        if now - self.last_external_fetch < 3600:  # Once per hour
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
        if now - self.last_heartbeat < 21600:  # 6 hours
            return

        equity = self.state.get('current_equity', STARTING_CAPITAL)
        cycle = self.state.get('cycle_count', 0)
        positions = self.state.get('positions', [])
        pos_status = "IN POSITION" if positions else "CASH"

        df_1h = self.candles.get_df('1h')
        regime = detect_regime(df_1h, self.fear_greed, self.funding_rate, self.breadth) if len(df_1h) > 55 else 'UNKNOWN'

        price = self.candles.get_current_price()

        send_alert(
            f"<b>HEARTBEAT</b>\n"
            f"Bot is alive and running\n"
            f"Cycle: #{cycle}\n"
            f"BTC: ${price:,.2f}\n"
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
        if now - self.last_daily_summary < 86400:  # 24 hours
            return

        equity = self.state.get('current_equity', STARTING_CAPITAL)
        peak = self.state.get('peak_equity', STARTING_CAPITAL)
        history = self.state.get('trade_history', [])

        # Count today's trades
        today = datetime.utcnow().strftime('%Y-%m-%d')
        today_trades = [t for t in history if t.get('exit_time', '').startswith(today)]
        wins = len([t for t in today_trades if t.get('pnl', 0) > 0])
        losses = len(today_trades) - wins
        pnl_today = sum(t.get('pnl', 0) for t in today_trades)

        alert_daily_summary(equity, peak, len(today_trades), wins, losses, pnl_today)
        self.last_daily_summary = now
        log.info("Daily summary sent to Telegram")

    def has_position(self) -> bool:
        """Check if we currently hold BTC (S2: one position at a time)."""
        return len(self.state.get('positions', [])) > 0

    def run_cycle(self):
        """Run one complete trading cycle through all 7 layers."""
        self.state['cycle_count'] = self.state.get('cycle_count', 0) + 1
        cycle = self.state['cycle_count']

        # ── Fetch current price ──
        try:
            raw_ticker = self.client.get_ticker(TRADING_PAIR)
            # Roostoo nests data under 'Data' -> pair name
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

        # Store tick
        self.candles.add_tick(price, volume, bid, ask)

        # ── Check halts ──
        if self.is_halted():
            log.info(f"Cycle {cycle}: Bot is HALTED. Price=${price:.2f}. Collecting data only.")
            return

        # ── Fetch external data (hourly) ──
        self.fetch_external_data()

        # Get DataFrames
        df_1h = self.candles.get_df('1h')

        if df_1h.empty or len(df_1h) < 55:
            log.info(f"Cycle {cycle}: Waiting for data ({len(df_1h)} 1H candles, need 55+). Price=${price:.2f}")
            return

        # ══════════════════════════════════════════
        # LAYER 1: REGIME DETECTION
        # ══════════════════════════════════════════
        regime = detect_regime(df_1h, self.fear_greed, self.funding_rate, self.breadth)

        # ══════════════════════════════════════════
        # LAYER 2: SIGNAL GENERATION
        # ══════════════════════════════════════════
        signal = generate_signal(df_1h, regime)
        direction = signal['direction']
        source = signal['source']

        if direction == 'HOLD':
            log.info(f"Cycle {cycle}: HOLD | Regime={regime} | Price=${price:.2f} | Source={source}")
            return

        # S1: SELL = exit only. No position = ignore sell signal
        if direction == 'SELL' and not self.has_position():
            log.info(f"Cycle {cycle}: SELL signal but no position. Ignoring.")
            return

        # S2: One position at a time
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
        if blocker_result.get('decision') == 'BLOCK':
            log.info(f"Cycle {cycle}: BLOCKED by reversal blocker. Reason={blocker_result.get('reason')}. Signal={direction} from {source}")
            return

        # ══════════════════════════════════════════
        # LAYER 4: MULTI-TIMEFRAME FILTER
        # ══════════════════════════════════════════
        df_4h = self.candles.get_df('4h')
        df_daily = self.candles.get_df('daily')

        tf_result = check_timeframe(df_1h, df_4h, df_daily, regime=regime)
        if not tf_result['pass']:
            log.info(
                f"Cycle {cycle}: BLOCKED by timeframe filter. "
                f"Score={tf_result['score']} Scores={tf_result['scores']}"
            )
            return

        # ══════════════════════════════════════════
        # LAYER 5: XGBOOST CONFIRMATION
        # ══════════════════════════════════════════
        if _USE_PRANATI_MODEL:
            # Use Pranati's model
            price_history = df_1h.to_dict('records')
            current_spread = (ask - bid) / price if price > 0 and ask > 0 and bid > 0 else 0.001
            xgb_decision, xgb_prob = get_xgboost_signal(
                price_history, breadth=self.breadth,
                spread_proxy=current_spread, threshold=XGBOOST_MIN_PROBABILITY
            )
        else:
            # Fallback to stub
            features = engineer_features(df_1h)
            xgb_prob = xgboost_confirm(features)

        if xgb_prob < XGBOOST_MIN_PROBABILITY:
            log.info(
                f"Cycle {cycle}: BLOCKED by XGBoost. "
                f"Prob={xgb_prob:.3f} < {XGBOOST_MIN_PROBABILITY}"
            )
            return

        # ══════════════════════════════════════════
        # LAYER 6: POSITION SIZING
        # ══════════════════════════════════════════
        equity = self.state.get('current_equity', STARTING_CAPITAL)
        trade_history = self.state.get('trade_history', [])

        pos_result = calculate_position(
            equity, regime, tf_result['multiplier'], xgb_prob, trade_history
        )

        if not pos_result['can_trade']:
            log.info(f"Cycle {cycle}: BLOCKED by position sizer: {pos_result['reason']}")
            return

        size_usd = pos_result['size_usd']
        size_btc = size_usd / price

        # ══════════════════════════════════════════
        # LAYER 7: EXECUTE
        # ══════════════════════════════════════════
        log.info(
            f"Cycle {cycle}: EXECUTING {direction} | "
            f"Regime={regime} | TF_score={tf_result['score']} | "
            f"XGB={xgb_prob:.3f} | Size=${size_usd:.0f} ({size_btc:.6f} BTC) | "
            f"Source={source} | Price=${price:.2f}"
        )

        exec_result = execute_trade(
            self.client, direction, size_btc, price,
            stop_level=price * 0.97,  # Default stop, Kireeti will improve
            signal_source=source,
        )

        if exec_result['filled']:
            # Track position
            if direction == 'BUY':
                self.state['positions'].append({
                    'order_id': exec_result['order_id'],
                    'entry_price': exec_result['fill_price'],
                    'quantity': exec_result['fill_qty'],
                    'entry_time': datetime.utcnow().isoformat(),
                })
            elif direction == 'SELL':
                # Close position, record trade
                self.state['positions'] = []

            log.info(f"Cycle {cycle}: Order filled. ID={exec_result['order_id']}")
            alert_trade(direction, price, size_usd, regime, source,
                       tf_result['score'], xgb_prob)
        else:
            log.warning(f"Cycle {cycle}: Order NOT filled. {exec_result.get('error', '')}")

        # Save state after every trade
        save_state(self.state)

    def run(self):
        """Main loop. Runs until stopped."""
        self.running = True
        log.info("=" * 60)
        log.info("TRADING BOT STARTED")
        log.info(f"Pair: {TRADING_PAIR}")
        log.info(f"Interval: {TRADE_INTERVAL_SECONDS}s")
        log.info(f"Strategy: Adaptive Regime Momentum (ARM v2) — 7 Layer Pipeline")
        log.info("=" * 60)

        # Bootstrap with historical data
        self.bootstrap()

        # Test connection and sync equity from API
        try:
            server_time = self.client.get_server_time()
            log.info(f"Connected to Roostoo. Server time: {server_time}")

            # Fetch real balance from Roostoo API
            balance = self.client.get_balance()
            balance_data = balance.get('Data', balance)
            usd_free = 0.0
            if isinstance(balance_data, dict):
                usd_info = balance_data.get('USD', {})
                usd_free = float(usd_info.get('Free', 0))
            if usd_free > 0:
                self.state['current_equity'] = usd_free
                self.state['peak_equity'] = max(self.state.get('peak_equity', 0), usd_free)
                log.info(f"Synced equity from API: ${usd_free:,.0f}")
                save_state(self.state)
        except Exception as e:
            log.error(f"Cannot connect to Roostoo API: {e}")
            alert_error(f"Cannot connect to Roostoo API: {e}")
            return

        # Startup alert
        df_1h = self.candles.get_df('1h')
        regime = detect_regime(df_1h) if len(df_1h) > 55 else 'UNKNOWN'
        alert_startup(self.state.get('current_equity', STARTING_CAPITAL), regime, len(df_1h))

        while self.running:
            try:
                self.run_cycle()
                self.send_heartbeat()
                self.send_daily_summary()
                save_state(self.state)
                time.sleep(TRADE_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                log.info("\nBot stopped by user (Ctrl+C)")
                self.running = False
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                alert_error(f"Main loop error: {str(e)[:200]}")
                time.sleep(TRADE_INTERVAL_SECONDS)

        log.info("BOT STOPPED")
        save_state(self.state)


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
