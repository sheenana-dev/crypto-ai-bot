import pytest
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

from agents.portfolio import PortfolioTracker
from database.db import init_db
from models.schemas import OrderSide, OrderStatus, SignalType, TradeLog


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database for testing."""
    path = str(tmp_path / "test_trades.db")
    # Patch settings.DB_PATH so init_db and get_connection use our temp DB
    with patch("database.db.settings") as mock_settings, \
         patch("agents.portfolio.get_connection") as mock_get_conn:
        mock_settings.DB_PATH = path

        # Create the real database at our temp path
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                filled REAL DEFAULT 0,
                fee REAL DEFAULT 0,
                status TEXT DEFAULT 'PENDING',
                signal_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_value_usdt REAL NOT NULL,
                available_balance REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                open_orders_count INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    yield path


def get_test_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def make_trade(
    order_id="test-001",
    pair="BTC/USDT",
    side=OrderSide.BUY,
    price=60000.0,
    amount=0.00017,
    filled=0.00017,
    fee=0.05,
    status=OrderStatus.FILLED,
) -> TradeLog:
    return TradeLog(
        order_id=order_id,
        pair=pair,
        side=side,
        price=price,
        amount=amount,
        filled=filled,
        fee=fee,
        status=status,
        signal_type=SignalType.GRID_BUY if side == OrderSide.BUY else SignalType.GRID_SELL,
        timestamp=datetime.now(timezone.utc),
    )


class TestRecordTrades:
    def test_records_single_trade(self, db_path):
        with patch("agents.portfolio.get_connection", return_value=get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)
            trade = make_trade(order_id="rec-001")
            tracker.record_trades([trade])

        conn = get_test_connection(db_path)
        row = conn.execute("SELECT * FROM trades WHERE order_id = 'rec-001'").fetchone()
        conn.close()

        assert row is not None
        assert row["pair"] == "BTC/USDT"
        assert row["side"] == "BUY"
        assert row["price"] == 60000.0

    def test_records_multiple_trades(self, db_path):
        with patch("agents.portfolio.get_connection", return_value=get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)
            trades = [make_trade(order_id=f"multi-{i}") for i in range(3)]
            tracker.record_trades(trades)

        conn = get_test_connection(db_path)
        count = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()["cnt"]
        conn.close()
        assert count == 3

    def test_upsert_updates_existing(self, db_path):
        with patch("agents.portfolio.get_connection", return_value=get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)

            # Insert initial
            trade = make_trade(order_id="upsert-001", filled=0.0, status=OrderStatus.OPEN)
            tracker.record_trades([trade])

        with patch("agents.portfolio.get_connection", return_value=get_test_connection(db_path)):
            # Update with fill
            updated = make_trade(order_id="upsert-001", filled=0.00017, status=OrderStatus.FILLED)
            tracker.record_trades([updated])

        conn = get_test_connection(db_path)
        row = conn.execute("SELECT * FROM trades WHERE order_id = 'upsert-001'").fetchone()
        conn.close()

        assert row["filled"] == 0.00017
        assert row["status"] == "FILLED"

    def test_empty_trades_does_nothing(self, db_path):
        tracker = PortfolioTracker(db_path)
        tracker.record_trades([])  # Should not raise


class TestGetSnapshot:
    def test_snapshot_on_empty_db(self, db_path):
        with patch("agents.portfolio.get_connection", side_effect=lambda: get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)
            snapshot = tracker.get_snapshot(current_balance=1000.0)

        assert snapshot.total_value_usdt == 1000.0
        assert snapshot.realized_pnl == 0.0
        assert snapshot.open_orders_count == 0

    def test_snapshot_counts_open_orders(self, db_path):
        # Insert an open order directly
        conn = get_test_connection(db_path)
        conn.execute("""
            INSERT INTO trades (order_id, pair, side, price, amount, filled, fee, status, signal_type, timestamp)
            VALUES ('snap-001', 'BTC/USDT', 'BUY', 60000, 0.00017, 0, 0, 'OPEN', 'GRID_BUY', '2025-01-01T00:00:00')
        """)
        conn.commit()
        conn.close()

        with patch("agents.portfolio.get_connection", side_effect=lambda: get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)
            snapshot = tracker.get_snapshot()

        assert snapshot.open_orders_count == 1


class TestGetTradeCount:
    def test_counts_trades(self, db_path):
        conn = get_test_connection(db_path)
        for i in range(5):
            conn.execute(f"""
                INSERT INTO trades (order_id, pair, side, price, amount, status, signal_type, timestamp)
                VALUES ('cnt-{i}', 'BTC/USDT', 'BUY', 60000, 0.00017, 'FILLED', 'GRID_BUY', '2025-01-01')
            """)
        conn.commit()
        conn.close()

        with patch("agents.portfolio.get_connection", return_value=get_test_connection(db_path)):
            tracker = PortfolioTracker(db_path)
            assert tracker.get_trade_count() == 5
