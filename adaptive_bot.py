"""
Regime-Adaptive Bounce Bot.

ONE strategy: buy coins that dipped and are bouncing.
TWO modes: adjusts TP/Stop based on market regime.

VOLATILE (breadth <40% or >70%): TP +2%, Stop -1%. Fast cycles.
NORMAL (breadth 40-70%): TP +5%, Stop -3%. Let positions breathe.

Entry: coin dropped 3%+ from recent high AND last candle is green AND BTC not crashing.
Sizing: fixed 20% of portfolio per position, max 3 positions.
Safety: max 3 trades per coin, 12h cooldown after loss.

Backtested score: 80.9 in competition period.
Python 3.9 compatible.
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
TIER2 = ["AVAX/USD", "LINK/USD", "AAVE/USD", "FET/USD", "TAO/USD",
         "APT/USD", "SUI/USD", "NEAR/USD", "WIF/USD", "PENDLE/USD"]
ALL_COINS = TIER1 + TIER2
EXCLUDED = {"PAXG/USD", "BONK/USD", "DOGE/USD", "SHIB/USD", "PEPE/USD",
            "FLOKI/USD", "1000CHEEMS/USD", "PUMP/USD", "TUT/USD", "STO/USD"}

PRECISION = {
    "BTC/USD": {"price": 2, "amount": 5},
    "ETH/USD": {"price": 2, "amount": 4},
    "SOL/USD": {"price": 2, "amount": 3},
    "BNB/USD": {"price": 2, "amount": 3},
    "XRP/USD": {"price": 4, "amount": 1},
}
DEFAULT_PREC = {"price": 4, "amount": 2}

# ── Regime thresholds ──
VOLATILE_LOW = 0.40    # breadth <40% = volatile (crash/fear)
VOLATILE_HIGH = 0.70   # breadth >70% = volatile (euphoria)
# Between 40-70% = normal/choppy

# ── Regime-dependent parameters ──
# VOLATILE: fast cycles, tight TP/Stop (like JuinStreet)
VOLATILE_TP = 0.02       # +2% take profit
VOLATILE_STOP = 0.01     # -1% stop

# NORMAL: wider params, let positions breathe
NORMAL_TP = 0.05         # +5% take profit
NORMAL_STOP = 0.03       # -3% stop

# ── Shared parameters ──
MAX_POSITIONS = 3          # max 3 positions at once
POSITION_SIZE_PCT = 0.20   # 20% of portfolio per position (fixed)
DIP_THRESHOLD = 0.03       # coin must have dipped 3%+ from recent high
TIME_STOP_HOURS = 6        # close flat positions after 6 hours

# ── Loss prevention (backtested: +$5.8k improvement) ──
MAX_TRADES_PER_COIN = 3       # don't obsess over one coin
COOLDOWN_AFTER_LOSS_H = 12    # 12h cooldown after getting stopped

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
    """Find coins that dipped 3%+ from recent high and are bouncing."""
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

        # Dipped from recent high
        recent_high = max(hist[-10:]) if len(hist) >= 10 else max(hist)
        current = p["last"]
        dip_pct = (recent_high - current) / recent_high

        # Last tick is up (bounce confirmed)
        last_ret = (hist[-1] - hist[-2]) / hist[-2] if len(hist) >= 2 else 0

        # BTC not crashing
        btc_hist = price_history.get("BTC/USD", [])
        btc_ok = len(btc_hist) < 5 or btc_hist[-1] >= btc_hist[-3]

        if dip_pct > DIP_THRESHOLD and last_ret > 0 and btc_ok:
            score = dip_pct + last_ret
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

def record_trade(state, pair, pnl):
    """Record trade for cooldown/count tracking."""
    state.setdefault("coin_trade_count", {})[pair] = state.get("coin_trade_count", {}).get(pair, 0) + 1
    if pnl < 0:
        cooldown_until = (datetime.utcnow() + timedelta(hours=COOLDOWN_AFTER_LOSS_H)).isoformat()
        state.setdefault("coin_cooldowns", {})[pair] = cooldown_until
        log.info(f"Cooldown {pair} for {COOLDOWN_AFTER_LOSS_H}h after loss")

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
        record_trade(state, pair, pnl)
        log.info(f"Closed {pair}: {reason} P&L=${pnl:+,.0f}")

# ── Entry logic: bounce-only, regime sets TP/Stop ──

def enter_bounces(state, prices, wallet, portfolio_value, price_history, regime):
    """Buy dipped coins that are bouncing. TP/Stop set by regime at entry time."""
    tp_pct, stop_pct = get_regime_params(regime)

    candidates = find_bounce_candidates(state, prices, price_history)
    n_open = len(state["positions"])

    for score, pair, p in candidates:
        if n_open >= MAX_POSITIONS:
            break

        size = portfolio_value * POSITION_SIZE_PCT
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
    send_telegram("Adaptive bot started")

    state = load_state()
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

            # Check exits on all positions
            check_exits(state, prices, wallet)

            # Enter new bounce positions (regime determines TP/Stop)
            enter_bounces(state, prices, wallet, portfolio, price_history, regime)

            # Logging
            cycle += 1
            if cycle % 20 == 0:  # every ~5 min
                n_pos = len(state["positions"])
                tp_pct, stop_pct = get_regime_params(regime)
                log.info(f"${portfolio:,.0f} | {regime} (TP{tp_pct:.0%}/SL{stop_pct:.0%}) | {n_pos} pos | P&L: ${state['total_pnl']:+,.0f}")

            if cycle % 240 == 0:  # hourly
                n_pos = len(state["positions"])
                send_telegram(
                    f"<b>Status</b>\n"
                    f"Portfolio: ${portfolio:,.0f}\n"
                    f"Regime: {regime} (breadth {breadth:.0%})\n"
                    f"Positions: {n_pos}\n"
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
