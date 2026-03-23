#!/usr/bin/env python3
"""
Prefill price buffer by rapid-polling Roostoo.
55 ticks in ~3 minutes instead of 55 minutes.
Run on EC2 BEFORE starting bot:
  python3 prefill_buffer.py && sudo systemctl start bot.service
"""
import json
import time
import sys
import os

sys.path.insert(0, '.')
from roostoo_client import RoostooClient

BUFFER_FILE = 'data/price_buffer.jsonl'
TICKS_NEEDED = 60  # slightly more than 55 for safety
POLL_INTERVAL = 3  # seconds between polls
EXCLUDED = {'BONK/USD', 'DOGE/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD',
            'PAXG/USD', '1000CHEEMS/USD', 'PUMP/USD'}


BINANCE_MAP = {
    'BTC/USD': 'BTCUSDT', 'ETH/USD': 'ETHUSDT', 'BNB/USD': 'BNBUSDT',
    'SOL/USD': 'SOLUSDT', 'XRP/USD': 'XRPUSDT', 'ADA/USD': 'ADAUSDT',
    'AVAX/USD': 'AVAXUSDT', 'LINK/USD': 'LINKUSDT', 'DOT/USD': 'DOTUSDT',
    'SUI/USD': 'SUIUSDT', 'HBAR/USD': 'HBARUSDT', 'FIL/USD': 'FILUSDT',
    'NEAR/USD': 'NEARUSDT', 'UNI/USD': 'UNIUSDT', 'AAVE/USD': 'AAVEUSDT',
    'LTC/USD': 'LTCUSDT', 'FET/USD': 'FETUSDT', 'WIF/USD': 'WIFUSDT',
    'PENDLE/USD': 'PENDLEUSDT', 'CRV/USD': 'CRVUSDT', 'TON/USD': 'TONUSDT',
    'ARB/USD': 'ARBUSDT', 'ONDO/USD': 'ONDOUSDT', 'DOGE/USD': 'DOGEUSDT',
    'TRX/USD': 'TRXUSDT', 'CAKE/USD': 'CAKEUSDT', 'XLM/USD': 'XLMUSDT',
    'EIGEN/USD': 'EIGENUSDT', 'FORM/USD': 'FORMUSDT', 'WLD/USD': 'WLDUSDT',
    'ICP/USD': 'ICPUSDT', 'ENA/USD': 'ENAUSDT', 'SEI/USD': 'SEIUSDT',
    'TRUMP/USD': 'TRUMPUSDT', 'APT/USD': 'APTUSDT', 'TAO/USD': 'TAOUSDT',
    'VIRTUAL/USD': 'VIRTUALUSDT', 'POL/USD': 'POLUSDT',
}


def try_binance_prefill():
    """Try to fetch 300 1-min candles from Binance for all coins. Returns True if successful."""
    import requests
    print("Trying Binance API from EC2...")
    try:
        resp = requests.get('https://api.binance.com/api/v3/klines',
            params={'symbol': 'BTCUSDT', 'interval': '1m', 'limit': 5}, timeout=10)
        if resp.status_code != 200:
            print("Binance returned %d — blocked from EC2" % resp.status_code)
            return False
        print("Binance WORKS from EC2! Fetching 300 candles for all coins...")
    except Exception as e:
        print("Binance blocked: %s" % e)
        return False

    all_ticks = {}
    fetched = 0
    for roostoo_pair, binance_sym in BINANCE_MAP.items():
        if roostoo_pair in EXCLUDED:
            continue
        try:
            resp = requests.get('https://api.binance.com/api/v3/klines',
                params={'symbol': binance_sym, 'interval': '1m', 'limit': 300}, timeout=10)
            if resp.status_code != 200:
                continue
            klines = resp.json()
            if not isinstance(klines, list) or len(klines) < 10:
                continue

            ticks = []
            for k in klines:
                ts = k[0] / 1000  # ms to seconds
                close = float(k[4])
                high = float(k[2])
                low = float(k[3])
                vol = float(k[5])  # base volume, not quote
                bid = close - (high - low) * 0.01  # approximate bid
                ask = close + (high - low) * 0.01  # approximate ask
                spread = (ask - bid) / close if close > 0 else 0
                ticks.append((ts, close, bid, ask, vol, spread))

            all_ticks[roostoo_pair] = ticks
            fetched += 1
            print("  %s (%s): %d candles" % (roostoo_pair, binance_sym, len(ticks)))
            time.sleep(0.1)  # Be nice to Binance
        except Exception as e:
            print("  %s: failed (%s)" % (binance_sym, str(e)[:50]))

    if fetched < 5:
        print("Only got %d coins from Binance — falling back to Roostoo polling" % fetched)
        return False

    # Write buffer
    print("\nWriting %d coins to buffer..." % len(all_ticks))
    with open(BUFFER_FILE, 'w') as f:
        for pair, ticks in all_ticks.items():
            for tk in ticks:
                f.write(json.dumps({'pair': pair, 't': list(tk)}) + '\n')

    total = sum(len(t) for t in all_ticks.values())
    print("1-min candles done: %d pairs, %d total ticks" % (len(all_ticks), total))

    # Also fetch 5-min and 1-hour for higher-timeframe trend context
    # Store in separate files the scanner can read
    for tf_label, tf_interval, tf_limit, tf_file in [
        ('5-min', '5m', 300, 'data/price_buffer_5m.jsonl'),
        ('1-hour', '1h', 300, 'data/price_buffer_1h.jsonl'),
    ]:
        print("\nFetching %s candles..." % tf_label)
        tf_ticks = {}
        tf_count = 0
        for roostoo_pair, binance_sym in BINANCE_MAP.items():
            if roostoo_pair in EXCLUDED:
                continue
            try:
                resp = requests.get('https://api.binance.com/api/v3/klines',
                    params={'symbol': binance_sym, 'interval': tf_interval, 'limit': tf_limit}, timeout=10)
                if resp.status_code != 200:
                    continue
                klines = resp.json()
                if not isinstance(klines, list) or len(klines) < 10:
                    continue
                ticks = []
                for k in klines:
                    ts = k[0] / 1000
                    close = float(k[4])
                    high = float(k[2])
                    low = float(k[3])
                    vol = float(k[5])
                    bid = close - (high - low) * 0.01
                    ask = close + (high - low) * 0.01
                    spread = (ask - bid) / close if close > 0 else 0
                    ticks.append((ts, close, bid, ask, vol, spread))
                tf_ticks[roostoo_pair] = ticks
                tf_count += 1
                time.sleep(0.1)
            except Exception:
                continue

        if tf_count > 0:
            with open(tf_file, 'w') as f:
                for pair, ticks in tf_ticks.items():
                    for tk in ticks:
                        f.write(json.dumps({'pair': pair, 't': list(tk)}) + '\n')
            print("  %s: %d pairs saved to %s" % (tf_label, tf_count, tf_file))

    print("\nBinance prefill complete: 1m + 5m + 1h candles")
    return True


