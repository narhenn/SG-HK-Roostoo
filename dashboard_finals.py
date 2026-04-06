"""
Finals Dashboard — Clean, real-time, shows all 3 strategies.
http://localhost:8080 — auto-refreshes every 10 seconds.
"""
import json, os, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from roostoo_client import RoostooClient
from config import TRADING_PAIR, STARTING_CAPITAL

client = RoostooClient()

def build_html():
    # ── Fetch live data ──
    try:
        all_ticker = client.get_ticker().get('Data', {})
        btc = all_ticker.get('BTC/USD', {})
        price = float(btc.get('LastPrice', 0))
        bid = float(btc.get('MaxBid', 0))
        ask = float(btc.get('MinAsk', 0))
        change = float(btc.get('Change', 0))
    except:
        all_ticker = {}; price = 0; bid = 0; ask = 0; change = 0

    # ── Portfolio ──
    try:
        bal = client.get_balance()
        wallet = bal.get('SpotWallet', {})
        usd = float(wallet.get('USD', {}).get('Free', 0))
        total_equity = usd
        holdings = []
        for coin, info in wallet.items():
            if coin == 'USD': continue
            free = float(info.get('Free', 0))
            if free > 0.0001:
                pair = f"{coin}/USD"
                cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
                val = free * cp
                chg = float(all_ticker.get(pair, {}).get('Change', 0))
                total_equity += val
                holdings.append({'coin': coin, 'qty': free, 'price': cp, 'value': val, 'change': chg})
        holdings.sort(key=lambda x: -x['value'])
    except:
        usd = 0; total_equity = STARTING_CAPITAL; holdings = []

    pnl = total_equity - STARTING_CAPITAL
    pnl_pct = pnl / STARTING_CAPITAL * 100
    deployed = total_equity - usd
    deployed_pct = deployed / total_equity * 100 if total_equity > 0 else 0

    # ── State ──
    state = {}
    try:
        with open('state.json') as f:
            state = json.load(f)
    except: pass

    breakout_pos = state.get('breakout_positions', {})
    alt_pos = state.get('alt_positions', {})
    btc_open = state.get('exec_position_open', False)
    cycle = state.get('cycle_count', 0)

    # ── Breakout trade history ──
    bt_hist = state.get('breakout_trade_history', [])
    bt_wins = len([t for t in bt_hist if t.get('pnl', 0) > 0])
    bt_total = len(bt_hist)
    bt_pnl = sum(t.get('pnl', 0) for t in bt_hist)
    bt_wr = bt_wins / bt_total * 100 if bt_total > 0 else 0

    alt_hist = state.get('alt_trade_history', [])
    alt_wins = len([t for t in alt_hist if t.get('pnl', 0) > 0])
    alt_total = len(alt_hist)
    alt_pnl = sum(t.get('pnl', 0) for t in alt_hist)

    btc_hist = state.get('trade_history', [])
    btc_wins = len([t for t in btc_hist if t.get('pnl', 0) > 0])
    btc_total_trades = len(btc_hist)
    btc_pnl = sum(t.get('pnl', 0) for t in btc_hist)

    # ── Market overview — top movers ──
    movers = []
    for pair, info in all_ticker.items():
        try:
            c = float(info.get('Change', 0))
            p = float(info.get('LastPrice', 0))
            if p > 0:
                movers.append((c, pair, p))
        except: pass
    movers.sort(key=lambda x: -x[0])
    top_gainers = movers[:5]
    top_losers = movers[-5:][::-1]
    green_count = len([m for m in movers if m[0] > 0])
    breadth = green_count / len(movers) * 100 if movers else 50

    # ── Build positions HTML ──
    pos_rows = ""
    all_positions = []
    for pair, pos in breakout_pos.items():
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry_price', 0)
        pnl_p = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        all_positions.append({'pair': pair, 'type': 'BREAKOUT', 'entry': entry, 'price': cp, 'pnl': pnl_p, 'value': val, 'signal': pos.get('signal_type', '')})
    for pair, pos in alt_pos.items():
        if pos.get('sell_failed'): continue
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry_price', 0)
        pnl_p = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        all_positions.append({'pair': pair, 'type': 'ALT', 'entry': entry, 'price': cp, 'pnl': pnl_p, 'value': val, 'signal': pos.get('entry_type', '')})
    if btc_open:
        ep = state.get('exec_entry_price', 0)
        pnl_p = (price - ep) / ep * 100 if ep > 0 else 0
        val = state.get('exec_btc_qty', 0) * price
        all_positions.append({'pair': 'BTC/USD', 'type': 'ARM', 'entry': ep, 'price': price, 'pnl': pnl_p, 'value': val, 'signal': state.get('exec_signal_source', '')})

    all_positions.sort(key=lambda x: -abs(x['pnl']))
    for p in all_positions:
        color = '#00e676' if p['pnl'] >= 0 else '#ff5252'
        pos_rows += f"""<tr>
            <td><span class="badge {'bg-blue' if p['type']=='BREAKOUT' else 'bg-purple' if p['type']=='ALT' else 'bg-orange'}">{p['type']}</span></td>
            <td class="fw">{p['pair']}</td>
            <td>${p['entry']:,.4f}</td>
            <td>${p['price']:,.4f}</td>
            <td style="color:{color}" class="fw">{p['pnl']:+.2f}%</td>
            <td>${p['value']:,.0f}</td>
            <td class="dim">{p['signal']}</td>
        </tr>"""

    if not all_positions:
        pos_rows = '<tr><td colspan="7" class="dim" style="text-align:center;padding:30px">Scanners warming up — positions will appear here</td></tr>'

    # ── Recent trades ──
    all_trades = []
    for t in bt_hist[-20:]:
        all_trades.append({**t, '_type': 'BREAKOUT'})
    for t in alt_hist[-10:]:
        all_trades.append({**t, '_type': 'ALT'})
    for t in btc_hist[-5:]:
        all_trades.append({**t, '_type': 'ARM'})
    all_trades.sort(key=lambda x: x.get('exit_time', ''), reverse=True)

    trade_rows = ""
    for t in all_trades[:15]:
        pnl_val = t.get('pnl', 0)
        pnl_pct_t = t.get('pnl_pct', 0) * 100
        color = '#00e676' if pnl_val > 0 else '#ff5252'
        trade_rows += f"""<tr>
            <td><span class="badge {'bg-blue' if t['_type']=='BREAKOUT' else 'bg-purple' if t['_type']=='ALT' else 'bg-orange'}">{t['_type']}</span></td>
            <td class="fw">{t.get('pair', 'BTC/USD')}</td>
            <td style="color:{color}" class="fw">{pnl_pct_t:+.2f}%</td>
            <td style="color:{color}">${pnl_val:+,.2f}</td>
            <td class="dim">{t.get('reason', t.get('exit_reason', ''))}</td>
            <td class="dim">{t.get('exit_time', '')[-8:]}</td>
        </tr>"""

    # ── Top movers ──
    gainer_rows = ""
    for c, pair, p in top_gainers:
        gainer_rows += f'<div class="mover"><span class="fw">{pair}</span><span style="color:#00e676">{c*100:+.1f}%</span></div>'
    loser_rows = ""
    for c, pair, p in top_losers:
        loser_rows += f'<div class="mover"><span class="fw">{pair}</span><span style="color:#ff5252">{c*100:+.1f}%</span></div>'

    # ── Colors ──
    pnl_color = '#00e676' if pnl >= 0 else '#ff5252'
    change_color = '#00e676' if change >= 0 else '#ff5252'
    eq_size = 'big-green' if pnl >= 0 else 'big-red'

    now = datetime.utcnow().strftime('%H:%M:%S UTC')

    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>QuantX Finals</title>
