"""
Patch state.json to adopt orphaned positions.
Run on EC2: sudo systemctl stop bot.service && python3 patch_orphans.py && sudo systemctl start bot.service
"""
import json
import shutil
import time

STATE_FILE = 'state.json'
BACKUP_FILE = f'state.json.backup.{int(time.time())}'

# Load current state
with open(STATE_FILE) as f:
    state = json.load(f)

# Backup first
shutil.copy2(STATE_FILE, BACKUP_FILE)
print(f"Backed up to {BACKUP_FILE}")

alt = state.get('alt_positions', {})
now = time.strftime('%Y-%m-%dT%H:%M:%S')

# 1. Add brand new orphaned positions (not in alt_positions at all)
new_orphans = {
    "FORM/USD": {
        "entry_price": 0.2539,
        "qty": 263479.7,
        "peak_price": 0.2539,
        "trail_pct": 0.02,
        "tp_price": 0.2577,
        "tp_pct": 0.015,
        "stop": 0.2488,
        "entry_time": now,
        "order_id": 0,
        "entry_change": 0.0139,
        "price_precision": 4,
        "amount_precision": 2
    },
    "OPEN/USD": {
        "entry_price": 0.1685,
        "qty": 235017.5,
        "peak_price": 0.1685,
        "trail_pct": 0.02,
        "tp_price": 0.171,
        "tp_pct": 0.015,
        "stop": 0.1651,
        "entry_time": now,
        "order_id": 0,
        "entry_change": 0.0977,
        "price_precision": 4,
        "amount_precision": 2
    },
    "TUT/USD": {
        "entry_price": 0.00993,
        "qty": 3600058.0,
        "peak_price": 0.00993,
        "trail_pct": 0.02,
        "tp_price": 0.0101,
        "tp_pct": 0.015,
        "stop": 0.0097,
        "entry_time": now,
        "order_id": 0,
        "entry_change": 0.0419,
        "price_precision": 4,
        "amount_precision": 2
    },
    "WLFI/USD": {
        "entry_price": 0.1015,
        "qty": 70850.2,
        "peak_price": 0.1015,
        "trail_pct": 0.02,
        "tp_price": 0.103,
        "tp_pct": 0.015,
        "stop": 0.0995,
        "entry_time": now,
        "order_id": 0,
        "entry_change": 0.0273,
        "price_precision": 4,
        "amount_precision": 2
    },
}

for pair, pos in new_orphans.items():
    if pair in alt:
        print(f"SKIP {pair} — already tracked")
    else:
        alt[pair] = pos
        print(f"ADDED {pair}: qty={pos['qty']}, stop={pos['stop']}")

# 2. Update WIF and AVAX to include full wallet quantity
# WIF: wallet has 112871.29, state tracks 14461.47
if 'WIF/USD' in alt:
    old_qty = alt['WIF/USD']['qty']
    alt['WIF/USD']['qty'] = 112871.29
    print(f"UPDATED WIF/USD qty: {old_qty} -> 112871.29")

# AVAX: wallet has 1497.1, state tracks 459.87
if 'AVAX/USD' in alt:
    old_qty = alt['AVAX/USD']['qty']
    alt['AVAX/USD']['qty'] = 1497.1
    print(f"UPDATED AVAX/USD qty: {old_qty} -> 1497.1")

state['alt_positions'] = alt

# Write back
with open(STATE_FILE, 'w') as f:
    json.dump(state, f, indent=2)

print(f"\nDone. {len(alt)} alt positions now tracked.")
print("Restart bot: sudo systemctl start bot.service")
