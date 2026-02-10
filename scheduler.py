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
from agents.pair_analyzer import PairAnalyzer
from agents.optimizer import OptimizerAgent
from agents.notifier import (
    send_telegram, format_cycle_report, format_daily_report,
    notify_kill_switch, notify_error,
)
from agents.telegram_handler import TelegramCommandHandler
from config.grid_config import GRID_PARAMS
from database.db import init_db, get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global kill switch
kill_switch_active = False

# Track last grid center price per pair — skip cancel/replace if price hasn't moved enough
last_grid_center = {}
GRID_REFRESH_THRESHOLD = 0.0005  # Only refresh grid if price moved >0.05% from center


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
            logger.error(f"Failed to fetch balance: {e}")
            send_telegram(f"⚠️ Balance fetch FAILED: {e}")
            usdt_balance = {"free": 0, "used": 0, "total": 0, "wallet_balance": 0, "realized_pnl": 0}

        # Fetch unrealized P&L from open positions + stop-loss check
        positions_pnl = {}
        pairs_closed_this_cycle = set()  # Skip grid refresh for pairs where TP/SL triggered
        try:
            positions = exchange.fetch_positions(settings.PAIRS)
            for pos in positions:
                amt = float(pos.get("contracts", 0) or 0)
                if amt > 0:
                    pair_key = pos.get("symbol", "")
                    entry_price = float(pos.get("entryPrice", 0) or 0)
                    mark_price = float(pos.get("markPrice", 0) or 0)
                    unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
                    side = pos.get("side", "")
                    notional = amt * entry_price

                    positions_pnl[pair_key] = {
                        "side": side,
                        "amount": amt,
                        "entry_price": entry_price,
                        "mark_price": mark_price,
                        "unrealized_pnl": unrealized_pnl,
                    }

                    # Stop-loss: close position if loss exceeds threshold
                    if notional > 0:
                        loss_pct = abs(unrealized_pnl) / notional
                        logger.info(
                            f"Position: {pair_key} {side} {amt} | entry={entry_price:.4f} mark={mark_price:.4f} | "
                            f"UPnL={unrealized_pnl:.2f} | loss_on_notional={loss_pct*100:.2f}% (threshold={settings.STOP_LOSS_PCT*100:.1f}%)"
                        )
                        if unrealized_pnl < 0 and loss_pct >= settings.STOP_LOSS_PCT:
                            close_side = "sell" if side == "long" else "buy"
                            logger.warning(
                                f"STOP LOSS TRIGGERED: Closing {side} {amt} {pair_key} "
                                f"(loss: {unrealized_pnl:.2f} USDT, {loss_pct*100:.2f}%)"
                            )
                            try:
                                exchange.create_order(
                                    symbol=pair_key, type="market",
                                    side=close_side, amount=amt,
                                    params={"reduceOnly": True},
                                )
                                logger.warning(
                                    f"STOP LOSS EXECUTED: Closed {side} {amt} {pair_key} at {mark_price:.2f} "
                                    f"(loss: {unrealized_pnl:.2f} USDT, {loss_pct*100:.2f}%)"
                                )
                                send_telegram(
                                    f"🛑 *STOP LOSS* triggered\n{pair_key} {side} closed\n"
                                    f"Loss: {unrealized_pnl:.2f} USDT ({loss_pct*100:.2f}%)"
                                )
                                pairs_closed_this_cycle.add(pair_key)
                            except Exception as e:
                                logger.error(f"Failed to execute stop loss for {pair_key}: {e}")
                                send_telegram(f"⚠️ STOP LOSS FAILED for {pair_key}: {e}")

                        # Take-profit: close position if profit exceeds grid spacing for this pair
                        if unrealized_pnl > 0:
                            pair_config = GRID_PARAMS.get(pair_key, {})
                            tp_threshold = pair_config.get("grid_spacing_pct", 0.005)
                            profit_pct = unrealized_pnl / notional
                            if profit_pct >= tp_threshold:
                                close_side = "sell" if side == "long" else "buy"
                                logger.info(
                                    f"TAKE PROFIT TRIGGERED: Closing {side} {amt} {pair_key} "
                                    f"(profit: {unrealized_pnl:.2f} USDT, {profit_pct*100:.2f}%)"
                                )
                                try:
                                    exchange.create_order(
                                        symbol=pair_key, type="market",
                                        side=close_side, amount=amt,
                                        params={"reduceOnly": True},
                                    )
                                    send_telegram(
                                        f"✅ <b>TAKE PROFIT</b>\n{pair_key} {side} closed\n"
                                        f"Profit: ${unrealized_pnl:.2f} ({profit_pct*100:.2f}% on notional)"
                                    )
                                    pairs_closed_this_cycle.add(pair_key)
                                except Exception as e:
                                    logger.error(f"Failed to execute take profit for {pair_key}: {e}")
                                    send_telegram(f"⚠️ TAKE PROFIT FAILED for {pair_key}: {e}")
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            send_telegram(f"⚠️ Position check FAILED: {e}")

        for pair in settings.PAIRS:
            if pair in pairs_closed_this_cycle:
                logger.info(f"{pair} | Skipping grid refresh — position was closed this cycle (TP/SL)")
                continue

            try:
                market_state = analyst.analyze(pair)

                # Smart grid refresh — only cancel/replace if price moved significantly
                current_price = market_state.current_price
                prev_center = last_grid_center.get(pair)
                if prev_center is not None:
                    price_move = abs(current_price - prev_center) / prev_center
                    if price_move < GRID_REFRESH_THRESHOLD:
                        logger.info(
                            f"{pair} | price moved {price_move*100:.3f}% < {GRID_REFRESH_THRESHOLD*100:.1f}% threshold, keeping existing grid"
                        )
                        continue

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

                signals = strategy.generate_signals(market_state)
                approved = risk_mgr.validate_signals(signals)
                trades = executor.execute_orders(approved)
                portfolio.record_trades(trades)
                snapshot = portfolio.get_snapshot()

                # Update grid center price
                last_grid_center[pair] = current_price

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

        # Send Telegram report every cycle
        send_telegram(format_cycle_report(results, usdt_balance))

        if kill_switch_active:
            notify_kill_switch(0)

    except Exception as e:
        logger.error(f"Cycle error: {traceback.format_exc()}")
        notify_error("SYSTEM", str(e))


