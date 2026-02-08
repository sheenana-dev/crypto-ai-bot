import pytest
import sqlite3
import os
import tempfile
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from agents.strategy import StrategyAgent
from models.schemas import (
    Indicators, MarketRegime, MarketState,
    OrderSide, SignalType,
)


def make_market_state(
    pair: str = "BTC/USDT",
    price: float = 60000.0,
    regime: MarketRegime = MarketRegime.RANGING,
) -> MarketState:
    """Create a MarketState for testing."""
    return MarketState(
        pair=pair,
        current_price=price,
        volume_24h=5000000.0,
        indicators=Indicators(
            rsi=50.0,
            ema_short=59800.0,
            ema_long=60200.0,
            bb_upper=61000.0,
            bb_middle=60000.0,
            bb_lower=59000.0,
            adx=18.0,
            price_change_24h_pct=0.001,
        ),
        regime=regime,
        timestamp=datetime.now(timezone.utc),
    )


def make_test_db():
    """Create a temporary SQLite DB with the dca_state table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE dca_state (
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
    conn.commit()
    conn.close()
    return path


def get_test_connection(path):
    """Return a fresh connection to the test DB."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def create_strategy() -> StrategyAgent:
    return StrategyAgent(MagicMock())


class TestGridSignals:
    def test_ranging_produces_symmetric_grid(self):
        strategy = create_strategy()
        state = make_market_state(regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        buys = [s for s in signals if s.side == OrderSide.BUY]
        sells = [s for s in signals if s.side == OrderSide.SELL]

        # 10 grids // 2 = 5 buys, 5 sells
        assert len(buys) == 5
        assert len(sells) == 5

    def test_all_buys_below_price(self):
        strategy = create_strategy()
        state = make_market_state(price=60000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        for s in signals:
            if s.side == OrderSide.BUY:
                assert s.price < 60000.0
                assert s.signal_type == SignalType.GRID_BUY

    def test_all_sells_above_price(self):
        strategy = create_strategy()
        state = make_market_state(price=60000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        for s in signals:
            if s.side == OrderSide.SELL:
                assert s.price > 60000.0
                assert s.signal_type == SignalType.GRID_SELL

    def test_grid_levels_are_evenly_spaced(self):
        strategy = create_strategy()
        state = make_market_state(price=60000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        buy_prices = sorted([s.price for s in signals if s.side == OrderSide.BUY], reverse=True)
        # Check spacing is approximately 0.5% of price
        for i in range(len(buy_prices) - 1):
            spacing = buy_prices[i] - buy_prices[i + 1]
            expected_spacing = 60000.0 * 0.005
            assert abs(spacing - expected_spacing) < 1.0  # Allow rounding tolerance

    def test_order_amounts_match_usdt_size(self):
        strategy = create_strategy()
        state = make_market_state(price=60000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        for s in signals:
            usdt_value = s.price * s.amount
            assert abs(usdt_value - 10.0) < 0.01  # ~10 USDT per order


class TestTrendingGrid:
    def test_trending_up_more_buys(self):
        strategy = create_strategy()
        state = make_market_state(regime=MarketRegime.TRENDING_UP)
        signals = strategy.generate_signals(state)

        buys = [s for s in signals if s.side == OrderSide.BUY]
        sells = [s for s in signals if s.side == OrderSide.SELL]

        assert len(buys) > len(sells)
        assert len(buys) == 7  # 70% of 10
        assert len(sells) == 3

    def test_trending_down_more_sells(self):
        strategy = create_strategy()
        state = make_market_state(regime=MarketRegime.TRENDING_DOWN)
        signals = strategy.generate_signals(state)

        buys = [s for s in signals if s.side == OrderSide.BUY]
        sells = [s for s in signals if s.side == OrderSide.SELL]

        assert len(sells) > len(buys)
        assert len(buys) == 3   # 30% of 10
        assert len(sells) == 7


class TestEdgeCases:
    def test_unknown_pair_returns_empty(self):
        strategy = create_strategy()
        state = make_market_state(pair="DOGE/USDT", regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        assert signals == []

    def test_signals_have_correct_pair(self):
        strategy = create_strategy()
        state = make_market_state(pair="BTC/USDT", regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        for s in signals:
            assert s.pair == "BTC/USDT"

    def test_signals_have_timestamps(self):
        strategy = create_strategy()
        state = make_market_state(regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        for s in signals:
            assert s.timestamp is not None


class TestDCASignals:
    """Tests for DCA mode — triggered on CRASH regime."""

    def setup_method(self):
        self.db_path = make_test_db()
        self._patcher = patch("agents.strategy.get_connection", side_effect=lambda: get_test_connection(self.db_path))
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()
        os.unlink(self.db_path)

    def test_crash_creates_first_dca_entry(self):
        """CRASH regime with no active DCA creates a DCA_BUY (TP comes on next call)."""
        strategy = create_strategy()
        state = make_market_state(price=50000.0, regime=MarketRegime.CRASH)
        signals = strategy.generate_signals(state)

        buys = [s for s in signals if s.signal_type == SignalType.DCA_BUY]
        assert len(buys) == 1
        assert buys[0].price == 50000.0
        assert buys[0].side == OrderSide.BUY

        # Buy amount should be 5% of 250 USDT reserve = 12.5 USDT worth
        expected_usdt = 250 * 0.05
        actual_usdt = buys[0].amount * buys[0].price
        assert abs(actual_usdt - expected_usdt) < 0.01

    def test_dca_take_profit_at_avg_plus_4pct(self):
        """Take-profit should be placed at avg_entry * 1.04 on subsequent crash calls."""
        strategy = create_strategy()

        # First call creates the DCA position
        strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        # Second call with same price — no new entry but TP is placed
        signals = strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        tps = [s for s in signals if s.signal_type == SignalType.DCA_TAKE_PROFIT]
        assert len(tps) == 1
        assert tps[0].price == round(50000.0 * 1.04, 2)
        assert tps[0].side == OrderSide.SELL

    def test_dca_state_persisted(self):
        """After first DCA entry, state should be saved in the database."""
        strategy = create_strategy()
        state = make_market_state(price=50000.0, regime=MarketRegime.CRASH)
        strategy.generate_signals(state)

        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()

        assert row is not None
        assert row["entries"] == 1
        assert row["avg_entry_price"] == 50000.0
        assert row["last_entry_price"] == 50000.0

    def test_dca_additional_entry_on_deeper_dip(self):
        """If price drops 3%+ from last entry, add another DCA entry."""
        strategy = create_strategy()

        # First entry at 50000
        state1 = make_market_state(price=50000.0, regime=MarketRegime.CRASH)
        strategy.generate_signals(state1)

        # Price drops 4% to 48000 — should trigger entry #2
        state2 = make_market_state(price=48000.0, regime=MarketRegime.CRASH)
        signals = strategy.generate_signals(state2)

        buys = [s for s in signals if s.signal_type == SignalType.DCA_BUY]
        assert len(buys) == 1
        assert buys[0].price == 48000.0

        # Verify DB shows 2 entries
        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()
        assert row["entries"] == 2

    def test_dca_no_entry_if_drop_too_small(self):
        """If price hasn't dropped 3% from last entry, no new DCA buy."""
        strategy = create_strategy()

        # First entry at 50000
        state1 = make_market_state(price=50000.0, regime=MarketRegime.CRASH)
        strategy.generate_signals(state1)

        # Price only drops 1% to 49500 — should NOT trigger entry #2
        state2 = make_market_state(price=49500.0, regime=MarketRegime.CRASH)
        signals = strategy.generate_signals(state2)

        buys = [s for s in signals if s.signal_type == SignalType.DCA_BUY]
        assert len(buys) == 0

        # Should still have take-profit though
        tps = [s for s in signals if s.signal_type == SignalType.DCA_TAKE_PROFIT]
        assert len(tps) == 1

    def test_dca_max_3_entries(self):
        """DCA should cap at 3 entries per dip event."""
        strategy = create_strategy()

        # Entry 1 at 50000
        strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        # Entry 2 at 48000 (4% drop)
        strategy.generate_signals(make_market_state(price=48000.0, regime=MarketRegime.CRASH))

        # Entry 3 at 46000 (4.2% drop from 48000)
        strategy.generate_signals(make_market_state(price=46000.0, regime=MarketRegime.CRASH))

        # Entry 4 attempt at 44000 — should be BLOCKED (max 3)
        signals = strategy.generate_signals(make_market_state(price=44000.0, regime=MarketRegime.CRASH))

        buys = [s for s in signals if s.signal_type == SignalType.DCA_BUY]
        assert len(buys) == 0

        # Verify DB shows 3 entries
        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()
        assert row["entries"] == 3

    def test_dca_avg_price_updates_correctly(self):
        """Average entry price should update as new entries are added."""
        strategy = create_strategy()

        # Entry 1 at 50000 — cost = 12.5 USDT, qty = 12.5/50000
        strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        # Entry 2 at 48000 — cost = 12.5 USDT, qty = 12.5/48000
        strategy.generate_signals(make_market_state(price=48000.0, regime=MarketRegime.CRASH))

        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()

        # avg = total_cost / total_qty = 25 / (12.5/50000 + 12.5/48000)
        expected_qty = round(12.5 / 50000, 8) + round(12.5 / 48000, 8)
        expected_avg = 25.0 / expected_qty
        assert abs(row["avg_entry_price"] - expected_avg) < 0.01

    def test_dca_recovery_closes_position(self):
        """When price recovers past take-profit while NOT in CRASH, DCA should close."""
        strategy = create_strategy()

        # Create DCA position during crash
        strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        # Market recovers — now RANGING at a price above take-profit (50000 * 1.04 = 52000)
        state = make_market_state(price=53000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        # Should have DCA_TAKE_PROFIT + grid signals
        tps = [s for s in signals if s.signal_type == SignalType.DCA_TAKE_PROFIT]
        assert len(tps) == 1
        assert tps[0].price == round(50000.0 * 1.04, 2)

        # DCA should now be closed
        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()
        assert row is None  # No active DCA

    def test_dca_not_recovered_keeps_tp_order(self):
        """When price hasn't recovered in non-CRASH regime, keep TP order active."""
        strategy = create_strategy()

        # Create DCA position during crash
        strategy.generate_signals(make_market_state(price=50000.0, regime=MarketRegime.CRASH))

        # Market shifts to RANGING but price is still below TP (50000 * 1.04 = 52000)
        state = make_market_state(price=51000.0, regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        tps = [s for s in signals if s.signal_type == SignalType.DCA_TAKE_PROFIT]
        assert len(tps) == 1

        # Grid signals should also be present
        grid = [s for s in signals if s.signal_type in (SignalType.GRID_BUY, SignalType.GRID_SELL)]
        assert len(grid) == 10  # Full grid

        # DCA should still be active
        conn = get_test_connection(self.db_path)
        row = conn.execute("SELECT * FROM dca_state WHERE pair = ? AND active = 1", ("BTC/USDT",)).fetchone()
        conn.close()
        assert row is not None

    def test_no_dca_no_extra_signals_in_ranging(self):
        """Without active DCA, RANGING should just produce grid signals."""
        strategy = create_strategy()
        state = make_market_state(regime=MarketRegime.RANGING)
        signals = strategy.generate_signals(state)

        dca_signals = [s for s in signals if s.signal_type in (SignalType.DCA_BUY, SignalType.DCA_TAKE_PROFIT)]
        assert len(dca_signals) == 0
        assert len(signals) == 10  # Pure grid