def main():
    client = RoostooClient()

    # Clear old buffer
    if os.path.exists(BUFFER_FILE):
        os.rename(BUFFER_FILE, BUFFER_FILE + '.old')
        print("Old buffer backed up")

    # Try Binance first (300 real 1-min candles instantly)
    if try_binance_prefill():
        print("\nBuffer prefilled from Binance! Scanner ready immediately.")
        print("Start bot: sudo systemctl start bot.service")
        return

    print("Prefilling buffer: %d ticks at %ds intervals (~%d min)" % (
        TICKS_NEEDED, POLL_INTERVAL, TICKS_NEEDED * POLL_INTERVAL // 60))

    all_ticks = {}  # {pair: [(ts, price, bid, ask, vol, spread), ...]}

    for tick in range(TICKS_NEEDED):
        try:
            ts = time.time()
            ticker = client.get_ticker()
            data = ticker.get('Data', {})

            count = 0
            for pair, info in data.items():
                if pair in EXCLUDED:
                    continue
                try:
                    price = float(info.get('LastPrice', 0))
                    bid = float(info.get('MaxBid', 0))
                    ask = float(info.get('MinAsk', 0))
                    vol = float(info.get('CoinTradeValue', 0))
                    if price <= 0:
                        continue
                    spread = (ask - bid) / price if price > 0 and bid > 0 and ask > 0 else 0

                    if pair not in all_ticks:
                        all_ticks[pair] = []
                    all_ticks[pair].append((ts, price, bid, ask, vol, spread))
                    count += 1
                except (ValueError, TypeError):
                    continue

            elapsed = time.time() - ts
            remaining = (TICKS_NEEDED - tick - 1) * POLL_INTERVAL
            print("  Tick %d/%d: %d coins, %.1fs elapsed, ~%ds remaining" % (
                tick + 1, TICKS_NEEDED, count, elapsed, remaining))

            if tick < TICKS_NEEDED - 1:
                time.sleep(max(0, POLL_INTERVAL - elapsed))

        except Exception as e:
            print("  Tick %d FAILED: %s" % (tick + 1, e))
            time.sleep(POLL_INTERVAL)

    # Write buffer file
    print("\nWriting buffer to %s..." % BUFFER_FILE)
    with open(BUFFER_FILE, 'w') as f:
        for pair, ticks in all_ticks.items():
            for tk in ticks:
                f.write(json.dumps({'pair': pair, 't': list(tk)}) + '\n')

    total_ticks = sum(len(t) for t in all_ticks.values())
    print("Done. %d pairs, %d total ticks." % (len(all_ticks), total_ticks))
    print("Max ticks per pair: %d" % max(len(t) for t in all_ticks.values()))
    print("\nBuffer ready. Start bot: sudo systemctl start bot.service")


if __name__ == '__main__':
    main()
