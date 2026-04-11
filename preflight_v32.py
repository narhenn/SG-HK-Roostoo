#!/usr/bin/env python3
"""
V3.2 PRE-FLIGHT CHECK
═══════════════════════════════════════════════════════════════════
Run BEFORE starting naked_v3_2.py in production.

Checks:
  1. Roostoo balance — cash vs positions breakdown
  2. Cash-to-total ratio (affects safe position_pct)
  3. Existing coin positions (to blacklist for V3.2)
  4. Any other bot state files that might collide
  5. Suggests the safe config for your actual balance

Usage: python3 preflight_v32.py
"""
import json
import os
import sys
from datetime import datetime, timezone

try:
    from config import API_KEY, SECRET_KEY, BASE_URL, STARTING_CAPITAL
    from roostoo_client import RoostooClient
except Exception as e:
    print(f"❌ cannot import config or client: {e}")
    sys.exit(1)


def main():
    print("═" * 70)
    print("NAKED V3.2 PRE-FLIGHT CHECK")
    print("═" * 70)
    print()

    # 1. Fetch balance
    print("1. Fetching Roostoo balance...")
    try:
        client = RoostooClient()
        bal = client.get_balance()
    except Exception as e:
        print(f"   ❌ balance fetch failed: {e}")
        print("   Check your API key + secret in config.py")
        return

    print(f"   ✅ raw response: {json.dumps(bal, indent=2, default=str)[:600]}")
    print()

    # 2. Extract cash + positions from SpotWallet shape
    positions = {}
    cash_usd = 0.0
    STABLES = {'USD', 'USDT', 'USDC', 'USD1', 'DAI', 'FDUSD', 'TUSD', 'BUSD'}

    wallet = bal.get('SpotWallet', {})
    for sym, info in wallet.items():
        if not isinstance(info, dict):
            continue
        qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
        if qty <= 1e-9:
            continue
        if sym in STABLES:
            cash_usd += qty
        else:
            # Price lookup
            try:
                from naked_v3_2 import binance_klines
                bars = binance_klines(sym, '1m', 1)
                usd = qty * bars[-1]['c'] if bars else 0
            except Exception:
                usd = 0
            positions[sym] = {'qty': qty, 'usd': usd}

    positions_value = sum(p['usd'] for p in positions.values())
    total_usd = cash_usd + positions_value

    print("2. Balance breakdown:")
    print(f"   total_usd (as bot sees):    ${total_usd:>12,.2f}")
    print(f"   cash (USD equivalents):     ${cash_usd:>12,.2f}")
    print(f"   positions value:            ${positions_value:>12,.2f}")
    print()

    if positions:
        print("3. Existing non-USD positions:")
        for sym, p in sorted(positions.items(), key=lambda x: -x[1]['usd']):
            print(f"     {sym:<8} qty={p['qty']:>14,.6f}  ${p['usd']:>11,.2f}")
    else:
        print("3. No existing coin positions. ✅ CLEAN SLATE.")
    print()

    # 4. Check for state file collisions
    state_files = [
        'data/naked_v3_2_state.json',
        'data/naked_v3_1_state.json',
        'data/naked_v2_state.json',
        'data/sniper_state.json',
        'data/milk_state.json',
        'data/bricks_state.json',
        'data/trader_state.json',
        'data/position_manager_state.json',
    ]
    print("4. Existing bot state files:")
    any_state = False
    for sf in state_files:
        if os.path.exists(sf):
            any_state = True
            size = os.path.getsize(sf)
            mtime = datetime.fromtimestamp(os.path.getmtime(sf), timezone.utc)
            print(f"   ⚠️  {sf}  ({size}b, {mtime:%Y-%m-%d %H:%M})")
    if not any_state:
        print("   ✅ no old state files")
    print()

    # 5. Running bot processes
    print("5. Check for other running bots:")
    print("   run manually: ps aux | grep -E 'python.*(naked|sniper|milk|bricks|trader|grid|v2|v3)' | grep -v grep")
    print()

    # 6. Recommendations
    print("═" * 70)
    print("RECOMMENDATIONS")
    print("═" * 70)
    print()

    if not positions and not any_state:
        print("✅ ALL CLEAR — safe to deploy V3.2 with default 90% position size")
        print()
        print("   Launch with:")
        print("     tmux new -s v32")
        print("     python3 naked_v3_2.py")
        return

    if positions:
        cash_fraction = cash_usd / total_usd if total_usd > 0 else 1.0
        safe_pct = round(0.90 * cash_fraction, 2)
        print(f"⚠️  Existing positions detected.")
        print(f"   cash fraction = {cash_fraction*100:.1f}%")
        print()
        print("   Choose ONE of these options:")
        print()
        print("   OPTION A (SAFEST): Liquidate existing positions first")
        print("     - Manually sell positions via Roostoo UI")
        print("     - Re-run this preflight")
        print("     - Deploy V3.2 with default settings")
        print()
        print("   OPTION B: Scale down V3.2 position size")
        print(f"     - Edit naked_v3_2.py: set CONFIG['position_pct'] = {safe_pct}")
        print(f"     - V3.2 will only buy ${total_usd * safe_pct:,.0f} per trade")
        print("     - Existing positions remain untouched")
        print()
        print("   OPTION C: Blacklist coins you hold")
        print("     - Pre-seed V3.2 state file with these coins in cooldowns")
        print(f"     - Prevents double-buy on: {', '.join(positions.keys())}")
        print("     - Run: python3 preflight_v32.py --seed-cooldowns")

    if any_state:
        print()
        print("   🗑  OLD STATE FILES FOUND:")
        print("     Delete them BEFORE starting v3.2 to avoid confusion:")
        for sf in state_files:
            if os.path.exists(sf) and 'v3_2' not in sf:
                print(f"       rm {sf}")

    print()
    print("═" * 70)


def seed_cooldowns():
    """Pre-seed V3.2 state with 72h cooldowns on any existing coin positions."""
    try:
        client = RoostooClient()
        bal = client.get_balance()
    except Exception as e:
        print(f"❌ {e}")
        return

    positions = {}
    STABLES = {'USD', 'USDT', 'USDC', 'USD1', 'DAI', 'FDUSD', 'TUSD', 'BUSD'}
    wallet = bal.get('SpotWallet', {})
    for sym, info in wallet.items():
        if not isinstance(info, dict):
            continue
        qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
        if sym not in STABLES and qty > 0:
            positions[sym] = qty

    if not positions:
        print("✅ no positions to blacklist")
        return

    import time
    cooldown_until = int(time.time()) + 72 * 3600

    os.makedirs('data', exist_ok=True)
    state_file = 'data/naked_v3_2_state.json'
    state = {}
    if os.path.exists(state_file):
        state = json.load(open(state_file))

    state.setdefault('cooldowns', {})
    for sym in positions:
        state['cooldowns'][sym] = cooldown_until

    state.setdefault('trades_fired', 0)
    state.setdefault('position', None)
    state.setdefault('trade_log', [])
    state.setdefault('started_at', int(time.time()))

    with open(state_file, 'w') as fp:
        json.dump(state, fp, indent=2, default=str)
    print(f"✅ seeded {len(positions)} coin cooldowns into {state_file}")
    print(f"   blacklisted: {', '.join(positions.keys())}")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--seed-cooldowns':
        seed_cooldowns()
    else:
        main()
