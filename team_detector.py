"""
Team Activity Detector — catches when another team buys/sells big.
Compares Roostoo vs Binance every 2 seconds.
When Roostoo diverges from Binance = a team is trading.

Signal: gap > 0.15% = team BUYING → we buy behind them
Signal: gap < -0.15% = team SELLING → we avoid / sell

Telegram alerts on detection.
"""

import requests
import time
from datetime import datetime
from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

client = RoostooClient()
EXCLUDED = {'PAXG/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD', '1000CHEEMS/USD'}

alerted = {}  # pair -> last alert time
GAP_THRESHOLD = 0.15  # 0.15% divergence = team activity
ALERT_COOLDOWN = 300  # 5 min cooldown per coin


def send_alert(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5)
    except: pass
    print(msg.replace('<b>','').replace('</b>',''))


def get_binance():
    try:
        r = requests.get('https://api.binance.com/api/v3/ticker/price', timeout=3)
        return {t['symbol']: float(t['price']) for t in r.json()}
    except:
        return None


def main():
    print("="*60)
    print("TEAM ACTIVITY DETECTOR")
    print("Comparing Roostoo vs Binance every 2 seconds")
    print("Alert when gap > 0.15% = another team is trading big")
    print("="*60)

    send_alert(
        "<b>TEAM DETECTOR ONLINE</b>\n"
        "Comparing Roostoo vs Binance every 2s\n"
        "Will alert when a team buys/sells big"
    )

    tick = 0
    while True:
        try:
            binance = get_binance()
            if not binance:
                time.sleep(2)
                continue

            td = client.get_ticker().get('Data', {})
            tick += 1
            now = time.time()

            for pair, info in td.items():
                if pair in EXCLUDED: continue
                coin = pair.split('/')[0]
                bsym = f'{coin}USDT'
                b_px = binance.get(bsym, 0)
                r_px = float(info.get('LastPrice', 0))

                if b_px <= 0 or r_px <= 0: continue

                gap = (r_px - b_px) / b_px * 100

                # Cooldown check
                if pair in alerted and now - alerted[pair] < ALERT_COOLDOWN:
                    continue

                if abs(gap) > GAP_THRESHOLD:
                    alerted[pair] = now

                    if gap > 0:
                        action = "TEAM BUYING"
                        emoji = "🟢"
                        advice = "Consider buying — riding their momentum"
                    else:
                        action = "TEAM SELLING"
                        emoji = "🔴"
                        advice = "AVOID — someone dumping"

                    vol = float(info.get('CoinTradeValue', 0))
                    spread = 0
                    bid = float(info.get('MaxBid', 0))
                    ask = float(info.get('MinAsk', 0))
                    if bid > 0: spread = (ask-bid)/bid*100

                    msg = (
                        f"<b>{emoji} {action}: {pair}</b>\n"
                        f"Roostoo: ${r_px:.4f}\n"
                        f"Binance: ${b_px:.4f}\n"
                        f"Gap: {gap:+.3f}%\n"
                        f"Volume: ${vol:,.0f}\n"
                        f"Spread: {spread:.3f}%\n"
                        f"\n{advice}"
                    )
                    send_alert(msg)

            # Status every 5 min
            if tick % 150 == 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Tick {tick} — all quiet")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    main()
