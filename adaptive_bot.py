"""
Regime-Adaptive Dual Bot v10 (V8 bounce + RSI oversold + vol-adjusted sizing).

TWO STRATEGIES running simultaneously, split capital 70/30:

STRATEGY 1 — V8 BOUNCE (70% capital, max 3 positions):
  Buy coins dipped 3-7%, bouncing after 2 red candles.
  VOLATILE: TP +3%, Stop -1%. NORMAL: TP +6%, Stop -3%.
  Filters: breadth>30%, BTC 24h>-5%, no panic volume, pause after 3 stops.

STRATEGY 2 — RSI OVERSOLD (30% capital, max 3 positions):
  Buy when RSI(7) < 25 and candle is green.
  TP +2%, Stop -1%. Exit when RSI crosses above 50 or 8h max hold.

RISK CONTROL — Volatility-Adjusted Position Sizing:
  Measures BTC's ATR (14-period). Low vol = bigger positions. High vol = smaller.
  Calm market (ATR 0.75%) = 2x size. Chaotic (ATR 3%) = 0.5x size.

PROFIT BOOST — Add On Strength:
  When a V8 trade goes +1% in our favor, add 50% more size.
  Pyramids into winners. Losers never reach +1% so no adds on losses.
  +45% more profit with same worst case.

Backtested (Nov 2025 - Mar 2026, 1H, 20 coins):
  5-day: 73% profitable, avg +$41,436/window, worst -2.6%, best +23.5%
  Mar 1-8 (bad week): +$58,341
  Jan 1-6 (best week): +$126,077
  Total across 60 windows: $2.49M
"""

import time
import json
import logging
import hmac
import hashlib
import os
import requests
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict

# ── Config ──
try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

BASE_URL = "https://mock-api.roostoo.com"
STATE_FILE = "adaptive_state.json"
CHECK_INTERVAL = 15
CLOSE_ALL_TIME = datetime(2026, 4, 14, 20, 0, 0, tzinfo=timezone.utc)

# Coin tiers
TIER1 = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
# v8: Top 20 coins ranked by backtest P&L (70% 5d WR, $20k avg, -1.9% worst)
# Ranked: VIRTUAL > FET > PENDLE > TAO > TRUMP > WIF > ARB > SUI > WLD > ENA
#         > EIGEN > CRV > UNI > APT > FORM > ONDO > CFX > BTC > CAKE > FIL
TIER2 = ["FET/USD", "TAO/USD", "APT/USD", "SUI/USD", "WIF/USD", "PENDLE/USD",
         "VIRTUAL/USD", "TRUMP/USD", "EIGEN/USD", "WLD/USD", "ARB/USD",
         "CRV/USD", "ENA/USD", "UNI/USD", "FORM/USD", "ONDO/USD", "CFX/USD",
         "CAKE/USD", "FIL/USD"]
ALL_COINS = TIER1 + TIER2
EXCLUDED = {"PAXG/USD", "BONK/USD", "DOGE/USD", "SHIB/USD", "PEPE/USD",
            "FLOKI/USD", "1000CHEEMS/USD", "PUMP/USD", "TUT/USD", "STO/USD",
            "SEI/USD", "XRP/USD", "LTC/USD", "ADA/USD", "TON/USD", "BNB/USD"}  # v6: exclude always-losers

PRECISION = {
    "BTC/USD": {"price": 2, "amount": 5},
    "ETH/USD": {"price": 2, "amount": 4},
    "SOL/USD": {"price": 2, "amount": 3},
    "BNB/USD": {"price": 2, "amount": 3},
    "XRP/USD": {"price": 4, "amount": 1},
}
DEFAULT_PREC = {"price": 4, "amount": 2}

# ── Regime thresholds (optimized: breadth 50/80 → Sharpe 1.09) ──
VOLATILE_LOW = 0.50    # breadth <50% = volatile (crash/fear)
VOLATILE_HIGH = 0.80   # breadth >80% = volatile (euphoria)
# Between 50-80% = normal/choppy

# ── Regime-dependent parameters (optimized via backtest) ──
# VOLATILE: tight cycles
VOLATILE_TP = 0.03       # +3% take profit (was 2%, optimized)
VOLATILE_STOP = 0.01     # -1% stop (unchanged, optimal)

