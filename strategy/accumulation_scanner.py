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
SCAN_INTERVAL = 60          # Accumulation scan every 60s (needs time for signals to develop)
LAG_SCAN_INTERVAL = 10      # BTC lag check every 10s (must catch the 1-3 min window)
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

# Funding rate thresholds
FUNDING_EXTREME_LONG = 0.0005   # >0.05% per 8h = overleveraged longs, DON'T BUY
FUNDING_EXTREME_SHORT = -0.0003 # <-0.03% = overleveraged shorts, buy the bounce
# Cascade detection
CASCADE_DROP_PCT = -0.02        # BTC dropped >2% in 60 ticks = cascade happening
CASCADE_FADE_DROP = -0.03       # BTC dropped >3% = cascade fade opportunity
CASCADE_FADE_LOOKBACK = 60      # Check over last 60 ticks

# BTC Beta Lag parameters
BTC_MOVE_THRESHOLD = 0.003  # BTC must move +0.3% in 3 ticks
BTC_MOVE_LOOKBACK = 3       # Check last 3 ticks
ALT_LAG_MAX_MOVE = 0.001    # Alt must have moved < 0.1% (hasn't followed)
BETA_MIN = 1.0              # Minimum BTC beta to qualify
BETA_WINDOW = 60            # 60-tick rolling window for beta calc
LAG_TRAIL_PCT = 0.025       # Backtested: wider trails win. 2.5% for lag (shorter hold)
LAG_TP_PCT = 0              # DISABLED: same reason as above
LAG_TIME_STOP_MIN = 999     # DISABLED: time stops hurt P&L in backtest
LAG_SIZE_MULT = 0.8         # Slightly smaller size (less conviction than accumulation)

# Position management
MAX_POSITIONS = 4           # Fewer, bigger (like JuinStreet)
POS_SIZE_BASE = 0.15        # 15% of portfolio = ~$150k per position
HARD_STOP_PCT = 0.05  # 5% hard stop — only fires if thesis is dead. No trailing.
TP_PCT = 0           # DISABLED: gunner TP caps winners at +3% but losses run to -3.5%. R:R = 0.43:1. Pure trail is better (+0.85% avg vs +0.54%)
TIME_STOP_MIN = 999  # DISABLED: backtested — time stops CUT winners, hurt P&L
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

    def volatility_ratio(self, pair, short=5, long=30):
        """Short-term vol / long-term vol. >1.2 = breakout starting."""
        p = self._prices(pair)
        if len(p) < long + 1:
            return 1.0
        # Compute returns
        ret = [(p[i] - p[i-1]) / p[i-1] for i in range(1, len(p)) if p[i-1] > 0]
        if len(ret) < long:
            return 1.0
        short_ret = ret[-short:]
        long_ret = ret[-long:]
        # Std dev
        short_mean = sum(short_ret) / len(short_ret)
        long_mean = sum(long_ret) / len(long_ret)
        short_vol = (sum((r - short_mean) ** 2 for r in short_ret) / len(short_ret)) ** 0.5
        long_vol = (sum((r - long_mean) ** 2 for r in long_ret) / len(long_ret)) ** 0.5
        return short_vol / long_vol if long_vol > 0 else 1.0

    def bid_momentum(self, pair, n=5):
        """Fraction of last N ticks where bid ticked UP. >0.65 = buying pressure."""
        if pair not in self.data:
            return 0.5
        ticks = list(self.data[pair])
        if len(ticks) < n + 1:
            return 0.5
        recent = ticks[-(n+1):]
        ups = sum(1 for i in range(1, len(recent)) if recent[i][2] > recent[i-1][2])
        return ups / n

    def returns(self, pair, n=None):
        """Get list of 1-tick returns for a pair."""
        prices = self._prices(pair, n)
        if len(prices) < 2:
            return []
        return [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices)) if prices[i-1] > 0]

    def beta(self, pair, window=BETA_WINDOW):
        """Compute rolling beta of alt vs BTC. Returns (beta, correlation)."""
        btc_ret = self.returns('BTC/USD', window + 1)
        alt_ret = self.returns(pair, window + 1)
        n = min(len(btc_ret), len(alt_ret))
        if n < 20:
            return 0.0, 0.0
        # Use last n returns
        br = btc_ret[-n:]
        ar = alt_ret[-n:]
        # Compute covariance and variance
        mean_b = sum(br) / n
        mean_a = sum(ar) / n
        cov = sum((ar[i] - mean_a) * (br[i] - mean_b) for i in range(n)) / n
        var_b = sum((br[i] - mean_b) ** 2 for i in range(n)) / n
        var_a = sum((ar[i] - mean_a) ** 2 for i in range(n)) / n
        if var_b == 0:
            return 0.0, 0.0
        beta_val = cov / var_b
        # Correlation
        denom = (var_a * var_b) ** 0.5
        corr = cov / denom if denom > 0 else 0.0
        return beta_val, corr


