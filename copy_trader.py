"""
Copy Trader — auto-trades when another team buys/sells.
Detects team activity by comparing Roostoo vs Binance prices.
When Roostoo diverges > threshold = a team is trading.

Strategy:
- Team BUYING (Roostoo > Binance): buy behind them, ride momentum
- Gap closes or reverses: sell
- Hard stop: -0.5% from entry
- Trail stop: 1% trailing from peak
- Max hold: 30 minutes (gaps are short-lived)
"""

import os
import time
import json
import logging
import requests
import threading
from datetime import datetime, timezone
from collections import deque
from roostoo_client import RoostooClient

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    API_KEY = os.environ.get("ROOSTOO_API_KEY", "")
    SECRET_KEY = os.environ.get("ROOSTOO_SECRET_KEY", "")
    TELEGRAM_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

try:
    os.makedirs("logs", exist_ok=True)
    _lf = "logs/copy_trader.log"
except:
    _lf = "copy_trader.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(_lf), logging.StreamHandler()])
log = logging.getLogger()

client = RoostooClient()

# Coins to skip (low liquidity, meme coins, or consistently lose)
EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD'}

# ── Config ──
SCAN_INTERVAL = 3           # check every 3 seconds
GAP_BUY_THRESHOLD = 0.20    # buy when Roostoo > Binance by 0.20%+
GAP_SELL_THRESHOLD = -0.10   # sell when gap closes below -0.10%
POSITION_SIZE = 100000       # $100k per copy trade
MAX_POSITIONS = 3            # max 3 simultaneous copy trades
HARD_STOP_PCT = 0.005        # 0.5% hard stop
TRAIL_STOP_PCT = 0.01        # 1% trailing stop
MAX_HOLD_SEC = 1800          # 30 min max hold (gaps are transient)
COOLDOWN_SEC = 600           # 10 min cooldown per coin after exit
MIN_GAP_TICKS = 2            # need gap for 2 consecutive ticks (filter noise)

# ── Precision (same as adaptive bot) ──
PRECISION = {
    "BTC/USD": {"price": 2, "amount": 5},
    "ETH/USD": {"price": 2, "amount": 3},
    "SOL/USD": {"price": 2, "amount": 2},
    "BNB/USD": {"price": 2, "amount": 3},
    "XRP/USD": {"price": 4, "amount": 1},
    "DOGE/USD": {"price": 5, "amount": 0},
    "ADA/USD": {"price": 4, "amount": 0},
    "AVAX/USD": {"price": 2, "amount": 2},
    "LINK/USD": {"price": 3, "amount": 1},
    "DOT/USD": {"price": 3, "amount": 1},
    "SUI/USD": {"price": 4, "amount": 1},
    "NEAR/USD": {"price": 3, "amount": 1},
    "HBAR/USD": {"price": 4, "amount": 0},
    "ARB/USD": {"price": 4, "amount": 0},
    "FET/USD": {"price": 4, "amount": 0},
    "CFX/USD": {"price": 4, "amount": 0},
    "CRV/USD": {"price": 4, "amount": 0},
    "TRUMP/USD": {"price": 3, "amount": 1},
}
DEFAULT_PREC = {"price": 4, "amount": 1}

# ── State ──
positions = {}       # pair -> {entry, qty, stop, peak, time, gap_at_entry}
cooldowns = {}       # pair -> cooldown_until timestamp
gap_streak = {}      # pair -> consecutive ticks with gap > threshold
gap_history = {}     # pair -> deque of last 20 gaps (for analysis)
total_pnl = 0
trade_count = 0
win_count = 0

STATE_FILE = "copy_state.json"


def prec(pair):
    return PRECISION.get(pair, DEFAULT_PREC)


def floor_qty(qty, decimals):
    import math
    mult = 10 ** decimals
    return math.floor(qty * mult) / mult


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=3)
        except: pass
    threading.Thread(target=_send, daemon=True).start()


def get_binance():
    """Fetch all Binance USDT prices in one call."""
    try:
        r = requests.get('https://api.binance.com/api/v3/ticker/price', timeout=3)
        return {t['symbol']: float(t['price']) for t in r.json()}
    except:
        return None


