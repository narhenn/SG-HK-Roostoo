#!/usr/bin/env python3
"""Fetch 1H candles from Binance for ALL coins and run pattern backtest."""
import json, requests, time, numpy as np, pandas as pd, sys
sys.path.insert(0, '.')
from pattern_encyclopedia import ChartPatternDetector

ALL_COINS = {
    'BTCUSDT':'BTC','ETHUSDT':'ETH','SOLUSDT':'SOL','BNBUSDT':'BNB',
    'XRPUSDT':'XRP','AVAXUSDT':'AVAX','LINKUSDT':'LINK','FETUSDT':'FET',
    'TAOUSDT':'TAO','APTUSDT':'APT','SUIUSDT':'SUI','NEARUSDT':'NEAR',
    'WIFUSDT':'WIF','PENDLEUSDT':'PENDLE','ADAUSDT':'ADA','DOTUSDT':'DOT',
    'UNIUSDT':'UNI','HBARUSDT':'HBAR','ARBUSDT':'ARB','EIGENUSDT':'EIGEN',
    'ENAUSDT':'ENA','CAKEUSDT':'CAKE','CFXUSDT':'CFX','CRVUSDT':'CRV',
    'FILUSDT':'FIL','TRUMPUSDT':'TRUMP','ONDOUSDT':'ONDO','WLDUSDT':'WLD',
    'AAVEUSDT':'AAVE','ICPUSDT':'ICP','LTCUSDT':'LTC','XLMUSDT':'XLM',
    'TONUSDT':'TON','TRXUSDT':'TRX','SEIUSDT':'SEI','DOGEUSDT':'DOGE',
    'ZECUSDT':'ZEC','ZENUSDT':'ZEN','POLUSDT':'POL','BIOUSDT':'BIO',
    'BONKUSDT':'BONK','SHIBUSDT':'SHIB','PEPEUSDT':'PEPE','FLOKIUSDT':'FLOKI',
}

print(f'Fetching 1H candles for {len(ALL_COINS)} coins from Binance...')
data = {}
for sym, name in ALL_COINS.items():
    try:
        r = requests.get(f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=168', timeout=10)
        raw = r.json()
        if isinstance(raw, list) and len(raw) > 50:
            data[name] = [{'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5])} for k in raw]
            print(f'  {name}: {len(data[name])} candles')
    except Exception as e:
        print(f'  {name}: error {e}')
    time.sleep(0.1)

with open('data/all_coins_1h.json', 'w') as f:
    json.dump(data, f)
print(f'Saved {len(data)} coins to data/all_coins_1h.json')
