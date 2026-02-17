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
from typing import List, Optional, Tuple

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

        # REGIME-AWARE TRADING PAUSE: Only trade grid in RANGING markets
        # In TRENDING markets, grid orders don't fill (0% fill rate) and waste API calls
        # Better to pause and wait for ranging conditions to return
        if regime == MarketRegime.RANGING:
            return dca_tp + self._grid_signals(market_state, bias=0, regime=regime)
        elif regime in [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]:
            adx = market_state.indicators.adx
            # CLOSE-ONLY: If we have an open position, place closing orders even during TRENDING
            # This prevents profitable positions from getting stranded with no exit orders
            close_signals = self._close_only_signals(market_state)
            if close_signals:
                logger.info(
                    f"{pair} TRENDING ({regime.value}, ADX={adx:.1f}) — "
                    f"grid paused, placing {len(close_signals)} close-only order(s)"
                )
                return dca_tp + close_signals
            logger.info(
                f"{pair} GRID PAUSED: {regime.value} market (ADX={adx:.1f}) — "
                f"no position, waiting for RANGING conditions"
            )
            return dca_tp

        return dca_tp

    def _grid_signals(self, market_state: MarketState, bias: int = 0, regime: MarketRegime = MarketRegime.RANGING) -> List[OrderSignal]:
        """Generate position-aware grid buy/sell signals.

        Checks current exchange position and biases the grid to close
        accumulated exposure, preventing one-sided position buildup.

        Only runs in RANGING markets (6 levels). TRENDING markets are
        handled by regime pause in generate_signals().
        """
        pair = market_state.pair
        price = market_state.current_price
        params = GRID_PARAMS[pair]

        # FUNDING RATE SAFETY CHECK: Skip grid if funding is heavily against position direction
        # Determine position direction based on current exposure
        position_info = self._get_position_info(pair)
        if position_info and position_info["amount"] > 0:
            # We have an open position — check funding for that direction
            position_side = position_info["side"].upper()
            is_safe, funding_rate = self._check_funding_rate_safety(pair, position_side)
            if not is_safe:
                logger.warning(f"{pair} grid skipped due to extreme funding rate: {funding_rate*100:.4f}%")
                return []  # Skip grid orders entirely if funding is too bad
        else:
            # No position yet — check funding for LONG (since grid typically accumulates longs in downtrends)
            # If funding is extremely negative, we might accumulate a long and bleed money
            is_safe, funding_rate = self._check_funding_rate_safety(pair, "LONG")
            if not is_safe:
                logger.warning(f"{pair} grid skipped due to extreme negative funding (would hurt potential longs)")
                return []

        num_grids = params["num_grids"]  # 6 levels (3 buy + 3 sell)

        order_size_usdt = params["order_size_usdt"]

        # HYBRID BB+ADX SPACING: BB measures actual range, ADX adds safety buffer for forming trends
        # BB base: spacing = BB_width / num_grids (measures what the market IS doing)
        # ADX multiplier: widens spacing as trend strengthens (prepares for what's COMING)
        # ADX ≤ 15: ×1.0 (dead market, pure BB), ADX 25: ×1.2, ADX 40+: ×1.5 (cap)
        # Confidence multiplier: widens spacing when regime classification is uncertain
        bb_upper = market_state.indicators.bb_upper
        bb_lower = market_state.indicators.bb_lower
        bb_width_pct = (bb_upper - bb_lower) / price if price > 0 else 0.01
        adx = market_state.indicators.adx
        adx_multiplier = min(1.5, max(1.0, 1.0 + (adx - 15) * 0.02)) if adx > 15 else 1.0

        # Low confidence = ambiguous regime → widen spacing for safety
        # Confidence < 0.5 means ≤1 of 4 indicators agree — be cautious
        confidence = market_state.regime_confidence
        confidence_mult = 1.3 if confidence < 0.5 else 1.0

        spacing_pct = max(0.004, min(0.02, (bb_width_pct / num_grids) * adx_multiplier * confidence_mult))

        # Check current position on exchange to determine bias
        position_bias = self._get_position_bias(pair)
        effective_bias = bias + position_bias

        if effective_bias >= 2:
            # CLOSE-ONLY: heavy long — place ONLY sells, zero buys that would add exposure
            num_buys = 0
            num_sells = num_grids
            logger.warning(f"{pair} CLOSE-ONLY MODE: long position ≥2x grid — 0 buys, {num_sells} sells")
        elif effective_bias <= -2:
            # CLOSE-ONLY: heavy short — place ONLY buys, zero sells that would add exposure
            num_buys = num_grids
            num_sells = 0
            logger.warning(f"{pair} CLOSE-ONLY MODE: short position ≥2x grid — {num_buys} buys, 0 sells")
        elif effective_bias > 0:
            # Slight long bias — fewer buys, more sells to reduce
            num_buys = max(1, num_grids // 2 - 1)
            num_sells = num_grids - num_buys
        elif effective_bias < 0:
            # Slight short bias — more buys, fewer sells to reduce
            num_sells = max(1, num_grids // 2 - 1)
            num_buys = num_grids - num_sells
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

        # Log hybrid BB+ADX+confidence spacing
        conf_note = " [LOW CONF→×1.3]" if confidence_mult > 1.0 else ""
        logger.info(
            f"{pair} SPACING: BB={bb_width_pct*100:.2f}% × ADX={adx_multiplier:.2f} × conf={confidence_mult:.1f} "
            f"→ {spacing_pct*100:.2f}%{conf_note} "
            f"(BB ${bb_lower:.2f}-${bb_upper:.2f}, ADX={adx:.1f}, regime_conf={confidence:.0%})"
        )

        logger.info(
            f"{pair} grid: {num_buys} buy, {num_sells} sell, "
            f"levels={num_grids} ({regime.value}), spacing={spacing_pct*100:.2f}%, "
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

    def _get_position_info(self, pair: str) -> Optional[dict]:
        """Get current position info from exchange.

        Returns:
            dict with 'side', 'amount', 'notional', 'entryPrice' or None if no position
        """
        try:
            positions = self.exchange.fetch_positions([pair])
            for pos in positions:
                amt = float(pos.get("contracts", 0) or 0)
                if amt > 0:
                    return {
                        "side": pos.get("side", ""),
                        "amount": amt,
                        "entryPrice": float(pos.get("entryPrice", 0) or 0),
                        "notional": amt * float(pos.get("entryPrice", 0) or 0),
                    }
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch position info: {e}")
            return None

    def _get_position_bias(self, pair: str) -> int:
        """Check exchange position and return bias to counter it.

        Bias scale:
            0  = no position, balanced grid
            ±1 = small position (1-2x grid), slightly favor closing side
            ±2 = medium position (2-3x grid), heavily favor closing side
            ±3 = large position (3x+ grid), CLOSE-ONLY mode (zero orders that add)

        Long position → positive bias → more sells
        Short position → negative bias → more buys
        """
        position_info = self._get_position_info(pair)
        if not position_info:
            return 0

        side = position_info["side"]
        notional = position_info["notional"]
        grid_notional = GRID_PARAMS[pair]["order_size_usdt"] * settings.LEVERAGE
        position_ratio = notional / grid_notional if grid_notional > 0 else 0

        if side == "long":
            if position_ratio >= 3:
                return 3   # Close-only: 0 buys, 6 sells
            elif position_ratio >= 2:
                return 2   # Close-only: 0 buys, 6 sells
            elif position_ratio >= 1:
                return 1   # Slight bias: 2 buys, 4 sells
        elif side == "short":
            if position_ratio >= 3:
                return -3  # Close-only: 6 buys, 0 sells
            elif position_ratio >= 2:
                return -2  # Close-only: 6 buys, 0 sells
            elif position_ratio >= 1:
                return -1  # Slight bias: 4 buys, 2 sells
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

        # Check if price already recovered past TP (bounce during crash)
        tp_price = self._round_price(pair, avg_entry * (1 + take_profit_pct))
        if price >= tp_price:
            # Check actual position — don't SELL if position is SHORT (would add to short)
            position_info = self._get_position_info(pair)
            if position_info and position_info["side"] == "short":
                self._close_dca(dca["id"])
                logger.info(f"{pair} DCA: closing tracking — position is SHORT, TP SELL would add to short")
                return signals  # Return any BUY entries only

            self._close_dca(dca["id"])
            logger.info(f"{pair} DCA: price {price:.2f} >= TP {tp_price:.2f}, taking profit")
            signals.append(OrderSignal(
                pair=pair, side=OrderSide.SELL, price=tp_price,
                amount=self._round_amount(pair, total_qty), signal_type=SignalType.DCA_TAKE_PROFIT, timestamp=now,
            ))
        else:
            logger.info(f"{pair} DCA: TP at {tp_price:.2f} (current {price:.2f}, need +{((tp_price/price)-1)*100:.2f}%)")

        return signals

    def _dca_take_profit_if_recovered(self, market_state: MarketState) -> List[OrderSignal]:
        """If there's an active DCA and price has recovered, place a take-profit sell.

        Position-aware: if actual position is SHORT, don't SELL (that would add to short).
        Instead, close the DCA tracking — the DCA buy already helped reduce the short.
        """
        pair = market_state.pair
        price = market_state.current_price
        dca = self._get_active_dca(pair)

        if dca is None:
            return []

        # Check actual position — if SHORT, DCA SELL would add to short
        position_info = self._get_position_info(pair)
        if position_info and position_info["side"] == "short":
            self._close_dca(dca["id"])
            logger.info(f"{pair} DCA: closing tracking — position is SHORT, TP SELL would add to short")
            return []

        # No position means DCA buy fully closed the short — close DCA tracking
        if position_info is None:
            self._close_dca(dca["id"])
            logger.info(f"{pair} DCA: closing tracking — no open position (DCA buy closed it)")
            return []

        # Position is LONG — standard DCA TP logic
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

        # Not recovered yet — wait and check again next cycle
        logger.info(f"{pair} DCA: waiting for TP at {tp_price:.2f} (current {price:.2f}, need +{((tp_price/price)-1)*100:.2f}%)")
        return []

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

    def _close_only_signals(self, market_state: MarketState) -> List[OrderSignal]:
        """Generate close-only orders when TRENDING with an open position.

        Places a single closing order at half base spacing from current price:
        - Long position → SELL order above price
        - Short position → BUY order below price

        Uses HALF base spacing — close-only goal is to EXIT on any bounce,
        not profit. Tighter = more likely to catch small pullbacks in trends.
        """
        pair = market_state.pair
        price = market_state.current_price
        params = GRID_PARAMS[pair]

        position_info = self._get_position_info(pair)
        if not position_info or position_info["amount"] == 0:
            return []

        side = position_info["side"]
        amount = position_info["amount"]
        # Half spacing for close-only: goal is to exit, not profit
        close_spacing = params["grid_spacing_pct"] * 0.5
        now = datetime.now(timezone.utc)

        if side == "long":
            # Close long → place SELL above current price
            close_price = self._round_price(pair, price * (1 + close_spacing))
            close_amount = self._round_amount(pair, amount)
            if close_amount <= 0:
                return []
            logger.info(
                f"{pair} TRENDING CLOSE-ONLY: sell {close_amount} @ ${close_price:.4f} "
                f"(+{close_spacing*100:.1f}% from ${price:.4f}) to close long"
            )
            return [OrderSignal(
                pair=pair, side=OrderSide.SELL, price=close_price,
                amount=close_amount, signal_type=SignalType.GRID_SELL, timestamp=now,
            )]
        elif side == "short":
            # Close short → place BUY below current price
            close_price = self._round_price(pair, price * (1 - close_spacing))
            close_amount = self._round_amount(pair, amount)
            if close_amount <= 0:
                return []
            logger.info(
                f"{pair} TRENDING CLOSE-ONLY: buy {close_amount} @ ${close_price:.4f} "
                f"(-{close_spacing*100:.1f}% from ${price:.4f}) to close short"
            )
            return [OrderSignal(
                pair=pair, side=OrderSide.BUY, price=close_price,
                amount=close_amount, signal_type=SignalType.GRID_BUY, timestamp=now,
            )]

        return []

    def _check_funding_rate_safety(self, pair: str, position_side: str) -> Tuple[bool, float]:
        """Check if current funding rate is safe for opening positions.

        Binance Futures charges funding every 8 hours (12 AM, 8 AM, 4 PM UTC).
        Negative funding = longs pay shorts (bearish sentiment)
        Positive funding = shorts pay longs (bullish sentiment)

        With 5-10x leverage, funding can cost 0.05-0.5% per 8 hours = $0.15-$1.50/day per $100 position.

        Returns:
            (is_safe, funding_rate): True if safe to trade, False if funding is heavily against position
        """
        try:
            # Fetch current funding rate (updated every 8 hours)
            funding_rate_info = self.exchange.fetch_funding_rate(pair)
            funding_rate = float(funding_rate_info.get('fundingRate', 0))

            # Funding rate thresholds (absolute value)
            EXTREME_FUNDING = 0.0005  # 0.05% per 8 hours = very expensive
            HIGH_FUNDING = 0.0003     # 0.03% per 8 hours = moderately expensive

            # Check if funding is against the position direction
            if position_side == "LONG":
                # Long position: negative funding means we PAY (bad)
                if funding_rate < -EXTREME_FUNDING:
                    logger.warning(
                        f"{pair} FUNDING WARNING: Extreme negative funding {funding_rate*100:.4f}% "
                        f"— LONGS PAY ${abs(funding_rate)*settings.LEVERAGE*100:.2f} per $100 every 8h — "
                        f"SKIPPING grid orders to avoid bleeding money"
                    )
                    return False, funding_rate
                elif funding_rate < -HIGH_FUNDING:
                    logger.info(
                        f"{pair} funding: {funding_rate*100:.4f}% (longs pay ${abs(funding_rate)*settings.LEVERAGE*100:.2f} per $100 every 8h)"
                    )
                    # Still trade, but user is aware of funding cost
                    return True, funding_rate
            elif position_side == "SHORT":
                # Short position: positive funding means we PAY (bad)
                if funding_rate > EXTREME_FUNDING:
                    logger.warning(
                        f"{pair} FUNDING WARNING: Extreme positive funding {funding_rate*100:.4f}% "
                        f"— SHORTS PAY ${funding_rate*settings.LEVERAGE*100:.2f} per $100 every 8h — "
                        f"SKIPPING grid orders to avoid bleeding money"
                    )
                    return False, funding_rate
                elif funding_rate > HIGH_FUNDING:
                    logger.info(
                        f"{pair} funding: {funding_rate*100:.4f}% (shorts pay ${funding_rate*settings.LEVERAGE*100:.2f} per $100 every 8h)"
                    )
                    return True, funding_rate

            # Funding is neutral or in our favor
            return True, funding_rate

        except Exception as e:
            logger.warning(f"{pair} failed to fetch funding rate: {e} — proceeding without funding check")
            # If we can't fetch funding, proceed cautiously (don't block trading)
            return True, 0.0
