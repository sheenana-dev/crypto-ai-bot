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
from typing import List, Optional, Tuple

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
        """
        Place an order on the exchange.

        - Grid orders: GTX limit orders (post-only, 0.02% maker fee)
        - DCA/TP: market orders (instant fill needed, 0.05% taker fee)

        GTX (Good-Til-Crossing) orders are rejected if they would immediately match.
        This is fine - missing a fill is better than paying taker fees + spread.
        """
        # Binance Futures minimum notional is $100
        notional = signal.price * signal.amount
        if notional < 100:
            logger.warning(f"Skipping {signal.pair} order: notional ${notional:.2f} < $100 minimum")
            return None

        self._ensure_leverage(signal.pair)

        # DCA/TP use market for instant fills, Grid uses GTX limit for maker fees
        is_dca = signal.signal_type in (SignalType.DCA_BUY, SignalType.DCA_TAKE_PROFIT)
        use_market = is_dca

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
                    # Limit order with GTX (post-only for maker fees)
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

                order_type = "MARKET" if use_market else "LIMIT GTX"
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
        """Cancel all open limit orders for a pair. Returns count cancelled.

        Note: Emergency stops are algo/conditional orders on Binance's Algo API.
        They do NOT appear in fetch_open_orders, so they're inherently safe from
        cancellation here. Managed separately by scheduler.manage_emergency_stops().
        """
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

    def selective_refresh(self, pair: str, new_signals: List[OrderSignal],
                          spacing_pct: float) -> Tuple[int, int, List[TradeLog]]:
        """Selectively cancel/replace grid orders. Only cancel orders outside the new grid.

        Compares existing open limit orders against new signals by (side, price).
        Orders within half-spacing tolerance of a new signal are KEPT (preserving
        near-filling orders). Unmatched orders are cancelled, unmatched signals are placed.

        Returns (kept_count, cancelled_count, newly_placed_trades).
        """
        try:
            existing = self.exchange.fetch_open_orders(pair)
        except Exception as e:
            logger.warning(f"Failed to fetch open orders for selective refresh on {pair}: {e}")
            # Fallback: place all signals (same as fresh grid)
            return 0, 0, self.execute_orders(new_signals)

        # Filter to limit orders only (skip stop orders)
        existing_limit = []
        for o in existing:
            order_type = (o.get("type") or "").lower()
            raw_type = (o.get("info", {}).get("type") or "").upper()
            if order_type == "limit" and raw_type not in ("STOP_MARKET", "STOP", "STOP_LIMIT"):
                existing_limit.append(o)

        if not existing_limit:
            # No existing orders — just place everything
            trades = self.execute_orders(new_signals)
            return 0, 0, trades

        # Separate grid signals from DCA/other signals
        grid_signals = [s for s in new_signals
                        if s.signal_type in (SignalType.GRID_BUY, SignalType.GRID_SELL)]
        non_grid_signals = [s for s in new_signals
                            if s.signal_type not in (SignalType.GRID_BUY, SignalType.GRID_SELL)]

        # Match existing orders to new signals
        signals_to_place = list(grid_signals)
        orders_to_cancel = []
        kept = 0
        tolerance = spacing_pct * 0.5  # Half-spacing tolerance

        for order in existing_limit:
            order_side = order["side"]  # "buy" or "sell"
            order_price = float(order.get("price", 0))

            # Find best matching signal (closest price, same side)
            best_match = None
            best_diff = float('inf')
            for signal in signals_to_place:
                if signal.side.value.lower() != order_side:
                    continue
                price_diff = abs(order_price - signal.price) / max(order_price, 1)
                if price_diff < tolerance and price_diff < best_diff:
                    best_match = signal
                    best_diff = price_diff

            if best_match:
                signals_to_place.remove(best_match)
                kept += 1
                logger.debug(
                    f"Keeping {pair} {order_side} @ ${order_price:.4f} "
                    f"(matches new signal @ ${best_match.price:.4f}, diff={best_diff*100:.3f}%)"
                )
            else:
                orders_to_cancel.append(order)

        # Cancel unmatched orders
        for order in orders_to_cancel:
            try:
                self.exchange.cancel_order(order["id"], pair)
                logger.info(
                    f"Selective cancel: {pair} {order['side'].upper()} @ ${float(order.get('price', 0)):.4f}"
                )
            except Exception as e:
                logger.warning(f"Failed to cancel {order['id']}: {e}")

        # Place remaining new grid signals + all non-grid signals (DCA etc)
        trades = self.execute_orders(signals_to_place + non_grid_signals)

        logger.info(
            f"{pair} selective refresh: kept {kept}, cancelled {len(orders_to_cancel)}, "
            f"placed {len(trades)} new"
        )
        return kept, len(orders_to_cancel), trades

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
