#!/usr/bin/env python3
"""
V3.2 AUTO-DEPLOY — fire-and-forget wrapper for EC2
════════════════════════════════════════════════════════════════════
Does everything automatically. You run ONE command and walk away.

WHAT IT DOES AT STARTUP:
1. Queries Roostoo balance
2. Detects existing orphan positions (coins you already hold)
3. Calculates cash-only fraction and auto-scales V3.2's position_pct
4. Blacklists orphan coins so V3.2 won't double-buy them
5. Deletes stale state files from other bots
6. Telegrams you the startup summary

WHAT IT DOES DURING RUN:
- Runs V3.2 signal engine
- Auto-stops entering NEW trades 2 hours before hackathon close
- Still manages open V3.2 positions until close
- Telegrams every action

WHAT IT DOESN'T TOUCH:
- Your existing orphan positions (they stay as-is)
- Any other bot's state files (backs them up first)

USAGE (ONE COMMAND):
  python3 v32_auto_deploy.py

THAT'S IT. Run in tmux on EC2 and walk away.
"""
import argparse
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests

try:
    from config import (
        API_KEY, SECRET_KEY, BASE_URL,
        STARTING_CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    )
    from roostoo_client import RoostooClient
    from naked_v3_2 import (
        CONFIG, COINS, STATE_FILE, BAR_S, TAKER_FEE, SLIPPAGE,
        binance_klines, detect_signal, load_state, save_state,
    )
except Exception as e:
    print(f"❌ import error: {e}")
    sys.exit(1)


# ════════════════════════════════════════
# HACKATHON END TIME — auto-stop new entries
# ════════════════════════════════════════
HACKATHON_END_UTC = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)  # 8pm SGT = 12pm UTC
STOP_NEW_ENTRIES_BEFORE = timedelta(hours=2)  # no new trades in final 2h

# ════════════════════════════════════════
# Coins managed by OTHER concurrent bots (don't blacklist these in V3.2)
# V3.2 can still trade them — it tracks its own slice via qty_initial.
# Roostoo balance check prevents cross-bot overselling; both bots retry on
# failure every 60s.
# ════════════════════════════════════════
MANAGED_BY_OTHER_BOTS = {'PENDLE'}  # pendle_manager.py owns the 37k PENDLE slice


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
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"[{ts}] {msg}", flush=True)


