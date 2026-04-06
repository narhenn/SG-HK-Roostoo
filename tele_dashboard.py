"""
Telegram Dashboard Bot — interactive commands + auto updates.
Commands: /d = dashboard, /m = market, /p = positions, /h = help
Also sends auto update every 10 minutes.
Runs on EC2 alongside adaptive_bot.py.
"""
import time, json, requests, os, threading
from datetime import datetime, timezone

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except:
    print("ERROR: config_secrets.py not found")
    exit(1)

from roostoo_client import RoostooClient

client = RoostooClient()
STARTING_CAPITAL = 1000000
AUTO_INTERVAL = 600  # auto update every 10 min
POLL_INTERVAL = 2    # check for commands every 2 sec
last_update_id = 0


def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def get_ticker_safe():
    try:
        return client.get_ticker().get('Data', {})
    except:
        return {}


def get_wallet_safe():
    try:
        bal = client.get_balance()
        return bal.get('SpotWallet', {})
    except:
        return {}


def get_state():
    try:
        with open('adaptive_state.json') as f:
            return json.load(f)
    except:
        return {}


def cmd_dashboard():
    """Full dashboard — /d"""
    all_ticker = get_ticker_safe()
    wallet = get_wallet_safe()
    state = get_state()
    if not all_ticker:
        return "API error"

    usd = float(wallet.get('USD', {}).get('Free', 0))
    total = usd
    holdings = []
    for coin, info in wallet.items():
        if coin == 'USD': continue
        free = float(info.get('Free', 0))
        if free > 0.0001:
            pair = f"{coin}/USD"
            cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
            val = free * cp
            chg = float(all_ticker.get(pair, {}).get('Change', 0))
            total += val
            holdings.append({'coin': coin, 'qty': free, 'price': cp, 'value': val, 'change': chg})
    holdings.sort(key=lambda x: -x['value'])

    pnl = total - STARTING_CAPITAL
    pnl_pct = pnl / STARTING_CAPITAL * 100
    deployed = total - usd
    dep_pct = deployed / total * 100 if total > 0 else 0

    v8_pos = state.get('positions', {})
    rsi_pos = state.get('rsi_positions', {})
    regime = state.get('regime', '?')
    bot_pnl = state.get('total_pnl', 0)
    cycle = state.get('_cycle', 0)
    consec = state.get('_consecutive_stops', 0)
    trade_results = state.get('_trade_results', [])

    movers = []
    for pair, info in all_ticker.items():
        try:
            c = float(info.get('Change', 0))
            p = float(info.get('LastPrice', 0))
            if p > 0: movers.append((c, pair, p))
        except: pass
    movers.sort(key=lambda x: -x[0])
    green = len([m for m in movers if m[0] > 0])
    breadth = green / len(movers) * 100 if movers else 50

    btc = all_ticker.get('BTC/USD', {})
    btc_price = float(btc.get('LastPrice', 0))
    btc_chg = float(btc.get('Change', 0))

    pnl_e = "🟢" if pnl >= 0 else "🔴"
    reg_e = "🔥" if regime == "VOLATILE" else "🌊" if regime == "NORMAL" else "❓"

    kill = ""
    if len(trade_results) >= 5 and sum(trade_results[-5:]) == 0:
        kill = "\n⛔ KILL SWITCH ACTIVE"
    elif consec >= 2:
        kill = f"\n⚠️ {consec} consecutive stops"

    msg = f"""<b>📊 QuantX V10 Dashboard</b>
━━━━━━━━━━━━━━━━━━

{pnl_e} <b>Portfolio: ${total:,.0f}</b>
   P&L: ${pnl:+,.0f} ({pnl_pct:+.2f}%)
   Cash: ${usd:,.0f} | Deployed: ${deployed:,.0f} ({dep_pct:.0f}%)

💰 <b>BTC: ${btc_price:,.2f}</b> ({btc_chg*100:+.1f}%)

{reg_e} <b>{regime}</b> | Breadth {breadth:.0f}% | Cycle #{cycle}
   Bot P&L: ${bot_pnl:+,.0f}{kill}"""

    if v8_pos:
        msg += "\n\n<b>📈 V8 Positions:</b>"
        for pair, pos in v8_pos.items():
            cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
            entry = pos.get('entry', 0)
            pp = (cp - entry) / entry * 100 if entry > 0 else 0
            val = pos.get('qty', 0) * cp
            added = " +ADD" if pos.get('added') else ""
            e = "🟢" if pp >= 0 else "🔴"
            msg += f"\n   {e} {pair} ${cp:.4f} ({pp:+.2f}%) ${val:,.0f}{added}"
            msg += f"\n      Stop: ${pos.get('stop',0):.4f} | TP: ${pos.get('tp',0):.4f}"

    if rsi_pos:
        msg += "\n\n<b>📊 RSI Positions:</b>"
        for pair, pos in rsi_pos.items():
            cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
            entry = pos.get('entry', 0)
            pp = (cp - entry) / entry * 100 if entry > 0 else 0
            val = pos.get('qty', 0) * cp
            e = "🟢" if pp >= 0 else "🔴"
            msg += f"\n   {e} {pair} ${cp:.4f} ({pp:+.2f}%) ${val:,.0f}"

    if not v8_pos and not rsi_pos:
        msg += "\n\n💤 No positions — waiting for signals"

    if holdings:
        msg += "\n\n<b>👛 Wallet:</b>"
        for h in holdings[:6]:
            msg += f"\n   {h['coin']}: ${h['value']:,.0f} ({h['change']*100:+.1f}%)"

    now = datetime.now(timezone.utc).strftime('%H:%M UTC')
    msg += f"\n\n<i>{now} | /d /m /p /h</i>"
    return msg


