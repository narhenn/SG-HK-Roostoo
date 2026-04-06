"""
V10 Finals Dashboard — shows V8 bounce + RSI positions, P&L, market regime, equity.
http://YOUR_IP:8080 — auto-refreshes every 10 seconds.
"""
import json, os, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from roostoo_client import RoostooClient
try:
    from config_secrets import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
except:
    TELEGRAM_TOKEN = ""; TELEGRAM_CHAT_ID = ""

client = RoostooClient()
STARTING_CAPITAL = 1000000

def build_html():
    try:
        all_ticker = client.get_ticker().get('Data', {})
    except:
        all_ticker = {}

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

    # V10 state
    state = {}
    for sf in ['adaptive_state.json']:
        try:
            with open(sf) as f:
                state = json.load(f)
        except: pass

    v8_pos = state.get('positions', {})
    rsi_pos = state.get('rsi_positions', {})
    regime = state.get('regime', 'LOADING')
    total_bot_pnl = state.get('total_pnl', 0)
    cycle = state.get('_cycle', 0)
    consec_stops = state.get('_consecutive_stops', 0)

    # Market breadth
    movers = []
    for pair, info in all_ticker.items():
        try:
            c = float(info.get('Change', 0))
            p = float(info.get('LastPrice', 0))
            v = float(info.get('UnitTradeValue', 0))
            if p > 0: movers.append((c, pair, p, v))
        except: pass
    movers.sort(key=lambda x: -x[0])
    green_count = len([m for m in movers if m[0] > 0])
    breadth = green_count / len(movers) * 100 if movers else 50
    top5 = movers[:5]
    bot5 = movers[-5:][::-1]

    # BTC
    btc_info = all_ticker.get('BTC/USD', {})
    btc_price = float(btc_info.get('LastPrice', 0))
    btc_chg = float(btc_info.get('Change', 0))

    # Build position rows
    pos_rows = ""
    for pair, pos in v8_pos.items():
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry', 0)
        pp = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        stop = pos.get('stop', 0)
        tp = pos.get('tp', 0)
        added = '+ ADDED' if pos.get('added') else ''
        color = '#00e676' if pp >= 0 else '#ff5252'
        pos_rows += f'<tr><td><span class="badge bg-blue">V8</span></td><td class="fw">{pair}</td><td>${entry:,.4f}</td><td>${cp:,.4f}</td><td style="color:{color}" class="fw">{pp:+.2f}%</td><td>${val:,.0f}</td><td>${stop:,.4f}</td><td>${tp:,.4f}</td><td class="dim">{added}</td></tr>'

    for pair, pos in rsi_pos.items():
        cp = float(all_ticker.get(pair, {}).get('LastPrice', 0))
        entry = pos.get('entry', 0)
        pp = (cp - entry) / entry * 100 if entry > 0 else 0
        val = pos.get('qty', 0) * cp
        rsi_entry = pos.get('rsi_at_entry', '?')
        color = '#00e676' if pp >= 0 else '#ff5252'
        pos_rows += f'<tr><td><span class="badge bg-purple">RSI</span></td><td class="fw">{pair}</td><td>${entry:,.4f}</td><td>${cp:,.4f}</td><td style="color:{color}" class="fw">{pp:+.2f}%</td><td>${val:,.0f}</td><td>—</td><td>—</td><td class="dim">RSI={rsi_entry}</td></tr>'

    if not pos_rows:
        pos_rows = '<tr><td colspan="9" class="dim" style="text-align:center;padding:30px">No open positions — waiting for signals</td></tr>'

    # Holdings rows
    hold_rows = ""
    for h in holdings[:10]:
        chg_color = '#00e676' if h['change'] >= 0 else '#ff5252'
        hold_rows += f'<tr><td class="fw">{h["coin"]}</td><td>{h["qty"]:,.4f}</td><td>${h["price"]:,.4f}</td><td>${h["value"]:,.0f}</td><td style="color:{chg_color}">{h["change"]*100:+.1f}%</td></tr>'

    # Top movers
    gainer_rows = ""
    for c, pair, p, v in top5:
        gainer_rows += f'<div class="mover"><span class="fw">{pair}</span><span style="color:#00e676">{c*100:+.1f}%</span></div>'
    loser_rows = ""
    for c, pair, p, v in bot5:
        loser_rows += f'<div class="mover"><span class="fw">{pair}</span><span style="color:#ff5252">{c*100:+.1f}%</span></div>'

    # Colors
    pnl_color = '#00e676' if pnl >= 0 else '#ff5252'
    btc_color = '#00e676' if btc_chg >= 0 else '#ff5252'
    eq_class = 'big-green' if pnl >= 0 else 'big-red'
    regime_color = 'bg-red' if regime == 'VOLATILE' else 'bg-green' if regime == 'NORMAL' else 'bg-orange'

    now = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    n_v8 = len(v8_pos)
    n_rsi = len(rsi_pos)

    # Kill switch status
    kill_status = ""
    trade_results = state.get('_trade_results', [])
    if len(trade_results) >= 5 and sum(trade_results[-5:]) == 0:
        kill_status = '<span class="badge bg-red">KILL SWITCH ACTIVE</span>'
    elif consec_stops >= 2:
        kill_status = f'<span class="badge bg-orange">{consec_stops} CONSEC STOPS</span>'
    else:
        kill_status = '<span class="badge bg-green">ACTIVE</span>'

    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>QuantX V10 Finals</title>