class AccumulationScanner(MomentumScanner):
    """Accumulation-based scanner. Inherits exit/close logic from MomentumScanner."""

    def __init__(self, client, state, save_state_fn=None):
        super().__init__(client, state, save_state_fn)
        self.buffer = PriceBuffer()
        # Load higher-timeframe buffers (from Binance prefill)
        self._htf_buffers = {}  # {'1h': {pair: [prices]}, '4h': {...}, '1d': {...}}
        for tf, filename in [('15m', 'data/price_buffer_15m.jsonl'),
                             ('1h', 'data/price_buffer_1h.jsonl'),
                             ('4h', 'data/price_buffer_4h.jsonl'),
                             ('1d', 'data/price_buffer_1d.jsonl')]:
            try:
                if not os.path.exists(filename):
                    continue
                buf = {}
                loaded = 0
                with open(filename, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            p = e['pair']
                            if p not in buf:
                                buf[p] = []
                            buf[p].append(e['t'][1])  # just price
                            loaded += 1
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
                if loaded > 0:
                    self._htf_buffers[tf] = buf
                    log.info("[AccumScan] Loaded %s buffer: %d ticks, %d pairs" % (tf, loaded, len(buf)))
            except Exception as e:
                log.error("[AccumScan] Failed to load %s buffer: %s" % (tf, e))
        self.last_scan = 0
        self._consecutive_losses = 0
        self._loss_cooldown_until = 0

    def _htf_trend_ok(self, pair):
        """Multi-timeframe trend check. Returns (ok, score).
        Score: +1 per bullish timeframe, -1 per bearish. Need >= 0 to enter."""
        score = 0
        checked = 0

        for tf, sma_period in [('15m', 20), ('1h', 20), ('4h', 20), ('1d', 20)]:
            buf = self._htf_buffers.get(tf, {})
            prices = buf.get(pair, [])
            if len(prices) < sma_period:
                continue
            checked += 1
            sma = sum(prices[-sma_period:]) / sma_period
            current = prices[-1]
            if current >= sma:
                score += 1  # Bullish on this timeframe
            else:
                score -= 1  # Bearish

        if checked == 0:
            return True  # No HTF data = don't block

        # Need at least neutral (score >= 0) to enter
        # Bonus: if all timeframes agree bullish, score = checked
        return score >= 0

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

        # Multi-timeframe trend check (15m/1h/4h/daily)
        if not self._htf_trend_ok(pair):
            return 0, ["htf_downtrend"]

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

        # RSI: backtested — RSI 40-60 = 81% WR / +1.08%, RSI<35 = LOSING
        rsi = self.buffer.rsi(pair)
        if 40 <= rsi <= 60:
            rs = 15  # Sweet spot — highest backtested WR
        elif 35 <= rsi < 40 or 60 < rsi <= 65:
            rs = 5
        elif rsi < 35:
            rs = 0   # Oversold = catching knives in this market
        else:
            rs = 0   # Overbought
        s += rs
        r.append("rsi%.0f=%d" % (rsi, rs))

        # Volatility ratio bonus: breakout starting (short vol > long vol)
        vr_ratio = self.buffer.volatility_ratio(pair)
        if vr_ratio > 1.2:
            vr_bonus = min(10, int((vr_ratio - 1.0) * 20))
            s += vr_bonus
            r.append("vr_brk%.1f=%d" % (vr_ratio, vr_bonus))

        # Bid momentum bonus: buying pressure building
        bm = self.buffer.bid_momentum(pair)
        if bm > 0.65:
            bm_bonus = min(10, int((bm - 0.5) * 40))
            s += bm_bonus
            r.append("bid%.0f%%=%d" % (bm * 100, bm_bonus))

        return s, r

    def _btc_gate(self, ticker_data=None):
        """Check if BTC allows alt entries. Returns (allowed, boost_factor).
        boost_factor > 1.0 means high-conviction regime (e.g., cascade fade)."""
        boost = 1.0

        # Gate 1: BTC 1-min RSI overbought
        br = self.buffer.rsi('BTC/USD', RSI_PERIOD)
        if br > BTC_RSI_1M_GATE:
            log.info("[Gate] BTC RSI %.0f > %d — blocked" % (br, BTC_RSI_1M_GATE))
            return False, 0

        # Gate 2: Market breadth (use already-fetched ticker if available)
        try:
            if ticker_data:
                d = ticker_data
            else:
                tk = self.client.get_ticker()
                d = tk.get('Data', {})
            total = len(d)
            green = sum(1 for v in d.values() if float(v.get('Change', 0)) > 0)
            breadth = green / total if total > 0 else 0.5
            if breadth < 0.30:
                log.info("[Gate] Breadth %.0f%% < 30%% — blocked" % (breadth * 100))
                return False, 0
        except Exception:
            breadth = 0.5

        # Gate 3: Funding rate — extreme positive = DON'T BUY (longs crowded)
        try:
            from data.fetchers import fetch_funding_rate
            funding = fetch_funding_rate()
            self._last_funding = funding
            if funding > FUNDING_EXTREME_LONG:
                log.info("[Gate] Funding %.4f%% > %.4f%% — longs crowded, blocked" % (
                    funding * 100, FUNDING_EXTREME_LONG * 100))
                return False, 0
            # Extreme negative funding = shorts crowded = bounce likely = BOOST
            if funding < FUNDING_EXTREME_SHORT:
                log.info("[Gate] Funding %.4f%% — shorts crowded, BOOSTING entries" % (funding * 100))
                boost = 1.5
        except Exception:
            pass

        # Gate 4: Cascade detection — BTC dropped >2% in last 60 ticks = active cascade
        if self.buffer.tick_count >= CASCADE_FADE_LOOKBACK:
            btc_drop = self.buffer.roc('BTC/USD', CASCADE_FADE_LOOKBACK)
            if btc_drop < CASCADE_DROP_PCT:
                # Check if cascade is STILL happening (recent momentum negative)
                btc_recent = self.buffer.roc('BTC/USD', 5)
                if btc_recent < -0.001:
                    log.info("[Gate] CASCADE ACTIVE: BTC down %.2f%% in %d ticks, still falling — blocked" % (
                        btc_drop * 100, CASCADE_FADE_LOOKBACK))
                    return False, 0
                else:
                    # Cascade happened but BTC stabilizing — potential fade opportunity
                    funding = getattr(self, '_last_funding', 0)
                    if btc_drop < CASCADE_FADE_DROP and funding < 0 and breadth > 0.30:
                        log.info("[Gate] CASCADE FADE: BTC dropped %.2f%%, funding negative, breadth recovering — BOOST" % (
                            btc_drop * 100))
                        boost = 2.0  # Double conviction for cascade fades

        return True, boost

    def _find_btc_lag_candidates(self, all_data):
        """Find alts that should follow BTC but haven't yet."""
        # Check if BTC made a significant move
        btc_move = self.buffer.roc('BTC/USD', BTC_MOVE_LOOKBACK)
        if btc_move < BTC_MOVE_THRESHOLD:
            return []  # BTC hasn't moved enough

        log.info("[BtcLag] BTC moved %.2f%% in %d ticks — scanning for lagging alts" % (
            btc_move * 100, BTC_MOVE_LOOKBACK))

        candidates = []
        for pair, info in all_data.items():
            if pair in EXCLUDED or pair == 'BTC/USD':
                continue
            if pair in self.state.get('alt_positions', {}):
                continue
            cd = self.state.get('alt_cooldowns', {}).get(pair, 0)
            if time.time() - cd < 1800:
                continue
            if not self.buffer.ready(pair, 30):
                continue

            # Check alt hasn't moved yet
            alt_move = abs(self.buffer.roc(pair, BTC_MOVE_LOOKBACK))
            if alt_move > ALT_LAG_MAX_MOVE:
                continue  # Alt already moved, no lag to exploit

            # Check beta — needs to be correlated with BTC
            beta_val, corr = self.buffer.beta(pair)
            if beta_val < BETA_MIN or corr < 0.3:
                continue  # Not correlated enough

            # Check spread and volume
            sp = self.buffer.spread(pair)
            if sp > SPREAD_MAX:
                continue
            vol24 = float(info.get('CoinTradeValue', 0))
            if vol24 < MIN_VOLUME_USD:
                continue

            # Check price is above SMA (not in downtrend)
            sm = self.buffer.sma(pair)
            cp = self.buffer.price(pair)
            if sm > 0 and cp < sm:
                continue

            # Higher timeframe trend check
            if not self._htf_trend_ok(pair):
                continue

            # Score by beta * correlation (higher = more likely to follow)
            score = beta_val * corr * 100
            candidates.append((score, pair, info, beta_val, corr))

        candidates.sort(key=lambda x: -x[0])
        return candidates[:3]  # Top 3 laggards

    def _run_lag_scan(self, all_data):
        """BTC beta lag scan — runs every 10s for fast response."""
        if self.state.get('_competition_protect'):
            return
        if time.time() < self._loss_cooldown_until:
            return

        all_pos = self.state.get('alt_positions', {})
        n_new = sum(1 for p in all_pos.values() if p.get('entry_type') in ('accumulation', 'btc_lag'))
        if n_new >= MAX_POSITIONS:
            return

        lag_cands = self._find_btc_lag_candidates(all_data)
        if not lag_cands:
            return

        exinfo = self._get_exchange_info()
        if not exinfo:
            return

        for score, pair, info, beta_val, corr in lag_cands:
            if n_new >= MAX_POSITIONS:
                break
            sz = self._calc_size(pair, info, boost=1.0)
            sz = sz * LAG_SIZE_MULT
            if sz < 1000:
                continue
            ask_price = float(info.get('MinAsk', 0))
            if ask_price <= 0:
                continue
            # HTF trend check
            if not self._htf_trend_ok(pair):
                continue
            prec = exinfo.get(pair, {})
            pp = int(prec.get('PricePrecision', 4))
            ap_val = int(prec.get('AmountPrecision', 2))
            am = 10 ** ap_val
            qty = math.floor(sz / ask_price * am) / am
            if qty <= 0:
                continue
            lp = round(ask_price, pp)
            log.info("[BtcLag] SIGNAL %s beta=%.2f corr=%.2f $%d" % (pair, beta_val, corr, sz))
            try:
                order = self.client.place_order(pair, "BUY", "LIMIT", qty, lp)
                det = order.get("OrderDetail", order)
                oid = det.get("OrderID") or order.get("OrderID")
                fq = float(det.get("FilledQuantity", 0) or 0)
                ap2 = float(det.get("FilledAverPrice", 0) or 0)
                st = str(det.get("Status", "")).upper()
                if not oid or (st not in ("FILLED", "COMPLETED", "") and fq <= 0):
                    continue
                fp = ap2 or lp
                fqty = fq or qty
                with _alt_lock:
                    self.state['alt_positions'][pair] = {
                        'entry_price': fp, 'qty': fqty, 'peak_price': fp,
                        'trail_pct': 0, 'tp_price': 0, 'tp_pct': 0,
                        'stop': round(fp * (1 - HARD_STOP_PCT), pp),  # Fixed -5% hard stop
                        'entry_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
                        'order_id': oid, 'entry_change': float(info.get('Change', 0)),
                        'price_precision': pp, 'amount_precision': ap_val,
                        'entry_type': 'btc_lag', 'entry_beta': beta_val, 'entry_corr': corr,
                    }
                    self._save()
                n_new += 1
                log.info("[BtcLag] BOUGHT %s: %s@$%.4f=$%.0f beta=%.2f" % (pair, fqty, fp, fqty*fp, beta_val))
                try:
                    from execution.alerts import send_alert
                    send_alert("<b>BTC LAG BUY %s</b>\nBeta: %.2f | $%s\nBTC moved, alt lagging" % (
                        pair, beta_val, format(int(fqty*fp), ',')))
                except Exception:
                    pass
                time.sleep(2)
            except Exception as e:
                log.error("[BtcLag] %s: BUY error: %s" % (pair, e))

    def _calc_size(self, pair, info, boost=1.0):
        eq = self.state.get('current_equity', 1000000)
        base = eq * POS_SIZE_BASE
        vr = self.buffer.vol_ratio(pair)
        if vr > 4.0:
            base *= 2.5
        elif vr > 3.0:
            base *= 2.0
        else:
            base *= 1.5
        # Apply regime boost (e.g., 1.5x for extreme short funding, 2x for cascade fade)
        base *= boost
        # Only count NEW positions against exposure cap (legacy positions manage themselves)
        try:
            all_pos = self.state.get('alt_positions', {})
            new_exposure = 0
            ticker = self.client.get_ticker()
            td = ticker.get('Data', {})
            for p, pos in all_pos.items():
                if pos.get('entry_type') in ('accumulation', 'btc_lag'):
                    cp = float(td.get(p, {}).get('LastPrice', 0))
                    if cp > 0:
                        new_exposure += cp * pos.get('qty', 0)
        except Exception:
            new_exposure = 0
        max_new_exposure = eq * 0.60  # 60% cap — deploy big like JuinStreet
        remaining = max_new_exposure - new_exposure
        return max(0, min(base, remaining, 150000))

    def run_cycle(self):
        """Update buffer + scan for entries. Lag every 10s, accumulation every 60s."""
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

        # BTC lag runs every 10s (fast response to BTC moves)
        last_lag = getattr(self, '_last_lag_scan', 0)
        if now - last_lag >= LAG_SCAN_INTERVAL and self.buffer.tick_count >= 30:
            self._last_lag_scan = now
            self._run_lag_scan(all_data)

        # Accumulation runs every 60s (needs time for signals to develop)
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

        gate_ok, regime_boost = self._btc_gate(ticker_data=all_data)
        if not gate_ok:
            return

        # Consecutive loss cooldown: 3 losses in a row = pause 30 min
        if time.time() < self._loss_cooldown_until:
            log.info("[AccumScan] Loss cooldown active (3 consecutive losses). Resuming in %.0f min" % (
                (self._loss_cooldown_until - time.time()) / 60))
            return

        # Count only NEW positions (accumulation + btc_lag) against the cap.
        all_pos = self.state.get('alt_positions', {})
        n_new = sum(1 for p in all_pos.values() if p.get('entry_type') in ('accumulation', 'btc_lag'))
        if n_new >= MAX_POSITIONS:
            return

        # ── Accumulation Detection (every 60s) ──
        # Score all coins
        cands = []
        for pair, info in all_data.items():
            if pair in EXCLUDED or pair in all_pos:
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
        slots = MAX_POSITIONS - n_new
        exinfo = self._get_exchange_info()
        if not exinfo:
            return

        opened = 0
        for sc, pair, info, reasons in cands:
            if opened >= slots:
                break
            sz = self._calc_size(pair, info, boost=regime_boost)
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
                        'trail_pct': 0,  # No trailing — hard stop only
                        'tp_price': 0,
                        'tp_pct': 0,
                        'stop': round(fp * (1 - HARD_STOP_PCT), pp),  # Fixed -5% hard stop
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

                # Time stop (accumulation + btc_lag, pre-gunner)
                etype = pos.get('entry_type', '')
                if etype in ('accumulation', 'btc_lag') and not pos.get('gunner_fired'):
                    ts_limit = LAG_TIME_STOP_MIN if etype == 'btc_lag' else TIME_STOP_MIN
                    et = pos.get('entry_time', '')
                    if et:
                        try:
                            edt = datetime.strptime(et, '%Y-%m-%dT%H:%M:%S')
                            mins = (datetime.utcnow() - edt).total_seconds() / 60
                            if mins > ts_limit and pnl < FLAT_THRESHOLD:
                                log.info("[AccumScan] %s: TIME STOP %.0fmin P&L=%.2f%% (type=%s)" % (pair, mins, pnl*100, etype))
                                to_close.append((pair, bid, 'TIME_STOP', chg))
                                continue
                        except (ValueError, TypeError):
                            pass

                # RSI exit (accumulation + btc_lag, in profit)
                if pos.get('entry_type') in ('accumulation', 'btc_lag') and pnl > 0.003:
                    cr = self.buffer.rsi(pair, RSI_PERIOD)
                    if cr > 80:
                        log.info("[AccumScan] %s: RSI EXIT %.0f P&L=%.2f%%" % (pair, cr, pnl*100))
                        to_close.append((pair, bid, 'RSI_OVERBOUGHT', chg))
                        continue

                # HARD STOP ONLY — no trailing, no gunner
                # Stop is fixed at entry * (1 - HARD_STOP_PCT), never moves
                # This lets winners run through normal 2-3% dips
                hard_stop = entry * (1 - HARD_STOP_PCT)
                if price <= hard_stop:
                    log.info("[AccumScan] %s: HARD STOP at %.2f%% loss" % (pair, pnl*100))
                    to_close.append((pair, bid, 'HARD_STOP', chg))
                    continue

            except Exception as e:
                log.error("[AccumScan] %s exit error: %s" % (pair, e))

        for i, (pair, bid, reason, chg) in enumerate(to_close):
            if i > 0:
                time.sleep(2)
            # Track consecutive losses for cooldown
            pos = self.state.get('alt_positions', {}).get(pair, {})
            entry = pos.get('entry_price', 0)
            if entry > 0 and bid < entry:
                self._consecutive_losses += 1
                if self._consecutive_losses >= 3:
                    self._loss_cooldown_until = time.time() + 1800  # 30 min cooldown
                    log.warning("[AccumScan] 3 consecutive losses — cooling down 30 min")
            else:
                self._consecutive_losses = 0  # Reset on any win
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
