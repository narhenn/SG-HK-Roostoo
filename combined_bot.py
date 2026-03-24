"""
Combined trading bot: SWING + SCANNER strategies sharing one state.
Swing: $750k across BTC/ETH/BNB/SOL, TP +5%, stop -4%, re-entry on BTC -2% dip.
Scanner: remaining cash on dipped alts, $25k each, TP +5%, stop -7%.
Competition close: March 30 2026 20:00 UTC.
Python 3.9 compatible. No classes.
"""

import time
import json
import logging
import hmac
import hashlib
import os
import requests
from datetime import datetime, timezone

# ── Config ──

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

BASE_URL = "https://mock-api.roostoo.com"
STATE_FILE = "combined_state.json"
CHECK_INTERVAL = 15
CLOSE_ALL_TIME = datetime(2026, 3, 30, 20, 0, 0, tzinfo=timezone.utc)

# ── Swing params ──
# Match JuinStreet: 5 coins, equal weight ~$170k each, 80% deployed
SWING_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
SWING_ALLOC = {
    "BTC/USD": 250000,
    "ETH/USD": 160000,
    "SOL/USD": 150000,
    "BNB/USD": 150000,
    "XRP/USD": 140000,
}
SWING_TP = 0.05
SWING_STOP = 0.04
DIP_BUY_PCT = 0.02

# ── Scanner params ──
# Scans ALL coins on exchange except swing coins and excluded junk
SCANNER_EXCLUDE = set(SWING_PAIRS) | {
    "PAXG/USD",  # gold token, doesn't move like crypto
}
SCANNER_PAIRS = []  # populated dynamically from exchange at startup
SCANNER_MAX_POS = 0            # flip to 8 to enable
# No fixed budget — scanner uses whatever cash is free after swing reserve
SCANNER_TP = 0.05
SCANNER_STOP = 0.10             # wide stop — backtest showed alts always bounce
SCANNER_DIP_THRESH = -0.04      # -4% dip trigger (backtest winner: +$3,237, 86% WR)

# ── Shared ──
LEGACY_STOP_PCT = 0.07
SELL_ON_START = ["WIF/USD"]

PRECISION = {
    "BTC/USD":  {"price": 2, "amount": 5},
    "ETH/USD":  {"price": 2, "amount": 4},
    "SOL/USD":  {"price": 2, "amount": 3},
    "BNB/USD":  {"price": 2, "amount": 3},
    "XRP/USD":  {"price": 4, "amount": 1},
}
DEFAULT_PREC = {"price": 4, "amount": 2}

# ── Logging ──
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/combined_bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("CombinedBot")

# ── API helpers ──

session = requests.Session()

def _ts():
    return str(int(time.time() * 1000))

def _sign(params):
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _headers(params):
    return {"RST-API-KEY": API_KEY, "MSG-SIGNATURE": _sign(params)}

def api_get(path, extra=None):
    params = extra or {}
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

def place_buy(pair, qty, price):
    p = prec(pair)
    qty = round(qty, p["amount"])
    price = round(price, p["price"])
    if qty <= 0:
        return None
    log.info(f"BUY {pair}: qty={qty} @ ${price:,.2f}")
    params = {"pair": pair, "side": "BUY", "type": "LIMIT",
              "quantity": str(qty), "price": str(price)}
    resp = api_post("/v3/place_order", params)
    detail = resp.get("OrderDetail", resp)
    filled = float(detail.get("FilledQuantity", 0) or 0)
    fill_price = float(detail.get("FilledAverPrice", 0) or 0)
    status = (detail.get("Status") or "").upper()
    log.info(f"  -> status={status} filled={filled} @ ${fill_price:,.2f}")
    return {"status": status, "filled": filled, "fill_price": fill_price or price}