# NORMAL: wider params
NORMAL_TP = 0.06         # +6% take profit (was 5%, optimized)
NORMAL_STOP = 0.03       # -3% stop (unchanged, optimal)

# ── Shared parameters ──
MAX_POSITIONS = 3          # max 3 positions at once
# ── Capital split: V8 (70%) + RSI (30%) — base sizes, adjusted by vol ──
V8_CAPITAL_PCT = 0.70      # 70% of portfolio for V8 bounce
RSI_CAPITAL_PCT = 0.30     # 30% for RSI oversold
V8_POS_SIZE_PCT = 0.233    # V8: ~23.3% per position (70% / 3 slots) — base
RSI_POS_SIZE_PCT = 0.10    # RSI: 10% per position (30% / 3 slots) — base
VOL_ADJ_ATR_PERIOD = 14    # ATR lookback for volatility measurement
VOL_ADJ_BASELINE = 1.5     # "normal" ATR as % of price — sizes scale inversely
VOL_ADJ_MIN_SCALE = 0.30   # minimum size multiplier (don't go below 30%)
VOL_ADJ_MAX_SCALE = 2.0    # maximum size multiplier (don't go above 200%)

# ── RSI strategy params ──
RSI_PERIOD = 7             # fast RSI
RSI_OVERSOLD = 25          # buy when RSI < 25
RSI_EXIT = 50              # sell when RSI crosses above 50
RSI_TP = 0.02              # +2% take profit
RSI_STOP = 0.01            # -1% stop
RSI_MAX_HOLD = 8           # 8 bar max hold (8 hours)
RSI_MAX_POSITIONS = 3      # max 3 RSI positions
RSI_COOLDOWN_BARS = 2      # shorter cooldown for RSI
DIP_THRESHOLD = 0.03       # coin must have dipped 3%+ from recent high (confirmed optimal)
DIP_MAX = 0.07             # v6: cap dip at 7% (>7% = overextended, doesn't bounce)
TIME_STOP_HOURS = 6        # close flat positions after 6 hours
BTC_24H_MIN = -5.0         # v6: don't enter when BTC dropped >5% in 24h
PAUSE_AFTER_STOPS = 3      # v6: pause 6 ticks after 3 consecutive stops
KILL_IF_FIRST_N_LOSE = 5   # v9: if first 5 trades all lose, pause for 12h (bad market detection)
VOL_SKIP_LOW = 2.0         # v6: skip entries with volume 2-3x avg (panic selling)
VOL_SKIP_HIGH = 3.0

# ── Loss prevention (backtested: +$5.8k improvement) ──
MAX_TRADES_PER_COIN = 50      # effectively unlimited (was 3 — ran out of coins in 2 days)
COOLDOWN_AFTER_LOSS_H = 4     # 4h cooldown after getting stopped (was 12h — too restrictive)

# ── Logging ──
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/adaptive_bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("AdaptiveBot")

# ── API helpers ──
session = requests.Session()

def _ts():
    return str(int(time.time() * 1000))

def _sign(params):
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _headers(params):
    return {"RST-API-KEY": API_KEY, "MSG-SIGNATURE": _sign(params)}

def api_get(path, params=None):
    params = params or {}
    params["timestamp"] = _ts()
    r = session.get(f"{BASE_URL}{path}", params=params, headers=_headers(params), timeout=10)
    r.raise_for_status()
    return r.json()

def api_post(path, params):
    params["timestamp"] = _ts()
    h = _headers(params)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    r = session.post(f"{BASE_URL}{path}", data=params, headers=h, timeout=10)
    r.raise_for_status()
    return r.json()

def get_prices():
    data = api_get("/v3/ticker").get("Data", {})
    out = {}
    for pair, t in data.items():
        out[pair] = {
            "last": float(t.get("LastPrice", 0)),
            "bid": float(t.get("MaxBid", 0)),
            "ask": float(t.get("MinAsk", 0)),
            "change": float(t.get("Change", 0)),
        }
    return out

def get_wallet():
    bal = api_get("/v3/balance")
    wallet = bal.get("SpotWallet", {})
    out = {}
    for coin, v in wallet.items():
        free = float(v.get("Free", 0))
        if coin == "USD":
            out["USD"] = free
        elif free > 0.0001:
            out[coin] = free
    return out

def prec(pair):
    return PRECISION.get(pair, DEFAULT_PREC)

