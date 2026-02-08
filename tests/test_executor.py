import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import ccxt

from agents.executor import ExecutionAgent
from models.schemas import OrderSide, OrderSignal, OrderStatus, SignalType


def make_signal(pair="BTC/USDT", side=OrderSide.BUY, price=59700.0, amount=0.00017) -> OrderSignal:
    return OrderSignal(
        pair=pair,
        side=side,
        price=price,
        amount=amount,
        signal_type=SignalType.GRID_BUY if side == OrderSide.BUY else SignalType.GRID_SELL,
        timestamp=datetime.now(timezone.utc),
    )


def make_order_response(order_id="123", status="open", filled=0.0, fee_cost=0.0):
    return {
        "id": order_id,
        "status": status,
        "filled": filled,
        "fee": {"cost": fee_cost, "currency": "USDT"},
    }


class TestExecuteOrders:
    def test_places_order_successfully(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.return_value = make_order_response("abc123")

        executor = ExecutionAgent(mock_exchange)
        signals = [make_signal()]
        trades = executor.execute_orders(signals)

        assert len(trades) == 1
        assert trades[0].order_id == "abc123"
        assert trades[0].status == OrderStatus.OPEN
        mock_exchange.create_order.assert_called_once()

    def test_order_call_params(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.return_value = make_order_response()

        executor = ExecutionAgent(mock_exchange)
        signal = make_signal(pair="ETH/USDT", side=OrderSide.SELL, price=3500.0, amount=0.01)
        executor.execute_orders([signal])

        mock_exchange.create_order.assert_called_once_with(
            symbol="ETH/USDT",
            type="limit",
            side="sell",
            amount=0.01,
            price=3500.0,
        )

    def test_multiple_orders(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.side_effect = [
            make_order_response("order1"),
            make_order_response("order2"),
            make_order_response("order3"),
        ]

        executor = ExecutionAgent(mock_exchange)
        signals = [make_signal() for _ in range(3)]
        trades = executor.execute_orders(signals)

        assert len(trades) == 3
        assert [t.order_id for t in trades] == ["order1", "order2", "order3"]


class TestRetryLogic:
    def test_retries_on_network_error(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.side_effect = [
            ccxt.NetworkError("timeout"),
            ccxt.NetworkError("timeout"),
            make_order_response("retry_success"),
        ]

        executor = ExecutionAgent(mock_exchange)
        trades = executor.execute_orders([make_signal()])

        assert len(trades) == 1
        assert trades[0].order_id == "retry_success"
        assert mock_exchange.create_order.call_count == 3

    def test_gives_up_after_max_retries(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.side_effect = ccxt.NetworkError("persistent failure")

        executor = ExecutionAgent(mock_exchange)
        trades = executor.execute_orders([make_signal()])

        assert len(trades) == 0
        assert mock_exchange.create_order.call_count == 3

    def test_no_retry_on_insufficient_funds(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.side_effect = ccxt.InsufficientFunds("no money")

        executor = ExecutionAgent(mock_exchange)
        trades = executor.execute_orders([make_signal()])

        assert len(trades) == 0
        assert mock_exchange.create_order.call_count == 1

    def test_no_retry_on_invalid_order(self):
        mock_exchange = MagicMock()
        mock_exchange.create_order.side_effect = ccxt.InvalidOrder("bad amount")

        executor = ExecutionAgent(mock_exchange)
        trades = executor.execute_orders([make_signal()])

        assert len(trades) == 0
        assert mock_exchange.create_order.call_count == 1


class TestStatusMapping:
    def test_open_status(self):
        assert ExecutionAgent._map_status("open") == OrderStatus.OPEN

    def test_closed_status(self):
        assert ExecutionAgent._map_status("closed") == OrderStatus.FILLED

    def test_canceled_status(self):
        assert ExecutionAgent._map_status("canceled") == OrderStatus.CANCELLED

    def test_unknown_defaults_to_pending(self):
        assert ExecutionAgent._map_status("weird") == OrderStatus.PENDING


class TestFeeExtraction:
    def test_extracts_fee(self):
        order = {"fee": {"cost": 0.05, "currency": "USDT"}}
        assert ExecutionAgent._extract_fee(order) == 0.05

    def test_no_fee_returns_zero(self):
        assert ExecutionAgent._extract_fee({}) == 0.0
        assert ExecutionAgent._extract_fee({"fee": None}) == 0.0


class TestCancelStaleOrders:
    def test_cancels_old_orders(self):
        mock_exchange = MagicMock()
        executor = ExecutionAgent(mock_exchange)

        # Order from 25 hours ago
        old_ts = (datetime.now(timezone.utc).timestamp() - 25 * 3600) * 1000
        orders = [{"id": "old1", "timestamp": old_ts}]

        cancelled = executor.cancel_stale_orders("BTC/USDT", orders, max_age_hours=24)
        assert cancelled == 1
        mock_exchange.cancel_order.assert_called_once_with("old1", "BTC/USDT")

    def test_keeps_fresh_orders(self):
        mock_exchange = MagicMock()
        executor = ExecutionAgent(mock_exchange)

        # Order from 1 hour ago
        recent_ts = (datetime.now(timezone.utc).timestamp() - 1 * 3600) * 1000
        orders = [{"id": "fresh1", "timestamp": recent_ts}]

        cancelled = executor.cancel_stale_orders("BTC/USDT", orders, max_age_hours=24)
        assert cancelled == 0
        mock_exchange.cancel_order.assert_not_called()
