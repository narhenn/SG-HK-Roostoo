"""
Collect Roostoo vs Binance gap data for backtesting copy trader.
Saves snapshots every 3 seconds to CSV.
Run: python3 gap_collector.py [minutes]  (default 30)
"""

import time
import csv
import sys
import requests
from datetime import datetime
from roostoo_client import RoostooClient

client = RoostooClient()
EXCLUDED = {'PAXG/USD', '1000CHEEMS/USD', 'BONK/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD'}

duration = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 1800  # default 30 min
outfile = f"gap_data_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

print(f"Collecting gap data for {duration//60} min -> {outfile}")

with open(outfile, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['ts', 'pair', 'roostoo_px', 'binance_px', 'gap_pct', 'roostoo_vol', 'roostoo_bid', 'roostoo_ask'])

    start = time.time()
    tick = 0
    while time.time() - start < duration:
        try:
            r = requests.get('https://api.binance.com/api/v3/ticker/price', timeout=3)
            binance = {t['symbol']: float(t['price']) for t in r.json()}

            td = client.get_ticker().get('Data', {})
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            tick += 1

            rows = 0
            for pair, info in td.items():
                if pair in EXCLUDED:
                    continue
                coin = pair.split('/')[0]
                bsym = f'{coin}USDT'
                b_px = binance.get(bsym, 0)
                r_px = float(info.get('LastPrice', 0))
                if b_px <= 0 or r_px <= 0:
                    continue

                gap = (r_px - b_px) / b_px * 100
                vol = float(info.get('CoinTradeValue', 0))
                bid = float(info.get('MaxBid', 0))
                ask = float(info.get('MinAsk', 0))

                w.writerow([ts, pair, f'{r_px:.6f}', f'{b_px:.6f}', f'{gap:.4f}', f'{vol:.0f}', f'{bid:.6f}', f'{ask:.6f}'])
                rows += 1

            if tick % 20 == 0:
                elapsed = int(time.time() - start)
                print(f"[{ts}] Tick {tick} | {elapsed}s/{duration}s | {rows} coins/tick")
                f.flush()

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(3)

print(f"\nDone! Collected {tick} snapshots -> {outfile}")
