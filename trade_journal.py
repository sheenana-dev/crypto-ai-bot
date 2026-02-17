#!/usr/bin/env python3
"""Trade Journal — Logs every fill to CSV with market context.

Polls Binance for new fills every 60 seconds and appends to trades_journal.csv.
Runs as a standalone background process alongside the bot.

Start: python3 trade_journal.py
"""

import csv
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

socket.setdefaulttimeout(20)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt
from config import settings
from agents.market_analyst import MarketAnalyst
from agents.notifier import send_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("trade_journal.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_journal.csv")
CSV_COLUMNS = [
    "date", "time", "trade_id", "pair", "side", "price", "amount",
    "fee", "realized_pnl", "regime", "adx", "rsi", "balance",
]
POLL_INTERVAL = 60  # seconds
SUMMARY_INTERVAL = 3600  # 1 hour


def create_exchange():
    exchange = ccxt.binanceusdm({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "future", "recvWindow": 60000},
    })
    if settings.TESTNET:
        exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def load_seen_trade_ids():
    """Load trade IDs already in CSV to avoid duplicates."""
    seen = set()
    if not os.path.exists(CSV_FILE):
        return seen
    try:
        with open(CSV_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                seen.add(row.get("trade_id", ""))
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
    return seen


def get_last_timestamp():
    """Get the latest timestamp from CSV, or default to today 00:00 UTC."""
    if not os.path.exists(CSV_FILE):
        return int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000)
    try:
        with open(CSV_FILE, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                last = rows[-1]
                dt = datetime.strptime(f"{last['date']} {last['time']}", "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
    except Exception as e:
        logger.error(f"Error reading last timestamp: {e}")
    return int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)


def ensure_csv_header():
    """Create CSV with header if it doesn't exist."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
        logger.info(f"Created {CSV_FILE}")


def fetch_realized_pnl(exchange, since_ms):
    """Fetch realized P&L events from Binance income API."""
    pnl_map = {}
    try:
        response = exchange.fapiPrivateGetIncome({
            "incomeType": "REALIZED_PNL",
            "startTime": since_ms,
        })
        for record in response:
            ts = int(record["time"])
            symbol = record["symbol"]
            income = float(record["income"])
            # Key by timestamp + symbol for matching
            key = f"{ts}_{symbol}"
            pnl_map[key] = pnl_map.get(key, 0) + income
    except Exception as e:
        logger.error(f"Failed to fetch realized PnL: {e}")
    return pnl_map


def get_market_context(analyst, pair):
    """Get current regime, ADX, RSI for a pair. Returns defaults on failure."""
    try:
        state = analyst.analyze(pair)
        return {
            "regime": state.regime.value,
            "adx": state.indicators.adx,
            "rsi": state.indicators.rsi,
        }
    except Exception as e:
        logger.error(f"Market context failed for {pair}: {e}")
        return {"regime": "UNKNOWN", "adx": 0, "rsi": 0}


def get_balance(exchange):
    """Get current wallet balance."""
    try:
        balance = exchange.fetch_balance()
        return float(balance.get("info", {}).get("totalWalletBalance", 0) or 0)
    except Exception:
        return 0


def poll_and_log(exchange, analyst, seen_ids, since_ms):
    """Poll for new fills and append to CSV. Returns updated since_ms."""
    new_trades = []

    for pair in settings.PAIRS:
        try:
            trades = exchange.fetch_my_trades(pair, since=since_ms)
            for t in trades:
                trade_id = str(t["id"])
                if trade_id in seen_ids:
                    continue
                new_trades.append(t)
                seen_ids.add(trade_id)
        except Exception as e:
            logger.error(f"Failed to fetch trades for {pair}: {e}")

    if not new_trades:
        return since_ms

    # Sort by timestamp
    new_trades.sort(key=lambda t: t["timestamp"])

    # Fetch realized PnL for matching
    pnl_map = fetch_realized_pnl(exchange, since_ms)

    # Get balance once
    balance = get_balance(exchange)

    # Get market context per unique pair (cache to avoid redundant API calls)
    context_cache = {}

    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        for t in new_trades:
            pair = t["symbol"]
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)

            # Get market context (cached per pair)
            if pair not in context_cache:
                context_cache[pair] = get_market_context(analyst, pair)
            ctx = context_cache[pair]

            # Match realized PnL by timestamp + symbol
            symbol_raw = pair.replace("/", "").replace(":USDT", "")
            pnl_key = f"{t['timestamp']}_{symbol_raw}"
            realized_pnl = pnl_map.get(pnl_key, 0)

            writer.writerow([
                ts.strftime("%Y-%m-%d"),
                ts.strftime("%H:%M:%S"),
                t["id"],
                pair,
                t["side"].upper(),
                t["price"],
                t["amount"],
                round(t["fee"]["cost"], 6),
                round(realized_pnl, 6),
                ctx["regime"],
                ctx["adx"],
                ctx["rsi"],
                round(balance, 2),
            ])

    latest_ts = max(t["timestamp"] for t in new_trades)
    logger.info(f"Logged {len(new_trades)} new trades to CSV")
    return latest_ts


def send_hourly_summary(exchange):
    """Send a 1-hour trade summary to Telegram."""
    if not os.path.exists(CSV_FILE):
        return

    try:
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        trades_count = 0
        total_fees = 0
        total_pnl = 0

        with open(CSV_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_dt = f"{row['date']} {row['time']}"
                if row_dt >= one_hour_ago:
                    trades_count += 1
                    total_fees += float(row.get("fee", 0))
                    total_pnl += float(row.get("realized_pnl", 0))

        if trades_count > 0:
            balance = get_balance(exchange)
            send_telegram(
                f"<b>Hourly Journal Summary</b>\n\n"
                f"Trades: {trades_count}\n"
                f"Realized PnL: <code>${total_pnl:.4f}</code>\n"
                f"Fees: <code>${total_fees:.4f}</code>\n"
                f"Net: <code>${total_pnl - total_fees:.4f}</code>\n"
                f"Balance: <code>${balance:.2f}</code>"
            )
    except Exception as e:
        logger.error(f"Hourly summary failed: {e}")


def main():
    logger.info("Starting trade journal...")

    exchange = create_exchange()
    analyst = MarketAnalyst(exchange)

    ensure_csv_header()
    seen_ids = load_seen_trade_ids()
    since_ms = get_last_timestamp()

    logger.info(f"Tracking {len(settings.PAIRS)} pairs, polling every {POLL_INTERVAL}s")
    logger.info(f"CSV: {CSV_FILE}")
    logger.info(f"Starting from: {datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat()}")

    # Backfill on startup
    since_ms = poll_and_log(exchange, analyst, seen_ids, since_ms)

    last_summary = time.time()

    def signal_handler(sig, frame):
        logger.info("Trade journal stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    send_telegram("📓 <b>Trade Journal Started</b>\n\nLogging all fills to CSV.")

    while True:
        try:
            since_ms = poll_and_log(exchange, analyst, seen_ids, since_ms)

            # Hourly summary
            if time.time() - last_summary >= SUMMARY_INTERVAL:
                send_hourly_summary(exchange)
                last_summary = time.time()

        except Exception as e:
            logger.error(f"Journal error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
