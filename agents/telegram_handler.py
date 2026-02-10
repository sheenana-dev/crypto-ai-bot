"""Telegram Command Handler — receives and processes commands via Telegram bot API.

Supports commands:
  /status    - Account overview (balance, open orders, pairs)
  /pnl       - Profit & loss breakdown
  /positions - All open positions with details
  /close_all - Close all open positions (emergency)
  /help      - List available commands

Uses getUpdates long polling in a background thread.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.parse

from config import settings
from agents.notifier import send_telegram

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    """Listens for Telegram commands and executes them."""

    def __init__(self, exchange_factory):
        """
        Args:
            exchange_factory: callable that returns a ccxt.Exchange instance
        """
        self.exchange_factory = exchange_factory
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.last_update_id = 0
        self._running = False
        self._thread = None

    def start(self):
        """Start the command listener in a background thread."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — command handler disabled")
            return

        # Flush old updates so we don't process stale commands
        self._flush_pending_updates()

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram command handler started")

    def stop(self):
        """Stop the command listener."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Telegram command handler stopped")

    def _flush_pending_updates(self):
        """Consume all pending updates so old commands aren't re-processed on restart."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset=-1&timeout=0"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                results = data.get("result", [])
                if results:
                    self.last_update_id = results[-1].get("update_id", 0)
                    logger.info(f"Flushed pending updates, last_id={self.last_update_id}")
        except Exception as e:
            logger.debug(f"Flush updates error: {e}")

    def _poll_loop(self):
        """Main polling loop — uses long polling (5s timeout)."""
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                time.sleep(5)

    def _get_updates(self):
        """Fetch new messages from Telegram using long polling."""
        params = urllib.parse.urlencode({
            "offset": self.last_update_id + 1,
            "timeout": 5,
            "allowed_updates": json.dumps(["message"]),
        })
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?{params}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
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

        # Only respond to our authorized chat
        if chat_id != self.chat_id:
            return

        if not text.startswith("/"):
            return

        command = text.split()[0].lower().split("@")[0]  # Handle /command@botname

        handlers = {
            "/status": self._cmd_status,
            "/pnl": self._cmd_pnl,
            "/positions": self._cmd_positions,
            "/close_all": self._cmd_close_all,
            "/help": self._cmd_help,
        }

        handler = handlers.get(command)
        if handler:
            logger.info(f"Processing Telegram command: {command}")
            try:
                handler()
            except Exception as e:
                logger.error(f"Command {command} error: {e}")
                self._reply(f"Command failed: {e}")
        else:
            self._reply(f"Unknown command: {command}\nUse /help to see available commands.")

    def _reply(self, message):
        """Send a reply message."""
        send_telegram(message)

    def _cmd_help(self):
        """List available commands."""
        self._reply(
            "<b>Available Commands</b>\n\n"
            "/status  -  Account overview\n"
            "/pnl  -  Profit &amp; loss breakdown\n"
            "/positions  -  Open positions\n"
            "/close_all  -  Close all positions\n"
            "/help  -  This message"
        )

    def _cmd_status(self):
        """Show account balance, open orders, running pairs."""
        exchange = self.exchange_factory()
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

        session_pnl = round(wallet - settings.TOTAL_CAPITAL, 2)
        pnl_emoji = "🟢" if session_pnl >= 0 else "🔴"

        self._reply(
            "<b>Account Status</b>\n\n"
            f"Wallet: <code>${wallet:,.2f}</code>\n"
            f"Available: <code>${available:,.2f}</code>\n"
            f"Margin Used: <code>${margin_used:,.2f}</code>\n"
            f"Unrealized PnL: <code>${unrealized:,.2f}</code>\n"
            f"{pnl_emoji} Session PnL: <code>${session_pnl:,.2f}</code>\n\n"
            f"Open Orders: {total_open}\n"
            f"Pairs: {len(settings.PAIRS)}\n"
            f"Leverage: {settings.LEVERAGE}x"
        )

    def _cmd_pnl(self):
        """Show PnL breakdown by pair."""
        exchange = self.exchange_factory()
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

        self._reply("\n".join(lines))

    def _cmd_positions(self):
        """Show detailed position info."""
        exchange = self.exchange_factory()
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

        self._reply("\n".join(lines))

    def _cmd_close_all(self):
        """Close all open positions."""
        exchange = self.exchange_factory()
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
            self._reply(f"<b>Closed All Positions</b>\n\n{report}")
        else:
            self._reply("No open positions to close")
