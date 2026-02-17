"""Autonomous Trading Bot Scheduler

Runs the full trading pipeline on a schedule:
  - Trading cycle: every 1 minute
  - Daily report:  every day at 8:00 AM

Start: python scheduler.py
Stop:  Ctrl+C

No external dependencies needed (no n8n, no cron).
"""

import json
import logging
import os
import signal
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import ccxt
import pytz

from config import settings
from agents.market_analyst import MarketAnalyst
from agents.strategy import StrategyAgent
from agents.risk_manager import RiskManager
from agents.executor import ExecutionAgent
from agents.portfolio import PortfolioTracker
from agents.pair_analyzer import PairAnalyzer, load_active_pairs, save_active_pairs
from agents.notifier import (
    send_telegram, format_cycle_report, format_daily_report,
    notify_kill_switch, notify_error,
)
from config.grid_config import GRID_PARAMS
from database.db import init_db, get_connection
from models.schemas import MarketRegime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global kill switch
kill_switch_active = False

# Track last grid center price per pair — skip cancel/replace if price hasn't moved enough
last_grid_center = {}
# Refresh threshold = 1× grid spacing per pair (gives orders time to fill before repositioning)

# Track last regime per pair — detect RANGING→TRENDING flip to cancel stale grid orders
last_regime = {}


def _get_algo_symbol(exchange, pair: str) -> str:
    """Convert ccxt pair (BTC/USDT:USDT) to Binance symbol (BTCUSDT) for Algo API."""
    return exchange.market(pair)["id"]


def _fetch_algo_stops(exchange, pair: str) -> list:
    """Fetch open algo/conditional stop orders for a pair from Binance Algo API.

    Binance moved STOP_MARKET to the Algo Order API (error -4120 on regular endpoint).
    Regular fetch_open_orders does NOT return algo orders — must use this dedicated method.
    """
    symbol = _get_algo_symbol(exchange, pair)
    try:
        result = exchange.fapiPrivateGetOpenAlgoOrders({"symbol": symbol})
        # API returns a list directly (not wrapped in {"orders": [...]})
        orders = result if isinstance(result, list) else result.get("orders", [])
        return [o for o in orders if o.get("orderType") in ("STOP_MARKET", "STOP")]
    except Exception as e:
        logger.warning(f"Failed to fetch algo stops for {pair}: {e}")
        return []


def _cancel_algo_order(exchange, algo_id: str, pair: str) -> bool:
    """Cancel a single algo order by algoId."""
    try:
        exchange.fapiPrivateDeleteAlgoOrder({"algoId": str(algo_id)})
        return True
    except Exception as e:
        logger.warning(f"Failed to cancel algo order {algo_id} for {pair}: {e}")
        return False