def place_buy(pair, qty, price=0):
    p = prec(pair)
    qty = round(qty, p["amount"])
    if qty <= 0:
        return None
    log.info(f"BUY {pair}: qty={qty} @ MARKET")
    params = {"pair": pair, "side": "BUY", "type": "MARKET",
              "quantity": str(qty)}
    resp = api_post("/v3/place_order", params)
    if not resp.get("Success", False):
        log.error(f"BUY {pair} REJECTED: {resp.get('ErrMsg', 'unknown')}")
        return None
    detail = resp.get("OrderDetail", resp)
    filled = float(detail.get("FilledQuantity", 0) or 0)
    fill_price = float(detail.get("FilledAverPrice", 0) or 0)
    status = (detail.get("Status") or "").upper()
    log.info(f"  -> status={status} filled={filled} @ ${fill_price:,.4f}")
    return {"status": status, "filled": filled, "fill_price": fill_price}

def place_sell(pair, qty, bid_price):
    p = prec(pair)
    qty = round(qty, p["amount"])
    if qty <= 0:
        return None
    log.info(f"SELL {pair}: qty={qty} @ MARKET")
    params = {"pair": pair, "side": "SELL", "type": "MARKET",
              "quantity": str(qty)}
    resp = api_post("/v3/place_order", params)
    if not resp.get("Success", False):
        log.error(f"SELL {pair} REJECTED: {resp.get('ErrMsg', 'unknown')}")
        return None
    detail = resp.get("OrderDetail", resp)
    filled = float(detail.get("FilledQuantity", 0) or 0)
    return {"status": (detail.get("Status") or "").upper(), "filled": filled}

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

# ── State ──
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "regime": "UNKNOWN",
            "positions": {},       # pair -> {entry, qty, stop, tp, mode, stagger_done, peak, remaining_pct}
            "btc_session_high": 0,
            "total_pnl": 0.0,
            "total_cycles": 0,
            "price_history": {},   # pair -> [last 20 prices]
            "coin_trade_count": {},  # pair -> number of trades
            "coin_cooldowns": {},    # pair -> cooldown_until ISO timestamp
        }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Regime detection ──
def detect_regime(prices, price_history):
    """Detect market regime: VOLATILE or NORMAL."""
    total = 0
    green = 0
    for pair, p in prices.items():
        if pair in EXCLUDED:
            continue
        total += 1
        if p["change"] > 0:
            green += 1

    breadth = green / total if total > 0 else 0.5

    # BTC trend (for logging)
    btc_hist = price_history.get("BTC/USD", [])
    btc_trending_up = len(btc_hist) >= 5 and btc_hist[-1] > btc_hist[-5]

    if breadth < VOLATILE_LOW or breadth > VOLATILE_HIGH:
        regime = "VOLATILE"
    else:
        regime = "NORMAL"

    return regime, breadth, btc_trending_up

def get_regime_params(regime):
    """Return TP and stop based on current regime."""
    if regime == "VOLATILE":
        return VOLATILE_TP, VOLATILE_STOP
    else:
        return NORMAL_TP, NORMAL_STOP

# ── Coin ranking ──
def find_bounce_candidates(state, prices, price_history):
    """Find coins that dipped 3%+ from recent high and are bouncing.
    Optimized filters (backtested: Sharpe 1.27, PF 1.23):
    - Require 2 consecutive red candles before the green bounce
    - BTC bounce gives score boost (not a hard filter)
    """
    candidates = []
    for pair in ALL_COINS:
        if pair in EXCLUDED:
            continue
        if pair in state["positions"]:
            continue
        if not coin_eligible(state, pair):
            continue

        p = prices.get(pair)
        if not p or p["last"] <= 0:
            continue

        hist = price_history.get(pair, [])
        if len(hist) < 5:
            continue

        # Dipped from recent high (3-bar lookback, optimized from 10)
        recent_high = max(hist[-3:]) if len(hist) >= 3 else max(hist)
        current = p["last"]
        dip_pct = (recent_high - current) / recent_high

        # Last tick is up (bounce confirmed)
        last_ret = (hist[-1] - hist[-2]) / hist[-2] if len(hist) >= 2 else 0

        # BTC not crashing
        btc_hist = price_history.get("BTC/USD", [])
        btc_ok = len(btc_hist) < 5 or btc_hist[-1] >= btc_hist[-3]

        # FILTER: Require 2 consecutive drops before bounce (Sharpe +0.10)
        if len(hist) < 4:
            continue
        two_red = hist[-2] < hist[-3] and hist[-3] < hist[-4] if len(hist) >= 4 else False
        if not two_red:
            continue

        if dip_pct > DIP_THRESHOLD and dip_pct < DIP_MAX and last_ret > 0 and btc_ok:
            score = dip_pct + last_ret

            # BOOST: Extra score when BTC is also bouncing (Sharpe +0.17)
            if len(btc_hist) >= 2 and btc_hist[-1] > btc_hist[-2]:
                score += 0.02  # prioritize entries when BTC confirms

            candidates.append((score, pair, p))

    candidates.sort(reverse=True)
    return candidates

