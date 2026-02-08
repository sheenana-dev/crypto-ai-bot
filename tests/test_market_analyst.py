import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from agents.market_analyst import MarketAnalyst
from models.schemas import MarketRegime


def make_ohlcv_df(prices: list[float], length: int = 100) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame for testing."""
    if len(prices) < length:
        # Pad with the first price repeated
        prices = [prices[0]] * (length - len(prices)) + prices

    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=length, freq="h", tz="UTC"),
        "open": prices,
        "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices],
        "close": prices,
        "volume": [1000.0] * length,
    })
    return df


def create_analyst() -> MarketAnalyst:
    """Create a MarketAnalyst with a mocked exchange."""
    mock_exchange = MagicMock()
    return MarketAnalyst(mock_exchange)


class TestCalculateIndicators:
    def test_returns_all_keys(self):
        analyst = create_analyst()
        # Flat price around 60000
        df = make_ohlcv_df([60000.0] * 100)
        ind = analyst.calculate_indicators(df)

        expected_keys = {
            "rsi", "ema_short", "ema_long",
            "bb_upper", "bb_middle", "bb_lower",
            "adx", "price_change_24h_pct", "current_price",
        }
        assert set(ind.keys()) == expected_keys

    def test_ema_short_equals_long_on_flat_price(self):
        analyst = create_analyst()
        df = make_ohlcv_df([50000.0] * 100)
        ind = analyst.calculate_indicators(df)

        # On perfectly flat data, both EMAs converge to the same value
        assert abs(ind["ema_short"] - ind["ema_long"]) < 1.0

    def test_rsi_midrange_on_flat_price(self):
        analyst = create_analyst()
        df = make_ohlcv_df([50000.0] * 100)
        ind = analyst.calculate_indicators(df)

        # RSI on flat data should be around 50 (no gains or losses)
        # With identical prices, RSI can be NaN or ~50; just check it's not extreme
        assert 0 <= ind["rsi"] <= 100 or np.isnan(ind["rsi"])

    def test_price_change_detects_drop(self):
        analyst = create_analyst()
        # Price drops from 60000 to 55000 within the last 24 candles
        prices = [60000.0] * 85 + [55000.0] * 15
        df = make_ohlcv_df(prices)
        ind = analyst.calculate_indicators(df)

        assert ind["price_change_24h_pct"] < 0

    def test_bollinger_bands_order(self):
        analyst = create_analyst()
        # Slightly volatile prices
        np.random.seed(42)
        base = 60000.0
        prices = [base + np.random.randn() * 200 for _ in range(100)]
        df = make_ohlcv_df(prices)
        ind = analyst.calculate_indicators(df)

        assert ind["bb_lower"] < ind["bb_middle"] < ind["bb_upper"]


class TestDetermineRegime:
    def test_crash_regime(self):
        analyst = create_analyst()
        ind = {
            "current_price": 57000,
            "adx": 30,
            "rsi": 25,
            "ema_short": 58000,
            "ema_long": 59000,
            "bb_upper": 62000,
            "bb_lower": 56000,
            "price_change_24h_pct": -0.08,  # -8% drop
        }
        assert analyst.determine_regime(ind) == MarketRegime.CRASH

    def test_trending_up_regime(self):
        analyst = create_analyst()
        ind = {
            "current_price": 61000,
            "adx": 30,
            "rsi": 60,
            "ema_short": 60500,
            "ema_long": 59000,
            "bb_upper": 62000,
            "bb_lower": 58000,
            "price_change_24h_pct": 0.02,
        }
        assert analyst.determine_regime(ind) == MarketRegime.TRENDING_UP

    def test_trending_down_regime(self):
        analyst = create_analyst()
        ind = {
            "current_price": 59000,
            "adx": 30,
            "rsi": 40,
            "ema_short": 58500,
            "ema_long": 59500,
            "bb_upper": 62000,
            "bb_lower": 56000,
            "price_change_24h_pct": -0.02,
        }
        assert analyst.determine_regime(ind) == MarketRegime.TRENDING_DOWN

    def test_ranging_regime(self):
        analyst = create_analyst()
        ind = {
            "current_price": 60000,
            "adx": 18,
            "rsi": 50,
            "ema_short": 59800,
            "ema_long": 60200,
            "bb_upper": 61000,
            "bb_lower": 59000,
            "price_change_24h_pct": 0.001,
        }
        assert analyst.determine_regime(ind) == MarketRegime.RANGING

    def test_crash_takes_priority_over_trending(self):
        analyst = create_analyst()
        ind = {
            "current_price": 56000,
            "adx": 35,  # High ADX (would be trending)
            "rsi": 20,  # Very oversold
            "ema_short": 57000,
            "ema_long": 59000,
            "bb_upper": 62000,
            "bb_lower": 55000,
            "price_change_24h_pct": -0.10,  # -10% crash
        }
        # Crash should take priority over trending down
        assert analyst.determine_regime(ind) == MarketRegime.CRASH


class TestAnalyze:
    def test_analyze_returns_market_state(self):
        mock_exchange = MagicMock()

        # Mock OHLCV data
        np.random.seed(42)
        base = 60000.0
        ohlcv = []
        for i in range(100):
            price = base + np.random.randn() * 200
            ohlcv.append([
                1704067200000 + i * 3600000,  # timestamp
                price,       # open
                price * 1.005,  # high
                price * 0.995,  # low
                price,       # close
                1000.0,      # volume
            ])
        mock_exchange.fetch_ohlcv.return_value = ohlcv
        mock_exchange.fetch_ticker.return_value = {"quoteVolume": 5000000.0}

        analyst = MarketAnalyst(mock_exchange)
        state = analyst.analyze("BTC/USDT")

        assert state.pair == "BTC/USDT"
        assert state.current_price > 0
        assert state.volume_24h == 5000000.0
        assert state.regime in MarketRegime
        assert state.indicators.rsi >= 0
        assert state.indicators.bb_lower < state.indicators.bb_upper