def manage_emergency_stops(exchange, positions_pnl, active_pairs):
    """Place/update exchange-side emergency stop losses for all open positions.

    Uses Binance Algo Order API (STOP_MARKET moved from regular order endpoint).
    These conditional orders live on Binance and trigger even if the bot is down.
    Placed at EMERGENCY_STOP_PCT (3%) from entry — wide enough to not interfere
    with normal grid trading, tight enough to prevent catastrophic loss on a crash.

    Uses MARK_PRICE trigger to avoid false triggers from single-exchange wicks.
    Uses reduceOnly to ensure stops can only close positions (never open new ones).
    """
    stop_pct = settings.EMERGENCY_STOP_PCT

    for pair in active_pairs:
        try:
            pos = positions_pnl.get(pair)

            if pos and pos.get("amount", 0) > 0:
                # Position exists — ensure emergency stop is in place
                side = pos["side"]
                entry = pos["entry_price"]
                amount = pos["amount"]

                if side == "long":
                    target_stop = entry * (1 - stop_pct)
                    stop_side = "sell"
                else:  # short
                    target_stop = entry * (1 + stop_pct)
                    stop_side = "buy"

                target_stop = float(exchange.price_to_precision(pair, target_stop))
                amount = float(exchange.amount_to_precision(pair, amount))

                # Check existing algo stops via Algo Order API
                existing_stops = _fetch_algo_stops(exchange, pair)

                # Check if existing stop is close enough to target (within 0.5%)
                has_valid_stop = False
                for stop in existing_stops:
                    trigger_price = float(stop.get("triggerPrice", 0) or 0)
                    if trigger_price > 0 and abs(trigger_price - target_stop) / target_stop < 0.005:
                        has_valid_stop = True
                        break

                if has_valid_stop:
                    continue  # Stop is already in place and valid

                # Cancel stale stops (entry price changed or duplicates)
                for stop in existing_stops:
                    _cancel_algo_order(exchange, stop["algoId"], pair)
                    logger.info(f"Cancelled stale algo stop {stop['algoId']} for {pair}")

                # Place new emergency stop via Algo Order API (stopLossPrice param)
                order = exchange.create_order(
                    symbol=pair,
                    type="market",
                    side=stop_side,
                    amount=amount,
                    params={
                        "stopLossPrice": target_stop,
                        "reduceOnly": True,
                        "workingType": "MARK_PRICE",
                    },
                )
                logger.info(
                    f"Emergency stop placed: {pair} {stop_side.upper()} {amount} "
                    f"@ stop=${target_stop:.4f} ({stop_pct*100:.0f}% from entry ${entry:.4f})"
                )
                send_telegram(
                    f"🛡️ <b>Emergency Stop</b>: {pair.split('/')[0]}\n"
                    f"{stop_side.upper()} {amount} @ ${target_stop:.4f}\n"
                    f"({stop_pct*100:.0f}% from entry ${entry:.4f})"
                )

            else:
                # No position — cancel any orphaned algo stop orders
                orphaned = _fetch_algo_stops(exchange, pair)
                for stop in orphaned:
                    _cancel_algo_order(exchange, stop["algoId"], pair)
                    logger.info(f"Cancelled orphaned algo stop {stop['algoId']} for {pair}")

        except Exception as e:
            logger.error(f"Emergency stop management failed for {pair}: {e}")
            send_telegram(f"⚠️ Emergency stop FAILED for {pair}: {e}")


