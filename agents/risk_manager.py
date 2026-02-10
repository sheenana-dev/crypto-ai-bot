"""Risk Manager Agent — validates order signals against risk rules.

- Max position size: 20% per pair
- Max open orders: 10
- Daily loss limit: 3% (30 USDT)
- Kill switch at 10% drawdown
"""

import logging
from typing import List

from config import settings
from database.db import get_connection
from models.schemas import OrderSignal, OrderSide

logger = logging.getLogger(__name__)


class RiskManager:
    """Validates order signals against portfolio risk rules before execution."""

    def __init__(self, current_balance: float = settings.TOTAL_CAPITAL):
        self.starting_capital = settings.TOTAL_CAPITAL
        self.current_balance = current_balance

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
        """Get today's realized P&L from the database."""
        try:
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
            row = cursor.fetchone()
            conn.close()
            return float(row["daily_pnl"])
        except Exception:
            return 0.0

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