def get_roostoo():
    """Fetch all Roostoo prices."""
    try:
        data = client.get_ticker().get('Data', {})
        out = {}
        for pair, t in data.items():
            out[pair] = {
                "last": float(t.get("LastPrice", 0)),
                "bid": float(t.get("MaxBid", 0)),
                "ask": float(t.get("MinAsk", 0)),
                "vol": float(t.get("CoinTradeValue", 0)),
            }
        return out
    except:
        return None


def place_buy(pair, qty):
    p = prec(pair)
    qty = floor_qty(qty, p["amount"])
    if qty <= 0:
        return None
    log.info(f"COPY BUY {pair}: qty={qty} @ MARKET")
    params = {"pair": pair, "side": "BUY", "type": "MARKET", "quantity": str(qty)}
    resp = client._post("/v3/place_order", params)
    if not resp.get("Success"):
        log.error(f"BUY {pair} REJECTED: {resp.get('ErrMsg', 'unknown')}")
        return None
    detail = resp.get("Data", {})
    filled = float(detail.get("FilledQuantity", 0))
    fill_px = float(detail.get("AvgFillPrice", 0)) if detail.get("AvgFillPrice") else 0
    return {"filled": filled, "fill_price": fill_px}


def place_sell(pair, qty):
    p = prec(pair)
    qty = floor_qty(qty, p["amount"])
    if qty <= 0:
        return None
    log.info(f"COPY SELL {pair}: qty={qty} @ MARKET")
    params = {"pair": pair, "side": "SELL", "type": "MARKET", "quantity": str(qty)}
    resp = client._post("/v3/place_order", params)
    if not resp.get("Success"):
        log.error(f"SELL {pair} REJECTED: {resp.get('ErrMsg', 'unknown')}")
        return None
    return True


def save_state():
    try:
        s = {
            "positions": {p: {**v, "time": v["time"]} for p, v in positions.items()},
            "cooldowns": {p: t for p, t in cooldowns.items()},
            "total_pnl": total_pnl,
            "trade_count": trade_count,
            "win_count": win_count,
        }
        json.dump(s, open(STATE_FILE, "w"), indent=2)
    except: pass


def load_state():
    global total_pnl, trade_count, win_count
    try:
        s = json.load(open(STATE_FILE))
        total_pnl = s.get("total_pnl", 0)
        trade_count = s.get("trade_count", 0)
        win_count = s.get("win_count", 0)
    except: pass