def create_exchange() -> ccxt.Exchange:
    # Set socket-level timeout to prevent TCP hangs (more aggressive than application timeout)
    socket.setdefaulttimeout(20)  # 20 second socket timeout

    exchange = ccxt.binanceusdm({
        "apiKey": settings.BINANCE_API_KEY,
        "secret": settings.BINANCE_API_SECRET,
        "enableRateLimit": True,
        "timeout": 30000,  # 30 second application timeout
        "options": {
            "defaultType": "future",
            "recvWindow": 60000,  # 60 second receive window
        },
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

        # Load active pairs from runtime state (can be updated by auto-rotation)
        active_pairs = load_active_pairs(default_pairs=settings.PAIRS)

        # Fetch account balance from exchange FIRST (needed for risk manager)
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
            wallet_balance = settings.TOTAL_CAPITAL  # Fallback to starting capital
            usdt_balance = {"free": 0, "used": 0, "total": 0, "wallet_balance": 0, "realized_pnl": 0}

        # Initialize agents (pass exchange + balance to risk manager for true realized P&L tracking)
        analyst = MarketAnalyst(exchange)
        strategy = StrategyAgent(exchange)
        risk_mgr = RiskManager(current_balance=wallet_balance, exchange=exchange)
        executor = ExecutionAgent(exchange)
        portfolio = PortfolioTracker(settings.DB_PATH)

        results = {}

        # Fetch unrealized P&L from open positions + stop-loss check
        positions_pnl = {}
        try:
            positions = exchange.fetch_positions(active_pairs)
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

                    # Log position info — no per-position SL/TP
                    # Grid sells handle take-profit, kill switch + daily limit handle risk
                    if notional > 0:
                        loss_pct = abs(unrealized_pnl) / notional if unrealized_pnl < 0 else 0
                        profit_pct = unrealized_pnl / notional if unrealized_pnl > 0 else 0
                        logger.info(
                            f"Position: {pair_key} {side} {amt} | entry={entry_price:.4f} mark={mark_price:.4f} | "
                            f"UPnL={unrealized_pnl:.2f} | {'profit' if unrealized_pnl >= 0 else 'loss'}={max(loss_pct, profit_pct)*100:.2f}%"
                        )
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            send_telegram(f"⚠️ Position check FAILED: {e}")

        # Manage exchange-side emergency stop losses (survive bot crashes)
        try:
            manage_emergency_stops(exchange, positions_pnl, active_pairs)
        except Exception as e:
            logger.error(f"Emergency stop management error: {e}")
            send_telegram(f"⚠️ Emergency stop management FAILED: {e}")

        for pair in active_pairs:
            try:
                market_state = analyst.analyze(pair)

                # REGIME FLIP DETECTION: When market turns TRENDING, cancel stale
                # grid orders immediately. Without this, grid orders placed during
                # RANGING stay on the book and fill sequentially as price crashes,
                # causing massive position accumulation (e.g. -$8 ETH loss on Feb 15).
                current_price = market_state.current_price
                current_regime = market_state.regime
                prev_regime = last_regime.get(pair)
                is_trending = current_regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN)
                regime_flipped_to_trending = (
                    prev_regime == MarketRegime.RANGING and is_trending
                )
                last_regime[pair] = current_regime

                if regime_flipped_to_trending:
                    logger.warning(
                        f"{pair} REGIME FLIP: RANGING → {current_regime.value} — "
                        f"forcing cancel of stale grid orders"
                    )

                # Smart grid refresh — only cancel/replace if price moved 1× grid spacing
                # Orders stay in place until price moves enough to fill them, then reposition
                # EXCEPTION: On regime flip (RANGING→TRENDING), always cancel stale grid orders
                # During continued TRENDING, close-only orders still respect the refresh threshold
                prev_center = last_grid_center.get(pair)
                # Hybrid BB+ADX refresh threshold — matches strategy.py's spacing formula
                # BB measures actual range, ADX multiplier adds safety buffer for forming trends
                bb_upper = market_state.indicators.bb_upper
                bb_lower = market_state.indicators.bb_lower
                num_grids = GRID_PARAMS.get(pair, {}).get("num_grids", 6)
                bb_width_pct = (bb_upper - bb_lower) / current_price if current_price > 0 else 0.01
                adx = market_state.indicators.adx
                adx_multiplier = min(1.5, max(1.0, 1.0 + (adx - 15) * 0.02)) if adx > 15 else 1.0
                pair_spacing = max(0.004, min(0.02, (bb_width_pct / num_grids) * adx_multiplier))
                if prev_center is not None and not regime_flipped_to_trending:
                    price_move = abs(current_price - prev_center) / prev_center
                    if price_move < pair_spacing:
                        logger.info(
                            f"{pair} | price moved {price_move*100:.3f}% < {pair_spacing*100:.1f}% threshold, keeping existing grid"
                        )
                        # Still report pair data even when grid is kept in place
                        pos_info = positions_pnl.get(pair, {})
                        results[pair] = {
                            "regime": market_state.regime.value,
                            "price": market_state.current_price,
                            "rsi": market_state.indicators.rsi,
                            "adx": market_state.indicators.adx,
                            "signals_generated": 0,
                            "signals_approved": 0,
                            "orders_executed": 0,
                            "open_orders": 0,
                            "position_side": pos_info.get("side", ""),
                            "position_amount": pos_info.get("amount", 0),
                            "entry_price": pos_info.get("entry_price", 0),
                            "unrealized_pnl": pos_info.get("unrealized_pnl", 0),
                            "regime_flip": False,
                            "grid_kept": True,
                        }
                        continue

                # Clear grid center on regime flip so fresh orders are placed immediately
                if regime_flipped_to_trending:
                    last_grid_center.pop(pair, None)

                # Generate new signals FIRST (needed for selective cancel comparison)
                signals = strategy.generate_signals(market_state)
                approved = risk_mgr.validate_signals(signals)

                kept = 0  # Track kept orders for grid center update
                if regime_flipped_to_trending:
                    # On regime flip, cancel ALL — stale grid orders are dangerous
                    cancelled = executor.cancel_all_open_orders(pair)
                    trades = executor.execute_orders(approved)
                else:
                    # SELECTIVE CANCEL: only cancel orders outside new grid range
                    # Keeps orders that are still at valid price levels (preserves near-fills)
                    kept, cancelled, trades = executor.selective_refresh(
                        pair, approved, pair_spacing
                    )
                    if kept > 0:
                        logger.info(f"{pair} kept {kept} existing orders (near-fill preservation)")

                # Mark DB orders as cancelled + record newly placed ones
                conn = get_connection()
                conn.execute(
                    "UPDATE trades SET status = 'CANCELLED' WHERE status IN ('PENDING', 'OPEN') AND pair = ?",
                    (pair,),
                )
                conn.commit()
                conn.close()

                portfolio.record_trades(trades)
                snapshot = portfolio.get_snapshot()

                # Update grid center price — only if orders were placed or kept
                # Prevents locking out future orders when signals were 0 (e.g. TRENDING no position)
                if len(trades) > 0 or kept > 0:
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
                    "regime_flip": regime_flipped_to_trending,
                }

                logger.info(
                    f"{pair} | {market_state.regime.value} | "
                    f"signals: {len(signals)} → approved: {len(approved)} → executed: {len(trades)}"
                )

                # Write heartbeat after each pair to prevent watchdog false positives
                # (a full 4-pair cycle with slow API calls can take 3+ minutes)
                write_heartbeat()

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

        logger.info("Daily report sent")

    except Exception as e:
        logger.error(f"Daily report error: {e}")
        send_telegram(f"⚠️ Daily report failed: {e}")


