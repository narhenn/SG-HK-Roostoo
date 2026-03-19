# SECTION 1 — IMPORTS AND CACHE STORAGE
import requests  # For making HTTP requests to APIs
import time      # For time-related operations like checking cache freshness
import datetime  # For datetime operations (though not used directly, imported as per requirements)
import threading # For thread-safe operations using locks

# Cache storage for each function: stores {'value': data, 'fetched_at': timestamp}
fear_greed_cache = {'value': None, 'fetched_at': 0}
funding_rate_cache = {'value': None, 'fetched_at': 0}
market_breadth_cache = {'value': None, 'fetched_at': 0}

# Thread locks to prevent concurrent access to caches
fear_greed_lock = threading.Lock()
funding_rate_lock = threading.Lock()
market_breadth_lock = threading.Lock()

# SECTION 2 — FUNCTION 1: get_fear_and_greed()
def get_fear_and_greed():
    """
    Fetches the Fear & Greed Index from Alternative.me API.
    Cache duration: 86400 seconds (24 hours) — API updates once per day.
    Returns integer 0-100.
    Interpretation:
      0-25   = Extreme Fear → buy signals more reliable, larger positions allowed
      25-45  = Fear → normal buying allowed
      45-55  = Neutral → no sentiment edge
      55-75  = Greed → tighten stops, reduce sizes
      75-100 = Extreme Greed → do not open new positions
    """
    cache_duration = 86400  # 24 hours in seconds
    current_time = time.time()

    with fear_greed_lock:  # Thread-safe access to cache
        if fear_greed_cache['value'] is not None and (current_time - fear_greed_cache['fetched_at']) < cache_duration:
            # Cache is fresh, use it
            value = fear_greed_cache['value']
            print(f"[D3] Using cached Fear & Greed: {value}")
            return value

    # Cache is stale or empty, fetch fresh data
    try:
        response = requests.get("https://api.alternative.me/fng/", timeout=10)
        response.raise_for_status()  # Raise exception for bad status codes
        data = response.json()
        value = int(data["data"][0]["value"])  # Extract and convert to int
        print(f"[D3] Fetched fresh Fear & Greed: {value}")

        with fear_greed_lock:  # Update cache thread-safely
            fear_greed_cache['value'] = value
            fear_greed_cache['fetched_at'] = current_time

        return value
    except Exception as e:
        # API failed, use cached value if available, else fallback to 50
        with fear_greed_lock:
            if fear_greed_cache['value'] is not None:
                value = fear_greed_cache['value']
                print(f"[D3] Using cached Fear & Greed: {value} (API failed: {e})")
                return value
            else:
                print(f"[D3] API failed, using fallback Fear & Greed: 50 ({e})")
                return 50

# SECTION 3 — FUNCTION 2: get_funding_rate()
def get_funding_rate():
    """
    Fetches the latest funding rate for BTCUSDT from Binance Futures API.
    Cache duration: 28800 seconds (8 hours) — funding settles every 8 hours.
    Returns float (positive or negative).
    Interpretation:
      Very negative (< -0.001) → shorts overcrowded → short squeeze likely → BUY confirmation
      Slightly negative (-0.001 to 0) → mild short bias → slightly bullish
      Near zero (-0.0001 to +0.0001) → balanced → no additional signal
      Slightly positive (0 to +0.001) → mild long bias → slightly cautious
      Very positive (> +0.001) → longs overcrowded → caution on buy signals
    """
    cache_duration = 28800  # 8 hours in seconds
    current_time = time.time()

    with funding_rate_lock:  # Thread-safe access to cache
        if funding_rate_cache['value'] is not None and (current_time - funding_rate_cache['fetched_at']) < cache_duration:
            # Cache is fresh, use it
            value = funding_rate_cache['value']
            print(f"[D4] Using cached Funding Rate: {value}")
            return value

    # Cache is stale or empty, fetch fresh data
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        response = session.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=10)
        response.raise_for_status()  # Raise exception for bad status codes
        data = response.json()
        value = float(data["lastFundingRate"])  # Extract and convert to float
        print(f"[D4] Fetched fresh Funding Rate: {value}")

        with funding_rate_lock:  # Update cache thread-safely
            funding_rate_cache['value'] = value
            funding_rate_cache['fetched_at'] = current_time

        return value
    except Exception as e:
        # API failed, use cached value if available, else fallback to 0.0
        with funding_rate_lock:
            if funding_rate_cache['value'] is not None:
                value = funding_rate_cache['value']
                print(f"[D4] Using cached Funding Rate: {value} (API failed: {e})")
                return value
            else:
                print(f"[D4] API failed, using fallback Funding Rate: 0.0 ({e})")
                return 0.0

