"""
Live Dashboard -Simple web UI showing bot status.
Reads from state.json and log files. Auto-refreshes every 30 seconds.

Run: python3 dashboard.py
Opens on http://localhost:8080
"""

import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from data.fetchers import fetch_fear_greed, fetch_market_breadth, get_order_precision
from roostoo_client import RoostooClient
from data.candle_builder import CandleBuilder
from strategy.regime import detect_regime
from config import TRADING_PAIR, STATE_FILE

# Global candle builder for regime detection
_candles = CandleBuilder()
_candles.bootstrap()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def get_recent_logs(n=20):
    log_file = "logs/bot.log"
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return lines[-n:]
    except:
        return []


def build_html():
    state = load_state()
    from config import STARTING_CAPITAL

    # Fetch live portfolio from Roostoo API (same account as EC2 bot)
    try:
        client = RoostooClient()
        balance = client.get_balance()
        wallet = balance.get('SpotWallet', balance.get('Data', {}))
        if isinstance(wallet, dict) and 'USD' in wallet:
            usd_free = float(wallet['USD'].get('Free', 0))
            # Check if we hold BTC (open position)
            btc_held = float(wallet.get('BTC', {}).get('Free', 0)) if 'BTC' in wallet else 0
            # Total equity = USD + BTC value at current price
            try:
                raw_t = client.get_ticker(TRADING_PAIR)
                btc_price = float(raw_t['Data'][TRADING_PAIR]['LastPrice']) if 'Data' in raw_t else 0
            except Exception:
                btc_price = 0
            current_equity = usd_free + btc_held * btc_price
        else:
            current_equity = state.get('current_equity', STARTING_CAPITAL)
            btc_held = 0
    except Exception:
        current_equity = state.get('current_equity', STARTING_CAPITAL)
        btc_held = 0

    # Fetch order history from API
    try:
        orders_resp = client.query_orders(pair='BTC/USD')
        order_list = orders_resp.get('OrderMatched', []) if isinstance(orders_resp, dict) else []
        filled_orders = [o for o in order_list if (o.get('Status') or '').upper() == 'FILLED']
        # Sort by OrderID (ascending) to get chronological order
        filled_orders.sort(key=lambda x: int(x.get('OrderID', 0)))
    except Exception:
        filled_orders = []

    # Build trade history from filled orders (pair BUY→SELL)
    trade_history = []
    buys_stack = []
    for o in filled_orders:
        side = (o.get('Side') or '').upper()
        if side == 'BUY':
            buys_stack.append(o)
        elif side == 'SELL' and buys_stack:
            buy_o = buys_stack.pop(0)
            entry_p = float(buy_o.get('FilledAverPrice', 0))
            exit_p = float(o.get('FilledAverPrice', 0))
            qty = float(buy_o.get('FilledQuantity', 0))
            fee_entry = entry_p * qty * 0.001  # 0.1% taker
            fee_exit = exit_p * qty * 0.001
            pnl = (exit_p - entry_p) * qty - fee_entry - fee_exit
            pnl_pct = pnl / (entry_p * qty) if entry_p > 0 else 0
            trade_history.append({
                'entry_price': entry_p,
                'exit_price': exit_p,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'fees': fee_entry + fee_exit,
                'side': 'BUY→SELL',
            })
    positions = []
    if btc_held > 0.00001:
        # We have an open position — find the last BUY
        buys = [o for o in filled_orders if (o.get('Side') or '').upper() == 'BUY']
        if buys:
            last_buy = buys[-1]
            positions = [{
                'entry_price': float(last_buy.get('FilledAverPrice', 0)),
                'quantity': btc_held,
                'entry_time': last_buy.get('CreateTime', ''),
            }]

    peak_equity = max(state.get('peak_equity', STARTING_CAPITAL), current_equity)
    cycle_count = state.get('cycle_count', 0)
    halt_until = state.get('halt_until')

    # Calculate drawdown
    drawdown = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0

    # Drawdown color
    if drawdown > 0.10:
        dd_color = "#F44336"
        dd_status = "EMERGENCY"
    elif drawdown > 0.05:
        dd_color = "#FF9800"
        dd_status = "WARNING"
    elif drawdown > 0.02:
        dd_color = "#FFC107"
        dd_status = "CAUTION"
    else:
        dd_color = "#4CAF50"
        dd_status = "NORMAL"

    # Try to get live data
    try:
        raw_ticker = client.get_ticker(TRADING_PAIR)
        if isinstance(raw_ticker, dict) and 'Data' in raw_ticker:
            ticker = raw_ticker['Data'].get(TRADING_PAIR, {})
        else:
            ticker = raw_ticker
        price = float(ticker.get('LastPrice', 0))
        bid = float(ticker.get('MaxBid', 0))
        ask = float(ticker.get('MinAsk', 0))
        change = float(ticker.get('Change', 0))
    except:
        price = 0
        bid = 0
        ask = 0
        change = 0

    # Fear & Greed
    try:
        fg = fetch_fear_greed()
    except:
        fg = 50

    if fg <= 25:
        fg_label = "Extreme Fear"
        fg_color = "#F44336"
    elif fg <= 45:
        fg_label = "Fear"
        fg_color = "#FF9800"
    elif fg <= 55:
        fg_label = "Neutral"
        fg_color = "#9E9E9E"
    elif fg <= 75:
        fg_label = "Greed"
        fg_color = "#8BC34A"
    else:
        fg_label = "Extreme Greed"
        fg_color = "#4CAF50"

    # Market breadth
    try:
        breadth = fetch_market_breadth()
    except:
        breadth = 0.5

    # Regime detection
    try:
        df_1h = _candles.get_df('1h')
        if len(df_1h) > 55:
            regime = detect_regime(df_1h, fg, 0.0, breadth)
        else:
            regime = "LOADING"
    except:
        regime = "UNKNOWN"

    regime_colors = {
        'TRENDING': '#2196F3',
        'SIDEWAYS': '#FF9800',
        'VOLATILE': '#F44336',
        'LOADING': '#9E9E9E',
        'UNKNOWN': '#9E9E9E',
    }

    # Position info — show ALL coins held in wallet with full detail
    all_ticker = {}
    try:
        all_ticker_raw = client.get_ticker()
        all_ticker = all_ticker_raw.get('Data', {})
    except Exception:
        pass

    # Get entry prices from filled orders for each coin
    entry_prices = {}
    for coin_name in wallet.keys() if isinstance(wallet, dict) else []:
        if coin_name == 'USD':
            continue
        pair = f"{coin_name}/USD"
        try:
            resp = client.query_orders(pair=pair)
            order_list = resp.get('OrderMatched', []) if isinstance(resp, dict) else []
            filled_buys = [o for o in order_list
                          if (o.get('Status') or '').upper() == 'FILLED'
                          and (o.get('Side') or '').upper() == 'BUY']
            filled_sells = [o for o in order_list
                           if (o.get('Status') or '').upper() == 'FILLED'
                           and (o.get('Side') or '').upper() == 'SELL']
            # Last unfilled buy = current position entry
            if filled_buys:
                # Sort by OrderID to get chronological
                filled_buys.sort(key=lambda x: int(x.get('OrderID', 0)))
                filled_sells.sort(key=lambda x: int(x.get('OrderID', 0)))
                # Find the last buy that hasn't been sold
                last_buy = filled_buys[-1]
                entry_prices[coin_name] = float(last_buy.get('FilledAverPrice', 0))
        except Exception:
            pass

    wallet_positions = []
    for coin_name in list(wallet.keys()) if isinstance(wallet, dict) else []:
        if coin_name == 'USD':
            continue
        coin_bal = wallet.get(coin_name, {})
        coin_free = float(coin_bal.get('Free', 0))
        if coin_free > 0.00001:
            pair = f"{coin_name}/USD"
            t_info = all_ticker.get(pair, {})
            coin_price = float(t_info.get('LastPrice', 0))
            coin_change = float(t_info.get('Change', 0))
            coin_bid = float(t_info.get('MaxBid', 0))
            coin_ask = float(t_info.get('MinAsk', 0))
            coin_value = coin_free * coin_price
            entry_p = entry_prices.get(coin_name, 0)
            if coin_value > 1 and coin_price > 0:
                unrealized = (coin_price - entry_p) * coin_free if entry_p > 0 else 0
                unrealized_pct = (coin_price - entry_p) / entry_p * 100 if entry_p > 0 else 0
                wallet_positions.append({
                    'coin': coin_name,
                    'pair': pair,
                    'qty': coin_free,
                    'price': coin_price,
                    'entry': entry_p,
                    'value': coin_value,
                    'change': coin_change,
                    'bid': coin_bid,
                    'ask': coin_ask,
                    'unrealized': unrealized,
                    'unrealized_pct': unrealized_pct,
                })

    if wallet_positions:
        pos_cards = ""
        total_pos_value = 0
        total_unrealized = 0
        for wp in wallet_positions:
            pnl_color = "#4CAF50" if wp['unrealized'] >= 0 else "#F44336"
            chg_color = "#4CAF50" if wp['change'] >= 0 else "#F44336"
            total_pos_value += wp['value']
            total_unrealized += wp['unrealized']

            # Format qty based on size
            if wp['qty'] > 10000:
                qty_str = f"{wp['qty']:,.0f}"
            elif wp['qty'] > 1:
                qty_str = f"{wp['qty']:,.2f}"
            else:
                qty_str = f"{wp['qty']:.5f}"

            pos_cards += f"""
        <div class="card">
            <h3>{wp['coin']} <span class="indicator" style="background:{chg_color};color:#fff">{wp['change']*100:+.1f}% 24h</span></h3>
            <div class="stat-row">
                <span>Price</span>
                <span>${wp['price']:,.4f}</span>
            </div>
            <div class="stat-row">
                <span>Entry</span>
                <span>${wp['entry']:,.4f}</span>
            </div>
            <div class="stat-row">
                <span>Quantity</span>
                <span>{qty_str} {wp['coin']}</span>
            </div>
            <div class="stat-row">
                <span>Value</span>
                <span>${wp['value']:,.2f}</span>
            </div>
            <div class="stat-row">
                <span>Bid / Ask</span>
                <span>${wp['bid']:,.4f} / ${wp['ask']:,.4f}</span>
            </div>
            <div class="stat-row">
                <span>Unrealized P&L</span>
                <span style="color:{pnl_color};font-weight:bold">${wp['unrealized']:+,.2f} ({wp['unrealized_pct']:+.2f}%)</span>
            </div>
        </div>"""

        summary_color = "#4CAF50" if total_unrealized >= 0 else "#F44336"
        position_html = f"""
        <div class="card full-width" style="margin-bottom:15px">
            <h3>Open Positions ({len(wallet_positions)})</h3>
            <div class="stat-row">
                <span>Total Exposure</span>
                <span>${total_pos_value:,.0f}</span>
            </div>
            <div class="stat-row">
                <span>Total Unrealized P&L</span>
                <span style="color:{summary_color};font-weight:bold">${total_unrealized:+,.2f}</span>
            </div>
        </div>
        {pos_cards}"""
    else:
        position_html = """
        <div class="card">
            <h3>Open Positions</h3>
            <p style="color: #9E9E9E; text-align: center; padding: 20px;">No open positions - waiting for signal</p>
        </div>
        """

    # Trade history table
    if trade_history:
        recent = trade_history[-10:][::-1]
        rows = ""
        for t in recent:
            pnl = t.get('pnl', 0)
            pnl_pct = t.get('pnl_pct', 0)
            color = "#4CAF50" if pnl > 0 else "#F44336"
            rows += f"""
            <tr>
                <td>{t.get('pair', TRADING_PAIR)}</td>
                <td>{t.get('side', 'BUY')}</td>
                <td>${t.get('entry_price', 0):,.2f}</td>
                <td>${t.get('exit_price', 0):,.2f}</td>
                <td style="color: {color}">{pnl_pct:+.2%}</td>
                <td style="color: {color}">${pnl:+,.2f}</td>
                <td style="color: #FF9800">${t.get('fees', 0):,.2f}</td>
                <td>{t.get('duration_seconds', 0):.0f}s</td>
            </tr>"""
        trades_html = f"""
        <div class="card full-width">
            <h3>Recent Trades</h3>
            <table>
                <thead>
                    <tr><th>Pair</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L %</th><th>P&L $</th><th>Fees</th><th>Duration</th></tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        """
    else:
        trades_html = """
        <div class="card full-width">
            <h3>Recent Trades</h3>
            <p style="color: #9E9E9E; text-align: center; padding: 20px;">No trades yet</p>
        </div>
        """

    # Recent logs
    logs = get_recent_logs(15)
    log_lines = ""
    for line in logs:
        line = line.strip()
        if "BUY" in line or "SELL" in line:
            log_lines += f'<div style="color: #4CAF50">{line}</div>'
        elif "STOP" in line or "ERROR" in line or "BLOCKED" in line:
            log_lines += f'<div style="color: #F44336">{line}</div>'
        elif "HOLD" in line:
            log_lines += f'<div style="color: #9E9E9E">{line}</div>'
        else:
            log_lines += f'<div>{line}</div>'

    # Win rate
    total_trades = len(trade_history)
    wins = len([t for t in trade_history if t.get('pnl', 0) > 0])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # Halt status
    halt_html = ""
    if halt_until:
        halt_html = f'<div style="background: #F44336; color: white; padding: 10px; text-align: center; border-radius: 8px; margin-bottom: 15px; font-weight: bold;">BOT HALTED UNTIL {halt_until}</div>'

    price_change_color = "#4CAF50" if change >= 0 else "#F44336"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>QuantX Trading Dashboard</title>
    <meta http-equiv="refresh" content="30">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background: #121212; color: #E0E0E0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; padding: 20px; }}
        h1 {{ color: #fff; margin-bottom: 5px; font-size: 24px; }}
        h3 {{ color: #90CAF9; margin-bottom: 12px; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; }}
        .subtitle {{ color: #9E9E9E; font-size: 12px; margin-bottom: 20px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; margin-bottom: 15px; }}
        .card {{ background: #1E1E1E; border-radius: 12px; padding: 20px; border: 1px solid #333; }}
        .full-width {{ grid-column: 1 / -1; }}
        .big-number {{ font-size: 32px; font-weight: bold; color: #fff; }}
        .stat-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #2a2a2a; }}
        .stat-row:last-child {{ border-bottom: none; }}
        .indicator {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ text-align: left; padding: 8px; color: #9E9E9E; border-bottom: 1px solid #333; font-size: 11px; text-transform: uppercase; }}
        td {{ padding: 8px; border-bottom: 1px solid #2a2a2a; }}
        .log-box {{ background: #0a0a0a; border-radius: 8px; padding: 15px; font-size: 11px; font-family: 'Courier New', monospace; max-height: 300px; overflow-y: auto; line-height: 1.6; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        .live-dot {{ display: inline-block; width: 8px; height: 8px; background: #4CAF50; border-radius: 50%; margin-right: 8px; animation: pulse 2s infinite; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1><span class="live-dot"></span>QuantX Trading Dashboard</h1>
            <div class="subtitle">Team177-QuantX (NTU) | {TRADING_PAIR} | Cycle #{cycle_count} | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</div>
        </div>
    </div>

    {halt_html}

    <div class="grid">
        <div class="card">
            <h3>BTC Price</h3>
            <div class="big-number">${price:,.2f}</div>
            <div style="margin-top: 8px;">
                <span style="color: {price_change_color}">{change:+.2%} (24h)</span>
            </div>
            <div class="stat-row" style="margin-top: 10px">
                <span>Bid</span><span>${bid:,.2f}</span>
            </div>
            <div class="stat-row">
                <span>Ask</span><span>${ask:,.2f}</span>
            </div>
        </div>

        <div class="card">
            <h3>Portfolio</h3>
            <div class="big-number">${current_equity:,.0f}</div>
            <div class="stat-row" style="margin-top: 10px">
                <span>Peak</span><span>${peak_equity:,.0f}</span>
            </div>
            <div class="stat-row">
                <span>Drawdown</span>
                <span style="color: {dd_color}; font-weight: bold">{drawdown:.2%} ({dd_status})</span>
            </div>
            <div class="stat-row">
                <span>Total Trades</span><span>{total_trades}</span>
            </div>
            <div class="stat-row">
                <span>Win Rate</span><span>{win_rate:.0f}%</span>
            </div>
        </div>

        <div class="card">
            <h3>Market Signals</h3>
            <div class="stat-row">
                <span>Fear & Greed</span>
                <span><span class="indicator" style="background: {fg_color}; color: #fff">{fg} -{fg_label}</span></span>
            </div>
            <div class="stat-row">
                <span>Market Breadth</span>
                <span>{breadth:.0%} coins up</span>
            </div>
            <div class="stat-row">
                <span>Regime</span>
                <span class="indicator" style="background: {regime_colors.get(regime, '#9E9E9E')}; color: #fff">{regime}</span>
            </div>
        </div>

        {position_html}
    </div>

    {trades_html}

    <div class="card full-width" style="margin-top: 15px">
        <h3>Live Logs</h3>
        <div class="log-box">{log_lines if log_lines else '<div style="color: #9E9E9E">No logs yet</div>'}</div>
    </div>
</body>
</html>"""
    return html


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(build_html().encode())

    def log_message(self, format, *args):
        pass  # Suppress request logs


if __name__ == "__main__":
    port = 8080
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Auto-refreshes every 30 seconds")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")
        server.server_close()
