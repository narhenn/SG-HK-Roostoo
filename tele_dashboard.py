"""
Telegram Dashboard — sends full status to Telegram every 5 minutes.
Shows: portfolio, positions, P&L, market, regime, top movers, bot health.
Runs on EC2 alongside adaptive_bot.py.
"""
import time, json, requests, os
from datetime import datetime, timezone

try:
    from config_secrets import API_KEY, SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except:
    print("ERROR: config_secrets.py not found")
    exit(1)

from roostoo_client import RoostooClient

client = RoostooClient()
STARTING_CAPITAL = 1000000
INTERVAL = 300  # 5 minutes

def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def get_status():
    # Ticker
    try:
        all_ticker = client.get_ticker().get('Data', {})
    except:
        return "❌ API error — can't fetch ticker"

    # Wallet
    try:
        bal = client.get_balance()
        wallet = bal.get('SpotWallet', {})
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
    except:
        return "❌ API error — can't fetch balance"

    pnl = total - STARTING_CAPITAL
    pnl_pct = pnl / STARTING_CAPITAL * 100
    deployed = total - usd
    dep_pct = deployed / total * 100 if total > 0 else 0

    # Bot state
    state = {}
    try:
        with open('adaptive_state.json') as f:
            state = json.load(f)
    except: pass

    v8_pos = state.get('positions', {})
    rsi_pos = state.get('rsi_positions', {})
    regime = state.get('regime', '?')
    bot_pnl = state.get('total_pnl', 0)
    cycle = state.get('_cycle', 0)
    consec = state.get('_consecutive_stops', 0)
    trade_results = state.get('_trade_results', [])

    # Market
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

    # Emoji
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    regime_emoji = "🔥" if regime == "VOLATILE" else "🌊" if regime == "NORMAL" else "❓"

    # Kill switch
    kill = ""
    if len(trade_results) >= 5 and sum(trade_results[-5:]) == 0:
        kill = "\n⛔ KILL SWITCH ACTIVE"
    elif consec >= 2:
        kill = f"\n⚠️ {consec} consecutive stops"

    # Build message
    msg = f"""<b>📊 QuantX V10 Dashboard</b>
━━━━━━━━━━━━━━━━━━

{pnl_emoji} <b>Portfolio: ${total:,.0f}</b>
   P&L: ${pnl:+,.0f} ({pnl_pct:+.2f}%)
   Cash: ${usd:,.0f}
   Deployed: ${deployed:,.0f} ({dep_pct:.0f}%)

💰 <b>BTC: ${btc_price:,.2f}</b> ({btc_chg*100:+.1f}%)

{regime_emoji} <b>Regime: {regime}</b>
   Breadth: {breadth:.0f}% ({green}/{len(movers)} green)
   Cycle: #{cycle}
   Bot P&L: ${bot_pnl:+,.0f}{kill}"""

    # V8 positions
    if v8_pos:
        msg += "\n\n<b>📈 V8 Bounce Positions:</b>"
        for pair, pos in v8_pos.items():
            cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
            entry = pos.get('entry', 0)
            pp = (cp - entry) / entry * 100 if entry > 0 else 0
            val = pos.get('qty', 0) * cp
            added = " +50%" if pos.get('added') else ""
            emoji = "🟢" if pp >= 0 else "🔴"
            msg += f"\n   {emoji} {pair}: ${cp:.4f} ({pp:+.2f}%) ${val:,.0f}{added}"

    # RSI positions
    if rsi_pos:
        msg += "\n\n<b>📊 RSI Positions:</b>"
        for pair, pos in rsi_pos.items():
            cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
            entry = pos.get('entry', 0)
            pp = (cp - entry) / entry * 100 if entry > 0 else 0
            val = pos.get('qty', 0) * cp
            emoji = "🟢" if pp >= 0 else "🔴"
            msg += f"\n   {emoji} {pair}: ${cp:.4f} ({pp:+.2f}%) ${val:,.0f}"

    if not v8_pos and not rsi_pos:
        msg += "\n\n💤 No open positions — waiting for signals"

    # Holdings
    if holdings:
        msg += "\n\n<b>👛 Wallet:</b>"
        for h in holdings[:8]:
            msg += f"\n   {h['coin']}: {h['qty']:.4f} × ${h['price']:.4f} = ${h['value']:,.0f} ({h['change']*100:+.1f}%)"

    # Top movers
    msg += "\n\n<b>🚀 Top 5 Gainers:</b>"
    for c, pair, p in movers[:5]:
        msg += f"\n   {pair}: {c*100:+.1f}%"

    msg += "\n\n<b>📉 Top 5 Losers:</b>"
    for c, pair, p in movers[-5:][::-1]:
        msg += f"\n   {pair}: {c*100:+.1f}%"

    now = datetime.now(timezone.utc).strftime('%H:%M UTC')
    msg += f"\n\n<i>Updated: {now} • Next in 5 min</i>"

    return msg


def main():
    print("Telegram Dashboard starting...")
    send_tg("📊 <b>Dashboard bot started</b>\nSending updates every 5 minutes")

    while True:
        try:
            msg = get_status()
            send_tg(msg)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Status sent to Telegram")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
