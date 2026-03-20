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
STARTING_CAPITAL = 50_000             # Roostoo gives $50K, not $1M
COMPETITION_CAPITAL = 50_000          # Update this once competition account is confirmed
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
RSI_OVERSOLD = 48                  # Buy in sideways regime (48 for compressed RSI in low-ADX)
RSI_OVERBOUGHT = 52                # Sell in sideways regime (52 for compressed RSI in low-ADX)
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
MAX_LOSS_PER_TRADE = 0.015         # 1.5% max loss per trade
RISK_PER_TRADE = 0.005             # 0.5% risk per trade
MAX_OPEN_ORDERS = 3                # Max concurrent positions

# Drawdown throttle thresholds
DRAWDOWN_LEVEL_1 = 0.02            # 2% → reduce to 25% max
DRAWDOWN_LEVEL_2 = 0.05            # 5% → reduce to 15% max
DRAWDOWN_LEVEL_3 = 0.08            # 8% → halt 4 hours
DRAWDOWN_LEVEL_4 = 0.10            # 10% → emergency mode
DRAWDOWN_KILL = 0.15               # 15% → kill switch
SHARPE_KILL = -0.5                 # Negative Sharpe → kill switch
HALT_HOURS = 4                     # Hours to halt after L3 drawdown
KILL_HALT_HOURS = 24               # Hours to halt after kill switch

# ── Layer 7: Execution ──
LIMIT_ORDER_TIMEOUT = 120          # Cancel unfilled limit after 2 min (seconds)
BREAKOUT_AGGRESSIVE_OFFSET = 0.0002  # bid + 0.02% for breakout entries
ATR_STOP_MULTIPLIER = 1.5          # Trailing stop = 1.5x ATR below peak
TIME_EXIT_HOURS = 8                # Close flat positions after 8 hours
FLAT_THRESHOLD = 0.002             # <0.2% P&L = "flat"
COOLDOWN_AFTER_STOP = 3600         # 1 hour cooldown after stop-loss (seconds)
PARTIAL_FILL_THRESHOLD = 0.5       # >50% filled = keep, <50% = sell back

# ── Layer 7: Fees ──
MAKER_FEE = 0.0005                 # 0.05% limit order fee
TAKER_FEE = 0.001                  # 0.1% market order fee

# ── Competition Timeline ──
CONSERVATIVE_DAYS = 2              # Days 1-2: 50% position sizes
PROTECT_DAYS_BEFORE_END = 2        # Last 2 days: tighten stops
FINAL_DAY_CLOSE_HOUR = 20          # Close all by hour 20 on last day

# ── Paths ──
DATA_DIR = "data"
MODELS_DIR = "models"
LOGS_DIR = "logs"
STATE_FILE = "state.json"
HISTORICAL_DATA_FILE = "data/btc_1h_90days.csv"

# ── Logging ──
LOG_FILE = "logs/bot.log"
LOG_LEVEL = "INFO"
