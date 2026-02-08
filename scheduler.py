"""Autonomous Trading Bot Scheduler

Runs the full trading pipeline on a schedule:
  - Trading cycle: every 5 minutes
  - Daily report:  every day at 8:00 AM

Start: python scheduler.py
Stop:  Ctrl+C

No external dependencies needed (no n8n, no cron).
"""

import json
import logging
import signal
import sys
import traceback

from apscheduler.schedulers.blocking import BlockingScheduler

import ccxt

from config import settings
from agents.market_analyst import MarketAnalyst
from agents.strategy import StrategyAgent
from agents.risk_manager import RiskManager
from agents.executor import ExecutionAgent
from agents.portfolio import PortfolioTracker
from agents.notifier import (
    send_telegram, format_cycle_report, format_daily_report,
    notify_kill_switch, notify_error,
)
from database.db import init_db, get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global kill switch
kill_switch_active = False


def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.binanceusdm({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
    })
    if settings.TESTNET:
        exchange.options["disableFuturesSandboxWarning"] = True
        exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def run_trading_cycle():
    """Run one full trading cycle — called every 5 minutes."""
    global kill_switch_active

    if kill_switch_active:
        logger.warning("Kill switch active — skipping cycle")
        return

    try:
        exchange = create_exchange()
        analyst = MarketAnalyst(exchange)
        strategy = StrategyAgent(exchange)
        risk_mgr = RiskManager()
        executor = ExecutionAgent(exchange)
        portfolio = PortfolioTracker(settings.DB_PATH)

        results = {}

        # Fetch account balance from exchange (real source of truth)
        try:
            balance = exchange.fetch_balance()
            info = balance.get("info", {})
            wallet_balance = float(info.get("totalWalletBalance", 0) or 0)
            usdt_balance = {
                "free": float(balance.get("USDT", {}).get("free", 0)),
                "used": float(balance.get("USDT", {}).get("used", 0)),
                "total": float(balance.get("USDT", {}).get("total", 0)),
                "wallet_balance": wallet_balance,
                "realized_pnl": round(wallet_balance - settings.TOTAL_CAPITAL, 2),
            }
        except Exception as e:
            logger.warning(f"Failed to fetch balance: {e}")
            usdt_balance = {"free": 0, "used": 0, "total": 0, "wallet_balance": 0, "realized_pnl": 0}

        # Fetch unrealized P&L from open positions
        positions_pnl = {}
        try:
            positions = exchange.fetch_positions(settings.PAIRS)
            for pos in positions:
                amt = float(pos.get("contracts", 0) or 0)
                if amt > 0:
                    pair_key = pos.get("symbol", "")
                    positions_pnl[pair_key] = {
                        "side": pos.get("side", ""),
                        "amount": amt,
                        "entry_price": float(pos.get("entryPrice", 0) or 0),
                        "mark_price": float(pos.get("markPrice", 0) or 0),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                    }
        except Exception as e:
            logger.warning(f"Failed to fetch positions: {e}")

        for pair in settings.PAIRS:
            try:
                # Cancel old grid orders on exchange + mark DB orders as cancelled
                cancelled = executor.cancel_all_open_orders(pair)
                if cancelled > 0:
                    logger.info(f"Cleared {cancelled} old orders for {pair}")
                conn = get_connection()
                conn.execute(
                    "UPDATE trades SET status = 'CANCELLED' WHERE status IN ('PENDING', 'OPEN') AND pair = ?",
                    (pair,),
                )
                conn.commit()
                conn.close()

                market_state = analyst.analyze(pair)
                signals = strategy.generate_signals(market_state)
                approved = risk_mgr.validate_signals(signals)
                trades = executor.execute_orders(approved)
                portfolio.record_trades(trades)
                snapshot = portfolio.get_snapshot()

                if risk_mgr.check_kill_switch():
                    kill_switch_active = True

                pos_info = positions_pnl.get(pair, {})

                results[pair] = {
                    "regime": market_state.regime.value,
                    "price": market_state.current_price,
                    "rsi": market_state.indicators.rsi,
                    "adx": market_state.indicators.adx,
                    "signals_generated": len(signals),
                    "signals_approved": len(approved),
                    "orders_executed": len(trades),
                    "open_orders": snapshot.open_orders_count,
                    "position_side": pos_info.get("side", ""),
                    "position_amount": pos_info.get("amount", 0),
                    "entry_price": pos_info.get("entry_price", 0),
                    "unrealized_pnl": pos_info.get("unrealized_pnl", 0),
                }

                logger.info(
                    f"{pair} | {market_state.regime.value} | "
                    f"signals: {len(signals)} → approved: {len(approved)} → executed: {len(trades)}"
                )

            except Exception as e:
                logger.error(f"Error processing {pair}: {e}")
                results[pair] = {"error": str(e)}
                notify_error(pair, str(e))

        # Send Telegram report
        send_telegram(format_cycle_report(results, usdt_balance))

        if kill_switch_active:
            notify_kill_switch(0)

    except Exception as e:
        logger.error(f"Cycle error: {traceback.format_exc()}")
        notify_error("SYSTEM", str(e))


def send_daily_report():
    """Send daily portfolio summary to Telegram — called once daily."""
    try:
        portfolio = PortfolioTracker(settings.DB_PATH)
        snapshot = portfolio.get_snapshot()
        daily_pnl = portfolio.get_daily_pnl()
        trade_count = portfolio.get_trade_count()

        portfolio_data = {
            "total_value_usdt": snapshot.total_value_usdt,
            "realized_pnl": snapshot.realized_pnl,
            "daily_pnl": daily_pnl,
            "open_orders": snapshot.open_orders_count,
            "total_trades": trade_count,
        }

        send_telegram(format_daily_report(portfolio_data))
        logger.info("Daily report sent to Telegram")

    except Exception as e:
        logger.error(f"Daily report error: {e}")


def main():
    init_db()
    logger.info(f"Starting trading bot (testnet={settings.TESTNET})")
    logger.info(f"Pairs: {settings.PAIRS}")
    logger.info("Schedule: trading cycle every 5 min, daily report at 8:00 AM")

    send_telegram("🤖 *Trading bot started*\n\nTestnet: " + str(settings.TESTNET))

    # Run one cycle immediately
    run_trading_cycle()

    # Set up scheduled jobs
    scheduler = BlockingScheduler()
    scheduler.add_job(run_trading_cycle, "interval", minutes=5, id="trading_cycle")
    scheduler.add_job(send_daily_report, "cron", hour=8, minute=0, id="daily_report")

    # Graceful shutdown on Ctrl+C
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        send_telegram("🛑 *Trading bot stopped*")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
