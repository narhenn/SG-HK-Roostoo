"""
pair_rotation.py
Trades relative outperformance between multiple coin pairs.
Market-neutral: profits from relative moves, not absolute direction.
Pairs: ETH/BTC, BNB/BTC, XRP/ETH, SOL/BNB
"""
import time, json, hmac, hashlib, requests, logging, os
from datetime import datetime, timezone
from collections import deque

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

BASE_URL   = "https://mock-api.roostoo.com"
STATE_FILE = "rotation_state.json"
CLOSE_TIME = datetime(2026, 3, 30, 19, 0, 0, tzinfo=timezone.utc)
INTERVAL   = 20

RATIO_WINDOW   = 60
ENTRY_ZSCORE   = 1.4
EXIT_ZSCORE    = 0.3
TRADE_SIZE_USD = 60000
MAX_POSITIONS  = 4
STOP_RATIO_PCT = 0.025
MAX_HOLD_H     = 6
COOLDOWN_MIN   = 30

# Each pair: (base, quote) — we trade base/quote ratio
ROTATION_PAIRS = [
    ("ETH", "BTC"),
    ("BNB", "BTC"),
    ("XRP", "ETH"),
    ("SOL", "BTC"),
]

COINS_NEEDED = {"ETH", "BTC", "BNB", "XRP", "SOL"}

PREC = {
    "BTC/USD": {"price": 2, "amount": 5},
    "ETH/USD": {"price": 2, "amount": 4},
    "SOL/USD": {"price": 2, "amount": 3},
    "BNB/USD": {"price": 2, "amount": 3},
    "XRP/USD": {"price": 4, "amount": 1},
}

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ROTATE] %(message)s",
    handlers=[
        logging.FileHandler("logs/pair_rotation.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Rotate")
session = requests.Session()

def _ts():
    return str(int(time.time() * 1000))

def _sign(p):
    return hmac.new(SECRET_KEY.encode(),
        "&".join(f"{k}={v}" for k, v in sorted(p.items())).encode(),
        hashlib.sha256).hexdigest()

def _h(p): return {"RST-API-KEY": API_KEY, "MSG-SIGNATURE": _sign(p)}

def api_get(path, extra=None):
    p = extra or {}
    p["timestamp"] = _ts()
    r = session.get(f"{BASE_URL}{path}", params=p, headers=_h(p), timeout=10)
    r.raise_for_status()
    return r.json()

def api_post(path, params):
    params["timestamp"] = _ts()
    h = _h(params)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    r = session.post(f"{BASE_URL}{path}", data=params, headers=h, timeout=10)
    r.raise_for_status()
    return r.json()

def get_prices():
    data = api_get("/v3/ticker").get("Data", {})
    out = {}
    for coin in COINS_NEEDED:
        pair = f"{coin}/USD"
        info = data.get(pair, {})
        out[coin] = {
            "last": float(info.get("LastPrice", 0)),
            "bid":  float(info.get("MaxBid", 0)),
            "ask":  float(info.get("MinAsk", 0)),
        }
    return out

def get_wallet():
    w = api_get("/v3/balance").get("SpotWallet", {})
    out = {"USD": float(w.get("USD", {}).get("Free", 0))}
    for coin in COINS_NEEDED:
        out[coin] = float(w.get(coin, {}).get("Free", 0))
    return out

def place_market(coin, side, size_usd, price):
    pair = f"{coin}/USD"
    p = PREC[pair]
    qty = round(size_usd / price, p["amount"])
    if qty <= 0: return None
    params = {"pair": pair, "side": side,
              "type": "MARKET", "quantity": str(qty)}
    r = api_post("/v3/place_order", params)
    d = r.get("OrderDetail", r)
    return {
        "filled": float(d.get("FilledQuantity", 0) or 0),
        "price":  float(d.get("FilledAverPrice", 0) or 0),
    }

def tg(msg):
    if not TELEGRAM_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except: pass

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except:
        return {"positions": {}, "cooldowns": {}, "total_pnl": 0.0}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2)

# Rolling ratio history per pair
ratio_histories = {f"{b}/{q}": deque(maxlen=RATIO_WINDOW)
                   for b, q in ROTATION_PAIRS}

def get_ratio(base, quote, prices):
    bp = prices[base]["last"]
    qp = prices[quote]["last"]
    if bp <= 0 or qp <= 0: return None
    return bp / qp

def zscore(history, current):
    if len(history) < 20: return 0
    vals = list(history)
    mean = sum(vals) / len(vals)
    std  = (sum((v-mean)**2 for v in vals)/len(vals))**0.5
    return (current - mean) / std if std > 1e-8 else 0

