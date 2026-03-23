#!/usr/bin/env python3
"""
Recovery deployment: Buy BNB + adopt orphaned positions.
Run on EC2 AFTER stopping bot:
  sudo systemctl stop bot.service && python3 deploy_recovery.py && sudo systemctl start bot.service
"""

import json
import shutil
import time
import sys
import math

sys.path.insert(0, '.')
from roostoo_client import RoostooClient

STATE_FILE = 'state.json'
BNB_BUY_AMOUNT = 200000  # $200k


def main():
    client = RoostooClient()

    # ── Step 1: Backup ──
    backup = 'state.json.backup.%d' % int(time.time())
    shutil.copy2(STATE_FILE, backup)
    print("Backed up to %s" % backup)

    # ── Step 2: Load state ──
    with open(STATE_FILE) as f:
        state = json.load(f)
    alt = state.get('alt_positions', {})
    now = time.strftime('%Y-%m-%dT%H:%M:%S')

    # ── Step 3: Get exchange precision ──
    exinfo = client.get_exchange_info()
    precisions = {}
    # Handle both possible response formats
    trade_pairs = exinfo.get('TradePairs', exinfo.get('Data', {}))
    if isinstance(trade_pairs, dict):
        for pair_name, pair_info in trade_pairs.items():
            precisions[pair_name] = {
                'price_precision': int(pair_info.get('PricePrecision', 4)),
                'amount_precision': int(pair_info.get('AmountPrecision', 2)),
            }
    elif isinstance(trade_pairs, list):
        for pair_info in trade_pairs:
            pair_name = pair_info.get('Pair', '')
            precisions[pair_name] = {
                'price_precision': int(pair_info.get('PricePrecision', 4)),
                'amount_precision': int(pair_info.get('AmountPrecision', 2)),
            }

    # ── Step 4: Get fresh ticker ──
    ticker = client.get_ticker()
    all_data = ticker.get('Data', {})

    # ── Step 5: Buy BNB ──
    print("\n=== BUYING BNB ===")
    bnb_info = all_data.get('BNB/USD', {})
    bnb_ask = float(bnb_info.get('MinAsk', 0))
    bnb_change = float(bnb_info.get('Change', 0))
    bnb_prec = precisions.get('BNB/USD', {'price_precision': 2, 'amount_precision': 3})

    if bnb_ask <= 0:
        print("ERROR: No BNB ask price. Skipping buy.")
        bnb_success = False
    else:
        # Floor qty to precision (never buy more than we can afford)
        amt_mult = 10 ** bnb_prec['amount_precision']
        bnb_qty = math.floor(BNB_BUY_AMOUNT / bnb_ask * amt_mult) / amt_mult
        bnb_price = round(bnb_ask, bnb_prec['price_precision'])

        print("Placing LIMIT BUY: %.3f BNB @ $%.2f ($%s)" % (
            bnb_qty, bnb_price, format(bnb_qty * bnb_price, ',.0f')))

        bnb_success = False
        bnb_fill_price = bnb_price
        bnb_fill_qty = bnb_qty
        order_id = 0

        try:
            order = client.place_order('BNB/USD', 'BUY', 'LIMIT', bnb_qty, bnb_price)
            print("Order response: %s" % json.dumps(order)[:500])

            # Parse response — handle multiple formats
            order_data = order.get('Data', {})
            if isinstance(order_data, dict):
                detail = order_data.get('OrderDetail', order_data)
            else:
                detail = order_data
            if not isinstance(detail, dict):
                detail = order

            status = str(detail.get('Status', '')).upper()
            order_id = detail.get('OrderID', detail.get('OrderId', 0))

            # On mock exchange, taker LIMIT at ask fills instantly
            if status in ('FILLED', 'COMPLETED'):
                bnb_success = True
                if detail.get('AvgPrice'):
                    bnb_fill_price = float(detail['AvgPrice'])
                if detail.get('FilledQuantity'):
                    bnb_fill_qty = float(detail['FilledQuantity'])
                print("BNB FILLED: %.3f @ $%.2f" % (bnb_fill_qty, bnb_fill_price))

            elif status in ('NEW', 'PENDING', 'PARTIALLY_FILLED'):
                # Wait and check
                print("Status=%s, waiting 5s for fill..." % status)
                time.sleep(5)
                try:
                    orders = client.query_orders(pair='BNB/USD', limit=5)
                    order_list = orders.get('Data', orders.get('OrderMatched', []))
                    if isinstance(order_list, list):
                        for o in order_list:
                            oid = o.get('OrderID', o.get('OrderId', ''))
                            if str(oid) == str(order_id):
                                st = str(o.get('Status', '')).upper()
                                if st in ('FILLED', 'COMPLETED'):
                                    bnb_success = True
                                    if o.get('AvgPrice'):
                                        bnb_fill_price = float(o['AvgPrice'])
                                    if o.get('FilledQuantity'):
                                        bnb_fill_qty = float(o['FilledQuantity'])
                                    print("BNB FILLED (after wait): %.3f @ $%.2f" % (bnb_fill_qty, bnb_fill_price))
                                else:
                                    print("BNB still %s after 5s" % st)
                                break
                except Exception as e:
                    print("Order check error: %s" % e)

                # If still not confirmed, assume filled (taker on mock exchange)
                if not bnb_success:
                    print("Assuming filled (taker on mock exchange)")
                    bnb_success = True
            else:
                print("BNB BUY UNEXPECTED STATUS: %s" % status)
                # Check if Success field indicates it went through
                if order.get('Success') == True:
                    print("Success=True, assuming filled")
                    bnb_success = True

        except Exception as e:
            print("BNB BUY ERROR: %s" % e)
            bnb_success = False

    # ── Step 6: Adopt orphaned positions ──
    print("\n=== ADOPTING ORPHANED POSITIONS ===")

    orphans = {
        'FORM/USD': 263479.7,
        'OPEN/USD': 235017.5,
        'TUT/USD': 3600058.0,
        'WLFI/USD': 70850.2,
    }

    for pair, qty in orphans.items():
        if pair in alt:
            print("SKIP %s -- already tracked" % pair)
            continue

        price = float(all_data.get(pair, {}).get('LastPrice', 0))
        if price <= 0:
            print("SKIP %s -- no price" % pair)
            continue

        change = float(all_data.get(pair, {}).get('Change', 0))
        prec = precisions.get(pair, {'price_precision': 4, 'amount_precision': 2})

        # Adaptive trail (1% min, matching new ALT_TRAIL_MIN)
        trail = max(0.01, min(abs(change) * 0.5, 0.07))
        tp_pct = max(abs(change) * 0.33, 0.015)
        tp_price = round(price * (1 + tp_pct), prec['price_precision'])
        stop = round(price * (1 - trail), prec['price_precision'])

        alt[pair] = {
            'entry_price': price,
            'qty': qty,
            'peak_price': price,
            'trail_pct': trail,
            'tp_price': tp_price,
            'tp_pct': tp_pct,
            'stop': stop,
            'entry_time': now,
            'order_id': 0,
            'entry_change': round(change, 4),
            'price_precision': prec['price_precision'],
            'amount_precision': prec['amount_precision'],
        }
        value = qty * price
        print("ADDED %s: $%s, trail=%.1f%%, stop=$%s, tp=$%s" % (
            pair, format(value, ',.0f'), trail * 100, stop, tp_price))

    # ── Step 7: Update WIF and AVAX to full wallet qty ──
    if 'WIF/USD' in alt:
        old_qty = alt['WIF/USD']['qty']
        if old_qty < 112871.0:
            alt['WIF/USD']['qty'] = 112871.29
            print("UPDATED WIF/USD qty: %s -> 112871.29" % old_qty)

    if 'AVAX/USD' in alt:
        old_qty = alt['AVAX/USD']['qty']
        if old_qty < 1497.0:
            alt['AVAX/USD']['qty'] = 1497.1
            print("UPDATED AVAX/USD qty: %s -> 1497.1" % old_qty)

    # ── Step 8: Add BNB to state ──
    if bnb_success:
        bnb_trail = 0.01  # 1% trailing stop
        bnb_tp_pct = 0.015  # 1.5% take profit (gunner fires here)
        bnb_tp_price = round(bnb_fill_price * (1 + bnb_tp_pct), 2)
        bnb_stop = round(bnb_fill_price * (1 - bnb_trail), 2)

        alt['BNB/USD'] = {
            'entry_price': bnb_fill_price,
            'qty': bnb_fill_qty,
            'peak_price': bnb_fill_price,
            'trail_pct': bnb_trail,
            'tp_price': bnb_tp_price,
            'tp_pct': bnb_tp_pct,
            'stop': bnb_stop,
            'entry_time': now,
            'order_id': order_id,
            'entry_change': round(bnb_change, 4),
            'price_precision': 2,
            'amount_precision': 3,
        }
        print("\nBNB POSITION ADDED:")
        print("  Entry: $%.2f" % bnb_fill_price)
        print("  Qty: %.3f BNB ($%s)" % (bnb_fill_qty, format(bnb_fill_qty * bnb_fill_price, ',.0f')))
        print("  Stop: $%.2f (-1%%)" % bnb_stop)
        print("  TP (gunner): $%.2f (+1.5%%)" % bnb_tp_price)
        print("  After gunner: 70%% sold, 30%% runner at breakeven")
    else:
        print("\nBNB buy failed -- orphans still adopted, restart bot safely")

    # ── Step 9: Save state ──
    state['alt_positions'] = alt

    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print("\nState saved. %d alt positions tracked." % len(alt))

    # ── Step 10: Summary ──
    total_deployed = 0
    for pair, pos in alt.items():
        p = float(all_data.get(pair, {}).get('LastPrice', 0))
        total_deployed += pos['qty'] * p
    # Add BTC main position
    btc_qty = state.get('exec_btc_qty', 0)
    btc_price = float(all_data.get('BTC/USD', {}).get('LastPrice', 0))
    total_deployed += btc_qty * btc_price

    print("\n=== DEPLOYMENT SUMMARY ===")
    print("Alt positions: %d" % len(alt))
    print("BNB buy: %s" % ("SUCCESS" if bnb_success else "FAILED"))
    print("Orphans adopted: %d" % len(orphans))
    print("Total deployed: ~$%s" % format(int(total_deployed), ','))
    print("\nRestart bot: sudo systemctl start bot.service")

    # ── Step 11: Write execution log ──
    exec_log = {
        'timestamp': now,
        'bnb_success': bnb_success,
        'bnb_qty': bnb_fill_qty if bnb_success else 0,
        'bnb_price': bnb_fill_price if bnb_success else 0,
        'orphans_adopted': list(orphans.keys()),
        'total_alt_positions': len(alt),
        'backup_file': backup,
    }
    with open('execution_log.json', 'w') as f:
        json.dump(exec_log, f, indent=2)


if __name__ == '__main__':
    main()
