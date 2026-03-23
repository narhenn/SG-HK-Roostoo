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


def main():
    client = RoostooClient()

    # Clear old buffer
    if os.path.exists(BUFFER_FILE):
        os.rename(BUFFER_FILE, BUFFER_FILE + '.old')
        print("Old buffer backed up")

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