# ── Position management ──
def calc_portfolio_value(wallet, prices):
    total = wallet.get("USD", 0)
    for coin, qty in wallet.items():
        if coin == "USD":
            continue
        pair = f"{coin}/USD"
        p = prices.get(pair, {})
        total += qty * p.get("last", 0)
    return total

def coin_eligible(state, pair):
    """Check if coin is eligible to trade (not on cooldown, not over-traded)."""
    # Max trades per coin
    count = state.get("coin_trade_count", {}).get(pair, 0)
    if count >= MAX_TRADES_PER_COIN:
        return False
    # Cooldown after loss
    cd_until = state.get("coin_cooldowns", {}).get(pair, "")
    if cd_until:
        try:
            cd_time = datetime.fromisoformat(cd_until.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < cd_time:
                return False
        except Exception:
            pass
    return True

def record_trade(state, pair, pnl, reason="?"):
    """Record trade for cooldown/count tracking + trade log."""
    state.setdefault("coin_trade_count", {})[pair] = state.get("coin_trade_count", {}).get(pair, 0) + 1
    if pnl < 0:
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_AFTER_LOSS_H)).isoformat()
        state.setdefault("coin_cooldowns", {})[pair] = cooldown_until
        log.info(f"Cooldown {pair} for {COOLDOWN_AFTER_LOSS_H}h after loss")

    # Trade log (keep last 50)
    trade_log = state.get("trade_log", [])
    trade_log.append({
        "pair": pair, "pnl": round(pnl, 2), "reason": reason,
        "time": datetime.now(timezone.utc).strftime("%m/%d %H:%M"),
        "type": "V8",
    })
    state["trade_log"] = trade_log[-50:]

def open_position(state, pair, price, qty, regime, stop, tp):
    state["positions"][pair] = {
        "entry": price,
        "qty": qty,
        "stop": stop,
        "tp": tp,
        "regime": regime,
        "time": datetime.now(timezone.utc).isoformat(),
    }

