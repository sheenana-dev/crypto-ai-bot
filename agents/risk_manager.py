"""Risk Manager Agent — validates order signals against risk rules.

- Max position size: 80% of capital per pair (10x leverage)
- Max open orders: 24 (4 pairs × 6 grids)
- Daily loss limit: 5% of starting capital
- Kill switch at 10% total drawdown
- Daily reset: 7 AM local time
- No per-position SL/TP — grid sells handle TP, kill switch handles risk
"""

import logging
from datetime import datetime, time
from typing import List, Optional

import ccxt

from config import settings
from database.db import get_connection
from models.schemas import OrderSignal, OrderSide

logger = logging.getLogger(__name__)


class RiskManager:
    """Validates order signals against portfolio risk rules before execution."""

    def __init__(self, current_balance: float = settings.TOTAL_CAPITAL, exchange: Optional[ccxt.Exchange] = None):
        self.starting_capital = settings.TOTAL_CAPITAL
        self.current_balance = current_balance
        self.exchange = exchange  # Need exchange for true realized P&L via income API
        self._ensure_daily_reset_table()
        self._check_and_reset_daily_balance()

    def validate_signals(self, signals: List[OrderSignal]) -> List[OrderSignal]:
        """Filter signals through risk rules. Returns only approved orders."""
        if self.check_kill_switch():
            logger.critical("KILL SWITCH ACTIVATED — drawdown exceeds limit. No orders allowed.")
            return []

        daily_pnl = self._get_daily_realized_pnl()
        daily_loss_limit = self.starting_capital * settings.DAILY_LOSS_LIMIT_PCT
        if daily_pnl <= -daily_loss_limit:
            logger.warning(f"Daily loss limit hit ({daily_pnl:.2f} USDT). No new orders.")
            return []

        open_order_count = self._get_open_order_count()
        approved = []

        for signal in signals:
            # Check max open orders
            if open_order_count + len(approved) >= settings.MAX_OPEN_ORDERS:
                logger.warning(f"Max open orders ({settings.MAX_OPEN_ORDERS}) reached, skipping remaining signals")
                break

            # Check position size limit (margin used by this order)
            signal_margin = signal.price * signal.amount / settings.LEVERAGE
            max_margin = self.starting_capital * settings.MAX_POSITION_PCT
            pair_margin = self._get_pair_exposure(signal.pair) / settings.LEVERAGE

            if signal.side == OrderSide.BUY:
                if pair_margin + signal_margin > max_margin:
                    logger.warning(
                        f"Position limit: {signal.pair} margin {pair_margin:.2f} + "
                        f"{signal_margin:.2f} > max {max_margin:.2f}, skipping"
                    )
                    continue

            approved.append(signal)

        logger.info(f"Risk check: {len(approved)}/{len(signals)} signals approved")
        return approved

    def check_kill_switch(self) -> bool:
        """Returns True if the kill switch should be activated (drawdown > threshold)."""
        drawdown = (self.starting_capital - self.current_balance) / self.starting_capital
        if drawdown >= settings.KILL_SWITCH_DRAWDOWN:
            logger.critical(
                f"Drawdown {drawdown*100:.1f}% >= {settings.KILL_SWITCH_DRAWDOWN*100:.1f}% kill threshold"
            )
            return True
        return False

    def _get_daily_realized_pnl(self) -> float:
        """Get today's TRUE realized P&L from Binance income API (excludes unrealized P&L).

        Daily reset happens at 7 AM local time. Uses Binance's income endpoint to fetch
        ONLY realized P&L (from closed trades) since the last 7 AM reset.

        This prevents unrealized P&L fluctuations from falsely triggering the daily loss limit.
        For example, if you have a -$20 unrealized loss on an open position, it won't count
        toward the daily limit until you actually close the position.

        Falls back to balance delta if exchange API is unavailable.
        """
        # If exchange is available, use income API for TRUE realized P&L
        if self.exchange:
            try:
                # Get last reset time (7 AM today or yesterday)
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT last_reset_time FROM daily_reset_state WHERE id = 1")
                row = cursor.fetchone()
                conn.close()

                if row:
                    last_reset_time = datetime.fromisoformat(row["last_reset_time"])
                    # Convert to milliseconds for Binance API
                    start_timestamp = int(last_reset_time.timestamp() * 1000)

                    # Fetch income records since last reset using Binance native API
                    # incomeType=REALIZED_PNL only includes closed positions (no unrealized)
                    # Using fapiPrivateGetIncome (Binance Futures native method)
                    response = self.exchange.fapiPrivateGetIncome({
                        'incomeType': 'REALIZED_PNL',
                        'startTime': start_timestamp,
                    })

                    # Binance returns a list of income records, each with 'income' field
                    # Sum up realized P&L from all records since last reset
                    income_records = response if isinstance(response, list) else []
                    daily_realized_pnl = sum(float(record.get('income', 0)) for record in income_records)
                    logger.debug(f"Daily realized P&L from Binance income API: ${daily_realized_pnl:.2f} ({len(income_records)} records)")
                    return daily_realized_pnl

            except Exception as e:
                logger.warning(f"Failed to fetch realized P&L from income API, falling back to balance delta: {e}")

        # Fallback: use balance delta (includes unrealized P&L, less accurate)
        daily_start_balance = self._get_daily_start_balance()
        daily_pnl = daily_start_balance - self.current_balance
        return -daily_pnl  # Negate so losses are negative, gains are positive

    def _ensure_daily_reset_table(self) -> None:
        """Create daily_reset_state table if it doesn't exist."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_reset_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    daily_start_balance REAL NOT NULL,
                    last_reset_time TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to create daily_reset_state table: {e}")

    def _check_and_reset_daily_balance(self) -> None:
        """Check if we need to reset daily balance at 7 AM, and do so if needed."""
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Get last reset time
            cursor.execute("SELECT daily_start_balance, last_reset_time FROM daily_reset_state WHERE id = 1")
            row = cursor.fetchone()

            now = datetime.now()
            reset_time_today = datetime.combine(now.date(), time(7, 0))  # 7 AM today

            # If no record exists, create initial record
            if row is None:
                cursor.execute("""
                    INSERT INTO daily_reset_state (id, daily_start_balance, last_reset_time)
                    VALUES (1, ?, ?)
                """, (self.current_balance, now.isoformat()))
                conn.commit()
                logger.info(f"Daily reset initialized: balance=${self.current_balance:.2f} at {now.strftime('%Y-%m-%d %H:%M')}")
            else:
                last_reset_time = datetime.fromisoformat(row["last_reset_time"])

                # Check if we've passed 7 AM since last reset
                # If current time >= 7 AM today AND last reset was before 7 AM today
                if now >= reset_time_today and last_reset_time < reset_time_today:
                    # Reset daily balance
                    cursor.execute("""
                        UPDATE daily_reset_state
                        SET daily_start_balance = ?, last_reset_time = ?
                        WHERE id = 1
                    """, (self.current_balance, now.isoformat()))
                    conn.commit()
                    logger.info(f"Daily reset at 7 AM: balance reset to ${self.current_balance:.2f}")

            conn.close()
        except Exception as e:
            logger.error(f"Failed to check/reset daily balance: {e}")

    def _get_daily_start_balance(self) -> float:
        """Get the balance at the start of the current day (7 AM reset)."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT daily_start_balance FROM daily_reset_state WHERE id = 1")
            row = cursor.fetchone()
            conn.close()

            if row:
                return float(row["daily_start_balance"])
            else:
                # No record yet, use current balance as fallback
                return self.current_balance
        except Exception:
            # Fallback to starting capital if query fails
            return self.starting_capital

    def _get_open_order_count(self) -> int:
        """Get count of currently open orders from the database."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM trades WHERE status IN ('PENDING', 'OPEN')")
            row = cursor.fetchone()
            conn.close()
            return int(row["cnt"])
        except Exception:
            return 0

    def _get_pair_exposure(self, pair: str) -> float:
        """Get total USDT exposure for a given pair from open buy orders."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(price * amount), 0) as exposure
                FROM trades
                WHERE pair = ? AND side = 'BUY' AND status IN ('PENDING', 'OPEN', 'PARTIALLY_FILLED')
            """, (pair,))
            row = cursor.fetchone()
            conn.close()
            return float(row["exposure"])
        except Exception:
            return 0.0
