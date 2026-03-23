#!/usr/bin/env python3
"""
Overnight deploy: Buy $200k BTC + $150k ETH with 1% trailing stops.
Run on EC2:
  sudo systemctl stop bot.service && python3 deploy_overnight.py && sudo systemctl start bot.service
"""
import json
import shutil
import time
import sys
import math

sys.path.insert(0, '.')
from roostoo_client import RoostooClient

STATE_FILE = 'state.json'


def buy(client, pair, usd_amount, price_prec, amount_prec):
    """Buy a coin. Returns (success, fill_price, fill_qty, order_id)."""
    ticker = client.get_ticker(pair)
    info = ticker.get('Data', {}).get(pair, {})
    ask = float(info.get('MinAsk', 0))
    if ask <= 0:
        print("  ERROR: no ask price for %s" % pair)
        return False, 0, 0, 0

    am = 10 ** amount_prec
    qty = math.floor(usd_amount / ask * am) / am
    lp = round(ask, price_prec)

    print("  Placing LIMIT BUY: %s @ $%s ($%s)" % (qty, lp, format(int(qty * lp), ',')))

    order = client.place_order(pair, "BUY", "LIMIT", qty, lp)
    det = order.get("OrderDetail", order)
    oid = det.get("OrderID") or order.get("OrderID")
    fq = float(det.get("FilledQuantity", 0) or 0)
    ap = float(det.get("FilledAverPrice", 0) or 0)
    st = str(det.get("Status", "")).upper()

    if not oid:
        print("  ERROR: no OrderID. Response: %s" % str(order)[:200])
        return False, 0, 0, 0

    fp = ap or lp
    fqty = fq or qty

    if st in ("FILLED", "COMPLETED"):
        print("  FILLED: %s @ $%s" % (fqty, fp))
    elif order.get("Success"):
        print("  Success=True (status=%s), assuming filled" % st)
    else:
        print("  WARNING: status=%s, proceeding anyway" % st)

    return True, fp, fqty, oid


def main():
    client = RoostooClient()

    # Backup state
    backup = 'state.json.backup.%d' % int(time.time())
    shutil.copy2(STATE_FILE, backup)
    print("Backed up to %s" % backup)

    # Load state
    with open(STATE_FILE) as f:
        state = json.load(f)
    alt = state.get('alt_positions', {})
    now = time.strftime('%Y-%m-%dT%H:%M:%S')

    results = []

    # Buy BTC ($200k)
    print("\n=== BUY BTC $200k ===")
    ok, fp, fq, oid = buy(client, 'BTC/USD', 200000, 2, 5)
    if ok:
        trail = 0.01
        tp_pct = 0.013
        alt['BTC2/USD'] = {
            'entry_price': fp,
            'qty': fq,
            'peak_price': fp,
            'trail_pct': trail,
            'tp_price': round(fp * (1 + tp_pct), 2),
            'tp_pct': tp_pct,
            'stop': round(fp * (1 - trail), 2),
            'entry_time': now,
            'order_id': oid,
            'entry_change': 0.035,
            'price_precision': 2,
            'amount_precision': 5,
            'entry_type': 'accumulation',
            'wallet_pair': 'BTC/USD',
        }
        results.append(('BTC', fq, fp, fq * fp))
        print("  Added as BTC2/USD (separate from main BTC position)")
    else:
        print("  BTC buy FAILED")

    time.sleep(3)

    # Buy ETH ($150k)
    print("\n=== BUY ETH $150k ===")
    ok2, fp2, fq2, oid2 = buy(client, 'ETH/USD', 150000, 2, 4)
    if ok2:
        trail = 0.01
        tp_pct = 0.013
        # We already have ETH/USD in alt_positions, need unique key
        eth_key = 'ETH/USD'
        if eth_key in alt:
            # Update existing ETH position qty and recalc
            old = alt[eth_key]
            old_cost = old['entry_price'] * old['qty']
            new_cost = fp2 * fq2
            total_qty = old['qty'] + fq2
            avg_entry = (old_cost + new_cost) / total_qty
            alt[eth_key]['qty'] = total_qty
            alt[eth_key]['entry_price'] = avg_entry
            alt[eth_key]['peak_price'] = max(old.get('peak_price', avg_entry), fp2)
            alt[eth_key]['stop'] = round(avg_entry * (1 - trail), 2)
            alt[eth_key]['tp_price'] = round(avg_entry * (1 + tp_pct), 2)
            alt[eth_key]['trail_pct'] = trail
            alt[eth_key]['tp_pct'] = tp_pct
            alt[eth_key]['entry_type'] = 'accumulation'
            print("  Merged with existing ETH position: total %.4f ETH @ avg $%.2f" % (total_qty, avg_entry))
        else:
            alt[eth_key] = {
                'entry_price': fp2,
                'qty': fq2,
                'peak_price': fp2,
                'trail_pct': trail,
                'tp_price': round(fp2 * (1 + tp_pct), 2),
                'tp_pct': tp_pct,
                'stop': round(fp2 * (1 - trail), 2),
                'entry_time': now,
                'order_id': oid2,
                'entry_change': 0.044,
                'price_precision': 2,
                'amount_precision': 4,
                'entry_type': 'accumulation',
            }
            print("  Added ETH/USD position")
        results.append(('ETH', fq2, fp2, fq2 * fp2))
    else:
        print("  ETH buy FAILED")

    # Save state
    state['alt_positions'] = alt
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print("\nState saved. %d alt positions." % len(alt))

    # Summary
    print("\n=== OVERNIGHT DEPLOY SUMMARY ===")
    for name, qty, price, val in results:
        print("  %s: %s @ $%.2f = $%s" % (name, qty, price, format(int(val), ',')))
    total_deployed = sum(r[3] for r in results)
    print("  Total new: $%s" % format(int(total_deployed), ','))
    print("  Fees: ~$%d" % int(total_deployed * 0.001))
    print("\nRestart bot: sudo systemctl start bot.service")


if __name__ == '__main__':
    main()
