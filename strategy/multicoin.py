"""
Cross-Sectional Momentum — Multi-coin hybrid layer.
Owner: Pranati

Ranks all Roostoo coins by 24h return, returns top 2 alt candidates.
Only activated if ENABLE_MULTICOIN = True in config.
Currently DISABLED — enable after BTC bot confirmed working.
"""

from config import TRADING_PAIR


def rank_coins(client) -> list:
    """
    Fetch all tickers, rank by 24h Change field.
    Returns top 2 non-BTC coins with positive momentum and real volume.
    """
    try:
        raw = client.get_ticker()
        ticker_data = raw.get('Data', {})

        ranked = []
        for pair, info in ticker_data.items():
            if pair == TRADING_PAIR:
                continue
            if not isinstance(info, dict):
                continue

            change = float(info.get('Change', 0) or 0)
            volume = float(info.get('CoinTradeValue', 0) or 0)
            price  = float(info.get('LastPrice', 0) or 0)
            bid    = float(info.get('MaxBid', 0) or 0)
            ask    = float(info.get('MinAsk', 0) or 0)

            # Skip zero-volume or zero-price coins
            if volume <= 0 or price <= 0:
                continue
            # Skip coins with no valid bid/ask
            if bid <= 0 or ask <= 0:
                continue

            ranked.append({
                'pair':   pair,
                'change': change,
                'volume': volume,
                'price':  price,
                'bid':    bid,
                'ask':    ask,
            })

        # Sort by 24h return descending
        ranked.sort(key=lambda x: x['change'], reverse=True)

        # Return top 2 with positive momentum only
        top = [c for c in ranked if c['change'] > 0][:2]
        return top

    except Exception:
        return []


def should_rotate(current_pair: str, top_coins: list) -> bool:
    """
    Returns True if current alt position should be rotated out.
    Only rotate if coin dropped out of top 5 (avoid over-trading).
    """
    top5_pairs = [c['pair'] for c in top_coins[:5]]
    return current_pair not in top5_pairs


def get_alt_position_size(total_equity: float, alt_capital_pct: float,
                          num_alts: int = 2) -> float:
    """
    Returns USD size per alt coin.
    Example: $1M equity, 30% alt allocation, 2 coins = $150k each.
    """
    if num_alts <= 0:
        return 0.0
    total_alt_usd = total_equity * alt_capital_pct
    return total_alt_usd / num_alts