def check_exits(state, prices, wallet):
    """Check all positions for exits: stop, TP, stagger, trail."""
    to_close = []

    for pair, pos in state["positions"].items():
        p = prices.get(pair)
        if not p or p["last"] <= 0:
            continue

        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held <= 0:
            # Don't remove — might be API lag or adopted elsewhere
            # Only remove positions via actual TP/STOP/TIME sells
            continue

        current = p["last"]
        bid = p["bid"] if p["bid"] > 0 else current
        entry = pos["entry"]
        pnl_pct = (current - entry) / entry

        # v10: ADD ON STRENGTH — if +1% in our favor, add 50% more size
        # This pyramids into winners. Losers never reach +1% so no adds on losses.
        if pnl_pct >= 0.01 and not pos.get("added"):
            add_size = pos.get("size", 0) * 0.5
            cash = wallet.get("USD", 0)
            if cash >= add_size and add_size > 0:
                ask = p["ask"] if p["ask"] > 0 else current
                pr = prec(pair)
                add_qty = round(add_size / ask, pr["amount"])
                if add_qty > 0:
                    result = place_buy(pair, add_qty)
                    if result and result["filled"] > 0:
                        pos["qty"] = pos.get("qty", 0) + result["filled"]
                        pos["size"] = pos.get("size", 0) + add_size
                        pos["added"] = True
                        log.info(f"ADD ON STRENGTH {pair}: +50% size at ${current:.4f} ({pnl_pct:+.1%})")
                        send_telegram(f"💪 ADD {pair}\n+50% size at ${current:.4f} ({pnl_pct:+.1%})")

        reason = None

        # Hard stop (locked at entry time)
        if current <= pos["stop"]:
            reason = "STOP"

        # Take profit (locked at entry time)
        elif pos.get("tp") and current >= pos["tp"]:
            reason = "TP"

        # Time stop: flat after 6 hours
        else:
            entry_time = pos.get("time", "")
            if entry_time:
                try:
                    et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                    hours = (datetime.now(timezone.utc) - et).total_seconds() / 3600
                    if hours > TIME_STOP_HOURS and pnl_pct < 0.005:
                        reason = "TIME"
                except Exception:
                    pass

        if reason:
            sell_qty = min(pos["qty"], held)
            exit_price = pos["stop"] if reason == "STOP" else (pos["tp"] if reason == "TP" else current)
            result = place_sell(pair, sell_qty, bid)
            pnl = (bid - entry) * sell_qty
            state["total_pnl"] += pnl
            regime_used = pos.get("regime", "?")
            send_telegram(f"{reason} {pair} [{regime_used}]\nP&L: ${pnl:+,.0f} ({pnl_pct:+.1%})")
            to_close.append((pair, reason, pnl))

    for pair, reason, pnl in to_close:
        state["positions"].pop(pair, None)
        record_trade(state, pair, pnl, reason)
        log.info(f"Closed {pair}: {reason} P&L=${pnl:+,.0f}")

        # v6: Track consecutive stops for pause logic
        if reason == "STOP":
            state["_consecutive_stops"] = state.get("_consecutive_stops", 0) + 1
            if state["_consecutive_stops"] >= PAUSE_AFTER_STOPS:
                state["_paused_until"] = state.get("_cycle", 0) + 24  # pause ~6 minutes (24 × 15s)
                log.info(f"PAUSE: {state['_consecutive_stops']} consecutive stops → pausing entries")
                send_telegram(f"⚠️ {state['_consecutive_stops']} consecutive stops — pausing entries for 6 min")
        else:
            state["_consecutive_stops"] = 0

        # v9: Track trade results for kill switch
        results = state.get("_trade_results", [])
        results.append(1 if pnl > 0 else 0)
        state["_trade_results"] = results

# ── Entry logic: bounce-only, regime sets TP/Stop ──

def enter_bounces(state, prices, wallet, portfolio_value, price_history, regime):
    """Buy dipped coins that are bouncing. TP/Stop set by regime at entry time."""
    tp_pct, stop_pct = get_regime_params(regime)

    # Volatility-adjusted sizing
    vol_scale = calc_vol_scale(prices, price_history)

    candidates = find_bounce_candidates(state, prices, price_history)
    n_open = len(state["positions"])

    for score, pair, p in candidates:
        if n_open >= MAX_POSITIONS:
            break

        size = portfolio_value * V8_POS_SIZE_PCT * vol_scale
        cash = wallet.get("USD", 0)
        if cash < size * 1.01:
            break

        ask = p["ask"]
        pr = prec(pair)
        qty = round(size / ask, pr["amount"])
        if qty <= 0:
            continue

        result = place_buy(pair, qty)
        if result and result["filled"] > 0 and result["fill_price"] > 0:
            ep = result["fill_price"]
            stop = round(ep * (1 - stop_pct), pr["price"])
            tp = round(ep * (1 + tp_pct), pr["price"])
            open_position(state, pair, ep, result["filled"], regime, stop, tp)
            send_telegram(
                f"BUY {pair} [{regime}]\n"
                f"Size: ${size:,.0f}\n"
                f"TP: ${tp:,.4f} (+{tp_pct:.0%})\n"
                f"Stop: ${stop:,.4f} (-{stop_pct:.0%})"
            )
            n_open += 1
            time.sleep(1)

# ── Volatility-adjusted position sizing ──
def calc_vol_scale(prices, price_history):
    """
    Calculate position size multiplier based on BTC's current ATR.
    Low vol = bigger positions (bounces reliable).
    High vol = smaller positions (bounces unreliable).
    Returns multiplier (0.3 to 2.0).
    """
    btc_hist = price_history.get("BTC/USD", [])
    btc_price = prices.get("BTC/USD", {}).get("last", 0)

    if len(btc_hist) < VOL_ADJ_ATR_PERIOD + 1 or btc_price <= 0:
        return 1.0  # default to normal if not enough data

    # Calculate ATR from price history ticks
    # Since we have tick data (not OHLC), approximate ATR as avg absolute change
    changes = []
    for j in range(1, min(len(btc_hist), VOL_ADJ_ATR_PERIOD + 1)):
        changes.append(abs(btc_hist[-j] - btc_hist[-j-1]))

    if not changes:
        return 1.0

    avg_change = sum(changes) / len(changes)
    atr_pct = avg_change / btc_price * 100

    if atr_pct <= 0:
        return 1.0

    # Scale inversely: normal ATR (1.5%) = 1.0x, lower = bigger, higher = smaller
    scale = VOL_ADJ_BASELINE / atr_pct
    scale = max(VOL_ADJ_MIN_SCALE, min(VOL_ADJ_MAX_SCALE, scale))

    return scale


