#!/usr/bin/env python3
"""
Standalone Telegram Command Handler
Runs independently from the trading bot, so commands work even if bot crashes.

Start: python3 telegram_standalone.py
Runs in background, separate from scheduler.py

Commands:
  /status    - Account balance, positions, bot status
  /pnl       - P&L breakdown by pair
  /positions - Detailed position info
  /close_all - Emergency: close all positions
  /restart   - Restart the trading bot
  /logs      - Show last 20 log lines
"""

import json
import logging
import signal
import sys
import time
import urllib.request
import urllib.parse
import subprocess
import os
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BOT_SCRIPT = "scheduler.py"
BOT_DIR = os.path.dirname(os.path.abspath(__file__))


def send_telegram(message: str) -> bool:
    """Send message via Telegram."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def create_exchange():
    """Create exchange instance."""
    exchange = ccxt.binanceusdm({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
    })
    if settings.TESTNET:
        exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def is_bot_running() -> tuple[bool, int]:
    """Check if trading bot is running. Returns (is_running, pid)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", BOT_SCRIPT],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split('\n')[0])
            return True, pid
        return False, 0
    except Exception:
        return False, 0


def cmd_status():
    """Show account status and bot health."""
    try:
        exchange = create_exchange()
        balance = exchange.fetch_balance()
        info = balance.get("info", {})

        wallet = float(info.get("totalWalletBalance", 0) or 0)
        unrealized = float(info.get("totalUnrealizedProfit", 0) or 0)
        available = float(balance.get("USDT", {}).get("free", 0))
        margin_used = float(balance.get("USDT", {}).get("used", 0))

        # Count open orders
        total_open = 0
        for pair in settings.PAIRS:
            try:
                orders = exchange.fetch_open_orders(pair)
                total_open += len(orders)
            except Exception:
                pass

        # Bot status
        is_running, pid = is_bot_running()
        bot_status = f"🟢 Running (PID {pid})" if is_running else "🔴 STOPPED"

        session_pnl = round(wallet - settings.TOTAL_CAPITAL, 2)
        pnl_emoji = "🟢" if session_pnl >= 0 else "🔴"

        send_telegram(
            f"<b>Account Status</b>\n\n"
            f"Bot: {bot_status}\n\n"
            f"Wallet: <code>${wallet:,.2f}</code>\n"
            f"Available: <code>${available:,.2f}</code>\n"
            f"Margin Used: <code>${margin_used:,.2f}</code>\n"
            f"Unrealized PnL: <code>${unrealized:,.2f}</code>\n"
            f"{pnl_emoji} Session PnL: <code>${session_pnl:,.2f}</code>\n\n"
            f"Open Orders: {total_open}\n"
            f"Pairs: {len(settings.PAIRS)}\n"
            f"Leverage: {settings.LEVERAGE}x"
        )
    except Exception as e:
        logger.error(f"Status command error: {e}")
        send_telegram(f"❌ Status command failed: {e}")


def cmd_pnl():
    """Show P&L breakdown by pair."""
    try:
        exchange = create_exchange()
        balance = exchange.fetch_balance()
        info = balance.get("info", {})

        wallet = float(info.get("totalWalletBalance", 0) or 0)
        session_pnl = round(wallet - settings.TOTAL_CAPITAL, 2)

        positions = exchange.fetch_positions(settings.PAIRS)

        lines = ["<b>P&amp;L Breakdown</b>\n"]
        lines.append(f"Wallet: <code>${wallet:,.2f}</code>")
        lines.append(f"Session PnL: <code>${session_pnl:,.2f}</code>\n")

        total_unrealized = 0
        has_positions = False

        for pos in positions:
            amt = float(pos.get("contracts", 0) or 0)
            if amt > 0:
                has_positions = True
                symbol = pos.get("symbol", "")
                side = pos.get("side", "")
                unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
                total_unrealized += unrealized_pnl
                emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                lines.append(f"{emoji} {symbol} ({side}): <code>${unrealized_pnl:,.2f}</code>")

        if not has_positions:
            lines.append("No open positions")
        else:
            lines.append(f"\nTotal Unrealized: <code>${total_unrealized:,.2f}</code>")

        send_telegram("\n".join(lines))
    except Exception as e:
        logger.error(f"PnL command error: {e}")
        send_telegram(f"❌ PnL command failed: {e}")


