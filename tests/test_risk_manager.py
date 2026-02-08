import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from agents.risk_manager import RiskManager
from models.schemas import OrderSide, OrderSignal, SignalType


def make_signal(pair="BTC/USDT", side=OrderSide.BUY, price=60000.0, amount=0.00017) -> OrderSignal:
    """Create a test signal. Default ~10 USDT value."""
    return OrderSignal(
        pair=pair,
        side=side,
        price=price,
        amount=amount,
        signal_type=SignalType.GRID_BUY if side == OrderSide.BUY else SignalType.GRID_SELL,
        timestamp=datetime.now(timezone.utc),
    )


class TestKillSwitch:
    def test_no_kill_switch_at_full_balance(self):
        rm = RiskManager(current_balance=1000.0)
        assert rm.check_kill_switch() is False

    def test_kill_switch_at_10pct_drawdown(self):
        rm = RiskManager(current_balance=900.0)
        assert rm.check_kill_switch() is True

    def test_kill_switch_at_large_drawdown(self):
        rm = RiskManager(current_balance=500.0)
        assert rm.check_kill_switch() is True

    def test_no_kill_switch_at_9pct(self):
        rm = RiskManager(current_balance=910.0)
        assert rm.check_kill_switch() is False

    def test_kill_switch_blocks_all_signals(self):
        rm = RiskManager(current_balance=850.0)
        signals = [make_signal() for _ in range(5)]
        approved = rm.validate_signals(signals)
        assert approved == []


class TestDailyLossLimit:
    @patch("agents.risk_manager.get_connection")
    def test_blocks_on_daily_loss(self, mock_conn):
        # Simulate -35 USDT daily P&L (exceeds 3% of 1000 = 30)
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            {"daily_pnl": -35.0},  # _get_daily_realized_pnl
        ]
        mock_conn.return_value.cursor.return_value = mock_cursor

        rm = RiskManager(current_balance=965.0)  # Under kill switch
        signals = [make_signal() for _ in range(3)]
        approved = rm.validate_signals(signals)
        assert approved == []


class TestMaxOpenOrders:
    @patch("agents.risk_manager.get_connection")
    def test_limits_open_orders(self, mock_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            {"daily_pnl": 0.0},     # _get_daily_realized_pnl
            {"cnt": 8},             # _get_open_order_count (8 already open)
            {"exposure": 0.0},      # _get_pair_exposure for signal 1
            {"exposure": 0.0},      # _get_pair_exposure for signal 2
        ]
        mock_conn.return_value.cursor.return_value = mock_cursor

        rm = RiskManager(current_balance=1000.0)
        signals = [make_signal() for _ in range(5)]
        approved = rm.validate_signals(signals)

        # Only 2 more allowed (max 10 - 8 existing)
        assert len(approved) == 2


class TestPositionLimit:
    @patch("agents.risk_manager.get_connection")
    def test_blocks_buy_exceeding_position_limit(self, mock_conn):
        mock_cursor = MagicMock()
        # Simulate 190 USDT already exposed (max = 200 @ 20% of 1000)
        mock_cursor.fetchone.side_effect = [
            {"daily_pnl": 0.0},
            {"cnt": 0},
            {"exposure": 190.0},  # Already near limit
        ]
        mock_conn.return_value.cursor.return_value = mock_cursor

        rm = RiskManager(current_balance=1000.0)
        # Signal worth ~10.2 USDT — would push over 200
        signal = make_signal(price=60000.0, amount=0.00017)
        approved = rm.validate_signals([signal])
        assert len(approved) == 0

    @patch("agents.risk_manager.get_connection")
    def test_allows_sell_regardless_of_exposure(self, mock_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            {"daily_pnl": 0.0},
            {"cnt": 0},
        ]
        mock_conn.return_value.cursor.return_value = mock_cursor

        rm = RiskManager(current_balance=1000.0)
        signal = make_signal(side=OrderSide.SELL)
        approved = rm.validate_signals([signal])
        # Sells don't check position limit
        assert len(approved) == 1


class TestNormalOperation:
    @patch("agents.risk_manager.get_connection")
    def test_approves_all_within_limits(self, mock_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            {"daily_pnl": 0.0},
            {"cnt": 0},
        ] + [{"exposure": 0.0}] * 5  # For each buy signal

        mock_conn.return_value.cursor.return_value = mock_cursor

        rm = RiskManager(current_balance=1000.0)
        signals = [make_signal() for _ in range(5)]
        approved = rm.validate_signals(signals)
        assert len(approved) == 5
