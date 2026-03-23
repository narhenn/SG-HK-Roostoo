"""
Accumulation Scanner v2 — detects pre-breakout accumulation patterns.
Inherits exit logic from MomentumScanner. Overrides entry logic.
Buys coins where volume is elevated but price is flat (someone loading up).
"""
import logging
import time
import json
import os
import math
import threading
from collections import deque
from datetime import datetime, timedelta

from strategy.momentum_scanner import MomentumScanner, _alt_lock, EMERGENCY_LOSS_PCT

log = logging.getLogger("TradingBot")

# ── Config ──
SCAN_INTERVAL = 60
COLD_START_TICKS = 55
BUFFER_MAX_TICKS = 300
BUFFER_FILE = 'data/price_buffer.jsonl'

# Indicator parameters (tuned for 1-min crypto)
RSI_PERIOD = 10
RSI_OB = 65
RSI_OS = 35
VOL_LOOKBACK = 20
VOL_ACCUM_THRESHOLD = 2.5
PRICE_FLAT_WINDOW = 12
PRICE_FLAT_MAX = 0.004
SMA_PERIOD = 50
SPREAD_MAX = 0.003
BTC_RSI_1M_GATE = 80
MIN_VOLUME_USD = 3_000_000
ENTRY_SCORE_MIN = 65

# Position management
MAX_POSITIONS = 6
POS_SIZE_BASE = 0.03
TRAIL_PCT = 0.007
TP_PCT = 0.013
TIME_STOP_MIN = 30
FLAT_THRESHOLD = 0.003

EXCLUDED = {'BONK/USD', 'DOGE/USD', 'SHIB/USD', 'PEPE/USD', 'FLOKI/USD',
            'PAXG/USD', '1000CHEEMS/USD', 'PUMP/USD'}


class PriceBuffer:
    """Rolling 1-min price/volume data for all coins."""

    def __init__(self):
        self.data = {}
        self.tick_count = 0
        self._load()

    def _load(self):
        if not os.path.exists(BUFFER_FILE):
            return
        loaded = 0
        try:
            with open(BUFFER_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        p = e['pair']
                        if p not in self.data:
                            self.data[p] = deque(maxlen=BUFFER_MAX_TICKS)
                        self.data[p].append(tuple(e['t']))
                        loaded += 1
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        except Exception as ex:
            log.error("[PriceBuffer] Load failed: %s" % ex)
        if loaded > 0:
            mx = max(len(d) for d in self.data.values()) if self.data else 0
            self.tick_count = mx
            log.info("[PriceBuffer] Loaded %d ticks, %d pairs, max=%d" % (loaded, len(self.data), mx))

    def _persist(self):
        tmp = BUFFER_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                for pair, ticks in self.data.items():
                    for tk in ticks:
                        f.write(json.dumps({'pair': pair, 't': list(tk)}) + '\n')
            os.rename(tmp, BUFFER_FILE)
        except Exception as ex:
            log.error("[PriceBuffer] Persist failed: %s" % ex)

    def update(self, ticker_data):
        ts = time.time()
        for pair, info in ticker_data.items():
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
                if pair not in self.data:
                    self.data[pair] = deque(maxlen=BUFFER_MAX_TICKS)
                self.data[pair].append((ts, price, bid, ask, vol, spread))
            except (ValueError, TypeError):
                continue
        self.tick_count += 1
        if self.tick_count % 5 == 0:
            self._persist()

    def ready(self, pair, n=None):
        if n is None:
            n = COLD_START_TICKS
        return pair in self.data and len(self.data[pair]) >= n

    def _prices(self, pair, n=None):
        if pair not in self.data:
            return []
        d = self.data[pair]
        if n is None:
            return [t[1] for t in d]
        return [t[1] for t in list(d)[-n:]]

    def _vols(self, pair, n=None):
        if pair not in self.data:
            return []
        d = self.data[pair]
        if n is None:
            return [t[4] for t in d]
        return [t[4] for t in list(d)[-n:]]

    def rsi(self, pair, period=RSI_PERIOD):
        prices = self._prices(pair)
        if len(prices) < period + 1:
            return 50.0
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(c, 0) for c in changes]
        losses = [max(-c, 0) for c in changes]
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
        if al == 0:
            return 100.0
        return 100 - (100 / (1 + ag / al))

    def roc(self, pair, n=5):
        p = self._prices(pair)
        if len(p) < n + 1:
            return 0.0
        old = p[-(n+1)]
        return (p[-1] - old) / old if old > 0 else 0.0

    def vol_ratio(self, pair, window=VOL_LOOKBACK):
        v = self._vols(pair)
        if len(v) < window + 1:
            return 1.0
        cur = v[-1]
        avg = sum(v[-(window+1):-1]) / window
        return cur / avg if avg > 0 else 1.0

    def spread(self, pair):
        if pair not in self.data or not self.data[pair]:
            return 999.0
        return self.data[pair][-1][5]

    def sma(self, pair, period=SMA_PERIOD):
        p = self._prices(pair, period)
        return sum(p) / len(p) if len(p) >= period else 0.0

    def price_change(self, pair, n=PRICE_FLAT_WINDOW):
        p = self._prices(pair)
        if len(p) < n + 1:
            return 999.0
        old = p[-(n+1)]
        return abs((p[-1] - old) / old) if old > 0 else 999.0

    def price(self, pair):
        if pair not in self.data or not self.data[pair]:
            return 0.0
        return self.data[pair][-1][1]

    def bid(self, pair):
        if pair not in self.data or not self.data[pair]:
            return 0.0
        return self.data[pair][-1][2]


