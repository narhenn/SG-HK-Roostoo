"""
Telegram Alerts — sends trade notifications to team group chat.
Owner: Narhen
"""

import requests
from datetime import datetime
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID


def send_alert(message: str):
    """Send a message to the team Telegram group."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass  # Don't let alert failures crash the bot


def alert_trade(direction: str, price: float, size_usd: float, regime: str,
                source: str, tf_score: int, xgb_prob: float):
    """Alert when a trade is executed."""
    emoji = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"{emoji} <b>{direction} EXECUTED</b>\n"
        f"Price: ${price:,.2f}\n"
        f"Size: ${size_usd:,.0f}\n"
        f"Regime: {regime}\n"
        f"Signal: {source}\n"
        f"TF Score: {tf_score}\n"
        f"ML Prob: {xgb_prob:.2f}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_alert(msg)


def alert_stop_loss(entry_price: float, exit_price: float, pnl_pct: float, pnl_usd: float):
    """Alert when stop-loss is hit."""
    msg = (
        f"🛑 <b>STOP-LOSS HIT</b>\n"
        f"Entry: ${entry_price:,.2f}\n"
        f"Exit: ${exit_price:,.2f}\n"
        f"P&L: {pnl_pct:+.2%} (${pnl_usd:+,.0f})\n"
        f"Cooldown: 1 hour"
    )
    send_alert(msg)


def alert_drawdown(level: str, drawdown_pct: float, equity: float):
    """Alert when drawdown threshold is crossed."""
    msg = (
        f"⚠️ <b>DRAWDOWN ALERT: {level}</b>\n"
        f"Drawdown: {drawdown_pct:.1%} from peak\n"
        f"Current Equity: ${equity:,.0f}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_alert(msg)


def alert_kill_switch(drawdown_pct: float, sharpe: float, equity: float):
    """Alert when kill switch triggers."""
    msg = (
        f"🚨🚨🚨 <b>KILL SWITCH TRIGGERED</b> 🚨🚨🚨\n"
        f"Drawdown: {drawdown_pct:.1%}\n"
        f"Rolling Sharpe: {sharpe:.2f}\n"
        f"Equity: ${equity:,.0f}\n"
        f"ALL POSITIONS CLOSED\n"
        f"BOT HALTED FOR 24 HOURS\n"
        f"CHECK LOGS IMMEDIATELY"
    )
    send_alert(msg)


def alert_startup(equity: float, regime: str, candles: int):
    """Alert when bot starts/restarts."""
    msg = (
        f"🤖 <b>BOT STARTED</b>\n"
        f"Equity: ${equity:,.0f}\n"
        f"Regime: {regime}\n"
        f"Historical candles: {candles}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_alert(msg)


def alert_daily_summary(equity: float, peak: float, trades_today: int,
                        wins: int, losses: int, pnl_today: float):
    """Daily summary alert."""
    dd = (peak - equity) / peak if peak > 0 else 0
    msg = (
        f"📊 <b>DAILY SUMMARY</b>\n"
        f"Equity: ${equity:,.0f}\n"
        f"Peak: ${peak:,.0f}\n"
        f"Drawdown: {dd:.1%}\n"
        f"Trades today: {trades_today} ({wins}W / {losses}L)\n"
        f"P&L today: ${pnl_today:+,.0f}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_alert(msg)


def alert_error(error_msg: str):
    """Alert on critical error."""
    msg = (
        f"❌ <b>BOT ERROR</b>\n"
        f"{error_msg}\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    send_alert(msg)
