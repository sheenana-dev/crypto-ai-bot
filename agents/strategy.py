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
            return dca_tp + self._grid_signals(market_state, bias=0, regime=regime)
        elif regime == MarketRegime.TRENDING_UP:
            return dca_tp + self._grid_signals(market_state, bias=1, regime=regime)
        elif regime == MarketRegime.TRENDING_DOWN:
            return dca_tp + self._grid_signals(market_state, bias=-1, regime=regime)

        return dca_tp

    def _grid_signals(self, market_state: MarketState, bias: int = 0, regime: MarketRegime = MarketRegime.RANGING) -> List[OrderSignal]:
        """Generate position-aware grid buy/sell signals.

        Checks current exchange position and biases the grid to close
        accumulated exposure, preventing one-sided position buildup.

        In TRENDING markets, uses reduced grid (5 levels) to minimize exposure.
        In RANGING markets, uses full grid (10 levels) to maximize profit.
        """
        pair = market_state.pair
        price = market_state.current_price
        params = GRID_PARAMS[pair]

        # Reduce grid size in trending markets to minimize risk
        base_num_grids = params["num_grids"]
        if regime in [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]:
            num_grids = max(5, base_num_grids // 2)  # Use 5 levels in trending markets
        else:
            num_grids = base_num_grids  # Full 10 levels in ranging markets

        spacing_pct = params["grid_spacing_pct"]
        order_size_usdt = params["order_size_usdt"]

        # Check current position on exchange to determine bias
        position_bias = self._get_position_bias(pair)
        effective_bias = bias + position_bias

        if effective_bias >= 2:
            # Heavy long — place mostly sells to reduce
            num_buys = 2
            num_sells = num_grids - num_buys
        elif effective_bias <= -2:
            # Heavy short — place mostly buys to reduce
            num_buys = num_grids - 2
            num_sells = 2
        elif effective_bias > 0:
            # Slight long bias — more sells
            num_buys = 3
            num_sells = num_grids - num_buys
        elif effective_bias < 0:
            # Slight short bias — more buys
            num_buys = num_grids - 3
            num_sells = 3
        else:
            num_buys = num_grids // 2
            num_sells = num_grids // 2

        signals = []
        now = datetime.now(timezone.utc)
        leverage = settings.LEVERAGE

        for i in range(1, num_buys + 1):
            level_price = self._round_price(pair, price * (1 - spacing_pct * i))
            amount = self._round_amount(pair, max(order_size_usdt * leverage / level_price, 0.001))
            if amount <= 0:
                continue
            signals.append(OrderSignal(
                pair=pair, side=OrderSide.BUY, price=level_price,
                amount=amount, signal_type=SignalType.GRID_BUY, timestamp=now,
            ))

        for i in range(1, num_sells + 1):
            level_price = self._round_price(pair, price * (1 + spacing_pct * i))
            amount = self._round_amount(pair, max(order_size_usdt * leverage / level_price, 0.001))
            if amount <= 0:
                continue
            signals.append(OrderSignal(
                pair=pair, side=OrderSide.SELL, price=level_price,
                amount=amount, signal_type=SignalType.GRID_SELL, timestamp=now,
            ))

        # Log grid summary
        buy_prices = [s.price for s in signals if s.side == OrderSide.BUY]
        sell_prices = [s.price for s in signals if s.side == OrderSide.SELL]
        buy_range = f"${min(buy_prices):.4f}-${max(buy_prices):.4f}" if buy_prices else "none"
        sell_range = f"${min(sell_prices):.4f}-${max(sell_prices):.4f}" if sell_prices else "none"

        logger.info(
            f"{pair} grid: {num_buys} buy, {num_sells} sell, "
            f"levels={num_grids} ({regime.value}), spacing={spacing_pct*100:.1f}%, "
            f"size={order_size_usdt} USDT, pos_bias={position_bias}, effective_bias={effective_bias}"
        )
        logger.info(
            f"{pair} grid placement: current=${price:.4f} | buys={buy_range} | sells={sell_range}"
        )
        return signals

    def _round_price(self, pair: str, price: float) -> float:
        """Round price to exchange's required precision for the pair."""
        try:
            return float(self.exchange.price_to_precision(pair, price))
        except Exception:
            return round(price, 6)

    def _round_amount(self, pair: str, amount: float) -> float:
        """Round amount to exchange's required precision for the pair."""
        try:
            return float(self.exchange.amount_to_precision(pair, amount))
        except Exception:
            return round(amount, 3)

    def _get_position_bias(self, pair: str) -> int:
        """Check exchange position and return bias to counter it.

        Returns:
            +1/+2 if short (need more sells to close) — wait, actually
            if we're long, we want more sells to reduce. If short, more buys.
            So: long position -> positive bias (triggers more sells)
                short position -> negative bias (triggers more buys)
        """
        try:
            positions = self.exchange.fetch_positions([pair])
            for pos in positions:
                amt = float(pos.get("contracts", 0) or 0)
                if amt > 0:
                    side = pos.get("side", "")
                    notional = amt * float(pos.get("entryPrice", 0) or 0)
                    # Scale bias by position size relative to grid order size
                    grid_notional = GRID_PARAMS[pair]["order_size_usdt"] * settings.LEVERAGE
                    position_ratio = notional / grid_notional if grid_notional > 0 else 0

                    if side == "long":
                        # Long position — bias toward sells to close
                        if position_ratio >= 3:
                            return 2
                        elif position_ratio >= 1:
                            return 1
                    elif side == "short":
                        # Short position — bias toward buys to close
                        if position_ratio >= 3:
                            return -2
                        elif position_ratio >= 1:
                            return -1
            return 0
        except Exception as e:
            logger.warning(f"Failed to check position for bias: {e}")
            return 0

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
            amount = self._round_amount(pair, max(buy_usdt * settings.LEVERAGE / price, 0.001))

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
                amount = self._round_amount(pair, max(buy_usdt * settings.LEVERAGE / price, 0.001))

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
        tp_price = self._round_price(pair, avg_entry * (1 + take_profit_pct))
        signals.append(OrderSignal(
            pair=pair, side=OrderSide.SELL, price=tp_price,
            amount=self._round_amount(pair, total_qty), signal_type=SignalType.DCA_TAKE_PROFIT, timestamp=now,
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
        tp_price = self._round_price(pair, avg_entry * (1 + take_profit_pct))

        if price >= tp_price:
            # Price recovered past take-profit — sell at market and close DCA
            self._close_dca(dca["id"])
            logger.info(f"{pair} DCA: price recovered to {price:.2f}, closing at take-profit {tp_price:.2f}")
            return [OrderSignal(
                pair=pair, side=OrderSide.SELL, price=tp_price,
                amount=self._round_amount(pair, total_qty), signal_type=SignalType.DCA_TAKE_PROFIT,
                timestamp=datetime.now(timezone.utc),
            )]

        # Not recovered yet — keep the take-profit order active
        return [OrderSignal(
            pair=pair, side=OrderSide.SELL, price=tp_price,
            amount=self._round_amount(pair, total_qty), signal_type=SignalType.DCA_TAKE_PROFIT,
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