class AccumulationScanner(MomentumScanner):
    """Accumulation-based scanner. Inherits exit/close logic from MomentumScanner."""

    def __init__(self, client, state, save_state_fn=None):
        super().__init__(client, state, save_state_fn)
        self.buffer = PriceBuffer()
        self.last_scan = 0

    def _score(self, pair, info):
        """Score coin for accumulation (0-100). Returns (score, [reasons])."""
        r = []

        if not self.buffer.ready(pair):
            return 0, ["cold"]

        vol24 = float(info.get('CoinTradeValue', 0))
        if vol24 < MIN_VOLUME_USD:
            return 0, ["low_vol"]

        sp = self.buffer.spread(pair)
        if sp > SPREAD_MAX:
            return 0, ["spread_%.2f%%" % (sp*100)]

        vr = self.buffer.vol_ratio(pair)
        if vr < VOL_ACCUM_THRESHOLD:
            return 0, ["vr_%.1f" % vr]

        pc = self.buffer.price_change(pair, PRICE_FLAT_WINDOW)
        if pc > PRICE_FLAT_MAX:
            return 0, ["not_flat_%.2f%%" % (pc*100)]

        sm = self.buffer.sma(pair)
        cp = self.buffer.price(pair)
        if sm > 0 and cp < sm:
            return 0, ["below_sma"]

        # ── Scoring ──
        s = 0

        # Volume ratio: 15 at 2.5x → 30 at 5x+
        vs = min(30, int(15 + (vr - VOL_ACCUM_THRESHOLD) / 2.5 * 15))
        s += vs
        r.append("vr%.1f=%d" % (vr, vs))

        # Quietness: 25 at 0% → 0 at 0.4%
        qs = max(0, int(25 * (1 - pc / PRICE_FLAT_MAX)))
        s += qs
        r.append("q%.2f%%=%d" % (pc*100, qs))

        # SMA distance: 10 at SMA → 20 at 1% above
        if sm > 0:
            dist = (cp - sm) / sm
            ss = min(20, max(0, int(10 + dist / 0.01 * 10)))
            s += ss
            r.append("sma=%d" % ss)

        # Spread: 15 at 0% → 0 at 0.3%
        sps = max(0, int(15 * (1 - sp / SPREAD_MAX)))
        s += sps

        # RSI: 10 for 40-60, 5 for 35-40/60-65
        rsi = self.buffer.rsi(pair)
        if 40 <= rsi <= 60:
            rs = 10
        elif 35 <= rsi <= 65:
            rs = 5
        else:
            rs = 0
        s += rs
        r.append("rsi%.0f=%d" % (rsi, rs))

        return s, r

    def _btc_gate(self):
        """Check if BTC allows alt entries."""
        br = self.buffer.rsi('BTC/USD', RSI_PERIOD)
        if br > BTC_RSI_1M_GATE:
            log.info("[AccumScan] BTC RSI %.0f > %d — blocked" % (br, BTC_RSI_1M_GATE))
            return False
        try:
            tk = self.client.get_ticker()
            d = tk.get('Data', {})
            total = len(d)
            green = sum(1 for v in d.values() if float(v.get('Change', 0)) > 0)
            if total > 0 and green / total < 0.30:
                log.info("[AccumScan] Breadth %.0f%% < 30%% — blocked" % (green/total*100))
                return False
        except Exception:
            pass
        return True

    def _calc_size(self, pair, info):
        eq = self.state.get('current_equity', 1000000)
        base = eq * POS_SIZE_BASE
        vr = self.buffer.vol_ratio(pair)
        if vr > 4.0:
            base *= 2.5
        elif vr > 3.0:
            base *= 2.0
        else:
            base *= 1.5
        max_total = eq * 0.20
        cur_exp = self._current_alt_exposure()
        remaining = max_total - cur_exp
        return max(0, min(base, remaining, 150000))

    def run_cycle(self):
        """Update buffer + scan for accumulation entries."""
        now = time.time()

        # Always update buffer
        try:
            tk = self.client.get_ticker()
            all_data = tk.get('Data', {})
            self.buffer.update(all_data)
        except Exception as e:
            log.error("[AccumScan] Ticker failed: %s" % e)
            return

        if self.state.get('_competition_protect'):
            return
        if now - self.last_scan < SCAN_INTERVAL:
            return
        self.last_scan = now

        if self.buffer.tick_count < COLD_START_TICKS:
            log.info("[AccumScan] Warming: %d/%d" % (self.buffer.tick_count, COLD_START_TICKS))
            return

        # Halt check
        hu = self.state.get('halt_until')
        if hu:
            try:
                hs = hu.replace('+00:00', '').replace('Z', '')
                hd = datetime.strptime(hs, '%Y-%m-%dT%H:%M:%S')
                if datetime.utcnow() < hd:
                    return
            except (ValueError, TypeError):
                pass

        if not self._btc_gate():
            return

        n_pos = len(self.state.get('alt_positions', {}))
        if n_pos >= MAX_POSITIONS:
            return

        # Score all coins
        cands = []
        for pair, info in all_data.items():
            if pair in EXCLUDED or pair in self.state.get('alt_positions', {}):
                continue
            cd = self.state.get('alt_cooldowns', {}).get(pair, 0)
            if now - cd < 1800:
                continue
            sc, reasons = self._score(pair, info)
            if sc >= ENTRY_SCORE_MIN:
                cands.append((sc, pair, info, reasons))

        if not cands:
            return

        cands.sort(key=lambda x: -x[0])
        slots = MAX_POSITIONS - n_pos
        exinfo = self._get_exchange_info()
        if not exinfo:
            return

        opened = 0
        for sc, pair, info, reasons in cands:
            if opened >= slots:
                break
            sz = self._calc_size(pair, info)
            if sz < 1000:
                continue

            ask_price = float(info.get('MinAsk', 0))
            if ask_price <= 0:
                continue

            prec = exinfo.get(pair, {})
            pp = int(prec.get('PricePrecision', 4))
            ap = int(prec.get('AmountPrecision', 2))
            am = 10 ** ap
            qty = math.floor(sz / ask_price * am) / am
            if qty <= 0:
                continue
            lp = round(ask_price, pp)

            log.info("[AccumScan] SIGNAL %s score=%d (%s) $%d" % (
                pair, sc, ','.join(reasons), sz))

            try:
                order = self.client.place_order(pair, "BUY", "LIMIT", qty, lp)
                det = order.get("OrderDetail", order)
                oid = det.get("OrderID") or order.get("OrderID")
                fq = float(det.get("FilledQuantity", 0) or 0)
                ap2 = float(det.get("FilledAverPrice", 0) or 0)
                st = str(det.get("Status", "")).upper()

                if not oid:
                    log.error("[AccumScan] %s: No OrderID" % pair)
                    continue
                if st not in ("FILLED", "COMPLETED", "") and fq <= 0:
                    log.error("[AccumScan] %s: Not filled (%s)" % (pair, st))
                    continue

                fp = ap2 or lp
                fqty = fq or qty

                with _alt_lock:
                    self.state['alt_positions'][pair] = {
                        'entry_price': fp,
                        'qty': fqty,
                        'peak_price': fp,
                        'trail_pct': TRAIL_PCT,
                        'tp_price': round(fp * (1 + TP_PCT), pp),
                        'tp_pct': TP_PCT,
                        'stop': round(fp * (1 - TRAIL_PCT), pp),
                        'entry_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
                        'order_id': oid,
                        'entry_change': float(info.get('Change', 0)),
                        'price_precision': pp,
                        'amount_precision': int(prec.get('AmountPrecision', 2)),
                        'entry_type': 'accumulation',
                        'entry_score': sc,
                    }
                    self._save()

                log.info("[AccumScan] BOUGHT %s: %s@$%.4f=$%.0f trail=%.1f%% tp=$%.4f" % (
                    pair, fqty, fp, fqty*fp, TRAIL_PCT*100, fp*(1+TP_PCT)))

                try:
                    from execution.alerts import send_alert
                    send_alert(
                        "<b>ACCUMULATION BUY %s</b>\n"
                        "Score: %d | $%s\n"
                        "Stop: -%.1f%% | TP: +%.1f%%\n"
                        "%s" % (pair, sc, format(int(fqty*fp), ','),
                                TRAIL_PCT*100, TP_PCT*100, ', '.join(reasons[:3])))
                except Exception:
                    pass

                opened += 1
                time.sleep(2)
            except Exception as e:
                log.error("[AccumScan] %s: BUY error: %s" % (pair, e))

    def _check_alt_exits(self):
        """Enhanced exits: time stop, RSI exit, tighter trails."""
        positions = self.state.get('alt_positions', {})
        if not positions:
            return

        # Emergency exits
        try:
            etk = self.client.get_ticker()
            ed = etk.get('Data', {})
            for pair, pos in list(positions.items()):
                if pos.get('sell_failed'):
                    continue
                ep = float(ed.get(pair, {}).get('LastPrice', 0))
                if ep <= 0:
                    continue
                entry = pos.get('entry_price', 0)
                if entry > 0 and (ep - entry) / entry < EMERGENCY_LOSS_PCT:
                    b = float(ed.get(pair, {}).get('MaxBid', 0))
                    if b > 0:
                        log.warning("[AccumScan] %s: EMERGENCY EXIT %.1f%%" % (pair, (ep-entry)/entry*100))
                        self._close_alt_position(pair, b, 'EMERGENCY_LOSS_CUT',
                            float(ed.get(pair, {}).get('Change', 0)))
                        time.sleep(1)
        except Exception as e:
            log.error("[AccumScan] Emergency check failed: %s" % e)

        positions = self.state.get('alt_positions', {})
        to_close = []

        try:
            atk = self.client.get_ticker()
            ad = atk.get('Data', {})
        except Exception:
            return

        for pair, pos in list(positions.items()):
            if pos.get('sell_failed'):
                continue
            try:
                ti = ad.get(pair, {})
                price = float(ti.get('LastPrice', 0))
                bid = float(ti.get('MaxBid', 0))
                chg = float(ti.get('Change', 0))
                if price <= 0 or bid <= 0:
                    continue

                entry = pos['entry_price']
                peak = pos.get('peak_price', entry)
                pnl = (price - entry) / entry

                # Time stop (accumulation only, pre-gunner)
                if pos.get('entry_type') == 'accumulation' and not pos.get('gunner_fired'):
                    et = pos.get('entry_time', '')
                    if et:
                        try:
                            edt = datetime.strptime(et, '%Y-%m-%dT%H:%M:%S')
                            mins = (datetime.utcnow() - edt).total_seconds() / 60
                            if mins > TIME_STOP_MIN and pnl < FLAT_THRESHOLD:
                                log.info("[AccumScan] %s: TIME STOP %.0fmin P&L=%.2f%%" % (pair, mins, pnl*100))
                                to_close.append((pair, bid, 'TIME_STOP', chg))
                                continue
                        except (ValueError, TypeError):
                            pass

                # RSI exit (accumulation, in profit)
                if pos.get('entry_type') == 'accumulation' and pnl > 0.003:
                    cr = self.buffer.rsi(pair, RSI_PERIOD)
                    if cr > 80:
                        log.info("[AccumScan] %s: RSI EXIT %.0f P&L=%.2f%%" % (pair, cr, pnl*100))
                        to_close.append((pair, bid, 'RSI_OVERBOUGHT', chg))
                        continue

                # Peak + trail update
                if price > peak:
                    pos['peak_price'] = price
                    tr = pos.get('trail_pct', TRAIL_PCT)
                    ns = price * (1 - tr)
                    if pos.get('gunner_fired'):
                        ns = max(ns, entry * 1.003)
                    if ns > pos.get('stop', 0):
                        pos['stop'] = ns

                # Gunner TP
                tp = pos.get('tp_price', 0)
                if tp > 0 and price >= tp and not pos.get('gunner_fired'):
                    gq = round(pos['qty'] * 0.7, pos.get('amount_precision', 2))
                    if gq > 0 and self._close_partial(pair, bid, gq, 'GUNNER_TP'):
                        pos['qty'] = round(pos['qty'] - gq, pos.get('amount_precision', 2))
                        pos['gunner_fired'] = True
                        pos['stop'] = entry * 1.003
                        pos['tp_price'] = 0
                        try:
                            from execution.alerts import send_alert
                            send_alert("<b>GUNNER %s</b>\n70%% sold at $%.4f\nRunner at breakeven" % (pair, bid))
                        except Exception:
                            pass
                    continue

                # Trailing stop
                if price <= pos['stop']:
                    to_close.append((pair, bid, 'TRAILING_STOP', chg))
                    continue

                # Legacy momentum reversal
                if not pos.get('entry_type') and chg < -0.005:
                    if pnl > 0.003:
                        to_close.append((pair, bid, 'MOMENTUM_REVERSAL', chg))
                    elif pnl < -0.01:
                        to_close.append((pair, bid, 'MOMENTUM_LOSS_CUT', chg))

            except Exception as e:
                log.error("[AccumScan] %s exit error: %s" % (pair, e))

        for i, (pair, bid, reason, chg) in enumerate(to_close):
            if i > 0:
                time.sleep(2)
            self._close_alt_position(pair, bid, reason, chg)
        self._save()

    def start_alt_monitor(self):
        """Start enhanced monitor thread."""
        def _loop():
            log.info("[AccumMonitor] Started. Checking every 1.5s.")
            while True:
                try:
                    if self.state.get('alt_positions'):
                        with _alt_lock:
                            self._check_alt_exits()
                except Exception as e:
                    log.error("[AccumMonitor] Error: %s" % e)
                time.sleep(1.5)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
