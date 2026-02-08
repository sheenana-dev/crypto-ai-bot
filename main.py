"""Crypto Trading Bot — Main Entry Point

Runs the agent pipeline:
  Market Analyst → Strategy → Risk Manager → Executor → Portfolio Tracker

Designed to be called by n8n on a schedule: python main.py
"""

import json
import logging

import ccxt

from config import settings
from agents.market_analyst import MarketAnalyst
from agents.strategy import StrategyAgent
from agents.risk_manager import RiskManager
from agents.executor import ExecutionAgent
from agents.portfolio import PortfolioTracker
from database.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_exchange() -> ccxt.Exchange:
    """Initialize Binance USDT-margined Futures connection."""
    exchange = ccxt.binanceusdm({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
    })

    if settings.TESTNET:
        exchange.options["disableFuturesSandboxWarning"] = True
        exchange.set_sandbox_mode(True)
        logger.info("Running in FUTURES TESTNET mode")

    exchange.load_markets()
    return exchange


def run() -> dict:
    """Run one cycle of the trading bot pipeline."""
    init_db()
    exchange = create_exchange()

    # Initialize agents
    analyst = MarketAnalyst(exchange)
    strategy = StrategyAgent(exchange)
    risk_mgr = RiskManager()
    executor = ExecutionAgent(exchange)
    portfolio = PortfolioTracker(settings.DB_PATH)

    results = {}

    for pair in settings.PAIRS:
        try:
            # 1. Analyze market
            market_state = analyst.analyze(pair)

            # 2. Generate signals
            signals = strategy.generate_signals(market_state)

            # 3. Risk check
            approved = risk_mgr.validate_signals(signals)

            # 4. Execute approved orders
            trades = executor.execute_orders(approved)

            # 5. Record trades
            portfolio.record_trades(trades)

            # 6. Snapshot
            snapshot = portfolio.get_snapshot()

            results[pair] = {
                "market_state": json.loads(market_state.model_dump_json()),
                "signals_generated": len(signals),
                "signals_approved": len(approved),
                "orders_executed": len(trades),
                "portfolio": json.loads(snapshot.model_dump_json()),
            }
            logger.info(
                f"{pair} | regime: {market_state.regime.value} | "
                f"signals: {len(signals)} → approved: {len(approved)} → executed: {len(trades)}"
            )

        except Exception as e:
            logger.error(f"Error processing {pair}: {e}")
            results[pair] = {"error": str(e)}

    return results


def main():
    results = run()
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
