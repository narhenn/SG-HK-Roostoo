"""
Data Fetchers: Fear & Greed, Funding Rate, Market Breadth, Order Precision
"""

import requests
import logging

log = logging.getLogger("TradingBot")

# Cache for exchange info (fetched once on startup)
_exchange_info_cache = None


def fetch_fear_greed() -> int:
    """
    Fetch Fear & Greed Index (0-100).
    Free API, no key needed.
    """
    try:
        resp = requests.get('https://api.alternative.me/fng/?limit=1', timeout=10)
        data = resp.json()
        return int(data['data'][0]['value'])
    except Exception as e:
        log.warning(f"Fear & Greed API failed, using default 50: {e}")
        return 50


def fetch_funding_rate() -> float:
    """
    Fetch BTC perpetual funding rate from Binance.
    Positive = longs pay shorts (overcrowded longs = bearish signal).
    Negative = shorts pay longs (overcrowded shorts = bullish signal).
    """
    try:
        resp = requests.get(
            'https://fapi.binance.com/fapi/v1/fundingRate',
            params={'symbol': 'BTCUSDT', 'limit': 1},
            timeout=10
        )
        data = resp.json()
        if data and len(data) > 0:
            return float(data[0]['fundingRate'])
    except Exception:
        pass

    # Fallback: try CoinGlass free endpoint
    try:
        resp = requests.get(
            'https://open-api.coinglass.com/public/v2/funding',
            params={'symbol': 'BTC', 'time_type': 'h8'},
            timeout=10
        )
        data = resp.json()
        if data.get('success') and data.get('data'):
            return float(data['data'][0].get('rate', 0))
    except Exception:
        pass

    log.warning("Funding rate API failed (both Binance and CoinGlass), using default 0.0")
    return 0.0


def fetch_market_breadth() -> float:
    """
    Calculate % of top coins trending up using Roostoo's own ticker data.
    Calls /v3/ticker without a pair to get all coins, checks 'Change' field.
    """
    try:
        resp = requests.get(
            'https://mock-api.roostoo.com/v3/ticker',
            params={'timestamp': str(int(__import__('time').time() * 1000))},
            timeout=10
        )
        data = resp.json()
        ticker_data = data.get('Data', data)

        if not isinstance(ticker_data, dict):
            return 0.5

        up = 0
        total = 0
        for pair, info in ticker_data.items():
            if isinstance(info, dict) and 'Change' in info:
                total += 1
                if float(info['Change']) > 0:
                    up += 1

        if total == 0:
            return 0.5

        return up / total

    except Exception as e:
        log.warning(f"Market breadth API failed, using default 0.5: {e}")
        return 0.5


def get_order_precision(client=None) -> dict:
    """
    Fetch PricePrecision and AmountPrecision from Roostoo exchangeInfo.
    Caches result after first call.
    """
    global _exchange_info_cache

    if _exchange_info_cache:
        return _exchange_info_cache

    try:
        if client:
            info = client.get_exchange_info()
        else:
            resp = requests.get('https://mock-api.roostoo.com/v3/exchangeInfo', timeout=10)
            info = resp.json()

        pairs = info.get('TradePairs', {})
        btc_info = pairs.get('BTC/USD', {})

        _exchange_info_cache = {
            'price_precision': int(btc_info.get('PricePrecision', 2)),
            'amount_precision': int(btc_info.get('AmountPrecision', 5)),
            'min_order': float(btc_info.get('MiniOrder', 1)),
            'all_pairs': {
                pair: {
                    'price_precision': int(p.get('PricePrecision', 2)),
                    'amount_precision': int(p.get('AmountPrecision', 5)),
                    'min_order': float(p.get('MiniOrder', 1)),
                    'can_trade': p.get('CanTrade', False),
                }
                for pair, p in pairs.items()
            }
        }
        return _exchange_info_cache

    except Exception as e:
        log.error(f"Error fetching exchange info: {e}")
        return {'price_precision': 2, 'amount_precision': 5, 'min_order': 1}


def round_price(price: float, precision: int = 2) -> float:
    """Round price to exchange precision."""
    return round(price, precision)


def round_amount(amount: float, precision: int = 5) -> float:
    """Round BTC amount to exchange precision."""
    return round(amount, precision)