def analyze_and_update_pairs():
    """Analyze market and AUTO-ROTATE trading pairs if better opportunities found."""
    try:
        logger.info("Running periodic pair analysis with auto-rotation...")
        exchange = create_exchange()
        analyzer = PairAnalyzer(exchange)

        # Load current active pairs from runtime state
        current_pairs = load_active_pairs(default_pairs=settings.PAIRS)

        # Perform auto-rotation (drops worst, adds best new candidate if score diff > threshold)
        new_pairs, rotation_info = analyzer.auto_rotate_pairs(current_pairs, max_pairs=4)

        # Save updated pairs if rotation happened
        if rotation_info["rotated"]:
            save_active_pairs(new_pairs)
            logger.info(f"Pairs rotated: {current_pairs} → {new_pairs}")

        # Get top 5 pairs for report
        top_pairs = analyzer.analyze_candidates(top_n=5)

        # Format Telegram report
        report = "📊 **Pair Analysis Report**\n\n"
        report += "**Top 5 Grid Trading Opportunities:**\n"

        for i, pair_data in enumerate(top_pairs, 1):
            symbol = pair_data['symbol']
            vol = pair_data['volatility']
            volume = pair_data['volume'] / 1e6
            score = pair_data['score']
            is_active = symbol in new_pairs

            status = "✅ ACTIVE" if is_active else ""
            report += f"{i}. `{symbol}` {status}\n"
            report += f"   Vol: {vol:.2f}% | Vol24h: ${volume:.1f}M | Score: {score:.2f}\n"

        # Add rotation info
        if rotation_info["rotated"]:
            report += f"\n🔄 **AUTO-ROTATION PERFORMED**\n"
            report += f"Removed: `{rotation_info['removed']}` (score {rotation_info['removed_score']:.2f})\n"
            report += f"Added: `{rotation_info['added']}` (score {rotation_info['added_score']:.2f})\n"
            report += f"Reason: {rotation_info['reason']}\n"
        else:
            report += f"\n✅ No rotation needed — current pairs performing well\n"

        report += f"\n**Active Pairs:** {', '.join([s.split('/')[0] for s in new_pairs])}\n"

        send_telegram(report)
        logger.info("Pair analysis complete")

    except Exception as e:
        logger.error(f"Pair analysis error: {e}")
        send_telegram(f"⚠️ Pair analysis failed: {str(e)}")