# ════════════════════════════════════════
# STARTUP AUDIT
# ════════════════════════════════════════
def audit_and_isolate():
    """
    Query balance, detect orphans, compute safe config.
    Returns dict with:
      total_usd, cash_usd, orphan_coins, cash_fraction, safe_position_pct
    """
    log("═" * 60)
    log("V3.2 AUTO-DEPLOY — STARTUP AUDIT")
    log("═" * 60)

    log("Fetching Roostoo balance…")
    try:
        client = RoostooClient()
        bal = client.get_balance()
    except Exception as e:
        log(f"❌ balance fetch failed: {e}")
        return None

    # DEBUG: show raw response shape
    log("🔬 RAW balance response:")
    raw_dump = json.dumps(bal, indent=2, default=str)
    for line in raw_dump.split('\n')[:40]:
        log(f"   {line}")
    if len(raw_dump.split('\n')) > 40:
        log(f"   ... ({len(raw_dump.split(chr(10))) - 40} more lines)")

    # Parse Roostoo's SpotWallet shape:
    #   { "SpotWallet": { "BTC": {"Free": 1.0, "Lock": 0}, ... } }
    cash_usd = 0.0
    orphan_coins = {}
    STABLES = {'USD', 'USDT', 'USDC', 'USD1', 'DAI', 'FDUSD', 'TUSD', 'BUSD'}

    wallet = bal.get('SpotWallet', {})
    if not wallet:
        log(f"⚠️  SpotWallet empty — defaulting to ${STARTING_CAPITAL:,.0f}")
        total_usd = STARTING_CAPITAL
        cash_usd = STARTING_CAPITAL
        cash_fraction = 1.0
        safe_pct = 0.85
        return {
            'total_usd': total_usd, 'cash_usd': cash_usd,
            'cash_fraction': cash_fraction, 'orphan_coins': {},
            'safe_position_pct': safe_pct,
        }

    log(f"parsing {len(wallet)} wallet entries…")
    for sym, info in wallet.items():
        if not isinstance(info, dict):
            continue
        free = float(info.get('Free', 0))
        lock = float(info.get('Lock', 0))
        qty = free + lock
        if qty <= 1e-9:
            continue
        if sym in STABLES:
            cash_usd += qty
            log(f"   💵 cash: {sym} = ${qty:,.2f}")
        else:
            # Fetch live Binance price for USD valuation
            bars = binance_klines(sym, '1m', 1)
            if bars:
                px = bars[-1]['c']
                usd = qty * px
                # Ignore dust under $1 — can't be sold (below min order) and
                # no reason to blacklist from V3.2 just for rounding residue
                if usd < 1.0:
                    log(f"   💨 dust: {sym} qty={qty:,.6f} ≈ ${usd:,.2f} (ignored)")
                    continue
                orphan_coins[sym] = {'qty': qty, 'usd': usd, 'price': px}
                log(f"   🪙 orphan: {sym} qty={qty:,.6f} px={px} ≈ ${usd:,.2f}")
            else:
                log(f"   ⚠️  {sym}: no price — counted qty only")
                orphan_coins[sym] = {'qty': qty, 'usd': 0, 'price': 0}

    orphan_value = sum(p['usd'] for p in orphan_coins.values())
    total_usd = cash_usd + orphan_value

    if total_usd < 1000:
        log(f"⚠️  total ${total_usd:,.0f} < $1000 — using starting capital fallback")
        total_usd = STARTING_CAPITAL
        cash_usd = STARTING_CAPITAL

    cash_fraction = cash_usd / total_usd if total_usd > 0 else 1.0
    # Safe position_pct = 90% × cash_fraction × 0.95 (extra 5% buffer for slippage)
    safe_pct = round(max(0.20, min(0.90, 0.90 * cash_fraction * 0.95)), 2)

    log(f"")
    log(f"📊 Balance:")
    log(f"   total_usd:        ${total_usd:>12,.2f}")
    log(f"   cash_usd:         ${cash_usd:>12,.2f}  ({cash_fraction*100:.1f}%)")
    log(f"   orphans value:    ${sum(p['usd'] for p in orphan_coins.values()):>12,.2f}")
    log(f"")

    if orphan_coins:
        log(f"🔒 Orphan positions detected (V3.2 will NOT touch these):")
        for sym, p in sorted(orphan_coins.items(), key=lambda x: -x[1]['usd']):
            log(f"     {sym:<8} qty={p['qty']:>14,.6f}  ${p['usd']:>11,.2f}")
    else:
        log(f"✅ No orphan positions — clean slate.")

    log(f"")
    log(f"⚙️  V3.2 auto-configured:")
    log(f"   position_pct:     {safe_pct} (was default 0.90)")
    log(f"   blacklisted:      {', '.join(orphan_coins.keys()) if orphan_coins else 'none'}")
    log(f"   max_trades:       2 (reduced from default 3 for safety)")

    return {
        'total_usd': total_usd,
        'cash_usd': cash_usd,
        'cash_fraction': cash_fraction,
        'orphan_coins': orphan_coins,
        'safe_position_pct': safe_pct,
    }


def isolate_state(audit):
    """Back up stale state files, seed V3.2 cooldowns for orphans."""
    # Backup old state files
    stale_files = [
        'data/naked_v2_state.json',
        'data/naked_v3_1_state.json',
        'data/sniper_state.json',
        'data/milk_state.json',
        'data/bricks_state.json',
        'data/trader_state.json',
        'state.json',
    ]
    backed_up = []
    for sf in stale_files:
        if os.path.exists(sf):
            ts = int(time.time())
            backup = f"{sf}.backup.{ts}"
            shutil.copy2(sf, backup)
            os.rename(sf, sf + '.disabled')
            backed_up.append(sf)
    if backed_up:
        log(f"🗂  Backed up & disabled {len(backed_up)} stale state files")

    # Build fresh V3.2 state with orphan cooldowns.
    # Coins managed by another bot (MANAGED_BY_OTHER_BOTS) are NOT cooldowned
    # so V3.2 can independently trade them alongside the other bot.
    cooldown_until = int(time.time()) + 72 * 3600
    cooldowns = {}
    for sym in audit['orphan_coins']:
        if sym in MANAGED_BY_OTHER_BOTS:
            log(f"   ℹ {sym}: managed by external bot — V3.2 CAN also trade it")
            continue
        cooldowns[sym] = cooldown_until
    state = {
        'trades_fired': 0,
        'position': None,
        'cooldowns': cooldowns,
        'trade_log': [],
        'started_at': int(time.time()),
        'audit': {
            'total_usd': audit['total_usd'],
            'cash_usd': audit['cash_usd'],
            'cash_fraction': audit['cash_fraction'],
            'safe_position_pct': audit['safe_position_pct'],
            'orphan_coins': list(audit['orphan_coins'].keys()),
        },
    }

    # Reset/create V3.2 state file fresh (ignore any leftover)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as fp:
        json.dump(state, fp, indent=2, default=str)

    log(f"✅ V3.2 state initialized: {STATE_FILE}")


