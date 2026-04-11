#!/usr/bin/env python3
"""
ORPHAN LIQUIDATION SCRIPT
════════════════════════════════════════════════════════════════════
Executes the pro trader's "surgical strike" plan entirely via code:

  DEFAULT (surgical strike):
    SELL: NEAR, BTC, BNB, SOL  (lock profits + cut the one loser)
    KEEP: PENDLE               (hot momentum play, 24h +3.8%)

  PRESETS:
    --all       sell EVERYTHING including PENDLE (clean slate)
    --surgical  the default (cut NEAR, lock majors, keep PENDLE)
    --near-only cut only NEAR (keep all profitable positions)

  FLAGS:
    --dry-run   show plan, don't execute
    --go        execute for real (without this, dry-run is default)

USAGE:
  python3 liquidate_orphans.py                  # dry run, surgical plan
  python3 liquidate_orphans.py --go             # EXECUTE surgical
  python3 liquidate_orphans.py --all --go       # EXECUTE sell-all
  python3 liquidate_orphans.py --near-only --go # just cut NEAR
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

from config import (
    API_KEY, SECRET_KEY, BASE_URL,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)
from roostoo_client import RoostooClient
from naked_v3_2 import binance_klines


STABLES = {'USD', 'USDT', 'USDC', 'USD1', 'DAI'}


def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg},
            timeout=4,
        )
    except Exception:
        pass


def log(msg):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def get_current_positions(client):
    """Return dict: {coin: qty} for all non-stable non-zero holdings."""
    bal = client.get_balance()
    wallet = bal.get('SpotWallet', {})
    positions = {}
    cash = 0.0
    for sym, info in wallet.items():
        if not isinstance(info, dict):
            continue
        qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
        if qty <= 1e-9:
            continue
        if sym in STABLES:
            cash += qty
        else:
            positions[sym] = qty
    return positions, cash


def sell_coin(client, coin, qty, dry_run=True):
    """Place a market sell order for coin. Returns (success, price, msg)."""
    pair = f"{coin}/USD"

    if dry_run:
        bars = binance_klines(coin, '1m', 1)
        px = bars[-1]['c'] if bars else 0
        usd = qty * px
        return True, px, f"DRY SELL {pair} qty={qty} ~${usd:,.2f}"

    try:
        r = client.place_order(pair, 'SELL', 'MARKET', qty)
        if not r.get('Success', False):
            err = r.get('ErrMsg', 'unknown error')
            return False, 0, f"FAIL: {err}"
        # Extract fill info
        fill = r.get('FilledAverPrice', 0)
        filled_qty = r.get('FilledQuantity', qty)
        return True, float(fill), f"FILLED qty={filled_qty} avg=${float(fill):.4f}"
    except Exception as e:
        return False, 0, f"EXCEPTION: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true',
                    help='sell ALL positions including PENDLE')
    ap.add_argument('--surgical', action='store_true',
                    help='default: sell NEAR+BTC+BNB+SOL, keep PENDLE')
    ap.add_argument('--near-only', action='store_true',
                    help='cut only NEAR, keep profitable positions')
    ap.add_argument('--go', action='store_true',
                    help='EXECUTE for real (without this flag, dry-run)')
    ap.add_argument('--dry-run', action='store_true', help='explicit dry-run')
    args = ap.parse_args()

    dry = not args.go or args.dry_run

    # Default plan is surgical
    if not (args.all or args.near_only):
        args.surgical = True

    log("═" * 70)
    log("ORPHAN LIQUIDATION")
    log(f"Mode: {'DRY RUN' if dry else '🔴 LIVE EXECUTION'}")
    log("═" * 70)

    client = RoostooClient()
    positions, cash = get_current_positions(client)
    log(f"")
    log(f"Current state:")
    log(f"  cash: ${cash:,.2f}")
    log(f"  positions: {len(positions)}")
    for coin, qty in positions.items():
        bars = binance_klines(coin, '1m', 1)
        px = bars[-1]['c'] if bars else 0
        usd = qty * px
        log(f"    {coin:<8} qty={qty:,.6f}  ~${usd:,.2f}")
    log(f"")

    # Pick the coins to sell based on preset
    if args.all:
        to_sell = {c: q for c, q in positions.items()}
        plan = "SELL ALL"
    elif args.near_only:
        to_sell = {c: q for c, q in positions.items() if c == 'NEAR'}
        plan = "CUT NEAR ONLY"
    else:  # surgical (default)
        # Sell losers + lock majors, keep momentum plays
        KEEP = {'PENDLE', 'TRUMP'}  # TRUMP is dust, leaving it
        to_sell = {c: q for c, q in positions.items() if c not in KEEP}
        plan = "SURGICAL (cut NEAR + lock BTC/BNB/SOL, keep PENDLE)"

    # Estimate value
    total_sell_value = 0.0
    for coin, qty in to_sell.items():
        bars = binance_klines(coin, '1m', 1)
        px = bars[-1]['c'] if bars else 0
        total_sell_value += qty * px

    log(f"PLAN: {plan}")
    log(f"Coins to SELL: {list(to_sell.keys())}")
    log(f"Estimated cash freed: ${total_sell_value:,.2f}")
    log(f"Estimated new cash: ${cash + total_sell_value:,.2f}")
    log(f"")

    if not to_sell:
        log("Nothing to sell. Done.")
        return

    if dry:
        log("━━━ DRY RUN — no orders placed ━━━")
        log("")
        log("To execute for real, add --go flag:")
        if args.all:
            log("  python3 liquidate_orphans.py --all --go")
        elif args.near_only:
            log("  python3 liquidate_orphans.py --near-only --go")
        else:
            log("  python3 liquidate_orphans.py --go")
        # Still run dry sells to confirm estimates
        log("")
        log("Dry-run fills:")
        for coin, qty in to_sell.items():
            ok, px, msg = sell_coin(client, coin, qty, dry_run=True)
            log(f"  {coin}: {msg}")
        return

    # LIVE EXECUTION
    tg(f"🔴 LIQUIDATING ORPHANS\n"
       f"Plan: {plan}\n"
       f"Est freed: ${total_sell_value:,.0f}")

    results = []
    for coin, qty in to_sell.items():
        log(f"")
        log(f"Selling {coin} qty={qty}…")
        ok, px, msg = sell_coin(client, coin, qty, dry_run=False)
        log(f"  {msg}")
        results.append({'coin': coin, 'qty': qty, 'ok': ok, 'price': px, 'msg': msg})
        if ok:
            tg(f"✅ SOLD {coin} qty={qty:.4f} @ ${px:.4f}")
        else:
            tg(f"❌ FAILED {coin}: {msg}")
        time.sleep(0.5)  # small delay between orders

    # Re-query balance to confirm
    log("")
    log("Re-querying balance after sales…")
    time.sleep(2)
    new_positions, new_cash = get_current_positions(client)
    log(f"")
    log(f"FINAL STATE:")
    log(f"  cash: ${new_cash:,.2f}  (was ${cash:,.2f}, delta ${new_cash - cash:+,.2f})")
    log(f"  remaining positions:")
    for coin, qty in new_positions.items():
        bars = binance_klines(coin, '1m', 1)
        px = bars[-1]['c'] if bars else 0
        usd = qty * px
        log(f"    {coin:<8} qty={qty:,.6f}  ~${usd:,.2f}")

    total_final = new_cash + sum(
        qty * (binance_klines(c, '1m', 1)[-1]['c'] if binance_klines(c, '1m', 1) else 0)
        for c, qty in new_positions.items()
    )
    log(f"")
    log(f"TOTAL ACCOUNT: ${total_final:,.2f}")

    tg(f"🏁 LIQUIDATION COMPLETE\n"
       f"cash: ${new_cash:,.2f}\n"
       f"total: ${total_final:,.2f}\n"
       f"remaining positions: {list(new_positions.keys())}")

    log("")
    log("Next step: start V3.2")
    log("  python3 v32_auto_deploy.py")


if __name__ == '__main__':
    main()