def cmd_positions():
    """Show detailed position info."""
    try:
        exchange = create_exchange()
        positions = exchange.fetch_positions(settings.PAIRS)

        lines = ["<b>Open Positions</b>\n"]
        has_positions = False

        for pos in positions:
            amt = float(pos.get("contracts", 0) or 0)
            if amt > 0:
                has_positions = True
                symbol = pos.get("symbol", "")
                side = pos.get("side", "")
                entry = float(pos.get("entryPrice", 0) or 0)
                mark = float(pos.get("markPrice", 0) or 0)
                unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
                notional = amt * entry
                loss_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0

                emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                lines.append(f"<b>{symbol}</b> — {side.upper()}")
                lines.append(f"  Size: {amt} | Notional: <code>${notional:,.2f}</code>")
                lines.append(f"  Entry: <code>${entry:,.4f}</code>")
                lines.append(f"  Mark:  <code>${mark:,.4f}</code>")
                lines.append(f"  {emoji} PnL: <code>${unrealized_pnl:,.2f}</code> ({loss_pct:+.2f}%)")
                lines.append("")

        if not has_positions:
            lines.append("No open positions")

        send_telegram("\n".join(lines))
    except Exception as e:
        logger.error(f"Positions command error: {e}")
        send_telegram(f"❌ Positions command failed: {e}")


def cmd_close_all():
    """Emergency: close all positions."""
    try:
        exchange = create_exchange()
        positions = exchange.fetch_positions(settings.PAIRS)

        closed = []
        for pos in positions:
            amt = float(pos.get("contracts", 0) or 0)
            if amt > 0:
                symbol = pos.get("symbol", "")
                side = pos.get("side", "")
                close_side = "sell" if side == "long" else "buy"

                try:
                    exchange.create_order(
                        symbol=symbol, type="market",
                        side=close_side, amount=amt,
                        params={"reduceOnly": True},
                    )
                    unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
                    closed.append(f"{symbol} {side}: ${unrealized_pnl:,.2f}")
                except Exception as e:
                    closed.append(f"{symbol}: FAILED - {e}")

        if closed:
            report = "\n".join(closed)
            send_telegram(f"<b>🚨 Closed All Positions</b>\n\n{report}")
        else:
            send_telegram("No open positions to close")
    except Exception as e:
        logger.error(f"Close all command error: {e}")
        send_telegram(f"❌ Close all failed: {e}")


def cmd_restart():
    """Restart the trading bot."""
    try:
        is_running, pid = is_bot_running()

        if is_running:
            # Kill bot
            subprocess.run(["kill", "-9", str(pid)])
            time.sleep(2)
            send_telegram(f"🔄 Killed bot (PID {pid})")

        # Start bot
        bot_path = os.path.join(BOT_DIR, BOT_SCRIPT)
        log_path = os.path.join(BOT_DIR, "bot.log")

        subprocess.Popen(
            ["python3", bot_path],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            cwd=BOT_DIR
        )

        time.sleep(3)
        is_running, new_pid = is_bot_running()

        if is_running:
            send_telegram(f"✅ Bot restarted (PID {new_pid})")
        else:
            send_telegram("❌ Bot restart failed - check logs")
    except Exception as e:
        logger.error(f"Restart command error: {e}")
        send_telegram(f"❌ Restart failed: {e}")


