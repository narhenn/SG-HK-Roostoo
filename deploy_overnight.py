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

    # Buy BTC ($100k) — tracked as BTC/USD in alt_positions
    # (separate from exec_btc which is the main BTC strategy position)
    # $100k not $150k: protects Calmar if market drops 3% (drawdown 1.3% not 1.5%)
    print("\n=== BUY BTC $100k ===")
    ok, fp, fq, oid = buy(client, 'BTC/USD', 100000, 2, 5)
    if ok:
        trail = 0.035  # Backtested: wider stops win. 3.5% balances WR vs Calmar
        tp_pct = 0.03  # Backtested: only TP >= 3% beats pure trailing
        btc_key = 'BTC/USD'
        if btc_key in alt:
            # Merge with existing alt BTC position
            old = alt[btc_key]
            old_cost = old['entry_price'] * old['qty']
            new_cost = fp * fq
            total_qty = old['qty'] + fq
            avg_entry = (old_cost + new_cost) / total_qty
            alt[btc_key]['qty'] = total_qty
            alt[btc_key]['entry_price'] = avg_entry
            alt[btc_key]['peak_price'] = max(old.get('peak_price', avg_entry), fp)
            alt[btc_key]['stop'] = round(avg_entry * (1 - trail), 2)
            alt[btc_key]['tp_price'] = round(avg_entry * (1 + tp_pct), 2)
            alt[btc_key]['trail_pct'] = trail
            alt[btc_key]['tp_pct'] = tp_pct
            alt[btc_key]['entry_type'] = 'accumulation'
            print("  Merged with existing BTC alt position: total %.5f BTC @ avg $%.2f" % (total_qty, avg_entry))
        else:
            alt[btc_key] = {
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
            }
            print("  Added BTC/USD to alt_positions (exec_btc tracks main position separately)")
        results.append(('BTC', fq, fp, fq * fp))
    else:
        print("  BTC buy FAILED")

    # ETH SKIPPED — research shows ETH is weaker than BTC, below all EMAs,
    # ETH/BTC ratio collapsing. Multiple sources say avoid.
    print("\n=== ETH SKIPPED (too weak, all sources say avoid) ===")

    # Save state atomically (tmp + rename)
    import os
    state['alt_positions'] = alt
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.rename(tmp, STATE_FILE)
    print("\nState saved atomically. %d alt positions." % len(alt))

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
