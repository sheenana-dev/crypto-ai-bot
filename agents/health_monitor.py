"""Health Monitor — checks if trading bot is running properly and alerts on issues.

Checks every hour:
- Bot process is running
- Recent activity in logs
- No critical errors
- Database is accessible
- Exchange API is working
- Sends Telegram status report
"""

import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import ccxt

from config import settings
from database.db import get_connection

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors trading bot health and sends alerts via Telegram."""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange
        self.bot_dir = Path(__file__).parent.parent
        self.log_file = self.bot_dir / "bot.log"
        self.scheduler_process_name = "scheduler.py"

    def check_health(self) -> Dict[str, any]:
        """Run all health checks and return results."""
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "process_running": self._check_process_running(),
            "recent_activity": self._check_recent_activity(),
            "recent_errors": self._check_recent_errors(),
            "database_health": self._check_database_health(),
            "exchange_health": self._check_exchange_health(),
            "overall_status": "healthy",
        }

        # Determine overall status
        critical_issues = []
        warnings = []

        if not results["process_running"]["running"]:
            critical_issues.append("Bot process not running")

        if not results["recent_activity"]["active"]:
            critical_issues.append("No recent activity (>10 min)")

        if results["recent_errors"]["error_count"] > 0:
            warnings.append(f"{results['recent_errors']['error_count']} errors in last hour")

        if not results["database_health"]["accessible"]:
            critical_issues.append("Database not accessible")

        if not results["exchange_health"]["connected"]:
            critical_issues.append("Exchange API not responding")

        if critical_issues:
            results["overall_status"] = "critical"
            results["issues"] = critical_issues
        elif warnings:
            results["overall_status"] = "warning"
            results["warnings"] = warnings

        return results

    def _check_process_running(self) -> Dict[str, any]:
        """Check if the scheduler.py process is running."""
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = result.stdout.split("\n")
            bot_processes = [
                line for line in lines
                if self.scheduler_process_name in line and "grep" not in line
            ]

            if bot_processes:
                # Extract PID from first matching process
                pid = bot_processes[0].split()[1]
                return {
                    "running": True,
                    "pid": pid,
                    "process_count": len(bot_processes),
                }
            else:
                return {"running": False, "pid": None}

        except Exception as e:
            logger.error(f"Failed to check process: {e}")
            return {"running": False, "error": str(e)}

    def _check_recent_activity(self) -> Dict[str, any]:
        """Check if bot has logged activity in the last 10 minutes."""
        try:
            if not self.log_file.exists():
                return {"active": False, "reason": "Log file not found"}

            # Get last modified time
            mtime = datetime.fromtimestamp(self.log_file.stat().st_mtime, tz=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - mtime).total_seconds() / 60

            # Read last 50 lines to check for recent activity
            with open(self.log_file, "r") as f:
                lines = f.readlines()
                last_lines = lines[-50:] if len(lines) > 50 else lines

            # Look for recent timestamp in logs
            recent_activity = False
            last_log_time = None
            for line in reversed(last_lines):
                if "[INFO]" in line or "[WARNING]" in line or "[ERROR]" in line:
                    # Try to parse timestamp from log line (format: 2026-02-12 07:07:55,796)
                    try:
                        timestamp_str = line.split(" [")[0]
                        last_log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
                        last_log_time = last_log_time.replace(tzinfo=timezone.utc)
                        log_age_minutes = (datetime.now(timezone.utc) - last_log_time).total_seconds() / 60
                        if log_age_minutes < 10:
                            recent_activity = True
                        break
                    except Exception:
                        continue

            return {
                "active": recent_activity,
                "last_log_age_minutes": log_age_minutes if last_log_time else age_minutes,
                "log_file_age_minutes": age_minutes,
            }

        except Exception as e:
            logger.error(f"Failed to check recent activity: {e}")
            return {"active": False, "error": str(e)}

    def _check_recent_errors(self) -> Dict[str, any]:
        """Check for ERROR or CRITICAL messages in the last hour."""
        try:
            if not self.log_file.exists():
                return {"error_count": 0, "critical_count": 0}

            # Read last 500 lines (roughly 1 hour of logs)
            with open(self.log_file, "r") as f:
                lines = f.readlines()
                last_lines = lines[-500:] if len(lines) > 500 else lines

            errors = []
            criticals = []
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

            for line in last_lines:
                try:
                    # Parse timestamp
                    timestamp_str = line.split(" [")[0]
                    log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
                    log_time = log_time.replace(tzinfo=timezone.utc)

                    if log_time < one_hour_ago:
                        continue

                    if "[ERROR]" in line:
                        errors.append(line.strip())
                    elif "[CRITICAL]" in line:
                        criticals.append(line.strip())

                except Exception:
                    continue

            return {
                "error_count": len(errors),
                "critical_count": len(criticals),
                "recent_errors": errors[-5:] if errors else [],  # Last 5 errors
                "recent_criticals": criticals[-5:] if criticals else [],  # Last 5 criticals
            }

        except Exception as e:
            logger.error(f"Failed to check recent errors: {e}")
            return {"error_count": 0, "error": str(e)}

    def _check_database_health(self) -> Dict[str, any]:
        """Check if database is accessible and has recent data."""
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Check if we can query
            cursor.execute("SELECT COUNT(*) as cnt FROM trades")
            total_trades = cursor.fetchone()["cnt"]

            # Check for recent trades (last 24 hours)
            one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            cursor.execute("SELECT COUNT(*) as cnt FROM trades WHERE timestamp > ?", (one_day_ago,))
            recent_trades = cursor.fetchone()["cnt"]

            # Check open orders
            cursor.execute("SELECT COUNT(*) as cnt FROM trades WHERE status IN ('PENDING', 'OPEN')")
            open_orders = cursor.fetchone()["cnt"]

            conn.close()

            return {
                "accessible": True,
                "total_trades": total_trades,
                "recent_trades_24h": recent_trades,
                "open_orders": open_orders,
            }

        except Exception as e:
            logger.error(f"Failed to check database health: {e}")
            return {"accessible": False, "error": str(e)}

    def _check_exchange_health(self) -> Dict[str, any]:
        """Check if exchange API is responding."""
        try:
            # Try to fetch balance (proves API is working)
            balance = self.exchange.fetch_balance()
            usdt_balance = balance.get("USDT", {}).get("free", 0)

            # Try to fetch a ticker (proves market data is working)
            ticker = self.exchange.fetch_ticker("BTC/USDT:USDT")
            btc_price = ticker.get("last", 0)

            return {
                "connected": True,
                "balance_usdt": usdt_balance,
                "btc_price": btc_price,
            }

        except Exception as e:
            logger.error(f"Failed to check exchange health: {e}")
            return {"connected": False, "error": str(e)}

    def format_health_report(self, results: Dict[str, any]) -> str:
        """Format health check results as a readable message."""
        status_emoji = {
            "healthy": "✅",
            "warning": "⚠️",
            "critical": "🚨",
        }

        emoji = status_emoji.get(results["overall_status"], "❓")
        timestamp = datetime.fromisoformat(results["timestamp"]).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            f"{emoji} **Bot Health Check** — {timestamp}",
            f"**Status**: {results['overall_status'].upper()}",
            "",
        ]

        # Process
        if results["process_running"]["running"]:
            lines.append(f"✅ Bot Running (PID {results['process_running']['pid']})")
        else:
            lines.append("🚨 Bot NOT Running!")

        # Activity
        if results["recent_activity"]["active"]:
            age = results["recent_activity"]["last_log_age_minutes"]
            lines.append(f"✅ Recent Activity ({age:.1f} min ago)")
        else:
            age = results["recent_activity"].get("last_log_age_minutes", "unknown")
            lines.append(f"🚨 No Recent Activity (last: {age:.1f} min ago)")

        # Errors
        error_count = results["recent_errors"]["error_count"]
        critical_count = results["recent_errors"]["critical_count"]
        if critical_count > 0:
            lines.append(f"🚨 {critical_count} CRITICAL errors in last hour")
        elif error_count > 0:
            lines.append(f"⚠️ {error_count} errors in last hour")
        else:
            lines.append("✅ No errors in last hour")

        # Database
        if results["database_health"]["accessible"]:
            db = results["database_health"]
            lines.append(
                f"✅ Database OK ({db['total_trades']} trades, {db['open_orders']} open orders)"
            )
        else:
            lines.append("🚨 Database NOT accessible")

        # Exchange
        if results["exchange_health"]["connected"]:
            ex = results["exchange_health"]
            lines.append(f"✅ Exchange OK (Balance: ${ex['balance_usdt']:.2f}, BTC: ${ex['btc_price']:.2f})")
        else:
            lines.append("🚨 Exchange NOT responding")

        # Issues/Warnings
        if "issues" in results:
            lines.append("")
            lines.append("**Critical Issues:**")
            for issue in results["issues"]:
                lines.append(f"• {issue}")

        if "warnings" in results:
            lines.append("")
            lines.append("**Warnings:**")
            for warning in results["warnings"]:
                lines.append(f"• {warning}")

        # Recent errors
        if results["recent_errors"]["recent_criticals"]:
            lines.append("")
            lines.append("**Recent CRITICAL errors:**")
            for error in results["recent_errors"]["recent_criticals"]:
                lines.append(f"```{error[:200]}```")

        return "\n".join(lines)