def cmd_kill():
    """Kill the trading bot WITHOUT restarting it."""
    try:
        is_running, pid = is_bot_running()

        if is_running:
            subprocess.run(["kill", "-9", str(pid)])
            time.sleep(2)

            # Verify bot is dead
            is_still_running, _ = is_bot_running()
            if not is_still_running:
                send_telegram(
                    f"🛑 <b>Bot Killed</b> (PID {pid})\n\n"
                    f"✅ Telegram is still alive!\n"
                    f"Use /restart to bring the bot back."
                )
            else:
                send_telegram(f"⚠️ Kill signal sent but bot still running. Retry or check manually.")
        else:
            send_telegram("Bot is already stopped")
    except Exception as e:
        logger.error(f"Kill command error: {e}")
        send_telegram(f"❌ Kill failed: {e}")


def cmd_logs():
    """Show last 20 lines of bot log."""
    try:
        log_path = os.path.join(BOT_DIR, "bot.log")
        result = subprocess.run(
            ["tail", "-20", log_path],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            logs = result.stdout.strip()
            # Truncate if too long (Telegram limit)
            if len(logs) > 3500:
                logs = logs[-3500:]
            send_telegram(f"<b>Recent Logs</b>\n\n<code>{logs}</code>")
        else:
            send_telegram("❌ Could not read logs")
    except Exception as e:
        logger.error(f"Logs command error: {e}")
        send_telegram(f"❌ Logs command failed: {e}")


def cmd_help():
    """Show available commands."""
    send_telegram(
        "<b>Available Commands</b>\n\n"
        "/status  -  Account balance, positions, bot health\n"
        "/pnl  -  Profit &amp; loss breakdown\n"
        "/positions  -  Detailed position info\n"
        "/close_all  -  🚨 Emergency: close all positions\n"
        "/kill  -  🛑 Kill bot (keeps Telegram alive)\n"
        "/restart  -  Restart the trading bot\n"
        "/logs  -  Show recent bot logs\n"
        "/help  -  This message\n\n"
        "<i>This command handler runs independently from the bot.</i>"
    )


class TelegramListener:
    """Listens for Telegram commands and executes them."""

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.last_update_id = 0
        self._running = False

        self.handlers = {
            "/status": cmd_status,
            "/pnl": cmd_pnl,
            "/positions": cmd_positions,
            "/close_all": cmd_close_all,
            "/kill": cmd_kill,
            "/restart": cmd_restart,
            "/logs": cmd_logs,
            "/help": cmd_help,
        }

    def start(self):
        """Start listening for commands."""
        if not self.token or not self.chat_id:
            logger.error("Telegram not configured")
            return

        self._flush_pending_updates()
        self._running = True

        logger.info("Standalone Telegram listener started")
        send_telegram("🤖 <b>Standalone Command Listener Active</b>\n\nCommands work even if bot crashes. Use /help for list.")

        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                time.sleep(5)

    def _flush_pending_updates(self):
        """Consume old updates."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset=-1&timeout=0"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                results = data.get("result", [])
                if results:
                    self.last_update_id = results[-1].get("update_id", 0)
        except Exception:
            pass

    def _get_updates(self):
        """Fetch new messages."""
        params = urllib.parse.urlencode({
            "offset": self.last_update_id + 1,
            "timeout": 30,
            "allowed_updates": json.dumps(["message"]),
        })
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?{params}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    return data.get("result", [])
        except Exception as e:
            logger.debug(f"getUpdates error: {e}")
        return []

    def _handle_update(self, update):
        """Process a single update."""
        self.last_update_id = update.get("update_id", self.last_update_id)

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        if chat_id != self.chat_id:
            return

        if not text.startswith("/"):
            return

        command = text.split()[0].lower().split("@")[0]

        handler = self.handlers.get(command)
        if handler:
            logger.info(f"Executing command: {command}")
            try:
                handler()
            except Exception as e:
                logger.error(f"Command {command} error: {e}")
                send_telegram(f"❌ Command failed: {e}")
        else:
            send_telegram(f"Unknown command: {command}\nUse /help for available commands.")

    def stop(self):
        """Stop the listener."""
        self._running = False
        logger.info("Stopping Telegram listener")


def main():
    """Main entry point."""
    listener = TelegramListener()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        listener.stop()
        send_telegram("🛑 <b>Standalone Command Listener Stopped</b>")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    listener.start()


if __name__ == "__main__":
    main()
