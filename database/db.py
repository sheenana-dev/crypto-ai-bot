import sqlite3
import os

from config import settings


def get_connection() -> sqlite3.Connection:
    """Get a SQLite database connection, creating the DB file if needed."""
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create database tables if they don't exist."""
    conn = get_connection()
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
        CREATE TABLE IF NOT EXISTS dca_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            entries INTEGER DEFAULT 0,
            total_qty REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            last_entry_price REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            started_at TEXT NOT NULL,
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