def cmd_market():
    """Market overview — /m"""
    all_ticker = get_ticker_safe()
    if not all_ticker:
        return "API error"

    movers = []
    for pair, info in all_ticker.items():
        try:
            c = float(info.get('Change', 0))
            p = float(info.get('LastPrice', 0))
            v = float(info.get('UnitTradeValue', 0))
            if p > 0: movers.append((c, pair, p, v))
        except: pass
    movers.sort(key=lambda x: -x[0])
    green = len([m for m in movers if m[0] > 0])
    breadth = green / len(movers) * 100 if movers else 50

    msg = f"""<b>🌍 Market Overview</b>
━━━━━━━━━━━━━━━━━━
Breadth: <b>{breadth:.0f}%</b> ({green}/{len(movers)} green)

<b>🚀 Top 10 Gainers:</b>"""
    for c, pair, p, v in movers[:10]:
        msg += f"\n   {pair}: <b>{c*100:+.1f}%</b> ${p:.4f} vol=${v:,.0f}"

    msg += "\n\n<b>📉 Top 5 Losers:</b>"
    for c, pair, p, v in movers[-5:][::-1]:
        msg += f"\n   {pair}: <b>{c*100:+.1f}%</b> ${p:.4f}"

    msg += f"\n\n<i>/d /m /p /h</i>"
    return msg