def write_heartbeat():
    """Write heartbeat file with current timestamp - watchdog checks this."""
    try:
        heartbeat_file = os.path.join(os.path.dirname(__file__), "bot_heartbeat.txt")
        with open(heartbeat_file, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"Failed to write heartbeat: {e}")


def main():
    init_db()

    # Load active pairs from runtime state (auto-rotation updates this file)
    active_pairs = load_active_pairs(default_pairs=settings.PAIRS)

    logger.info(f"Starting trading bot (testnet={settings.TESTNET})")
    logger.info(f"Active pairs: {active_pairs} (loaded from active_pairs.json)")
    logger.info("Schedule: trading cycle every 1 min, daily report at 10:00 AM PHT, pair analysis every 6 hours")

    send_telegram(f"🤖 **Trading bot started**\n\nTestnet: {settings.TESTNET}\nActive pairs: {', '.join([p.split('/')[0] for p in active_pairs])}")

    # Track last run times
    last_cycle_time = datetime.now(timezone.utc)
    last_daily_report_time = datetime.now(timezone.utc) - timedelta(days=1)
    last_pair_analysis_time = datetime.now(timezone.utc) - timedelta(hours=6)
    last_heartbeat_time = datetime.now(timezone.utc)

    # Run one cycle immediately
    run_trading_cycle()
    write_heartbeat()

    # Graceful shutdown on Ctrl+C
    shutdown_flag = False

    def shutdown(signum, frame):
        nonlocal shutdown_flag
        logger.info("Shutting down...")
        send_telegram("🛑 *Trading bot stopped*")
        shutdown_flag = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # Ignore SIGHUP — survives parent shell exit

    logger.info("🔄 Simple scheduler running (no APScheduler - bulletproof)")

    # Main loop - simple and bulletproof
    while not shutdown_flag:
        try:
            now = datetime.now(timezone.utc)

            # Write heartbeat every 5 minutes (watchdog checks this)
            if (now - last_heartbeat_time).total_seconds() >= 300:  # 5 minutes
                write_heartbeat()
                last_heartbeat_time = now

            # Trading cycle every 1 minute — faster = more responsive grid (dynamic-style)
            if (now - last_cycle_time).total_seconds() >= 60:  # 1 minute
                logger.info("⏰ Running scheduled trading cycle")
                run_trading_cycle()
                last_cycle_time = datetime.now(timezone.utc)  # Fresh timestamp AFTER cycle, not stale 'now'
                write_heartbeat()  # Also write heartbeat after each cycle

            # Daily report at 10:00 AM PHT (02:00 UTC)
            now_manila = datetime.now(pytz.timezone('Asia/Manila'))
            if now_manila.hour == 10 and now_manila.minute == 0:
                if (now - last_daily_report_time).total_seconds() >= 3600:  # At least 1 hour since last
                    logger.info("⏰ Running daily report")
                    send_daily_report()
                    last_daily_report_time = now

            # Pair analysis every 6 hours
            if (now - last_pair_analysis_time).total_seconds() >= 21600:  # 6 hours
                logger.info("⏰ Running pair analysis")
                analyze_and_update_pairs()
                last_pair_analysis_time = now

            # Sleep for 30 seconds before next check
            time.sleep(30)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            send_telegram(f"⚠️ Main loop error: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

    logger.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
