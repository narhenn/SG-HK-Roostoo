"""
News Sentiment Layer — scans crypto news for coin mentions + bullish/bearish keywords.
Adds sentiment score to pattern trading bot.
Free API: cryptocurrency.cv (no auth, no rate limit)
"""

import urllib.request
import json
import time
import logging

log = logging.getLogger()

NEWS_API = "https://cryptocurrency.cv/api/news"

# Coin name mapping for news mentions
COIN_KEYWORDS = {
    'BTC/USD': ['bitcoin', 'btc'],
    'ETH/USD': ['ethereum', 'eth', 'ether'],
    'SOL/USD': ['solana', 'sol'],
    'BNB/USD': ['binance', 'bnb'],
    'XRP/USD': ['ripple', 'xrp'],
    'AVAX/USD': ['avalanche', 'avax'],
    'LINK/USD': ['chainlink', 'link'],
    'DOGE/USD': ['dogecoin', 'doge'],
    'ADA/USD': ['cardano', 'ada'],
    'DOT/USD': ['polkadot', 'dot'],
    'SUI/USD': ['sui'],
    'NEAR/USD': ['near protocol', 'near'],
    'HBAR/USD': ['hedera', 'hbar'],
    'UNI/USD': ['uniswap', 'uni'],
    'AAVE/USD': ['aave'],
    'FET/USD': ['fetch.ai', 'fetch ai', 'fet'],
    'PENDLE/USD': ['pendle'],
    'CAKE/USD': ['pancakeswap', 'cake'],
    'ARB/USD': ['arbitrum', 'arb'],
    'WLD/USD': ['worldcoin', 'wld'],
    'TRUMP/USD': ['trump coin', 'trump token', 'trump crypto'],
    'ONDO/USD': ['ondo'],
    'ZEC/USD': ['zcash', 'zec'],
    'LTC/USD': ['litecoin', 'ltc'],
    'PEPE/USD': ['pepe'],
    'SHIB/USD': ['shiba', 'shib'],
    'FLOKI/USD': ['floki'],
    'FIL/USD': ['filecoin', 'fil'],
    'TAO/USD': ['bittensor', 'tao'],
    'SEI/USD': ['sei'],
}

# Bullish keywords
BULLISH = [
    'surge', 'pump', 'rally', 'breakout', 'bullish', 'soar', 'moon', 'rocket',
    'all-time high', 'ath', 'approval', 'etf approved', 'partnership', 'launch',
    'upgrade', 'adoption', 'institutional', 'buy', 'accumulate', 'whale',
    'recover', 'bounce', 'green', 'gain', 'profit', 'up %', 'rises',
    'outperform', 'milestone', 'record', 'massive', 'explode',
]

# Bearish keywords
BEARISH = [
    'crash', 'dump', 'plunge', 'bearish', 'sell-off', 'selloff', 'hack',
    'exploit', 'vulnerability', 'sec lawsuit', 'ban', 'regulation',
    'liquidat', 'bankrupt', 'scam', 'fraud', 'rug pull', 'collapse',
    'fear', 'panic', 'decline', 'drop', 'fall', 'loss', 'red',
    'warning', 'risk', 'concern', 'investigation',
]

# Cache
_news_cache = {'data': None, 'time': 0}
CACHE_TTL = 300  # refresh every 5 minutes


def fetch_news():
    """Fetch latest crypto news. Returns list of articles."""
    now = time.time()
    if _news_cache['data'] and now - _news_cache['time'] < CACHE_TTL:
        return _news_cache['data']

    try:
        url = f"{NEWS_API}?limit=30"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        articles = data.get('articles', [])
        _news_cache['data'] = articles
        _news_cache['time'] = now
        return articles
    except Exception as e:
        log.info(f'News fetch failed: {e}')
        return _news_cache['data'] or []


def score_sentiment(pair):
    """
    Score news sentiment for a trading pair.
    Returns: score (-5 to +5), list of relevant headlines
    """
    articles = fetch_news()
    if not articles:
        return 0, []

    keywords = COIN_KEYWORDS.get(pair, [])
    if not keywords:
        # Try to extract coin name from pair
        coin = pair.split('/')[0].lower()
        keywords = [coin]

    score = 0
    relevant = []

    for article in articles:
        title = (article.get('title', '') or '').lower()
        desc = (article.get('description', '') or '').lower()
        text = title + ' ' + desc

        # Check if article mentions this coin
        mentioned = any(kw in text for kw in keywords)
        if not mentioned:
            continue

        # Score the sentiment
        article_score = 0
        for word in BULLISH:
            if word in text:
                article_score += 1
        for word in BEARISH:
            if word in text:
                article_score -= 1

        score += article_score
        if article_score != 0:
            relevant.append(f"{'+'if article_score>0 else ''}{article_score}: {article.get('title', '')[:60]}")

    # Cap the score
    score = max(-5, min(5, score))

    return score, relevant


def get_market_sentiment():
    """
    Overall market sentiment from latest news.
    Returns: score (-5 to +5)
    """
    articles = fetch_news()
    if not articles:
        return 0

    score = 0
    for article in articles[:20]:  # last 20 articles
        title = (article.get('title', '') or '').lower()
        desc = (article.get('description', '') or '').lower()
        text = title + ' ' + desc

        for word in BULLISH:
            if word in text:
                score += 1
        for word in BEARISH:
            if word in text:
                score -= 1

    # Normalize to -5 to +5
    score = max(-5, min(5, score // 3))
    return score
