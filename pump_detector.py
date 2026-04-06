"""
Pump + News Detector — watches 66 coins + crypto news feeds.
Sends Telegram alert when:
1. Price/volume pump detected (same as before)
2. A Roostoo coin is mentioned in breaking news
3. BOTH happen = highest confidence signal

Run: python3 pump_detector.py
"""

import time
import requests
import json
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime

from roostoo_client import RoostooClient
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

client = RoostooClient()

# ── Price/Volume tracking ──
history = {}
HISTORY_MAX = 60
alerted = {}
EXCLUDED = {'PAXG/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD', '1000CHEEMS/USD'}

# ── News tracking ──
seen_news = set()
NEWS_CHECK_INTERVAL = 300  # Check news every 5 minutes
last_news_check = 0

# Map coin names/tickers to Roostoo pairs
COIN_NAMES = {}  # built dynamically from ticker data
NEWS_FEEDS = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://cointelegraph.com/rss',
]

# Keywords that suggest positive price action
BULLISH_KEYWORDS = [
    'surge', 'pump', 'rally', 'breakout', 'soar', 'spike', 'jump', 'moon',
    'bullish', 'upgrade', 'partnership', 'launch', 'mainnet', 'listing',
    'whale', 'accumulate', 'buy', 'all-time high', 'ath', 'record',
    'approved', 'adoption', 'integrate', 'billion',
]


def send_alert(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5)
    except:
        pass
    print(msg.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', ''))


def build_coin_map(ticker_data):
    """Build mapping from coin names to Roostoo pairs."""
    for pair in ticker_data:
        coin = pair.split('/')[0]
        COIN_NAMES[coin.lower()] = pair
        COIN_NAMES[coin.upper()] = pair
        # Common names
        name_map = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'BNB': 'binance',
            'XRP': 'ripple', 'ADA': 'cardano', 'DOGE': 'dogecoin', 'DOT': 'polkadot',
            'LINK': 'chainlink', 'AVAX': 'avalanche', 'NEAR': 'near protocol',
            'SUI': 'sui', 'SEI': 'sei', 'CAKE': 'pancakeswap', 'UNI': 'uniswap',
            'AAVE': 'aave', 'FET': 'fetch', 'TAO': 'bittensor', 'WIF': 'dogwifhat',
            'TRUMP': 'trump', 'EIGEN': 'eigenlayer', 'PENDLE': 'pendle',
            'ONDO': 'ondo', 'TRX': 'tron', 'HBAR': 'hedera', 'FIL': 'filecoin',
            'ICP': 'internet computer', 'LTC': 'litecoin', 'STO': 'stakestone',
            'HEMI': 'hemi', 'LINEA': 'linea', 'XPL': 'plasma',
        }
        if coin in name_map:
            COIN_NAMES[name_map[coin]] = pair
            COIN_NAMES[name_map[coin].upper()] = pair


