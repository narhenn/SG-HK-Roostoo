"""
Configuration for the Roostoo Trading Bot
API keys are loaded from config_secrets.py (git-ignored)
"""
import os

# ── API Credentials ──
# Create config_secrets.py with your keys (git-ignored)
try:
    from config_secrets import API_KEY, SECRET_KEY
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")

BASE_URL = "https://mock-api.roostoo.com"

# ── Trading Settings ──
TRADING_PAIR = "BTC/USD"              # Roostoo uses / not _, USD not USDT
ORDER_TYPE = "LIMIT"
TRADE_INTERVAL_SECONDS = 60           # Main loop interval
STOP_MONITOR_INTERVAL = 15            # Trailing stop check interval (seconds)
# ══════════════════════════════════════════
# MARCH 21 CHECKLIST — DO BEFORE 00:00 SGT
# 1. Swap API keys in config_secrets.py to competition keys
# 2. Change STARTING_CAPITAL to 1_000_000 (CONFIRMED: competition = $1M)
# 3. Delete state.json on EC2: rm -f state.json
# 4. sudo systemctl restart bot.service
# ══════════════════════════════════════════
STARTING_CAPITAL = 1_000_000           # Competition account (fallback if API sync fails)
COMPETITION_CAPITAL = 1_000_000       # CONFIRMED via API: competition account is $1M
BTC_PRICE_PRECISION = 2               # From exchangeInfo
BTC_AMOUNT_PRECISION = 5              # From exchangeInfo
BTC_MIN_ORDER = 1                     # Minimum order amount

# ── Layer 1: Regime Detection ──
ATR_PERIOD = 14
ADX_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2.0
REGIME_LOOKBACK_DAYS = 14          # For ATR percentile comparison
ADX_TREND_THRESHOLD = 25           # ADX > 25 = trending
ADX_NOTREND_THRESHOLD = 20         # ADX < 20 = sideways
BREADTH_BULLISH = 0.60             # >60% coins up = risk-on
BREADTH_BEARISH = 0.40             # <40% coins up = risk-off

# ── Layer 2: Signal Generation ──
# Donchian
DONCHIAN_UPPER_PERIOD = 20         # Breakout entry: 20-period high
DONCHIAN_LOWER_PERIOD = 10         # Breakout exit: 10-period low
# EMA alignment
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
# RSI (mean-reversion in sideways)
RSI_PERIOD = 14
RSI_OVERSOLD = 45                  # Buy on dips (48 was firing constantly, 45 catches real dips)
RSI_OVERBOUGHT = 58                # Sell on genuine strength (52 was constant noise)
# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ── Layer 3: Reversal Blocker ──
SPIKE_THRESHOLD = 0.02             # 2% move in 1-3 candles = spike
SPREAD_MULTIPLIER = 1.5            # Spread > 1.5x avg = widening
VOLUME_SPIKE_MULTIPLIER = 3.0      # Volume > 3x avg = abnormal
BLOCKER_COOLDOWN_CYCLES = 2        # Skip 2 cycles after block

# ── Layer 4: Multi-Timeframe ──
TF_MIN_SCORE = 2                   # Minimum sum to trade (+2 or +3)

# ── Layer 5: XGBoost ──
XGBOOST_MIN_PROBABILITY = 0.55     # Minimum ML confidence (loosened for bear market)
XGBOOST_RETRAIN_HOURS = 24         # Retrain every 24 hours
PROFIT_THRESHOLD = 0.001           # 0.1% minimum expected profit above fees

# ── Layer 6: Position Sizing ──
KELLY_FRACTION = 0.25              # Quarter-Kelly
KELLY_MIN_POSITION = 0.02          # 2% minimum position
KELLY_MAX_POSITION = 0.15          # 15% maximum position
MAX_POSITION_PCT = 0.35            # Hard cap: 35% of capital
MAX_LOSS_PER_TRADE = 0.010         # 1.0% max loss per trade (tighter for Calmar)
RISK_PER_TRADE = 0.005             # 0.5% risk per trade
MAX_OPEN_ORDERS = 3                # Max concurrent positions

# Drawdown throttle thresholds
DRAWDOWN_LEVEL_1 = 0.02            # 2% → reduce to 25% max
DRAWDOWN_LEVEL_2 = 0.05            # 5% → reduce to 15% max
DRAWDOWN_LEVEL_3 = 0.08            # 8% → halt 4 hours
DRAWDOWN_LEVEL_4 = 0.10            # 10% → emergency mode
DRAWDOWN_KILL = 0.15               # 15% → kill switch
SHARPE_KILL = -1.0                 # Negative Sharpe → kill switch (-0.5 triggers on 2 losses in 3 trades)
HALT_HOURS = 4                     # Hours to halt after L3 drawdown
KILL_HALT_HOURS = 24               # Hours to halt after kill switch

# ── Layer 7: Execution ──
LIMIT_ORDER_TIMEOUT = 120          # Cancel unfilled limit after 2 min (seconds)
BREAKOUT_AGGRESSIVE_OFFSET = 0.0002  # bid + 0.02% for breakout entries
ATR_STOP_MULTIPLIER = 1.5          # Trailing stop = 1.5x ATR below peak
TIME_EXIT_HOURS = 4                # Close flat positions after 4 hours (faster turnover)
FLAT_THRESHOLD = 0.002             # <0.2% P&L = "flat"
COOLDOWN_AFTER_STOP = 600          # 10 min cooldown after stop-loss (seconds)
PARTIAL_FILL_THRESHOLD = 0.5       # >50% filled = keep, <50% = sell back

# ── Layer 7: Fees ──
MAKER_FEE = 0.0005                 # 0.05% limit order fee
TAKER_FEE = 0.001                  # 0.1% market order fee

# ── Competition Timeline ──
CONSERVATIVE_DAYS = 0              # Disabled — no time to waste in 10-day competition
PROTECT_DAYS_BEFORE_END = 2        # Last 2 days: tighten stops
FINAL_DAY_CLOSE_HOUR = 20          # Close all by hour 20 on last day

# ── Urgency Mode (prevents score=0 from zero trades) ──
URGENCY_DAYS = 2                   # After 2 days with 0 trades, relax entry criteria
URGENCY_RSI_TRENDING = 38          # RSI threshold in trending urgency (normally 30)
URGENCY_RSI_TRENDING_NOCONFIRM = 42  # RSI threshold in trending critical urgency (no confirmation)
CRITICAL_URGENCY_DAYS = 7          # After 7 days: bypass XGBoost for urgency signals

# ── Paths ──
DATA_DIR = "data"
MODELS_DIR = "models"
LOGS_DIR = "logs"
STATE_FILE = "state.json"
HISTORICAL_DATA_FILE = "data/btc_1h_90days.csv"

# ── Logging ──
LOG_FILE = "logs/bot.log"
LOG_LEVEL = "INFO"



# ── Multi-coin Hybrid (disabled until BTC bot confirmed working) ──
ENABLE_MULTICOIN = False        # Flip to True after BTC bot verified
ALT_CAPITAL_PCT = 0.30          # 30% of capital for alts
ALT_COINS_COUNT = 2             # Top 2 momentum coins
ALT_ROTATE_INTERVAL = 24        # Re-rank every 24 cycles (not every minute)