def cmd_positions():
    """Detailed positions — /p"""
    all_ticker = get_ticker_safe()
    state = get_state()
    wallet = get_wallet_safe()

    v8_pos = state.get('positions', {})
    rsi_pos = state.get('rsi_positions', {})

    if not v8_pos and not rsi_pos:
        # Show wallet instead
        usd = float(wallet.get('USD', {}).get('Free', 0))
        msg = f"💤 <b>No bot positions open</b>\nCash: ${usd:,.0f}\n"
        holdings = []
        for coin, info in wallet.items():
            if coin == 'USD': continue
            free = float(info.get('Free', 0))
            if free > 0.0001:
                pair = f"{coin}/USD"
                cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
                holdings.append((coin, free, cp, free * cp))
        if holdings:
            msg += "\n<b>Wallet holdings:</b>"
            for coin, qty, price, val in sorted(holdings, key=lambda x: -x[3]):
                msg += f"\n   {coin}: {qty:,.4f} × ${price:.4f} = ${val:,.0f}"
        return msg

    msg = "<b>📋 All Positions</b>\n━━━━━━━━━━━━━━━━━━"

    total_pos_pnl = 0
    for pair, pos in v8_pos.items():
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry', 0)
        pp = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        pnl_usd = (cp - entry) * pos.get('qty', 0)
        total_pos_pnl += pnl_usd
        added = " 💪+50%" if pos.get('added') else ""
        e = "🟢" if pp >= 0 else "🔴"
        entry_time = pos.get('time', '?')[:16]
        msg += f"\n\n{e} <b>V8 {pair}</b>{added}"
        msg += f"\n   Entry: ${entry:.4f} → Now: ${cp:.4f}"
        msg += f"\n   P&L: ${pnl_usd:+,.0f} ({pp:+.2f}%)"
        msg += f"\n   Stop: ${pos.get('stop',0):.4f} | TP: ${pos.get('tp',0):.4f}"
        msg += f"\n   Size: ${val:,.0f} | Since: {entry_time}"

    for pair, pos in rsi_pos.items():
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry', 0)
        pp = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        pnl_usd = (cp - entry) * pos.get('qty', 0)
        total_pos_pnl += pnl_usd
        e = "🟢" if pp >= 0 else "🔴"
        msg += f"\n\n{e} <b>RSI {pair}</b> (RSI@entry={pos.get('rsi_at_entry','?')})"
        msg += f"\n   Entry: ${entry:.4f} → Now: ${cp:.4f}"
        msg += f"\n   P&L: ${pnl_usd:+,.0f} ({pp:+.2f}%)"
        msg += f"\n   Size: ${val:,.0f}"

    te = "🟢" if total_pos_pnl >= 0 else "🔴"
    msg += f"\n\n{te} <b>Total open P&L: ${total_pos_pnl:+,.0f}</b>"
    msg += f"\n\n<i>/d /m /p /h</i>"
    return msg


def cmd_help():
    return """<b>📖 Commands</b>
━━━━━━━━━━━━━━━━━━
/d — Full dashboard (portfolio + positions + market)
/m — Market overview (top gainers/losers, breadth)
/p — Detailed positions (entry, stop, TP, P&L)
/h — This help message

Auto updates every 10 minutes.
Bot: V10 Adaptive (V8 bounce + RSI oversold)"""


def poll_commands():
    """Poll Telegram for commands."""
    global last_update_id
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35,
            ).json()

            for update in resp.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                if text in ["/d", "/dashboard", "d"]:
                    send_tg(cmd_dashboard())
                elif text in ["/m", "/market", "m"]:
                    send_tg(cmd_market())
                elif text in ["/p", "/positions", "/pos", "p"]:
                    send_tg(cmd_positions())
                elif text in ["/h", "/help", "h", "/start"]:
                    send_tg(cmd_help())

        except Exception as e:
            print(f"Poll error: {e}")
            time.sleep(5)


def auto_updates():
    """Send dashboard every 10 minutes."""
    while True:
        time.sleep(AUTO_INTERVAL)
        try:
            send_tg(cmd_dashboard())
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Auto update sent")
        except Exception as e:
            print(f"Auto error: {e}")


def main():
    print("Telegram Dashboard Bot starting...")
    send_tg(cmd_help())

    # Start command polling in background
    t1 = threading.Thread(target=poll_commands, daemon=True)
    t1.start()

    # Start auto updates in background
    t2 = threading.Thread(target=auto_updates, daemon=True)
    t2.start()

    print("Listening for commands + auto updates every 10 min")

    # Keep main thread alive
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