def place_sell(pair, qty, bid_price):
    p = prec(pair)
    qty = round(qty, p["amount"])
    price = round(bid_price, p["price"])
    if qty <= 0:
        return None
    log.info(f"SELL {pair}: qty={qty} @ ${price:,.2f}")
    params = {"pair": pair, "side": "SELL", "type": "LIMIT",
              "quantity": str(qty), "price": str(price)}
    resp = api_post("/v3/place_order", params)
    detail = resp.get("OrderDetail", resp)
    status = (detail.get("Status") or "").upper()
    filled = float(detail.get("FilledQuantity", 0) or 0)
    log.info(f"  -> status={status} filled={filled}")
    return {"status": status, "filled": filled}

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
        return default_state()

def default_state():
    return {
        "swing": {
            "phase": "WAITING",
            "entries": {},
            "session_high": {},
            "total_cycles": 0,
            "total_pnl": 0.0,
        },
        "scanner": {
            "entries": {},
        },
        "legacy": {},
        "last_hourly": 0,
        "last_portfolio_log": 0,
        "startup_sells_done": False,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Swing strategy ──

def swing_update_highs(state, prices):
    sh = state["swing"]["session_high"]
    for pair in SWING_PAIRS:
        p = prices.get(pair, {}).get("last", 0)
        if p > sh.get(pair, 0):
            sh[pair] = p

def swing_should_buy(state, prices):
    if state["swing"]["phase"] != "WAITING":
        return False
    btc_price = prices.get("BTC/USD", {}).get("last", 0)
    btc_high = state["swing"]["session_high"].get("BTC/USD", 0)
    if btc_high <= 0:
        log.info("Swing: first run, no session high — buying now")
        return True
    dip = (btc_high - btc_price) / btc_high if btc_high > 0 else 0
    if dip >= DIP_BUY_PCT:
        log.info(f"Swing: BTC dip {dip:.1%} from high ${btc_high:,.0f} — buying")
        return True
    return False

def swing_deploy(state, prices):
    log.info("=" * 40 + " SWING DEPLOY " + "=" * 40)
    entries = {}
    for pair in SWING_PAIRS:
        p = prices.get(pair)
        if not p or p["ask"] <= 0:
            continue
        usd = SWING_ALLOC.get(pair, 200000)
        ask = p["ask"]
        qty = usd / ask
        result = place_buy(pair, qty, ask)
        if result and result["filled"] > 0:
            ep = result["fill_price"]
            entries[pair] = {
                "entry_price": ep, "qty": result["filled"],
                "tp": round(ep * (1 + SWING_TP), prec(pair)["price"]),
                "stop": round(ep * (1 - SWING_STOP), prec(pair)["price"]),
                "time": datetime.now(timezone.utc).isoformat(),
            }
            send_telegram(f"SWING BUY {pair}\nQty: {result['filled']}\nPrice: ${ep:,.2f}")
    state["swing"]["entries"] = entries
    state["swing"]["phase"] = "DEPLOYED"
    state["swing"]["session_high"] = {}  # reset highs for next cycle

def swing_check_exits(state, prices, wallet):
    entries = state["swing"]["entries"]
    to_remove = []
    for pair, pos in entries.items():
        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held < pos["qty"] * 0.01:
            log.info(f"Swing: {pair} no longer in wallet, removing")
            to_remove.append(pair)
            continue
        p = prices.get(pair, {})
        bid = p.get("bid", 0)
        last = p.get("last", 0)
        if last <= 0:
            continue
        sell_qty = min(pos["qty"], held)
        reason = None
        if last >= pos["tp"]:
            reason = "TP"
        elif last <= pos["stop"]:
            reason = "STOP"
        if reason:
            result = place_sell(pair, sell_qty, bid)
            pnl = (bid - pos["entry_price"]) * sell_qty
            state["swing"]["total_pnl"] += pnl
            send_telegram(f"SWING {reason} {pair}\nP&L: ${pnl:+,.0f}\nPrice: ${bid:,.2f}")
            log.info(f"Swing {reason} {pair}: P&L ${pnl:+,.0f}")
            to_remove.append(pair)
    for pair in to_remove:
        entries.pop(pair, None)
    # All positions closed -> back to WAITING
    if state["swing"]["phase"] == "DEPLOYED" and len(entries) == 0:
        state["swing"]["phase"] = "WAITING"
        state["swing"]["total_cycles"] += 1
        log.info(f"Swing cycle complete #{state['swing']['total_cycles']}, waiting for next dip")
        send_telegram(f"Swing cycle #{state['swing']['total_cycles']} done. Total P&L: ${state['swing']['total_pnl']:+,.0f}")

# ── Scanner strategy ──

def scanner_cash_available(wallet, state):
    """Cash available for scanner = total cash - swing reserve if swing is WAITING."""
    cash = wallet.get("USD", 0)
    if state["swing"]["phase"] == "WAITING":
        # Reserve swing capital for re-entry
        cash = max(0, cash - sum(SWING_ALLOC.values()))
    return cash

def scanner_calc_size(change, cash_available):
    """Dynamic sizing based on available cash and dip magnitude.
    Allocates a % of available cash. Bigger dip = bigger %.
    -3% to -4% dip: 15% of cash.  -4% to -6%: 20%.  -6%+: 30%."""
    dip = abs(change)
    if dip >= 0.06:
        pct = 0.30
    elif dip >= 0.04:
        pct = 0.20
    else:
        pct = 0.15
    size = cash_available * pct
    # Floor at $5k, cap at $40k
    size = max(min(size, 40000), 0)
    if size < 5000:
        return 0
    return size

def scanner_check_entries(state, prices, wallet):
    entries = state["scanner"]["entries"]
    if len(entries) >= SCANNER_MAX_POS:
        return
    cash = scanner_cash_available(wallet, state)
    if cash < 5000:
        return

    # Collect all signals, sort by biggest dip first
    signals = []
    for pair in SCANNER_PAIRS:
        if pair in entries:
            continue
        # Skip coins already in wallet (legacy positions)
        coin = pair.split("/")[0]
        if wallet.get(coin, 0) > 0:
            continue
        p = prices.get(pair)
        if not p or p["ask"] <= 0:
            continue
        change = p.get("change", 0)
        if change > SCANNER_DIP_THRESH:
            continue
        # Skip if spread is too wide (>0.5% = illiquid)
        if p["bid"] > 0 and p["ask"] > 0:
            spread = (p["ask"] - p["bid"]) / p["bid"]
            if spread > 0.005:
                continue
        signals.append((change, pair, p))

    signals.sort()  # most negative (biggest dip) first

    for change, pair, p in signals:
        if len(entries) >= SCANNER_MAX_POS:
            break
        size = scanner_calc_size(change, cash)
        if size < 5000:
            continue
        ask = p["ask"]
        pr = prec(pair)
        # Cross the spread: buy at ask + 0.05% to ensure fill on mock exchange
        buy_price = round(ask * 1.0005, pr["price"])
        qty = round(size / buy_price, pr["amount"])
        if qty <= 0:
            continue
        log.info(f"Scanner signal: {pair} change={change:.1%} size=${size:,.0f}")
        result = place_buy(pair, qty, buy_price)
        if result and result["filled"] > 0:
            ep = result["fill_price"]
            actual_cost = ep * result["filled"]
            entries[pair] = {
                "entry_price": ep, "qty": result["filled"],
                "size_usd": actual_cost,
                "tp": round(ep * (1 + SCANNER_TP), prec(pair)["price"]),
                "stop": round(ep * (1 - SCANNER_STOP), prec(pair)["price"]),
                "time": datetime.now(timezone.utc).isoformat(),
            }
            send_telegram(f"SCANNER BUY {pair}\nDip: {change:.1%}\nSize: ${actual_cost:,.0f}\nPrice: ${ep:,.4f}")
            cash -= actual_cost
        else:
            # Failed to fill — cooldown this pair for 30 min
            entries[pair] = {"failed": True, "time": datetime.now(timezone.utc).isoformat()}
            log.info(f"Scanner: {pair} order not filled, cooling down 30min")

def scanner_check_exits(state, prices, wallet):
    entries = state["scanner"]["entries"]
    to_remove = []
    for pair, pos in entries.items():
        # Remove failed/cooldown entries after 30 min
        if pos.get("failed"):
            from datetime import datetime, timezone
            try:
                t = datetime.fromisoformat(pos["time"].replace("Z",""))
                if (datetime.now(timezone.utc).replace(tzinfo=None) - t).total_seconds() > 120:
                    to_remove.append(pair)
            except Exception:
                to_remove.append(pair)
            continue
        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held < pos.get("qty", 0) * 0.01:
            log.info(f"Scanner: {pair} no longer in wallet, removing")
            to_remove.append(pair)
            continue
        p = prices.get(pair, {})
        bid = p.get("bid", 0)
        last = p.get("last", 0)
        if last <= 0:
            continue
        sell_qty = min(pos["qty"], held)
        reason = None
        if last >= pos["tp"]:
            reason = "TP"
        elif last <= pos["stop"]:
            reason = "STOP"
        if reason:
            result = place_sell(pair, sell_qty, bid)
            pnl = (bid - pos["entry_price"]) * sell_qty
            send_telegram(f"SCANNER {reason} {pair}\nP&L: ${pnl:+,.0f}\nPrice: ${bid:,.2f}")
            log.info(f"Scanner {reason} {pair}: P&L ${pnl:+,.0f}")
            to_remove.append(pair)
    for pair in to_remove:
        entries.pop(pair, None)

# ── Legacy & startup ──

def adopt_legacy(state, wallet, prices):
    """Auto-adopt wallet positions not tracked by swing or scanner with -7% stops."""
    tracked_pairs = set(state["swing"]["entries"].keys()) | set(state["scanner"]["entries"].keys()) | set(state["legacy"].keys())
    for coin, held in wallet.items():
        if coin == "USD":
            continue
        pair = f"{coin}/USD"
        if pair in tracked_pairs:
            continue
        if pair in SELL_ON_START:
            continue
        p = prices.get(pair, {})
        last = p.get("last", 0)
        if last <= 0 or held * last < 50:  # skip dust
            continue
        state["legacy"][pair] = {
            "entry_price": last,  # assume current price as entry
            "qty": held,
            "stop": round(last * (1 - LEGACY_STOP_PCT), prec(pair)["price"]),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        log.info(f"Legacy adopted: {pair} qty={held} stop=${state['legacy'][pair]['stop']:,.2f}")

def legacy_check_exits(state, prices, wallet):
    to_remove = []
    for pair, pos in state["legacy"].items():
        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held < pos["qty"] * 0.01:
            to_remove.append(pair)
            continue
        p = prices.get(pair, {})
        last = p.get("last", 0)
        bid = p.get("bid", 0)
        if last <= 0:
            continue
        if last <= pos["stop"]:
            sell_qty = min(pos["qty"], held)
            place_sell(pair, sell_qty, bid)
            pnl = (bid - pos["entry_price"]) * sell_qty
            send_telegram(f"LEGACY STOP {pair}\nP&L: ${pnl:+,.0f}")
            to_remove.append(pair)
    for pair in to_remove:
        state["legacy"].pop(pair, None)

def startup_sells(state, wallet, prices):
    if state.get("startup_sells_done"):
        return
    for pair in SELL_ON_START:
        coin = pair.split("/")[0]
        held = wallet.get(coin, 0)
        if held > 0.0001:
            bid = prices.get(pair, {}).get("bid", 0)
            if bid > 0:
                log.info(f"Startup: selling {pair} (bad R:R)")
                place_sell(pair, held, bid)
                send_telegram(f"Startup sold {pair}: qty={held}")
    state["startup_sells_done"] = True

# ── Close all for competition end ──

def close_all(wallet, prices):
    log.info("COMPETITION CLOSE — selling everything")
    send_telegram("COMPETITION CLOSE — liquidating all positions")
    for coin, held in wallet.items():
        if coin == "USD" or held < 0.0001:
            continue
        pair = f"{coin}/USD"
        bid = prices.get(pair, {}).get("bid", 0)
        if bid > 0:
            place_sell(pair, held, bid)
            send_telegram(f"CLOSE sold {pair}: qty={held}")

# ── Status reporting ──

def hourly_status(state, wallet, prices):
    now = time.time()
    if now - state.get("last_hourly", 0) < 3600:
        return
    state["last_hourly"] = now
    # Compute portfolio value
    total = wallet.get("USD", 0)
    lines = [f"USD: ${wallet.get('USD', 0):,.0f}"]
    for coin, held in wallet.items():
        if coin == "USD":
            continue
        pair = f"{coin}/USD"
        px = prices.get(pair, {}).get("last", 0)
        val = held * px
        total += val
        lines.append(f"{coin}: {held} (${val:,.0f})")
    swing = state["swing"]
    msg = (
        f"<b>Hourly Status</b>\n"
        f"Portfolio: ${total:,.0f}\n"
        f"Swing: {swing['phase']} | Cycles: {swing['total_cycles']} | P&L: ${swing['total_pnl']:+,.0f}\n"
        f"Scanner positions: {len(state['scanner']['entries'])}\n"
        f"Legacy positions: {len(state['legacy'])}\n"
        f"\n".join(lines)
    )
    send_telegram(msg)
    log.info(f"Portfolio: ${total:,.0f} | Swing: {swing['phase']} | Scanner: {len(state['scanner']['entries'])} pos")

def portfolio_log(state, wallet, prices):
    now = time.time()
    if now - state.get("last_portfolio_log", 0) < 300:
        return
    state["last_portfolio_log"] = now
    total = wallet.get("USD", 0)
    for coin, held in wallet.items():
        if coin == "USD":
            continue
        px = prices.get(f"{coin}/USD", {}).get("last", 0)
        total += held * px
    log.info(f"PORTFOLIO ${total:,.0f} | Cash ${wallet.get('USD', 0):,.0f} | Swing: {state['swing']['phase']}")

# ── Main loop ──

def main():
    global SCANNER_PAIRS
    log.info("Combined bot starting")
    send_telegram("Combined bot started (swing + scanner)")

    # Load all tradeable pairs for scanner dynamically
    try:
        info = api_get("/v3/exchangeInfo")
        all_pairs = list(info.get("TradePairs", {}).keys())
        SCANNER_PAIRS = [p for p in all_pairs if p not in SCANNER_EXCLUDE]
        log.info(f"Scanner watching {len(SCANNER_PAIRS)} coins (excluded {len(SCANNER_EXCLUDE)})")
    except Exception as e:
        log.error(f"Failed to load exchange pairs: {e}")
        SCANNER_PAIRS = ["CAKE/USD", "AVAX/USD", "AAVE/USD", "LINK/USD",
                         "FET/USD", "PENDLE/USD", "TAO/USD", "SUI/USD"]

    state = load_state()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # Competition close check
            if now_utc >= CLOSE_ALL_TIME:
                prices = get_prices()
                wallet = get_wallet()
                close_all(wallet, prices)
                save_state(state)
                log.info("Competition over. Exiting.")
                send_telegram("Competition over. Bot stopped.")
                return

            prices = get_prices()
            wallet = get_wallet()

            # Startup sells (once)
            startup_sells(state, wallet, prices)

            # Adopt any untracked legacy positions
            adopt_legacy(state, wallet, prices)

            # ── SWING ──
            swing_check_exits(state, prices, wallet)
            if swing_should_buy(state, prices):
                swing_deploy(state, prices)
            swing_update_highs(state, prices)

            # ── SCANNER ──
            scanner_check_exits(state, prices, wallet)
            scanner_check_entries(state, prices, wallet)

            # ── LEGACY ──
            legacy_check_exits(state, prices, wallet)

            # ── Reporting ──
            hourly_status(state, wallet, prices)
            portfolio_log(state, wallet, prices)

            save_state(state)

        except requests.exceptions.RequestException as e:
            log.warning(f"API error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
