"""Execution Agent — places and manages orders on Binance Futures via ccxt.

- Uses ccxt.binanceusdm for USDT-margined futures
- Places limit orders for grid, market orders for DCA
- Sets leverage per pair
- Manages open orders (cancel stale ones)
- Handles API errors with retries
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import ccxt

from config import settings
from models.schemas import OrderSignal, OrderStatus, SignalType, TradeLog

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class ExecutionAgent:
    """Executes approved orders on the exchange and tracks their status."""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange
        self._leverage_set = {}  # Track which pairs have leverage set

    def execute_orders(self, signals: List[OrderSignal]) -> List[TradeLog]:
        """Place orders on the exchange and return trade logs."""
        trades = []

        for signal in signals:
            trade = self._place_order(signal)
            if trade:
                trades.append(trade)

        logger.info(f"Executed {len(trades)}/{len(signals)} orders")
        return trades

    def _place_order(self, signal: OrderSignal) -> Optional[TradeLog]:
        """Place a single order with retry logic. Uses market for DCA, limit for grid."""
        # Binance Futures minimum notional is $100
        notional = signal.price * signal.amount
        if notional < 100:
            logger.warning(f"Skipping {signal.pair} order: notional ${notional:.2f} < $100 minimum")
            return None

        self._ensure_leverage(signal.pair)

        # Use market orders for DCA (fills instantly on testnet)
        use_market = signal.signal_type in (SignalType.DCA_BUY, SignalType.DCA_TAKE_PROFIT)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if use_market:
                    order = self.exchange.create_order(
                        symbol=signal.pair,
                        type="market",
                        side=signal.side.value.lower(),
                        amount=signal.amount,
                    )
                else:
                    order = self.exchange.create_order(
                        symbol=signal.pair,
                        type="limit",
                        side=signal.side.value.lower(),
                        amount=signal.amount,
                        price=signal.price,
                        params={"timeInForce": "GTX"},  # Post-only: maker fees only (0.02% vs 0.05% taker)
                    )

                fill_price = float(order.get("average", 0) or order.get("price", 0) or signal.price)

                trade = TradeLog(
                    order_id=order.get("id", str(uuid.uuid4())),
                    pair=signal.pair,
                    side=signal.side,
                    price=fill_price,
                    amount=signal.amount,
                    filled=order.get("filled", 0.0) or 0.0,
                    fee=self._extract_fee(order),
                    status=self._map_status(order.get("status", "open")),
                    signal_type=signal.signal_type,
                    timestamp=datetime.now(timezone.utc),
                )

                order_type = "MARKET" if use_market else "LIMIT"
                logger.info(
                    f"Order placed: {order_type} {signal.side.value} {signal.amount} {signal.pair} "
                    f"@ {fill_price} → id={trade.order_id}"
                )
                return trade

            except ccxt.InsufficientFunds as e:
                logger.error(f"Insufficient margin for {signal.pair}: {e}")
                return None

            except ccxt.InvalidOrder as e:
                logger.error(f"Invalid order for {signal.pair}: {e}")
                return None

            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {signal.pair}: {e}")
                if attempt == MAX_RETRIES:
                    logger.error(f"All retries exhausted for {signal.pair}")
                    return None

        return None

    def _ensure_leverage(self, pair: str) -> None:
        """Set leverage for a pair if not already set."""
        leverage = settings.LEVERAGE
        if self._leverage_set.get(pair) == leverage:
            return
        try:
            self.exchange.set_leverage(leverage, pair)
            self._leverage_set[pair] = leverage
            logger.info(f"Set leverage {leverage}x for {pair}")
        except Exception as e:
            logger.warning(f"Failed to set leverage for {pair}: {e}")

    def cancel_all_open_orders(self, pair: str) -> int:
        """Cancel all open orders for a pair. Returns count cancelled."""
        try:
            open_orders = self.exchange.fetch_open_orders(pair)
            if not open_orders:
                return 0
            cancelled = 0
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order["id"], pair)
                    cancelled += 1
                except Exception as e:
                    logger.warning(f"Failed to cancel order {order['id']}: {e}")
            logger.info(f"Cancelled {cancelled}/{len(open_orders)} old orders for {pair}")
            return cancelled
        except Exception as e:
            logger.warning(f"Failed to fetch open orders for {pair}: {e}")
            return 0

    def cancel_stale_orders(self, pair: str, open_orders: List[dict], max_age_hours: int = 24) -> int:
        """Cancel orders older than max_age_hours. Returns count of cancelled orders."""
        cancelled = 0
        now = datetime.now(timezone.utc)

        for order in open_orders:
            order_time = datetime.fromtimestamp(order["timestamp"] / 1000, tz=timezone.utc)
            age_hours = (now - order_time).total_seconds() / 3600

            if age_hours > max_age_hours:
                try:
                    self.exchange.cancel_order(order["id"], pair)
                    cancelled += 1
                    logger.info(f"Cancelled stale order {order['id']} ({age_hours:.1f}h old)")
                except Exception as e:
                    logger.error(f"Failed to cancel order {order['id']}: {e}")

        return cancelled

    def sync_open_orders(self, pair: str) -> List[TradeLog]:
        """Fetch current open orders from exchange and return as TradeLog list."""
        try:
            open_orders = self.exchange.fetch_open_orders(pair)
        except Exception as e:
            logger.error(f"Failed to fetch open orders for {pair}: {e}")
            return []

        trades = []
        for order in open_orders:
            trades.append(TradeLog(
                order_id=order["id"],
                pair=pair,
                side=order["side"].upper(),
                price=order["price"],
                amount=order["amount"],
                filled=order.get("filled", 0.0) or 0.0,
                fee=self._extract_fee(order),
                status=self._map_status(order.get("status", "open")),
                signal_type="GRID_BUY" if order["side"] == "buy" else "GRID_SELL",
                timestamp=datetime.fromtimestamp(order["timestamp"] / 1000, tz=timezone.utc),
            ))

        return trades

    @staticmethod
    def _extract_fee(order: dict) -> float:
        """Extract fee from order response."""
        fee_info = order.get("fee")
        if fee_info and isinstance(fee_info, dict):
            return float(fee_info.get("cost", 0.0) or 0.0)
        return 0.0

    @staticmethod
    def _map_status(exchange_status: str) -> OrderStatus:
        """Map exchange order status to our OrderStatus enum."""
        mapping = {
            "open": OrderStatus.OPEN,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "expired": OrderStatus.CANCELLED,
        }
        return mapping.get(exchange_status, OrderStatus.PENDING)