<meta http-equiv="refresh" content="10">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'SF Pro',system-ui,sans-serif;padding:16px;max-width:1400px;margin:0 auto}}
.grid{{display:grid;gap:12px;margin-bottom:12px}}
.g4{{grid-template-columns:repeat(4,1fr)}}
.g2{{grid-template-columns:2fr 1fr}}
.g3{{grid-template-columns:1fr 1fr 1fr}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px}}
h1{{font-size:20px;color:#f0f6fc;margin-bottom:4px}}
h2{{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}}
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
.bar{{display:flex;gap:12px;align-items:center;margin-bottom:16px}}
.bar span{{font-size:12px;color:#8b949e}}
</style></head><body>

<div class="bar">
    <h1><span class="live"></span>QuantX V10 Finals</h1>
    <span>Cycle #{cycle} &middot; {now} &middot; {kill_status}</span>
</div>

<div class="grid g4">
    <div class="card">
        <h2>Portfolio</h2>
        <div class="{eq_class}">${total_equity:,.0f}</div>
        <div class="stat"><span>P&L</span><span style="color:{pnl_color}" class="fw">${pnl:+,.0f} ({pnl_pct:+.2f}%)</span></div>
        <div class="stat"><span>Cash</span><span>${usd:,.0f}</span></div>
        <div class="stat"><span>Deployed</span><span>${deployed:,.0f} ({deployed_pct:.0f}%)</span></div>
        <div class="stat"><span>Bot P&L</span><span class="fw">${total_bot_pnl:+,.0f}</span></div>
    </div>
    <div class="card">
        <h2>BTC/USD</h2>
        <div class="big">${btc_price:,.2f}</div>
        <div class="stat"><span>24h</span><span style="color:{btc_color}" class="fw">{btc_chg*100:+.2f}%</span></div>
        <div class="stat"><span>Coins</span><span>{len(all_ticker)}</span></div>
    </div>
    <div class="card">
        <h2>Bot Status</h2>
        <div class="stat"><span>Regime</span><span class="badge {regime_color}">{regime}</span></div>
        <div class="stat"><span>Breadth</span><span class="fw">{breadth:.0f}% green</span></div>
        <div class="stat"><span>V8 Positions</span><span class="fw">{n_v8}/3</span></div>
        <div class="stat"><span>RSI Positions</span><span class="fw">{n_rsi}/3</span></div>
        <div class="stat"><span>Consec Stops</span><span>{consec_stops}</span></div>
    </div>
    <div class="card">
        <h2>Market</h2>
        <div style="font-size:11px;color:#8b949e;margin-bottom:4px">TOP GAINERS</div>
        {gainer_rows}
        <div style="font-size:11px;color:#8b949e;margin:8px 0 4px">TOP LOSERS</div>
        {loser_rows}
    </div>
</div>

<div class="grid g2">
    <div class="card">
        <h2>Open Positions ({n_v8 + n_rsi})</h2>
        <div style="max-height:400px;overflow-y:auto">
        <table>
            <tr><th>Type</th><th>Pair</th><th>Entry</th><th>Now</th><th>P&L</th><th>Value</th><th>Stop</th><th>TP</th><th>Note</th></tr>
            {pos_rows}
        </table>
        </div>
    </div>
    <div class="card">
        <h2>Wallet Holdings</h2>
        <table>
            <tr><th>Coin</th><th>Qty</th><th>Price</th><th>Value</th><th>24h</th></tr>
            {hold_rows}
        </table>
    </div>
</div>

</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        try:
            self.wfile.write(build_html().encode())
        except:
            self.wfile.write(b'<h1>Error loading dashboard</h1>')
    def log_message(self, *a): pass


if __name__ == '__main__':
    port = 8080
    print(f"Dashboard: http://0.0.0.0:{port}")
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