def send_daily_report():
    """Send daily portfolio summary + optimization insights to Telegram."""
    try:
        # Portfolio snapshot
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

        # Send portfolio report
        send_telegram(format_daily_report(portfolio_data))

        # Add daily optimization insights
        optimizer = OptimizerAgent()
        daily_optimization = optimizer.generate_daily_report()
        send_telegram(daily_optimization)

        logger.info("Daily report + optimization insights sent")

    except Exception as e:
        logger.error(f"Daily report error: {e}")
        send_telegram(f"⚠️ Daily report failed: {e}")


def analyze_and_update_pairs():
    """Analyze market and update trading pairs if better opportunities found."""
    try:
        logger.info("Running periodic pair analysis...")
        exchange = create_exchange()
        analyzer = PairAnalyzer(exchange)

        # Get top 5 pairs by grid trading potential
        top_pairs = analyzer.analyze_candidates(top_n=5)

        # Format Telegram report
        report = "📊 *Pair Analysis Report*\n\n"
        report += "*Top 5 Grid Trading Opportunities:*\n"

        for i, pair_data in enumerate(top_pairs, 1):
            symbol = pair_data['symbol']
            vol = pair_data['volatility']
            volume = pair_data['volume'] / 1e6
            score = pair_data['score']

            report += f"{i}. `{symbol}`\n"
            report += f"   Vol: {vol:.2f}% | Vol24h: ${volume:.1f}M | Score: {score:.2f}\n"

        # Check if current pairs are still optimal
        current_symbols = set(settings.PAIRS)
        recommended_symbols = set([p['symbol'] for p in top_pairs])

        if current_symbols != recommended_symbols:
            report += f"\n⚠️ *Recommendation:* Switch pairs for better opportunities\n"
            report += f"Current: {', '.join([s.split('/')[0] for s in settings.PAIRS])}\n"
            report += f"Suggested: {', '.join([s.split('/')[0] for s in recommended_symbols])}\n"
        else:
            report += f"\n✅ Current pairs are optimal!\n"

        send_telegram(report)
        logger.info("Pair analysis complete")

    except Exception as e:
        logger.error(f"Pair analysis error: {e}")
        send_telegram(f"⚠️ Pair analysis failed: {str(e)}")


def send_weekly_optimization_report():
    """Generate and send weekly performance review with optimization recommendations."""
    try:
        logger.info("Generating weekly optimization report...")
        optimizer = OptimizerAgent()

        # Generate comprehensive report
        report = optimizer.generate_weekly_report()

        # Send to Telegram
        send_telegram(report)
        logger.info("Weekly optimization report sent")

    except Exception as e:
        logger.error(f"Weekly optimization report error: {e}")
        send_telegram(f"⚠️ Weekly report failed: {str(e)}")


def main():
    init_db()
    logger.info(f"Starting trading bot (testnet={settings.TESTNET})")
    logger.info(f"Pairs: {settings.PAIRS}")
    logger.info("Schedule: trading cycle every 3 min, daily report at 10:00 AM PHT, pair analysis every 6 hours, weekly optimization Sundays 10:30 AM PHT")

    send_telegram("🤖 *Trading bot started*\n\nTestnet: " + str(settings.TESTNET))

    # Start Telegram command listener (background thread)
    cmd_handler = TelegramCommandHandler(create_exchange)
    cmd_handler.start()

    # Run one cycle immediately
    run_trading_cycle()

    # Set up scheduled jobs
    scheduler = BlockingScheduler(timezone='Asia/Manila')
    scheduler.add_job(run_trading_cycle, "interval", minutes=3, id="trading_cycle")
    scheduler.add_job(send_daily_report, "cron", hour=10, minute=0, id="daily_report")
    scheduler.add_job(analyze_and_update_pairs, "interval", hours=6, id="pair_analysis")
    scheduler.add_job(send_weekly_optimization_report, "cron", day_of_week="sun", hour=10, minute=30, id="weekly_optimization")

    # Graceful shutdown on Ctrl+C
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        cmd_handler.stop()
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