# SECTION 4 — FUNCTION 3: get_market_breadth()
def get_market_breadth():
    """
    Calculates market breadth from Binance 24hr ticker data.
    Cache duration: 300 seconds (5 minutes) — balance freshness vs rate limits.
    Returns float 0.0 to 1.0 (fraction of top 25 USDT pairs trending up).
    Interpretation:
      > 0.6  → healthy market, most coins rising, BTC buy signals reliable
      0.4-0.6 → mixed market, neutral signal
      < 0.4  → most coins falling, BTC buy signals likely fakeouts
      < 0.3  → danger zone, VOLATILE regime almost certain in Layer 1
    Why Binance not Roostoo: Roostoo is mock exchange with zero-volume coins.
    Breadth from Roostoo = hackathon behavior, not real market health.
    """
    cache_duration = 300  # 5 minutes in seconds
    current_time = time.time()

    with market_breadth_lock:  # Thread-safe access to cache
        if market_breadth_cache['value'] is not None and (current_time - market_breadth_cache['fetched_at']) < cache_duration:
            # Cache is fresh, use it
            value = market_breadth_cache['value']
            print(f"[D5] Using cached Market Breadth: {value:.2%}")
            return value

    # Cache is stale or empty, fetch fresh data
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        
        # Use CoinGecko API instead of Binance for better reliability
        url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=25&page=1&sparkline=false&price_change_percentage=1h"
        response = session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Count how many coins have positive 1-hour price change
        up_count = sum(1 for coin in data if coin.get('price_change_percentage_1h_in_currency', 0) > 0)

        # Calculate breadth as fraction
        value = up_count / 25.0

        print(f"[D5] Fetched fresh Market Breadth: {value:.2%} ({up_count}/25 coins up)")

        with market_breadth_lock:  # Update cache thread-safely
            market_breadth_cache['value'] = value
            market_breadth_cache['fetched_at'] = current_time

        return value
    except Exception as e:
        # API failed, use cached value if available, else fallback to 0.5
        with market_breadth_lock:
            if market_breadth_cache['value'] is not None:
                value = market_breadth_cache['value']
                print(f"[D5] Using cached Market Breadth: {value:.2%} (API failed: {e})")
                return value
            else:
                print(f"[D5] API failed, using fallback Market Breadth: 50.00% ({e})")
                return 0.5

# SECTION 5 — TEST BLOCK
if __name__ == "__main__":
    print("=== DATA FEEDS TEST ===")

    # First call - should fetch fresh data
    print("First calls (should fetch fresh):")
    fg = get_fear_and_greed()
    print(f"Fear & Greed: {fg}")
    fr = get_funding_rate()
    print(f"Funding Rate: {fr}")
    mb = get_market_breadth()
    print(f"Market Breadth: {mb:.2%}")

    print("\nWaiting 3 seconds...")
    time.sleep(3)

    # Second call - should use cached data
    print("Second calls (should use cached):")
    fg = get_fear_and_greed()
    print(f"Fear & Greed: {fg}")
    fr = get_funding_rate()
    print(f"Funding Rate: {fr}")
    mb = get_market_breadth()
    print(f"Market Breadth: {mb:.2%}")

    print("\n=== ALL FEEDS WORKING ===")