def check_entries(state, prices, wallet):
    if len(state["positions"]) >= MAX_POSITIONS: return
    cash = wallet.get("USD", 0)

    candidates = []
    for base, quote in ROTATION_PAIRS:
        pair_key = f"{base}/{quote}"
        if pair_key in state["positions"]: continue
        cd = state["cooldowns"].get(pair_key, 0)
        if time.time() - cd < COOLDOWN_MIN * 60: continue

        ratio = get_ratio(base, quote, prices)
        if ratio is None: continue
        ratio_histories[pair_key].append(ratio)
        if len(ratio_histories[pair_key]) < RATIO_WINDOW: continue

        z = zscore(ratio_histories[pair_key], ratio)
        if abs(z) >= ENTRY_ZSCORE:
            candidates.append((abs(z), z, pair_key, base, quote, ratio))

    candidates.sort(reverse=True)

    for abs_z, z, pair_key, base, quote, ratio in candidates:
        if len(state["positions"]) >= MAX_POSITIONS: break
        if cash < TRADE_SIZE_USD * 0.8: break

        # z > 0: base expensive vs quote → sell base, buy quote
        # z < 0: base cheap vs quote → buy base, sell quote
        if z > 0:
            direction = "LONG_QUOTE"
            buy_coin  = quote
            sell_coin = base
        else:
            direction = "LONG_BASE"
            buy_coin  = base
            sell_coin = quote

        buy_price  = prices[buy_coin]["ask"]
        sell_price = prices[sell_coin]["bid"]

        if buy_price <= 0 or sell_price <= 0: continue

        # Buy with cash
        buy_result = place_market(buy_coin, "BUY", TRADE_SIZE_USD, buy_price)

        # Sell equivalent from holdings
        sell_held = wallet.get(sell_coin, 0)
        sell_val  = sell_held * sell_price
        sell_size = min(TRADE_SIZE_USD, sell_val)
        sell_result = None
        if sell_size > 1000:
            sell_result = place_market(sell_coin, "SELL", sell_size, sell_price)

        if buy_result and buy_result["filled"] > 0:
            state["positions"][pair_key] = {
                "direction":   direction,
                "entry_ratio": ratio,
                "entry_ts":    time.time(),
                "stop_ratio":  ratio * (1 + STOP_RATIO_PCT) if z > 0
                               else ratio * (1 - STOP_RATIO_PCT),
                "base":  base,
                "quote": quote,
                "z":     z,
            }
            cash -= TRADE_SIZE_USD
            log.info(f"ENTER {direction} {pair_key}: z={z:.2f} ratio={ratio:.6f}")
            tg(f"ROTATE {direction} {pair_key}\nz={z:.2f}\n"
               f"Bought {buy_coin} ${TRADE_SIZE_USD:,}\n"
               f"Sold {sell_coin} ${sell_size:,.0f}")
            time.sleep(2)

def check_exits(state, prices, wallet):
    for pair_key in list(state["positions"].keys()):
        pos   = state["positions"][pair_key]
        base  = pos["base"]
        quote = pos["quote"]

        ratio = get_ratio(base, quote, prices)
        if ratio is None: continue
        ratio_histories[pair_key].append(ratio)

        z      = zscore(ratio_histories[pair_key], ratio)
        held_h = (time.time() - pos["entry_ts"]) / 3600
        entry  = pos["entry_ratio"]
        reason = None

        if pos["direction"] == "LONG_QUOTE":
            pnl_proxy = (entry - ratio) / entry
            if z <= EXIT_ZSCORE:         reason = "REVERT"
            elif ratio >= pos["stop_ratio"]: reason = "STOP"
            elif held_h > MAX_HOLD_H:    reason = "TIME"
        else:
            pnl_proxy = (ratio - entry) / entry
            if z >= -EXIT_ZSCORE:        reason = "REVERT"
            elif ratio <= pos["stop_ratio"]: reason = "STOP"
            elif held_h > MAX_HOLD_H:    reason = "TIME"

        if reason:
            pnl = pnl_proxy * TRADE_SIZE_USD
            state["total_pnl"] += pnl
            state["cooldowns"][pair_key] = time.time()
            del state["positions"][pair_key]
            log.info(f"EXIT {reason} {pair_key}: z={z:.2f} pnl=${pnl:+,.0f}")
            tg(f"ROTATE EXIT {reason} {pair_key}\n"
               f"P&L: ${pnl:+,.0f} | Total: ${state['total_pnl']:+,.0f}")

def close_all(state):
    state["positions"] = {}
    tg(f"ROTATE CLOSED ALL\nTotal P&L: ${state['total_pnl']:+,.0f}")

def main():
    log.info("Multi-pair rotation bot starting")
    tg("Multi-pair rotation started: ETH/BTC, BNB/BTC, XRP/ETH, SOL/BTC")
    state = load_state()
    log.info(f"Warming up {RATIO_WINDOW} ticks ({RATIO_WINDOW*INTERVAL//60} min)...")

    while True:
        try:
            if datetime.now(timezone.utc) >= CLOSE_TIME:
                close_all(state)
                save_state(state)
                return

            prices = get_prices()
            wallet = get_wallet()
            check_exits(state, prices, wallet)
            check_entries(state, prices, wallet)
            save_state(state)

        except requests.exceptions.RequestException as e:
            log.warning(f"API error: {e}")
        except Exception as e:
            log.exception(f"Error: {e}")

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()