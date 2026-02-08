"""Strategy Agent — generates trading signals based on market regime.

Grid Trading (RANGING / TRENDING):
  - Creates a grid of buy orders below current price and sell orders above
  - Bias shifts based on trend direction

DCA Mode (CRASH):
  - Triggered when regime = CRASH (RSI < 30, price drop > 5%)
  - Buys 5% of DCA reserve per entry
  - Additional entries if price drops another 3% from last entry
  - Max 3 entries per dip event
  - Take-profit sell at average entry + 4%
"""

import logging
from datetime import datetime, timezone
from typing import List

import ccxt

from config import settings
from config.grid_config import GRID_PARAMS, DCA_PARAMS
from database.db import get_connection
from models.schemas import MarketRegime, MarketState, OrderSide, OrderSignal, SignalType

logger = logging.getLogger(__name__)


class StrategyAgent:
    """Determines which strategy to run based on market regime and generates order signals."""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    def generate_signals(self, market_state: MarketState) -> List[OrderSignal]:
        """Generate order signals based on current market state and regime."""
        pair = market_state.pair
        regime = market_state.regime

        if pair not in GRID_PARAMS:
            logger.warning(f"No grid config for {pair}, skipping")
            return []

        if regime == MarketRegime.CRASH:
            return self._dca_signals(market_state)

        # If not crashing, close any active DCA by placing take-profit if we have a position
        dca_tp = self._dca_take_profit_if_recovered(market_state)

        if regime == MarketRegime.RANGING:
            return dca_tp + self._grid_signals(market_state, bias=0)
        elif regime == MarketRegime.TRENDING_UP:
            return dca_tp + self._grid_signals(market_state, bias=1)
        elif regime == MarketRegime.TRENDING_DOWN:
            return dca_tp + self._grid_signals(market_state, bias=-1)

        return dca_tp

    def _grid_signals(self, market_state: MarketState, bias: int = 0) -> List[OrderSignal]:
        """Generate grid buy/sell signals."""
        pair = market_state.pair
        price = market_state.current_price
        params = GRID_PARAMS[pair]

        num_grids = params["num_grids"]
        spacing_pct = params["grid_spacing_pct"]
        order_size_usdt = params["order_size_usdt"]

        if bias == 0:
            num_buys = num_grids // 2
            num_sells = num_grids // 2
        elif bias > 0:
            num_buys = int(num_grids * 0.7)
            num_sells = num_grids - num_buys
        else:
            num_buys = int(num_grids * 0.3)
            num_sells = num_grids - num_buys

        signals = []
        now = datetime.now(timezone.utc)
        leverage = settings.LEVERAGE

        for i in range(1, num_buys + 1):
            level_price = round(price * (1 - spacing_pct * i), 2)
            amount = round(max(order_size_usdt * leverage / level_price, 0.001), 3)
            signals.append(OrderSignal(
                pair=pair, side=OrderSide.BUY, price=level_price,
                amount=amount, signal_type=SignalType.GRID_BUY, timestamp=now,
            ))

        for i in range(1, num_sells + 1):
            level_price = round(price * (1 + spacing_pct * i), 2)
            amount = round(max(order_size_usdt * leverage / level_price, 0.001), 3)
            signals.append(OrderSignal(
                pair=pair, side=OrderSide.SELL, price=level_price,
                amount=amount, signal_type=SignalType.GRID_SELL, timestamp=now,
            ))

        logger.info(
            f"{pair} grid: {num_buys} buy levels, {num_sells} sell levels, "
            f"spacing={spacing_pct*100:.1f}%, size={order_size_usdt} USDT"
        )
        return signals

    def _dca_signals(self, market_state: MarketState) -> List[OrderSignal]:
        """Generate DCA buy signals for a crash/dip market."""
        pair = market_state.pair
        price = market_state.current_price
        now = datetime.now(timezone.utc)

        entry_pct = DCA_PARAMS["entry_pct"]
        additional_drop_pct = DCA_PARAMS["additional_drop_pct"]
        max_entries = DCA_PARAMS["max_entries_per_dip"]
        take_profit_pct = DCA_PARAMS["take_profit_pct"]

        dca = self._get_active_dca(pair)

        if dca is None:
            # Start a new DCA position — first entry at market price
            buy_usdt = settings.DCA_RESERVE * entry_pct
            amount = round(max(buy_usdt * settings.LEVERAGE / price, 0.001), 3)

            self._create_dca(pair, price, amount, buy_usdt)
            logger.info(f"{pair} DCA: new position, entry #1 at {price:.2f} (${buy_usdt:.2f})")

            return [OrderSignal(
                pair=pair, side=OrderSide.BUY, price=price,
                amount=amount, signal_type=SignalType.DCA_BUY, timestamp=now,
            )]

        entries = dca["entries"]
        last_entry_price = dca["last_entry_price"]
        avg_entry = dca["avg_entry_price"]
        total_qty = dca["total_qty"]

        signals = []

        # Check if we can add another entry (price dropped further)
        if entries < max_entries:
            drop_from_last = (last_entry_price - price) / last_entry_price
            if drop_from_last >= additional_drop_pct:
                buy_usdt = settings.DCA_RESERVE * entry_pct
                amount = round(max(buy_usdt * settings.LEVERAGE / price, 0.001), 3)

                new_total_qty = total_qty + amount
                new_total_cost = dca["total_cost"] + buy_usdt
                new_avg = new_total_cost / new_total_qty

                self._update_dca(dca["id"], entries + 1, new_total_qty, new_total_cost, new_avg, price)
                logger.info(
                    f"{pair} DCA: entry #{entries + 1} at {price:.2f} "
                    f"(drop {drop_from_last*100:.1f}% from last), new avg: {new_avg:.2f}"
                )

                signals.append(OrderSignal(
                    pair=pair, side=OrderSide.BUY, price=price,
                    amount=amount, signal_type=SignalType.DCA_BUY, timestamp=now,
                ))

                avg_entry = new_avg
                total_qty = new_total_qty
            else:
                logger.info(
                    f"{pair} DCA: waiting for deeper dip "
                    f"(need {additional_drop_pct*100:.0f}% drop, currently {drop_from_last*100:.1f}%)"
                )

        # Always place a take-profit order at avg entry + take_profit_pct
        tp_price = round(avg_entry * (1 + take_profit_pct), 2)
        signals.append(OrderSignal(
            pair=pair, side=OrderSide.SELL, price=tp_price,
            amount=round(total_qty, 8), signal_type=SignalType.DCA_TAKE_PROFIT, timestamp=now,
        ))

        logger.info(f"{pair} DCA: take-profit at {tp_price:.2f} for {total_qty:.8f}")
        return signals

    def _dca_take_profit_if_recovered(self, market_state: MarketState) -> List[OrderSignal]:
        """If there's an active DCA and price has recovered, place a take-profit sell."""
        pair = market_state.pair
        price = market_state.current_price
        dca = self._get_active_dca(pair)

        if dca is None:
            return []

        avg_entry = dca["avg_entry_price"]
        total_qty = dca["total_qty"]
        take_profit_pct = DCA_PARAMS["take_profit_pct"]
        tp_price = round(avg_entry * (1 + take_profit_pct), 2)

        if price >= tp_price:
            # Price recovered past take-profit — sell at market and close DCA
            self._close_dca(dca["id"])
            logger.info(f"{pair} DCA: price recovered to {price:.2f}, closing at take-profit {tp_price:.2f}")
            return [OrderSignal(
                pair=pair, side=OrderSide.SELL, price=tp_price,
                amount=round(total_qty, 8), signal_type=SignalType.DCA_TAKE_PROFIT,
                timestamp=datetime.now(timezone.utc),
            )]

        # Not recovered yet — keep the take-profit order active
        return [OrderSignal(
            pair=pair, side=OrderSide.SELL, price=tp_price,
            amount=round(total_qty, 8), signal_type=SignalType.DCA_TAKE_PROFIT,
            timestamp=datetime.now(timezone.utc),
        )]

    # --- DCA state persistence ---

    def _get_active_dca(self, pair: str) -> dict:
        """Get the active DCA position for a pair, or None."""
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM dca_state WHERE pair = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (pair,),
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def _create_dca(self, pair: str, price: float, qty: float, cost: float) -> None:
        """Create a new DCA position."""
        conn = get_connection()
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            INSERT INTO dca_state (pair, entries, total_qty, total_cost, avg_entry_price, last_entry_price, active, started_at, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, 1, ?, ?)
        """, (pair, qty, cost, price, price, now, now))
        conn.commit()
        conn.close()

    def _update_dca(self, dca_id: int, entries: int, total_qty: float, total_cost: float, avg_price: float, last_price: float) -> None:
        """Update an existing DCA position with a new entry."""
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE dca_state SET entries = ?, total_qty = ?, total_cost = ?, avg_entry_price = ?,
                last_entry_price = ?, updated_at = ? WHERE id = ?
        """, (entries, total_qty, total_cost, avg_price, last_price, datetime.now(timezone.utc).isoformat(), dca_id))
        conn.commit()
        conn.close()

    def _close_dca(self, dca_id: int) -> None:
        """Mark a DCA position as inactive (closed)."""
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE dca_state SET active = 0, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), dca_id),
        )
        conn.commit()
        conn.close()