def check_news():
    """Scan RSS feeds for mentions of Roostoo coins with bullish keywords."""
    global last_news_check
    now = time.time()
    if now - last_news_check < NEWS_CHECK_INTERVAL:
        return []
    last_news_check = now

    alerts = []
    for feed_url in NEWS_FEEDS:
        try:
            r = requests.get(feed_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            for item in root.iter('item'):
                title = (item.find('title').text or '').lower()
                desc = (item.find('description').text or '').lower() if item.find('description') is not None else ''
                link = item.find('link').text or '' if item.find('link') is not None else ''

                # Skip if we've seen this
                if title in seen_news:
                    continue
                seen_news.add(title)

                # Check if any Roostoo coin is mentioned
                text = title + ' ' + desc
                matched_coins = []
                for name, pair in COIN_NAMES.items():
                    if len(name) >= 3 and name.lower() in text:
                        if pair not in matched_coins:
                            matched_coins.append(pair)

                if not matched_coins:
                    continue

                # Check for bullish keywords
                bullish_score = sum(1 for kw in BULLISH_KEYWORDS if kw in text)
                if bullish_score == 0:
                    continue

                for pair in matched_coins:
                    alerts.append({
                        'pair': pair,
                        'title': item.find('title').text or '',
                        'bullish_score': bullish_score,
                        'source': feed_url.split('/')[2],
                    })
        except Exception as e:
            pass

    return alerts


def check_pump(pair, data):
    """Check if a coin is starting to pump."""
    if pair in EXCLUDED:
        return False, None

    price = float(data.get('LastPrice', 0))
    volume = float(data.get('CoinTradeValue', 0))
    change_24h = float(data.get('Change', 0))
    bid = float(data.get('MaxBid', 0))
    ask = float(data.get('MinAsk', 0))

    if price <= 0 or bid <= 0:
        return False, None

    if pair not in history:
        history[pair] = deque(maxlen=HISTORY_MAX)

    now = time.time()
    history[pair].append((now, price, volume))

    h = history[pair]
    if len(h) < 10:
        return False, None

    oldest_price = h[0][1]
    price_change = (price - oldest_price) / oldest_price * 100

    recent_price = h[-10][1]
    price_5min = (price - recent_price) / recent_price * 100

    volumes = [t[2] for t in h]
    avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else volumes[0]
    vol_ratio = volume / avg_vol if avg_vol > 0 else 1

    spread = (ask - bid) / bid * 100 if bid > 0 else 999

    if change_24h > 0.30 or spread > 0.5:
        return False, None

    if pair in alerted and now - alerted[pair] < 1800:
        return False, None

    signal = None

    # SIGNAL 1: Strong early pump
    if price_5min > 2.0 and vol_ratio > 3.0:
        signal = 'STRONG PUMP'

    # SIGNAL 2: Volume explosion
    elif vol_ratio > 5.0 and price_5min > 1.0:
        signal = 'VOLUME EXPLOSION'

    # SIGNAL 3: Steady climb
    elif len(h) >= 30 and price_change > 3.0:
        segments = [h[i][1] for i in range(0, len(h), 10)]
        if len(segments) >= 3 and all(segments[i] > segments[i-1] for i in range(1, len(segments))):
            signal = 'STEADY CLIMB'

    if signal:
        alerted[pair] = now
        return True, {
            'type': signal,
            'price': price,
            'price_5min': price_5min,
            'price_30min': price_change,
            'change_24h': change_24h * 100,
            'vol_ratio': vol_ratio,
            'volume': volume,
            'spread': spread,
        }

    return False, None


def main():
    print("=" * 60)
    print("PUMP + NEWS DETECTOR STARTED")
    print("Watching 66 coins (price/volume) every 30 seconds")
    print("Scanning CoinDesk + CoinTelegraph every 5 minutes")
    print("=" * 60)

    send_alert(
        "<b>PUMP + NEWS DETECTOR ONLINE</b>\n"
        "Price/volume scan: every 30s\n"
        "News scan: every 5 min\n"
        "Will alert on pump OR bullish news"
    )

    tick = 0
    while True:
        try:
            td = client.get_ticker().get('Data', {})
            tick += 1

            # Build coin name map on first tick
            if tick == 1:
                build_coin_map(td)
                print(f"Tracking {len(td)} coins, mapped {len(COIN_NAMES)} names")

            # ── Check price/volume pumps ──
            for pair, data in td.items():
                is_pump, details = check_pump(pair, data)
                if is_pump:
                    # Check if there's also news for this coin
                    news_alerts = check_news()
                    has_news = any(n['pair'] == pair for n in news_alerts)
                    confidence = "CONFIRMED (price + news)" if has_news else "price/volume only"

                    msg = (
                        f"<b>{'🚨🚨' if has_news else '🚨'} PUMP: {pair}</b>\n"
                        f"Signal: {details['type']}\n"
                        f"Confidence: {confidence}\n"
                        f"Price: ${details['price']:.4f}\n"
                        f"5min: {details['price_5min']:+.1f}% | 30min: {details['price_30min']:+.1f}%\n"
                        f"24h: {details['change_24h']:+.1f}%\n"
                        f"Volume: {details['vol_ratio']:.1f}x avg (${details['volume']:,.0f})\n"
                        f"Spread: {details['spread']:.2f}%\n"
                        f"\n$500k → if +5% = +$25,000"
                    )
                    send_alert(msg)

            # ── Check news (every 5 min) ──
            news_alerts = check_news()
            for na in news_alerts:
                pair = na['pair']
                # Check if this coin is also showing price movement
                h = history.get(pair)
                price_moving = False
                if h and len(h) >= 5:
                    recent = h[-5][1]
                    current = h[-1][1]
                    if recent > 0 and (current - recent) / recent > 0.005:
                        price_moving = True

                confidence = "CONFIRMED (news + price)" if price_moving else "news only"
                emoji = "📰🚨" if price_moving else "📰"

                msg = (
                    f"<b>{emoji} NEWS: {pair}</b>\n"
                    f"<i>{na['title'][:100]}</i>\n"
                    f"Source: {na['source']}\n"
                    f"Bullish score: {na['bullish_score']}/5\n"
                    f"Price moving: {'YES' if price_moving else 'not yet'}\n"
                    f"Confidence: {confidence}"
                )
                send_alert(msg)

            # Status log every 5 minutes
            if tick % 10 == 0:
                coins_tracked = len(history)
                coins_rising = sum(1 for p in history
                                  if len(history[p]) >= 2
                                  and history[p][-1][1] > history[p][-2][1])
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Tick {tick} | "
                      f"{coins_tracked} coins | {coins_rising} rising | "
                      f"News articles seen: {len(seen_news)}")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    main()