<meta http-equiv="refresh" content="10">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'SF Pro',system-ui,sans-serif;padding:16px}}
.grid{{display:grid;gap:12px;margin-bottom:12px}}
.g4{{grid-template-columns:repeat(4,1fr)}}
.g3{{grid-template-columns:1fr 1fr 1fr}}
.g2{{grid-template-columns:2fr 1fr}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px}}
h1{{font-size:20px;color:#f0f6fc;margin-bottom:4px}}
h2{{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}}
.sub{{color:#8b949e;font-size:11px}}
.big{{font-size:36px;font-weight:700;color:#f0f6fc;letter-spacing:-1px}}
.big-green{{font-size:36px;font-weight:700;color:#00e676;letter-spacing:-1px}}
.big-red{{font-size:36px;font-weight:700;color:#ff5252;letter-spacing:-1px}}
.stat{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #21262d;font-size:13px}}
.stat:last-child{{border:none}}
.dim{{color:#8b949e}}
.fw{{font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:8px 6px;color:#8b949e;border-bottom:1px solid #30363d;font-size:11px;text-transform:uppercase;letter-spacing:0.5px}}
td{{padding:7px 6px;border-bottom:1px solid #21262d}}
.badge{{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;letter-spacing:0.5px}}
.bg-blue{{background:#1f6feb;color:#fff}}
.bg-purple{{background:#8957e5;color:#fff}}
.bg-orange{{background:#d29922;color:#fff}}
.bg-green{{background:#238636;color:#fff}}
.bg-red{{background:#da3633;color:#fff}}
.live{{display:inline-block;width:6px;height:6px;background:#00e676;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.mover{{display:flex;justify-content:space-between;padding:4px 0;font-size:12px}}
.bar{{display:flex;gap:8px;align-items:center;margin-bottom:16px}}
.bar span{{font-size:12px;color:#8b949e}}
</style></head><body>

<div class="bar">
    <h1><span class="live"></span>QuantX Finals Dashboard</h1>
    <span>Cycle #{cycle} &middot; {now} &middot; 10s refresh</span>
</div>

<div class="grid g4">
    <div class="card">
        <h2>Portfolio</h2>
        <div class="{eq_size}">${total_equity:,.0f}</div>
        <div class="stat"><span>P&L</span><span style="color:{pnl_color}" class="fw">{pnl:+,.0f} ({pnl_pct:+.2f}%)</span></div>
        <div class="stat"><span>Cash</span><span>${usd:,.0f}</span></div>
        <div class="stat"><span>Deployed</span><span>${deployed:,.0f} ({deployed_pct:.0f}%)</span></div>
        <div class="stat"><span>Peak</span><span>${max(total_equity, state.get('peak_equity', STARTING_CAPITAL)):,.0f}</span></div>
    </div>
    <div class="card">
        <h2>BTC/USD</h2>
        <div class="big">${price:,.2f}</div>
        <div class="stat"><span>24h</span><span style="color:{change_color}" class="fw">{change*100:+.2f}%</span></div>
        <div class="stat"><span>Bid</span><span>${bid:,.2f}</span></div>
        <div class="stat"><span>Ask</span><span>${ask:,.2f}</span></div>
    </div>
    <div class="card">
        <h2>Strategies</h2>
        <div class="stat"><span><span class="badge bg-blue">BREAKOUT</span></span><span class="fw">{len(breakout_pos)} pos &middot; {bt_total} trades &middot; ${bt_pnl:+,.0f}</span></div>
        <div class="stat"><span><span class="badge bg-purple">ALT</span></span><span class="fw">{len(alt_pos)} pos &middot; {alt_total} trades &middot; ${alt_pnl:+,.0f}</span></div>
        <div class="stat"><span><span class="badge bg-orange">ARM</span></span><span class="fw">{'OPEN' if btc_open else 'FLAT'} &middot; {btc_total_trades} trades &middot; ${btc_pnl:+,.0f}</span></div>
        <div class="stat"><span>Total Trades</span><span class="fw">{bt_total + alt_total + btc_total_trades}</span></div>
    </div>
    <div class="card">
        <h2>Market</h2>
        <div class="stat"><span>Breadth</span><span class="fw">{breadth:.0f}% coins up</span></div>
        <div class="stat"><span>Fear & Greed</span><span class="fw">{state.get('fear_greed', '—')}</span></div>
        <div class="stat"><span>Regime</span><span class="badge {'bg-blue' if 'TREND' in str(state.get('last_regime','')) else 'bg-red' if 'VOL' in str(state.get('last_regime','')) else 'bg-orange'}">{state.get('last_regime', 'LOADING')}</span></div>
        <div class="stat"><span>Coins Tracked</span><span>{len(all_ticker)}</span></div>
    </div>
</div>

<div class="grid g2">
    <div class="card">
        <h2>Open Positions ({len(all_positions)})</h2>
        <div style="max-height:350px;overflow-y:auto">
        <table>
            <thead><tr><th>Strategy</th><th>Pair</th><th>Entry</th><th>Current</th><th>P&L</th><th>Value</th><th>Signal</th></tr></thead>
            <tbody>{pos_rows}</tbody>
        </table></div>
    </div>
    <div class="card">
        <h2>Top Movers</h2>
        <div style="margin-bottom:8px;font-size:11px;color:#8b949e">GAINERS</div>
        {gainer_rows}
        <div style="margin:10px 0 8px;font-size:11px;color:#8b949e">LOSERS</div>
        {loser_rows}
    </div>
</div>

<div class="card">
    <h2>Recent Trades</h2>
    <div style="max-height:300px;overflow-y:auto">
    <table>
        <thead><tr><th>Strategy</th><th>Pair</th><th>P&L %</th><th>P&L $</th><th>Reason</th><th>Time</th></tr></thead>
        <tbody>{trade_rows if trade_rows else '<tr><td colspan="6" class="dim" style="text-align:center;padding:20px">No trades yet — scanners warming up</td></tr>'}</tbody>
    </table></div>
</div>

<div style="text-align:center;padding:12px;color:#30363d;font-size:11px">
Team177-QuantX (NTU) &middot; ARM v2 + Breakout Scanner &middot; SG vs HK Quant Trading Hackathon 2026
</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(build_html().encode())
    def log_message(self, *a): pass

if __name__ == "__main__":
    print("Dashboard running at http://localhost:8080")
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