def scan_and_trade():
    """Main loop: scan for gaps, enter/exit copy trades."""
    global total_pnl, trade_count, win_count

    load_state()

    log.info("=" * 60)
    log.info("COPY TRADER STARTING")
    log.info(f"Gap threshold: {GAP_BUY_THRESHOLD}% | Size: ${POSITION_SIZE:,} | Max: {MAX_POSITIONS} pos")
    log.info(f"Stop: {HARD_STOP_PCT*100}% | Trail: {TRAIL_STOP_PCT*100}% | Max hold: {MAX_HOLD_SEC//60}min")
    log.info("=" * 60)
    send_telegram(
        "<b>COPY TRADER ONLINE</b>\n"
        f"Gap threshold: {GAP_BUY_THRESHOLD}%\n"
        f"Size: ${POSITION_SIZE:,} | Max: {MAX_POSITIONS} pos\n"
        f"Stop: {HARD_STOP_PCT*100}% | Trail: {TRAIL_STOP_PCT*100}%"
    )

    tick = 0

    while True:
        try:
            binance = get_binance()
            if not binance:
                time.sleep(SCAN_INTERVAL)
                continue

            roostoo = get_roostoo()
            if not roostoo:
                time.sleep(SCAN_INTERVAL)
                continue

            now = time.time()
            tick += 1

            # ── Check exits on existing positions ──
            to_close = []
            for pair, pos in positions.items():
                r = roostoo.get(pair)
                if not r or r["last"] <= 0:
                    continue

                current = r["last"]
                entry = pos["entry"]
                pnl_pct = (current - entry) / entry

                # Update peak for trailing stop
                if current > pos["peak"]:
                    pos["peak"] = current

                # Check gap — if gap closed, team stopped buying
                coin = pair.split('/')[0]
                bsym = f'{coin}USDT'
                b_px = binance.get(bsym, 0)
                gap = (current - b_px) / b_px * 100 if b_px > 0 else 0

                reason = None

                # Hard stop
                if pnl_pct <= -HARD_STOP_PCT:
                    reason = "STOP"

                # Trail stop (from peak)
                elif pos["peak"] > 0 and current <= pos["peak"] * (1 - TRAIL_STOP_PCT):
                    reason = "TRAIL"

                # Gap reversed — team stopped buying or started selling
                elif gap < GAP_SELL_THRESHOLD:
                    reason = "GAP_CLOSE"

                # Time stop
                elif now - pos["time"] > MAX_HOLD_SEC:
                    reason = "TIME"

                if reason:
                    pnl = (current - entry) * pos["qty"]
                    total_pnl += pnl
                    trade_count += 1
                    if pnl > 0:
                        win_count += 1

                    log.info(f"COPY EXIT {pair} ({reason}): P&L=${pnl:+,.0f} ({pnl_pct:+.2%}) gap={gap:+.3f}%")
                    emoji = "+" if pnl > 0 else "-"
                    send_telegram(
                        f"<b>COPY EXIT {pair}</b>\n"
                        f"Reason: {reason}\n"
                        f"P&L: ${pnl:+,.0f} ({pnl_pct:+.2%})\n"
                        f"Gap now: {gap:+.3f}%\n"
                        f"Total P&L: ${total_pnl:+,.0f} ({trade_count} trades, {win_count}W)"
                    )
                    place_sell(pair, pos["qty"])
                    to_close.append(pair)
                    cooldowns[pair] = now + COOLDOWN_SEC

            for pair in to_close:
                positions.pop(pair, None)

            # ── Scan for new entries ──
            if len(positions) < MAX_POSITIONS:
                # Get wallet for cash check
                try:
                    wallet = client.get_balance().get('SpotWallet', {})
                    cash = float(wallet.get('USD', {}).get('Free', 0))
                except:
                    cash = 0

                for pair, r in roostoo.items():
                    if pair in EXCLUDED or pair in positions:
                        continue
                    if len(positions) >= MAX_POSITIONS:
                        break

                    # Cooldown check
                    if pair in cooldowns and now < cooldowns[pair]:
                        continue

                    coin = pair.split('/')[0]
                    bsym = f'{coin}USDT'
                    b_px = binance.get(bsym, 0)
                    r_px = r["last"]

                    if b_px <= 0 or r_px <= 0:
                        continue

                    gap = (r_px - b_px) / b_px * 100

                    # Track gap history
                    if pair not in gap_history:
                        gap_history[pair] = deque(maxlen=20)
                    gap_history[pair].append(gap)

                    # Track consecutive positive gaps
                    if gap >= GAP_BUY_THRESHOLD:
                        gap_streak[pair] = gap_streak.get(pair, 0) + 1
                    else:
                        gap_streak[pair] = 0

                    # Entry: gap sustained for MIN_GAP_TICKS consecutive scans
                    if gap_streak.get(pair, 0) >= MIN_GAP_TICKS:
                        if cash < POSITION_SIZE * 1.01:
                            continue

                        ask = r["ask"] if r["ask"] > 0 else r_px
                        p = prec(pair)
                        qty = floor_qty(POSITION_SIZE / ask, p["amount"])
                        if qty <= 0:
                            continue

                        result = place_buy(pair, qty)
                        if result and result["filled"] > 0 and result["fill_price"] > 0:
                            ep = result["fill_price"]
                            positions[pair] = {
                                "entry": ep,
                                "qty": result["filled"],
                                "stop": ep * (1 - HARD_STOP_PCT),
                                "peak": ep,
                                "time": now,
                                "gap_at_entry": gap,
                            }
                            cash -= result["filled"] * ep
                            gap_streak[pair] = 0  # reset streak

                            log.info(f"COPY BUY {pair}: {result['filled']:.4f} @ ${ep:.4f} gap={gap:+.3f}%")
                            send_telegram(
                                f"<b>COPY BUY {pair}</b>\n"
                                f"Gap: {gap:+.3f}% (team buying)\n"
                                f"Price: ${ep:.4f}\n"
                                f"Size: ${result['filled']*ep:,.0f}\n"
                                f"Stop: ${ep*(1-HARD_STOP_PCT):.4f}"
                            )

            # ── Status logging ──
            if tick % 100 == 0:  # every ~5 min
                wr = f"{win_count}/{trade_count}" if trade_count > 0 else "0/0"
                log.info(f"Copy: {len(positions)} pos | {wr} trades | P&L=${total_pnl:+,.0f}")
                # Log current gaps for active positions
                for pair, pos in positions.items():
                    coin = pair.split('/')[0]
                    b_px = binance.get(f'{coin}USDT', 0)
                    r_px = roostoo.get(pair, {}).get("last", 0)
                    gap = (r_px - b_px) / b_px * 100 if b_px > 0 else 0
                    pnl_pct = (r_px - pos["entry"]) / pos["entry"] * 100
                    log.info(f"  {pair}: gap={gap:+.3f}% pnl={pnl_pct:+.2f}% held={int(now-pos['time'])}s")

            save_state()

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(SCAN_INTERVAL)