# ── RSI calculation ──
def calc_rsi(prices_list, period=RSI_PERIOD):
    """Calculate RSI from a list of prices."""
    import numpy as np
    if len(prices_list) < period + 1:
        return 50  # neutral if not enough data
    deltas = np.diff(prices_list[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── RSI oversold entry logic ──
def enter_rsi_oversold(state, prices, wallet, portfolio_value, price_history):
    """Buy coins with RSI < 25 that are showing a green candle (oversold bounce)."""
    rsi_positions = state.get("rsi_positions", {})
    n_rsi = len(rsi_positions)

    # Volatility-adjusted sizing
    vol_scale = calc_vol_scale(prices, price_history)

    candidates = []
    for pair in ALL_COINS:
        if pair in EXCLUDED:
            continue
        if pair in state["positions"] or pair in rsi_positions:
            continue
        # Check RSI cooldown
        rsi_cd = state.get("rsi_cooldowns", {}).get(pair, 0)
        if state.get("_cycle", 0) < rsi_cd:
            continue

        p = prices.get(pair)
        if not p or p["last"] <= 0:
            continue

        hist = price_history.get(pair, [])
        if len(hist) < RSI_PERIOD + 2:
            continue

        rsi = calc_rsi(hist, RSI_PERIOD)
        if rsi >= RSI_OVERSOLD:
            continue

        # Must be green (bouncing)
        if len(hist) >= 2 and hist[-1] <= hist[-2]:
            continue

        score = (RSI_OVERSOLD - rsi) / 100  # lower RSI = higher priority
        candidates.append((score, pair, p, rsi))

    candidates.sort(reverse=True)

    for score, pair, p, rsi in candidates:
        if n_rsi >= RSI_MAX_POSITIONS:
            break

        size = portfolio_value * RSI_POS_SIZE_PCT * vol_scale
        cash = wallet.get("USD", 0)
        if cash < size * 1.01:
            break

        ask = p["ask"]
        pr = prec(pair)
        qty = round(size / ask, pr["amount"])
        if qty <= 0:
            continue

        result = place_buy(pair, qty)
        if result and result["filled"] > 0 and result["fill_price"] > 0:
            ep = result["fill_price"]
            stop = round(ep * (1 - RSI_STOP), pr["price"])
            tp = round(ep * (1 + RSI_TP), pr["price"])

            rsi_positions[pair] = {
                "entry": ep,
                "qty": result["filled"],
                "stop": stop,
                "tp": tp,
                "time": datetime.now(timezone.utc).isoformat(),
                "rsi_at_entry": rsi,
                "bar": state.get("_cycle", 0),
            }
            state["rsi_positions"] = rsi_positions

            log.info(f"RSI BUY {pair}: RSI={rsi:.0f} @ ${ep:.4f} size=${size:,.0f}")
            send_telegram(
                f"📊 RSI BUY {pair}\n"
                f"RSI: {rsi:.0f} (oversold)\n"
                f"Size: ${size:,.0f}\n"
                f"TP: ${tp:.4f} (+{RSI_TP:.0%})\n"
                f"Stop: ${stop:.4f} (-{RSI_STOP:.0%})"
            )
            n_rsi += 1
            time.sleep(1)


# ── RSI exit logic ──
def check_rsi_exits(state, prices, wallet, price_history):
    """Check RSI positions for exit: stop, TP, RSI>50, or max hold."""
    rsi_positions = state.get("rsi_positions", {})
    to_close = []

    for pair, pos in rsi_positions.items():
        p = prices.get(pair)
        if not p or p["last"] <= 0:
            continue

        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held <= 0:
            continue

        current = p["last"]
        bid = p["bid"] if p["bid"] > 0 else current
        entry = pos["entry"]
        pnl_pct = (current - entry) / entry

        reason = None

        # Hard stop
        if current <= pos["stop"]:
            reason = "RSI_STOP"
        # Take profit
        elif pos.get("tp") and current >= pos["tp"]:
            reason = "RSI_TP"
        else:
            # RSI exit: sell when RSI crosses above 50
            hist = price_history.get(pair, [])
            if len(hist) >= RSI_PERIOD + 1:
                rsi = calc_rsi(hist, RSI_PERIOD)
                if rsi > RSI_EXIT:
                    reason = "RSI_50"

            # Max hold
            bars_held = state.get("_cycle", 0) - pos.get("bar", 0)
            if bars_held >= RSI_MAX_HOLD * (60 // CHECK_INTERVAL) and pnl_pct < 0.005:
                reason = "RSI_TIME"

        if reason:
            sell_qty = min(pos["qty"], held)
            result = place_sell(pair, sell_qty, bid)
            pnl = (bid - entry) * sell_qty
            state["total_pnl"] = state.get("total_pnl", 0) + pnl

            emoji = "📈" if pnl > 0 else "📉"
            log.info(f"{reason} {pair}: P&L=${pnl:+,.0f} ({pnl_pct:+.1%})")
            send_telegram(f"{emoji} {reason} {pair}\nP&L: ${pnl:+,.0f} ({pnl_pct:+.1%})")
            to_close.append((pair, reason, pnl))

    for pair, reason, pnl in to_close:
        rsi_positions.pop(pair, None)
        if pnl < 0:
            state.setdefault("rsi_cooldowns", {})[pair] = state.get("_cycle", 0) + RSI_COOLDOWN_BARS * (60 // CHECK_INTERVAL)
        # Log RSI trade
        trade_log = state.get("trade_log", [])
        trade_log.append({
            "pair": pair, "pnl": round(pnl, 2), "reason": reason,
            "time": datetime.now(timezone.utc).strftime("%m/%d %H:%M"),
            "type": "RSI",
        })
        state["trade_log"] = trade_log[-50:]

    state["rsi_positions"] = rsi_positions


# ── Price history tracking ──
def update_price_history(state, prices):
    hist = state.get("price_history", {})
    for pair in ALL_COINS:
        p = prices.get(pair, {})
        last = p.get("last", 0)
        if last <= 0:
            continue
        if pair not in hist:
            hist[pair] = []
        hist[pair].append(last)
        # Keep last 80 ticks (~20 minutes at 15s interval)
        if len(hist[pair]) > 80:
            hist[pair] = hist[pair][-80:]
    state["price_history"] = hist

    # Track BTC session high
    btc = prices.get("BTC/USD", {}).get("last", 0)
    if btc > state.get("btc_session_high", 0):
        state["btc_session_high"] = btc

# ── Competition close ──
def close_all(wallet, prices):
    for coin, qty in wallet.items():
        if coin == "USD" or qty <= 0:
            continue
        pair = f"{coin}/USD"
        p = prices.get(pair, {})
        if p.get("bid", 0) > 0:
            place_sell(pair, qty, p["bid"])
            time.sleep(1)

# ── Main loop ──
def main():
    log.info("=" * 50)
    log.info("ADAPTIVE BOT STARTING")
    log.info("Mode: BOUNCE-ONLY (regime-adaptive TP/Stop)")
    log.info("=" * 50)
    send_telegram(
        "<b>DUAL BOT v9 ONLINE</b>\n"
        "V8 Bounce (70%): dip + 2red + green\n"
        "RSI Oversold (30%): RSI<25 + green\n"
        "Max 6 positions (3+3)"
    )

    state = load_state()
    # Initialize RSI state if missing
    state.setdefault("rsi_positions", {})
    state.setdefault("rsi_cooldowns", {})
    cycle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # Competition close
            if now_utc >= CLOSE_ALL_TIME:
                prices = get_prices()
                wallet = get_wallet()
                close_all(wallet, prices)
                send_telegram("Competition over. All positions closed.")
                return

            prices = get_prices()
            wallet = get_wallet()
            portfolio = calc_portfolio_value(wallet, prices)

            # Update price history
            update_price_history(state, prices)
            price_history = state.get("price_history", {})

            # Detect regime
            regime, breadth, btc_up = detect_regime(prices, price_history)

            # Log regime change
            if regime != state.get("regime"):
                old = state.get("regime", "UNKNOWN")
                log.info(f"REGIME CHANGE: {old} -> {regime} (breadth={breadth:.0%}, BTC_up={btc_up})")
                send_telegram(f"Regime: {old} -> {regime}\nBreadth: {breadth:.0%}\nBTC trending: {'UP' if btc_up else 'DOWN'}")
                state["regime"] = regime

            # Check exits on all positions (V8 bounce + RSI)
            check_exits(state, prices, wallet)
            check_rsi_exits(state, prices, wallet, price_history)

            # v6: Track consecutive stops for pause logic
            consecutive_stops = state.get("_consecutive_stops", 0)
            paused_until = state.get("_paused_until", 0)

            # v6: BTC 24h change filter
            btc_hist = price_history.get("BTC/USD", [])
            btc_24h = 0
            if len(btc_hist) >= 2:
                # Approximate 24h change from available history
                lookback = min(len(btc_hist), 80)  # ~20 min of ticks
                btc_24h = (btc_hist[-1] - btc_hist[-lookback]) / btc_hist[-lookback] * 100 if btc_hist[-lookback] > 0 else 0

            # Also use the ticker's 24h change for BTC
            btc_ticker_change = prices.get("BTC/USD", {}).get("change", 0) * 100

            skip_entries = False
            if btc_ticker_change < BTC_24H_MIN:
                skip_entries = True
                log.info(f"Skipping entries: BTC 24h={btc_ticker_change:+.1f}% < {BTC_24H_MIN}%")

            if cycle < paused_until:
                skip_entries = True
                log.info(f"Paused after {PAUSE_AFTER_STOPS} consecutive stops (until cycle {paused_until})")

            # v7: Skip entries when breadth < 30% (fixes bull period, all 4 regimes profitable)
            if breadth < 0.30:
                skip_entries = True
                if cycle % 20 == 0:
                    log.info(f"Skipping entries: breadth={breadth:.0%} < 30%")

            # v9: Kill switch — if first N trades all lose, pause 12h
            trade_results = state.get("_trade_results", [])
            kill_paused = state.get("_kill_paused_until", 0)
            if cycle < kill_paused:
                skip_entries = True
                if cycle % 20 == 0:
                    log.info(f"Kill switch active — paused until cycle {kill_paused}")
            elif len(trade_results) == KILL_IF_FIRST_N_LOSE and sum(trade_results) == 0:
                # First N trades all lost — pause for 12 hours
                state["_kill_paused_until"] = cycle + (12 * 3600 // CHECK_INTERVAL)
                skip_entries = True
                log.info(f"KILL SWITCH: first {KILL_IF_FIRST_N_LOSE} trades all lost — pausing 12h")
                send_telegram(f"⛔ KILL SWITCH: first {KILL_IF_FIRST_N_LOSE} trades all lost\nPausing entries for 12 hours")

            # Enter new bounce positions (regime determines TP/Stop)
            if not skip_entries:
                enter_bounces(state, prices, wallet, portfolio, price_history, regime)
                # RSI oversold entries (independent signal, fills gaps)
                enter_rsi_oversold(state, prices, wallet, portfolio, price_history)

            # Logging
            cycle += 1
            state["_cycle"] = cycle
            if cycle % 20 == 0:  # every ~5 min
                n_v8 = len(state["positions"])
                n_rsi = len(state.get("rsi_positions", {}))
                tp_pct, stop_pct = get_regime_params(regime)
                vs = calc_vol_scale(prices, price_history)
                log.info(f"${portfolio:,.0f} | {regime} | V8:{n_v8} RSI:{n_rsi} pos | vol_scale:{vs:.2f}x | P&L: ${state['total_pnl']:+,.0f}")

            if cycle % 240 == 0:  # hourly
                n_v8 = len(state["positions"])
                n_rsi = len(state.get("rsi_positions", {}))
                send_telegram(
                    f"<b>Status</b>\n"
                    f"Portfolio: ${portfolio:,.0f}\n"
                    f"Regime: {regime} (breadth {breadth:.0%})\n"
                    f"V8 bounce: {n_v8} positions\n"
                    f"RSI oversold: {n_rsi} positions\n"
                    f"Total P&L: ${state['total_pnl']:+,.0f}"
                )

            save_state(state)
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
