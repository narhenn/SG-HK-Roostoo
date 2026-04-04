"""
Watchdog — runs separately from the bot.
Checks if main.py is alive every 2 minutes.
If dead: waits 30s, restarts, alerts Telegram.
All events logged to logs/watchdog.log.

Run via systemd (bot.service) or: python3 watchdog.py
"""

import subprocess
import time
import logging
import os
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
CHECK_INTERVAL = 120  # Check every 2 minutes
RESTART_DELAY = 30    # Wait before restart to avoid crash loops
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BOT_DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "watchdog.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("Watchdog")


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception:
        log.warning("Failed to send Telegram alert")


def is_bot_running():
    """Check if main.py is running as a process."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*main\\.py"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False


def start_bot():
    """Start the bot in the background."""
    try:
        subprocess.Popen(
            ["python3", "main.py"],
            cwd=BOT_DIR,
            stdout=open(os.path.join(LOG_DIR, "bot_stdout.log"), "a"),
            stderr=open(os.path.join(LOG_DIR, "bot_stderr.log"), "a"),
        )
        return True
    except Exception as e:
        log.exception("Failed to start bot")
        send_telegram(f"<b>WATCHDOG ERROR</b>\nFailed to restart bot: {e}")
        return False


if __name__ == "__main__":
    log.info("Watchdog started")
    send_telegram("<b>WATCHDOG STARTED</b>\nMonitoring bot every 2 minutes. Auto-restart enabled.")

    consecutive_failures = 0

    while True:
        try:
            if is_bot_running():
                if consecutive_failures > 0:
                    log.info("Bot recovered")
                    send_telegram("<b>BOT RECOVERED</b>\nBot is running again.")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(f"Bot is DOWN (check #{consecutive_failures})")

                if consecutive_failures >= 5:
                    log.error("Bot failed to stay alive after 5 attempts — stopping restarts")
                    send_telegram(
                        "<b>WATCHDOG GIVING UP</b>\n"
                        "Bot crashed 5 times in a row. Manual intervention needed.\n"
                        "Check: tail -50 logs/bot_stderr.log"
                    )
                    # Keep monitoring but stop restarting
                    time.sleep(CHECK_INTERVAL)
                    continue

                send_telegram(
                    f"<b>BOT CRASHED</b>\n"
                    f"Attempt #{consecutive_failures}\n"
                    f"Waiting {RESTART_DELAY}s then restarting..."
                )

                log.info(f"Waiting {RESTART_DELAY}s before restart")
                time.sleep(RESTART_DELAY)

                if start_bot():
                    log.info("Bot restart initiated")
                    time.sleep(10)  # Give it a moment
                    if is_bot_running():
                        log.info("Bot restarted successfully")
                        send_telegram("<b>BOT AUTO-RESTARTED</b>\nBot is back up and running.")
                    else:
                        log.error("Bot failed to start")
                        send_telegram("<b>RESTART FAILED</b>\nBot did not come back up. Check EC2.")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Watchdog stopped by user")
            break
        except Exception as e:
            log.exception(f"Watchdog error: {e}")
            time.sleep(CHECK_INTERVAL)
