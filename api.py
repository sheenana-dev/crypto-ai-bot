"""Flask API server for n8n integration.

Endpoints:
  POST /run          — Run one trading cycle (full pipeline) + Telegram alert
  GET  /status       — Get current portfolio snapshot
  POST /kill         — Activate kill switch (cancel all orders, stop trading)
  GET  /health       — Health check
  GET  /report       — Send daily portfolio report to Telegram

Start: python api.py
n8n calls these endpoints on a schedule.
"""

import logging
import traceback

from flask import Flask, jsonify, request

import ccxt

from config import settings
from agents.market_analyst import MarketAnalyst
from agents.strategy import StrategyAgent
from agents.risk_manager import RiskManager
from agents.executor import ExecutionAgent
from agents.portfolio import PortfolioTracker
from agents.notifier import (
    send_telegram, format_cycle_report, format_daily_report, notify_kill_switch, notify_error,
)
from database.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global kill switch state
_kill_switch_active = False


def create_exchange() -> ccxt.Exchange:
    """Initialize the Binance exchange connection."""
    exchange = ccxt.binance({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if settings.TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "testnet": settings.TESTNET, "kill_switch": _kill_switch_active})


@app.route("/run", methods=["POST"])
def run_cycle():
    """Run one trading cycle through the full pipeline."""
    global _kill_switch_active

    if _kill_switch_active:
        return jsonify({"status": "blocked", "reason": "Kill switch is active. POST /kill with action=reset to resume."}), 403

    try:
        init_db()
        exchange = create_exchange()

        analyst = MarketAnalyst(exchange)
        strategy = StrategyAgent(exchange)
        risk_mgr = RiskManager()
        executor = ExecutionAgent(exchange)
        portfolio = PortfolioTracker(settings.DB_PATH)

        results = {}

        for pair in settings.PAIRS:
            try:
                market_state = analyst.analyze(pair)
                signals = strategy.generate_signals(market_state)
                approved = risk_mgr.validate_signals(signals)
                trades = executor.execute_orders(approved)
                portfolio.record_trades(trades)
                snapshot = portfolio.get_snapshot()

                if risk_mgr.check_kill_switch():
                    _kill_switch_active = True

                results[pair] = {
                    "regime": market_state.regime.value,
                    "price": market_state.current_price,
                    "rsi": market_state.indicators.rsi,
                    "adx": market_state.indicators.adx,
                    "signals_generated": len(signals),
                    "signals_approved": len(approved),
                    "orders_executed": len(trades),
                    "open_orders": snapshot.open_orders_count,
                    "realized_pnl": snapshot.realized_pnl,
                }

                logger.info(
                    f"{pair} | {market_state.regime.value} | "
                    f"signals: {len(signals)} → approved: {len(approved)} → executed: {len(trades)}"
                )

            except Exception as e:
                logger.error(f"Error processing {pair}: {e}")
                results[pair] = {"error": str(e)}
                notify_error(pair, str(e))

        # Send Telegram alert for this cycle
        send_telegram(format_cycle_report(results))

        if _kill_switch_active:
            notify_kill_switch(0)

        return jsonify({
            "status": "ok",
            "kill_switch": _kill_switch_active,
            "results": results,
        })

    except Exception as e:
        logger.error(f"Pipeline error: {traceback.format_exc()}")
        notify_error("SYSTEM", str(e))
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/status", methods=["GET"])
def get_status():
    """Get current portfolio status and open orders."""
    try:
        init_db()
        portfolio = PortfolioTracker(settings.DB_PATH)
        snapshot = portfolio.get_snapshot()
        daily_pnl = portfolio.get_daily_pnl()
        trade_count = portfolio.get_trade_count()

        return jsonify({
            "status": "ok",
            "kill_switch": _kill_switch_active,
            "portfolio": {
                "total_value_usdt": snapshot.total_value_usdt,
                "realized_pnl": snapshot.realized_pnl,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "open_orders": snapshot.open_orders_count,
                "daily_pnl": daily_pnl,
                "total_trades": trade_count,
            },
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/report", methods=["GET"])
def daily_report():
    """Send daily portfolio report to Telegram."""
    try:
        init_db()
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

        message = format_daily_report(portfolio_data)
        sent = send_telegram(message)

        return jsonify({"status": "ok", "telegram_sent": sent, "portfolio": portfolio_data})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/kill", methods=["POST"])
def kill_switch():
    """Activate or reset the kill switch."""
    global _kill_switch_active

    action = request.json.get("action", "activate") if request.json else "activate"

    if action == "reset":
        _kill_switch_active = False
        logger.warning("Kill switch RESET — trading resumed")
        send_telegram("✅ *Kill switch reset* — trading resumed.")
        return jsonify({"status": "ok", "kill_switch": False, "message": "Kill switch reset. Trading resumed."})

    # Activate kill switch and cancel all open orders
    _kill_switch_active = True
    logger.critical("KILL SWITCH ACTIVATED — cancelling all open orders")

    cancelled = 0
    try:
        exchange = create_exchange()
        for pair in settings.PAIRS:
            open_orders = exchange.fetch_open_orders(pair)
            for order in open_orders:
                try:
                    exchange.cancel_order(order["id"], pair)
                    cancelled += 1
                except Exception as e:
                    logger.error(f"Failed to cancel order {order['id']}: {e}")
    except Exception as e:
        logger.error(f"Error during kill switch: {e}")

    notify_kill_switch(cancelled)

    return jsonify({
        "status": "ok",
        "kill_switch": True,
        "orders_cancelled": cancelled,
        "message": "Kill switch activated. All orders cancelled.",
    })


if __name__ == "__main__":
    logger.info(f"Starting API server (testnet={settings.TESTNET})")
    app.run(host="0.0.0.0", port=5001, debug=False)
