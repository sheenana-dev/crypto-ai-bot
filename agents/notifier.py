"""Telegram Notifier — sends alerts and reports via Telegram bot API.

Uses raw HTTP requests (no extra dependency) to send messages.
Uses HTML parse mode (more forgiving than Markdown with special characters).
"""

import logging
import urllib.request
import urllib.parse
import json

from config import settings

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    """Send a message via Telegram bot API. Returns True on success."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
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
            if result.get("ok"):
                logger.info("Telegram message sent")
                return True
            else:
                logger.error(f"Telegram API error: {result}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def format_cycle_report(results: dict, balance: dict = None) -> str:
    """Format a trading cycle result into a Telegram message."""
    lines = ["<b>Trading Cycle Report</b>\n"]

    # Balance section (from exchange — source of truth)
    if balance:
        wallet = balance.get("wallet_balance", 0)
        realized = balance.get("realized_pnl", 0)
        pnl_emoji = "🟢" if realized >= 0 else "🔴"
        lines.append("<b>Account Balance</b>")
        lines.append(f"  Wallet: <code>${wallet:,.2f}</code>")
        lines.append(f"  Available: <code>${balance.get('free', 0):,.2f}</code>")
        lines.append(f"  In Use: <code>${balance.get('used', 0):,.2f}</code>")
        lines.append(f"  {pnl_emoji} Session P&L: <code>${realized:,.2f}</code>")
        lines.append("")

    total_orders = 0
    for pair, data in results.items():
        if "error" in data:
            lines.append(f"<b>{pair}</b>: Error - {_escape(data['error'])}")
            continue

        regime = data.get("regime", "?")
        price = data.get("price", 0)
        rsi = data.get("rsi", 0)
        executed = data.get("orders_executed", 0)
        generated = data.get("signals_generated", 0)
        open_orders = data.get("open_orders", 0)
        total_orders += executed

        emoji = {"RANGING": "↔️", "TRENDING_UP": "📈", "TRENDING_DOWN": "📉", "CRASH": "🚨"}.get(regime, "❓")

        regime_flip = data.get("regime_flip", False)
        grid_kept = data.get("grid_kept", False)
        adx = data.get("adx", 0)
        tag = " [FLIP]" if regime_flip else (" [HELD]" if grid_kept else "")
        lines.append(f"<b>{pair}</b> {emoji} {regime}{tag}")
        lines.append(f"  Price: <code>${price:,.2f}</code> | RSI: <code>{rsi:.1f}</code> | ADX: <code>{adx:.1f}</code>")
        if not grid_kept:
            lines.append(f"  Orders placed: {executed}/{generated} | Open: {open_orders}")

        # Position info
        pos_side = data.get("position_side", "")
        pos_amount = data.get("position_amount", 0)
        entry_price = data.get("entry_price", 0)
        unrealized = data.get("unrealized_pnl", 0)

        if pos_side and pos_amount > 0:
            pnl_emoji = "🟢" if unrealized >= 0 else "🔴"
            lines.append(
                f"  Position: {pos_side.upper()} {pos_amount} @ <code>${entry_price:,.2f}</code>"
            )
            lines.append(f"  {pnl_emoji} Unrealized P&L: <code>${unrealized:,.2f}</code>")
        else:
            lines.append("  No open position")

    lines.append(f"\nTotal orders placed: <b>{total_orders}</b>")
    return "\n".join(lines)


def format_daily_report(portfolio: dict) -> str:
    """Format a daily portfolio summary into a Telegram message."""
    return (
        "<b>Daily Portfolio Summary</b>\n\n"
        f"Total value: <code>${portfolio.get('total_value_usdt', 0):,.2f}</code>\n"
        f"Realized P&L: <code>${portfolio.get('realized_pnl', 0):,.2f}</code>\n"
        f"Daily P&L: <code>${portfolio.get('daily_pnl', 0):,.2f}</code>\n"
        f"Open orders: {portfolio.get('open_orders', 0)}\n"
        f"Total trades: {portfolio.get('total_trades', 0)}"
    )


def notify_kill_switch(cancelled: int) -> None:
    """Send kill switch alert."""
    send_telegram(
        "🚨 <b>KILL SWITCH ACTIVATED</b>\n\n"
        f"All trading stopped. {cancelled} orders cancelled.\n"
        "Use the /kill endpoint with action=reset to resume."
    )


def notify_error(pair: str, error: str) -> None:
    """Send error alert."""
    send_telegram(f"⚠️ <b>Error on {pair}</b>\n\n<code>{_escape(error)}</code>")


def _escape(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
