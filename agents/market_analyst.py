import logging
from datetime import datetime, timezone

import ccxt
import pandas as pd
import ta

from config import settings
from models.schemas import Indicators, MarketRegime, MarketState

logger = logging.getLogger(__name__)


class MarketAnalyst:
    """Fetches market data and calculates technical indicators to determine market regime."""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    def fetch_ohlcv(self, pair: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candlestick data from the exchange."""
        raw = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def calculate_indicators(self, df: pd.DataFrame) -> dict:
        """Calculate technical indicators on OHLCV data."""
        close = df["close"]
        high = df["high"]
        low = df["low"]

        rsi = ta.momentum.RSIIndicator(close=close, window=settings.RSI_PERIOD).rsi()
        ema_short = ta.trend.EMAIndicator(close=close, window=settings.EMA_SHORT).ema_indicator()
        ema_long = ta.trend.EMAIndicator(close=close, window=settings.EMA_LONG).ema_indicator()
        bb = ta.volatility.BollingerBands(close=close, window=settings.BB_PERIOD, window_dev=settings.BB_STD)
        adx = ta.trend.ADXIndicator(high=high, low=low, close=close, window=settings.ADX_PERIOD).adx()

        latest = len(df) - 1
        price_24h_ago_idx = max(0, latest - 24)  # Approximate: 24 candles back on 1h timeframe
        price_change_24h = (close.iloc[latest] - close.iloc[price_24h_ago_idx]) / close.iloc[price_24h_ago_idx]

        return {
            "rsi": rsi.iloc[latest],
            "ema_short": ema_short.iloc[latest],
            "ema_long": ema_long.iloc[latest],
            "bb_upper": bb.bollinger_hband().iloc[latest],
            "bb_middle": bb.bollinger_mavg().iloc[latest],
            "bb_lower": bb.bollinger_lband().iloc[latest],
            "adx": adx.iloc[latest],
            "price_change_24h_pct": price_change_24h,
            "current_price": close.iloc[latest],
        }

    def determine_regime(self, ind: dict) -> MarketRegime:
        """Classify the current market regime based on indicators."""
        price = ind["current_price"]
        adx = ind["adx"]
        rsi = ind["rsi"]
        price_change = ind["price_change_24h_pct"]

        # CRASH takes priority: big drop + oversold
        if price_change <= -settings.CRASH_DROP_PCT and rsi <= settings.CRASH_RSI_THRESHOLD:
            return MarketRegime.CRASH

        # TRENDING: ADX above threshold
        if adx >= settings.ADX_TRENDING_THRESHOLD:
            if ind["ema_short"] > ind["ema_long"]:
                return MarketRegime.TRENDING_UP
            else:
                return MarketRegime.TRENDING_DOWN

        # RANGING: ADX below threshold and price within Bollinger Bands
        if ind["bb_lower"] <= price <= ind["bb_upper"]:
            return MarketRegime.RANGING

        # Default to ranging if nothing else matches
        return MarketRegime.RANGING

    def analyze(self, pair: str) -> MarketState:
        """Run full analysis for a trading pair. Returns a MarketState object."""
        logger.info(f"Analyzing {pair}...")

        df = self.fetch_ohlcv(pair, timeframe="1h", limit=100)
        ind = self.calculate_indicators(df)
        regime = self.determine_regime(ind)

        ticker = self.exchange.fetch_ticker(pair)
        volume_24h = ticker.get("quoteVolume", 0.0) or 0.0

        indicators = Indicators(
            rsi=round(ind["rsi"], 2),
            ema_short=round(ind["ema_short"], 2),
            ema_long=round(ind["ema_long"], 2),
            bb_upper=round(ind["bb_upper"], 2),
            bb_middle=round(ind["bb_middle"], 2),
            bb_lower=round(ind["bb_lower"], 2),
            adx=round(ind["adx"], 2),
            price_change_24h_pct=round(ind["price_change_24h_pct"], 4),
        )

        market_state = MarketState(
            pair=pair,
            current_price=round(ind["current_price"], 2),
            volume_24h=round(volume_24h, 2),
            indicators=indicators,
            regime=regime,
            timestamp=datetime.now(timezone.utc),
        )

        logger.info(f"{pair} | Price: {market_state.current_price} | Regime: {regime.value}")
        return market_state