# ── Dry run / backtest mode ──
def dry_run(duration_sec=300):
    """Run for N seconds without trading, just log what WOULD happen."""
    log.info(f"DRY RUN for {duration_sec}s — no real trades")
    send_telegram(f"Copy Trader DRY RUN for {duration_sec//60}min")

    signals = []
    start = time.time()
    tick = 0

    while time.time() - start < duration_sec:
        try:
            binance = get_binance()
            roostoo = get_roostoo()
            if not binance or not roostoo:
                time.sleep(SCAN_INTERVAL)
                continue

            tick += 1
            now = time.time()

            for pair, r in roostoo.items():
                if pair in EXCLUDED:
                    continue
                coin = pair.split('/')[0]
                bsym = f'{coin}USDT'
                b_px = binance.get(bsym, 0)
                r_px = r["last"]
                if b_px <= 0 or r_px <= 0:
                    continue

                gap = (r_px - b_px) / b_px * 100

                if abs(gap) > GAP_BUY_THRESHOLD:
                    direction = "BUY" if gap > 0 else "SELL"
                    signals.append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "pair": pair,
                        "gap": gap,
                        "direction": direction,
                        "r_px": r_px,
                        "b_px": b_px,
                        "vol": r["vol"],
                    })
                    log.info(f"SIGNAL: {direction} {pair} gap={gap:+.3f}% roostoo=${r_px:.4f} binance=${b_px:.4f}")

            if tick % 20 == 0:
                elapsed = int(time.time() - start)
                log.info(f"Dry run: {elapsed}s / {duration_sec}s | {len(signals)} signals so far")

        except Exception as e:
            log.error(f"Dry run error: {e}")

        time.sleep(SCAN_INTERVAL)

    # Summary
    log.info(f"\n{'='*60}")
    log.info(f"DRY RUN COMPLETE — {len(signals)} signals in {duration_sec}s")
    if signals:
        pairs_seen = set(s["pair"] for s in signals)
        log.info(f"Coins with signals: {len(pairs_seen)}")
        for pair in pairs_seen:
            pair_signals = [s for s in signals if s["pair"] == pair]
            avg_gap = sum(s["gap"] for s in pair_signals) / len(pair_signals)
            log.info(f"  {pair}: {len(pair_signals)} signals, avg gap={avg_gap:+.3f}%")
    else:
        log.info("No signals detected — market is quiet or gap threshold too high")
    log.info(f"{'='*60}")

    return signals


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "dry":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 300
        dry_run(duration)
    else:
        import fcntl
        lock = open('/tmp/copy_trader.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            print("Copy Trader already running!")
            exit(1)
        scan_and_trade()
