"""Health Check Scheduler — runs health monitoring every hour.

This runs as a separate process from the main trading bot.
Checks bot health every hour and sends alerts via Telegram.
"""

import logging
import sys
import time
from datetime import datetime

import ccxt
import pytz

from agents.health_monitor import HealthMonitor
from agents.notifier import send_telegram
from config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("health_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


def run_health_check():
    """Run health check and send report via Telegram."""
    try:
        logger.info("Starting health check...")

        # Initialize exchange connection
        exchange = ccxt.binanceusdm({
            "apiKey": settings.BINANCE_API_KEY,
            "secret": settings.BINANCE_API_SECRET,
            "timeout": 30000,  # 30 second timeout prevents indefinite hangs
            "options": {
                "defaultType": "future",
                "recvWindow": 60000,
            },
        })

        if settings.TESTNET:
            exchange.set_sandbox_mode(True)

        # Run health check
        monitor = HealthMonitor(exchange)
        results = monitor.check_health()

        # Format and send report
        report = monitor.format_health_report(results)

        # Only send Telegram alert if status is critical or warning
        # For healthy status, only send every 6 hours (to reduce spam)
        should_send = results["overall_status"] in ["critical", "warning"]

        if should_send:
            send_telegram(report)
            logger.info(f"Health check complete: {results['overall_status']} (alert sent)")
        else:
            logger.info(f"Health check complete: {results['overall_status']} (no alert needed)")

        # Log detailed results
        logger.info(f"Process running: {results['process_running']['running']}")
        logger.info(f"Recent activity: {results['recent_activity']['active']}")
        logger.info(f"Errors: {results['recent_errors']['error_count']}")
        logger.info(f"Database: {results['database_health']['accessible']}")
        logger.info(f"Exchange: {results['exchange_health']['connected']}")

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        try:
            send_telegram(f"🚨 **Health Check Failed**\n\nError: {str(e)}")
        except Exception as telegram_error:
            logger.error(f"Failed to send Telegram alert: {telegram_error}")


def send_daily_health_summary():
    """Send a comprehensive daily health summary (runs at 8 AM)."""
    try:
        logger.info("Sending daily health summary...")

        # Initialize exchange connection
        exchange = ccxt.binanceusdm({
            "apiKey": settings.BINANCE_API_KEY,
            "secret": settings.BINANCE_API_SECRET,
            "timeout": 30000,  # 30 second timeout prevents indefinite hangs
            "options": {
                "defaultType": "future",
                "recvWindow": 60000,
            },
        })

        if settings.TESTNET:
            exchange.set_sandbox_mode(True)

        # Run health check
        monitor = HealthMonitor(exchange)
        results = monitor.check_health()

        # Always send daily summary
        report = monitor.format_health_report(results)
        send_telegram(f"📊 **Daily Health Summary**\n\n{report}")

        logger.info("Daily health summary sent")

    except Exception as e:
        logger.error(f"Daily health summary failed: {e}")
        try:
            send_telegram(f"🚨 **Daily Health Summary Failed**\n\nError: {str(e)}")
        except Exception as telegram_error:
            logger.error(f"Failed to send Telegram alert: {telegram_error}")


def write_heartbeat():
    """Write heartbeat file with current timestamp - watchdog checks this."""
    try:
        import os
        heartbeat_file = os.path.join(os.path.dirname(__file__), "health_heartbeat.txt")
        with open(heartbeat_file, "w") as f:
            from datetime import timezone
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"Failed to write heartbeat: {e}")


def main():
    """Start the health check scheduler - simple while loop (no APScheduler)."""
    logger.info("Starting health check scheduler...")
    logger.info("Health checks will run every hour")
    logger.info("Daily summary will be sent at 8:00 AM PHT")

    # Track last run times
    from datetime import timedelta, timezone
    import pytz

    last_check_time = datetime.now(timezone.utc) - timedelta(hours=1)  # Trigger first check immediately
    last_daily_summary_time = datetime.now(timezone.utc) - timedelta(days=1)
    last_heartbeat_time = datetime.now(timezone.utc)

    # Run first health check immediately
    logger.info("Running initial health check...")
    run_health_check()
    write_heartbeat()

    logger.info("🔄 Simple scheduler running (no APScheduler - bulletproof)")

    # Main loop - simple and bulletproof
    try:
        while True:
            now = datetime.now(timezone.utc)

            # Write heartbeat every 5 minutes (watchdog checks this)
            if (now - last_heartbeat_time).total_seconds() >= 300:  # 5 minutes
                write_heartbeat()
                last_heartbeat_time = now

            # Health check every hour
            if (now - last_check_time).total_seconds() >= 3600:  # 1 hour
                logger.info("⏰ Running scheduled health check")
                run_health_check()
                last_check_time = now
                write_heartbeat()  # Also write heartbeat after each check

            # Daily summary at 8:00 AM PHT
            now_manila = datetime.now(pytz.timezone('Asia/Manila'))
            if now_manila.hour == 8 and now_manila.minute == 0:
                if (now - last_daily_summary_time).total_seconds() >= 3600:  # At least 1 hour since last
                    logger.info("⏰ Sending daily health summary")
                    send_daily_health_summary()
                    last_daily_summary_time = now

            # Sleep for 30 seconds before next check
            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("Health check scheduler stopped")
    except Exception as e:
        logger.error(f"Main loop error: {e}")
        send_telegram(f"⚠️ Health monitor crashed: {e}")


if __name__ == "__main__":
    main()
