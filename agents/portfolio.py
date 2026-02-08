"""Portfolio Tracker Agent — tracks P&L and performance metrics.

- Records trades to SQLite
- Tracks realized and unrealized P&L per pair
- Provides portfolio snapshots
- Calculates daily P&L for risk management
"""

import logging
from datetime import datetime, timezone
from typing import List

from database.db import get_connection
from models.schemas import PortfolioSnapshot, TradeLog

logger = logging.getLogger(__name__)


class PortfolioTracker:
    """Tracks portfolio performance and maintains trade history."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def record_trades(self, trades: List[TradeLog]) -> None:
        """Save trades to the database. Updates existing orders by order_id."""
        if not trades:
            return

        conn = get_connection()
        cursor = conn.cursor()

        for trade in trades:
            cursor.execute("""
                INSERT INTO trades (order_id, pair, side, price, amount, filled, fee, status, signal_type, timestamp, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    filled = excluded.filled,
                    fee = excluded.fee,
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """, (
                trade.order_id,
                trade.pair,
                trade.side.value,
                trade.price,
                trade.amount,
                trade.filled,
                trade.fee,
                trade.status.value,
                trade.signal_type.value,
                trade.timestamp.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))

        conn.commit()
        conn.close()
        logger.info(f"Recorded {len(trades)} trades to database")

    def get_snapshot(self, current_balance: float = 0.0) -> PortfolioSnapshot:
        """Get current portfolio snapshot with P&L calculations."""
        conn = get_connection()
        cursor = conn.cursor()

        # Realized P&L: sum of completed sell trades minus their corresponding buys
        cursor.execute("""
            SELECT COALESCE(SUM(
                CASE WHEN side = 'SELL' THEN price * filled - fee
                     WHEN side = 'BUY' THEN -(price * filled + fee)
                END
            ), 0) as realized_pnl
            FROM trades WHERE status = 'FILLED'
        """)
        realized_pnl = float(cursor.fetchone()["realized_pnl"])

        # Open orders count
        cursor.execute("SELECT COUNT(*) as cnt FROM trades WHERE status IN ('PENDING', 'OPEN')")
        open_orders = int(cursor.fetchone()["cnt"])

        # Unrealized P&L (approximate: value of open positions)
        cursor.execute("""
            SELECT COALESCE(SUM(price * filled), 0) as open_value
            FROM trades
            WHERE status IN ('OPEN', 'PARTIALLY_FILLED') AND side = 'BUY'
        """)
        unrealized = float(cursor.fetchone()["open_value"])

        conn.close()

        snapshot = PortfolioSnapshot(
            total_value_usdt=current_balance + unrealized,
            available_balance=current_balance,
            unrealized_pnl=unrealized,
            realized_pnl=realized_pnl,
            open_orders_count=open_orders,
            timestamp=datetime.now(timezone.utc),
        )

        # Save snapshot
        self._save_snapshot(snapshot)
        return snapshot

    def get_daily_pnl(self) -> float:
        """Calculate today's realized P&L."""
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(
                CASE WHEN side = 'SELL' THEN price * filled - fee
                     WHEN side = 'BUY' THEN -(price * filled + fee)
                END
            ), 0) as daily_pnl
            FROM trades
            WHERE status = 'FILLED'
            AND date(timestamp) = date('now')
        """)
        pnl = float(cursor.fetchone()["daily_pnl"])
        conn.close()
        return pnl

    def get_trade_count(self) -> int:
        """Get total number of trades in the database."""
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM trades")
        count = int(cursor.fetchone()["cnt"])
        conn.close()
        return count

    def _save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Save a portfolio snapshot to the database."""
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO portfolio_snapshots (total_value_usdt, available_balance, unrealized_pnl, realized_pnl, open_orders_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            snapshot.total_value_usdt,
            snapshot.available_balance,
            snapshot.unrealized_pnl,
            snapshot.realized_pnl,
            snapshot.open_orders_count,
            snapshot.timestamp.isoformat(),
        ))
        conn.commit()
        conn.close()