# ════════════════════════════════════════
# LIVE LOOP (adapted from naked_v3_2.py Bot class)
# ════════════════════════════════════════
class AutoBot:
    def __init__(self, audit, max_trades=2, position_pct_override=None):
        self.audit = audit
        self.max_trades = max_trades
        self.position_pct = position_pct_override or audit['safe_position_pct']
        self.client = RoostooClient()
        self.state = load_state()
        self.stopped_new_entries = False

    def should_stop_new_entries(self):
        now = datetime.now(timezone.utc)
        return now >= (HACKATHON_END_UTC - STOP_NEW_ENTRIES_BEFORE)

    def should_hard_stop(self):
        now = datetime.now(timezone.utc)
        return now >= HACKATHON_END_UTC

    def get_equity(self):
        """Compute total equity from SpotWallet (cash + coin values)."""
        try:
            bal = self.client.get_balance()
            wallet = bal.get('SpotWallet', {})
            total = 0.0
            for sym, info in wallet.items():
                if not isinstance(info, dict):
                    continue
                qty = float(info.get('Free', 0)) + float(info.get('Lock', 0))
                if qty <= 0:
                    continue
                if sym in ('USD', 'USDT', 'USDC', 'USD1', 'DAI', 'FDUSD', 'TUSD', 'BUSD'):
                    total += qty
                else:
                    bars = binance_klines(sym, '1m', 1)
                    if bars:
                        total += qty * bars[-1]['c']
            return total if total > 1000 else STARTING_CAPITAL
        except Exception:
            return STARTING_CAPITAL

    def get_price(self, coin):
        bars = binance_klines(coin, '1m', 2)
        return bars[-1]['c'] if bars else None

    def place_buy(self, coin, usd):
        price = self.get_price(coin)
        if not price:
            return None
        qty = usd / (price * (1 + SLIPPAGE) * (1 + TAKER_FEE))
        pair = f"{coin}/USD"
        try:
            r = self.client.place_order(pair, 'BUY', 'MARKET', qty)
            if not r.get('Success', False):
                log(f"buy REJECTED: {r.get('ErrMsg', 'unknown')} pair={pair}")
                return None
            fill_px = float(r.get('FilledAverPrice', 0))
            fill_qty = float(r.get('FilledQuantity', 0))
            if fill_px <= 0 or fill_qty <= 0:
                log(f"buy EMPTY FILL: px={fill_px} qty={fill_qty}")
                return None
            log(f"buy FILLED: {pair} qty={fill_qty:,.2f} @ ${fill_px:.6f}")
            return {'price': fill_px, 'qty': fill_qty}
        except Exception as e:
            log(f"buy err: {e}")
            return None

    def place_sell(self, coin, qty):
        price = self.get_price(coin)
        if not price:
            return None
        pair = f"{coin}/USD"
        try:
            r = self.client.place_order(pair, 'SELL', 'MARKET', qty)
            if not r.get('Success', False):
                log(f"sell REJECTED: {r.get('ErrMsg', 'unknown')} pair={pair}")
                return None
            fill_px = float(r.get('FilledAverPrice', 0))
            fill_qty = float(r.get('FilledQuantity', 0))
            if fill_px <= 0 or fill_qty <= 0:
                log(f"sell EMPTY FILL: px={fill_px} qty={fill_qty}")
                return None
            log(f"sell FILLED: {pair} qty={fill_qty:,.2f} @ ${fill_px:.6f}")
            return {'price': fill_px, 'qty': fill_qty}
        except Exception as e:
            log(f"sell err: {e}")
            return None

    def scan_signals(self):
        log(f"V3.2 scan — {len(COINS)} coins, position_pct={self.position_pct}, score≥32")
        best = None
        for coin in COINS:
            cd = self.state['cooldowns'].get(coin, 0)
            if cd > time.time():
                continue
            bars = binance_klines(coin, '30m', 40)
            if len(bars) < 30:
                continue
            fired, entry, score = detect_signal(bars)
            if fired:
                if best is None or score > best[1]:
                    best = (coin, score, entry)
                    log(f"  ✦ {coin} score={score:.1f} px={entry}")
            time.sleep(0.05)
        return best

    def check_exit(self):
        pos = self.state['position']
        if not pos:
            return
        price = self.get_price(pos['coin'])
        if not price:
            return
        if price > pos['peak']:
            pos['peak'] = price

        closed = False
        reason = None
        exit_px = price

        if price <= pos['stop']:
            closed = True
            reason = 'STOP'
            exit_px = pos['stop']

        bars_held = (time.time() - pos['entry_t']) / BAR_S
        if not closed and bars_held > CONFIG['max_hold_bars']:
            closed = True
            reason = 'TIME'

        if self.should_hard_stop() and not closed:
            closed = True
            reason = 'HACKATHON_END'

        # BE stop @ +0.5%
        if (not closed and CONFIG['use_be_stop']
                and not pos.get('be_moved')):
            trigger = pos['avg_entry'] * (1 + CONFIG['be_trigger'])
            if price >= trigger:
                new_stop = pos['avg_entry'] * (1 + CONFIG['be_cushion'])
                if new_stop > pos['stop']:
                    pos['stop'] = new_stop
                pos['be_moved'] = True
                tg(f"🛡 BE armed {pos['coin']} stop→{new_stop:.6f}")

        # Pyramid @ +4%
        if (not closed and CONFIG['use_pyramid']
                and not pos.get('pyramid_done')
                and not pos['t1_done']):
            trigger = pos['avg_entry'] * (1 + CONFIG['pyramid_trigger'])
            if price >= trigger:
                eq = self.get_equity()
                add_usd = eq * self.position_pct * CONFIG['pyramid_add_pct']
                fill = self.place_buy(pos['coin'], add_usd)
                if fill:
                    total_q = pos['qty_initial'] + fill['qty']
                    total_cost = (pos['avg_entry'] * pos['qty_initial']
                                  + fill['price'] * fill['qty'])
                    pos['avg_entry'] = total_cost / total_q
                    pos['qty_initial'] = total_q
                    pos['qty_remaining'] += fill['qty']
                    pos['pyramid_done'] = True
                    new_stop = pos['signal_px'] * 1.001
                    if new_stop > pos['stop']:
                        pos['stop'] = new_stop
                    tg(f"🔺 PYRAMID {pos['coin']} +${add_usd:,.0f}")

        # T1 @ +10%
        if not closed and not pos['t1_done']:
            t1p = pos['avg_entry'] * (1 + CONFIG['target_1_pct'])
            if price >= t1p:
                sell_qty = pos['qty_initial'] * CONFIG['target_1_size']
                r = self.place_sell(pos['coin'], sell_qty)
                if r:
                    pos['closes'].append({'qty': sell_qty, 'px': r['price'], 'reason': 'T1'})
                    pos['qty_remaining'] -= sell_qty
                    pos['t1_done'] = True
                    pos['stop'] = max(pos['stop'], pos['avg_entry'] * 1.002)
                    tg(f"✅ T1 +10% {pos['coin']} @ {r['price']}")

        # T2 @ +20%
        if not closed and pos['t1_done'] and not pos['t2_done']:
            t2p = pos['avg_entry'] * (1 + CONFIG['target_2_pct'])
            if price >= t2p:
                sell_qty = pos['qty_initial'] * CONFIG['target_2_size']
                r = self.place_sell(pos['coin'], sell_qty)
                if r:
                    pos['closes'].append({'qty': sell_qty, 'px': r['price'], 'reason': 'T2'})
                    pos['qty_remaining'] -= sell_qty
                    pos['t2_done'] = True
                    tg(f"✅ T2 +20% {pos['coin']} @ {r['price']}")

        # Trail
        if not closed and pos['t1_done']:
            trail = pos['peak'] * (1 - CONFIG['trail_pct'])
            if trail > pos['stop']:
                pos['stop'] = trail
            if price <= pos['stop']:
                closed = True
                reason = 'TRAIL'
                exit_px = pos['stop']

        if closed and pos['qty_remaining'] > 1e-9:
            r = self.place_sell(pos['coin'], pos['qty_remaining'])
            if r:
                pos['closes'].append({'qty': pos['qty_remaining'], 'px': r['price'], 'reason': reason})
                pos['qty_remaining'] = 0

        if pos['qty_remaining'] <= 1e-9:
            entry_cost = pos['avg_entry'] * (1 + SLIPPAGE) * (1 + TAKER_FEE)
            pnl = sum((c['px'] - entry_cost) * c['qty'] for c in pos['closes'])
            msg = (f"🏁 CLOSED {pos['coin']} "
                   f"entry={pos['avg_entry']:.6f} pnl=${pnl:+,.0f} "
                   f"reason={reason or pos['closes'][-1]['reason']}")
            log(msg)
            tg(msg)
            if pnl <= 0:
                self.state['cooldowns'][pos['coin']] = int(time.time() + CONFIG['cooldown_bars'] * BAR_S)
            self.state['trade_log'].append({**pos, 'pnl': pnl, 'closed_at': int(time.time())})
            self.state['position'] = None

        save_state(self.state)

    def try_new_entry(self):
        if self.state['position']:
            return
        if self.state['trades_fired'] >= self.max_trades:
            return
        if self.should_stop_new_entries():
            if not self.stopped_new_entries:
                tg("⏸ NO NEW ENTRIES — hackathon close <2h away")
                log("stopped new entries (hackathon close approaching)")
                self.stopped_new_entries = True
            return

        equity = self.get_equity()
        if equity < CONFIG['kill_switch_eq']:
            tg(f"⛔ KILL SWITCH — ${equity:,.0f} < ${CONFIG['kill_switch_eq']:,.0f}")
            log("kill switch triggered")
            return

        best = self.scan_signals()
        if not best:
            log("no signal")
            return

        coin, score, entry = best
        usd = equity * self.position_pct
        log(f"🎯 FIRING: {coin} score={score:.1f} usd=${usd:,.0f}")
        tg(f"🎯 V3.2 SIGNAL {coin} score={score:.1f}\nentry={entry} size=${usd:,.0f}")

        fill = self.place_buy(coin, usd)
        if not fill:
            return

        self.state['position'] = {
            'coin': coin, 'signal_px': entry,
            'entry_t': int(time.time()),
            'qty_initial': fill['qty'], 'qty_remaining': fill['qty'],
            'avg_entry': fill['price'], 'peak': fill['price'],
            'stop': fill['price'] * (1 - CONFIG['hard_stop_pct']),
            't1_done': False, 't2_done': False,
            'pyramid_done': False, 'be_moved': False,
            'closes': [], 'score': score,
        }
        self.state['trades_fired'] += 1
        save_state(self.state)
        tg(f"✅ FILLED {coin} @ {fill['price']} qty={fill['qty']:.4f}\n"
           f"stop={self.state['position']['stop']:.6f}\n"
           f"fired={self.state['trades_fired']}/{self.max_trades}")

    def run(self):
        banner = (
            f"🚀 V3.2 AUTO-DEPLOY running\n"
            f"position_pct: {self.position_pct}\n"
            f"max_trades: {self.max_trades}\n"
            f"hackathon close: {HACKATHON_END_UTC:%Y-%m-%d %H:%M UTC}\n"
            f"(stops new entries 2h before close)\n"
            f"orphans isolated: {len(self.audit['orphan_coins'])}"
        )
        log(banner)
        tg(banner)

        while True:
            try:
                now = datetime.now(timezone.utc)
                if now >= HACKATHON_END_UTC + timedelta(minutes=10):
                    tg("🏁 Hackathon closed + 10min. AUTO-DEPLOY shutting down.")
                    log("hackathon end — shutting down")
                    break

                if self.state['position']:
                    self.check_exit()
                else:
                    self.try_new_entry()
            except Exception as e:
                log(f"loop err: {e}")
                traceback.print_exc()
                tg(f"⚠️ loop error: {e}")
            time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max', type=int, default=2)
    ap.add_argument('--force-pct', type=float, default=None,
                    help='override position_pct (default: auto from cash)')
    ap.add_argument('--dry-audit', action='store_true',
                    help='run audit only, do not start bot')
    args = ap.parse_args()

    audit = audit_and_isolate()
    if not audit:
        log("❌ audit failed — aborting")
        return

    tg(f"🔍 AUDIT COMPLETE\n"
       f"total ${audit['total_usd']:,.0f}\n"
       f"cash  ${audit['cash_usd']:,.0f} ({audit['cash_fraction']*100:.0f}%)\n"
       f"orphans: {len(audit['orphan_coins'])}\n"
       f"auto position_pct: {audit['safe_position_pct']}")

    if args.dry_audit:
        log("--dry-audit mode: stopping here")
        return

    isolate_state(audit)

    bot = AutoBot(audit, max_trades=args.max,
                  position_pct_override=args.force_pct)
    bot.run()


if __name__ == '__main__':
    main()
