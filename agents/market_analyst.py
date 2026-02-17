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

    def fetch_ohlcv(self, pair: str, timeframe: str = "15m", limit: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candlestick data from the exchange."""
        raw = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def calculate_indicators(self, df: pd.DataFrame, timeframe: str = "15m") -> dict:
        """Calculate technical indicators on OHLCV data."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        rsi = ta.momentum.RSIIndicator(close=close, window=settings.RSI_PERIOD).rsi()
        ema_short = ta.trend.EMAIndicator(close=close, window=settings.EMA_SHORT).ema_indicator()
        ema_long = ta.trend.EMAIndicator(close=close, window=settings.EMA_LONG).ema_indicator()
        bb = ta.volatility.BollingerBands(close=close, window=settings.BB_PERIOD, window_dev=settings.BB_STD)
        adx = ta.trend.ADXIndicator(high=high, low=low, close=close, window=settings.ADX_PERIOD).adx()

        # Volume ratio: fast(5) / slow(20) MA — detects volume spikes vs dead markets
        vol_fast = volume.rolling(5).mean()
        vol_slow = volume.rolling(20).mean()
        vol_ratio_series = vol_fast / vol_slow.replace(0, float('nan'))

        latest = len(df) - 1
        vol_ratio = vol_ratio_series.iloc[latest]
        if pd.isna(vol_ratio):
            vol_ratio = 1.0

        # Calculate 24h lookback based on timeframe (15m = 96 candles, 1h = 24 candles)
        candles_per_24h = {"15m": 96, "1h": 24, "5m": 288, "30m": 48}
        lookback = candles_per_24h.get(timeframe, 24)
        price_24h_ago_idx = max(0, latest - lookback)
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
            "volume_ratio": round(vol_ratio, 2),
        }

    def determine_regime(self, ind: dict) -> tuple:
        """Classify market regime and confidence (0.0-1.0) based on multi-indicator agreement.

        Confidence = how many of 4 indicators agree on the regime:
          ADX strength, BB position, Volume ratio, RSI context
        High confidence (0.75-1.0) = act normally
        Low confidence (< 0.5) = conservative mode (wider spacing)

        Returns: (MarketRegime, confidence: float)
        """
        price = ind["current_price"]
        adx = ind["adx"]
        rsi = ind["rsi"]
        price_change = ind["price_change_24h_pct"]
        vol_ratio = ind.get("volume_ratio", 1.0)

        # CRASH takes priority: strict criteria = high confidence by definition
        if price_change <= -settings.CRASH_DROP_PCT and rsi <= settings.CRASH_RSI_THRESHOLD:
            return MarketRegime.CRASH, 1.0

        # TRENDING: ADX above threshold
        if adx >= settings.ADX_TRENDING_THRESHOLD:
            regime = MarketRegime.TRENDING_UP if ind["ema_short"] > ind["ema_long"] else MarketRegime.TRENDING_DOWN
            agreeing = 1  # ADX confirms trending
            if adx > 30:
                agreeing += 1  # Strongly trending (not borderline 25-30)
            if vol_ratio > 1.5:
                agreeing += 1  # Volume spike confirms trend
            if (regime == MarketRegime.TRENDING_UP and rsi > 55) or \
               (regime == MarketRegime.TRENDING_DOWN and rsi < 45):
                agreeing += 1  # RSI confirms direction
            return regime, round(agreeing / 4, 2)

        # RANGING: ADX below threshold
        agreeing = 1  # ADX confirms ranging (< threshold)
        if adx < 20:
            agreeing += 1  # Strongly ranging (not borderline 20-25)
        if vol_ratio < 1.5:
            agreeing += 1  # No volume spike = calm market
        if 40 < rsi < 60:
            agreeing += 1  # RSI neutral = range-bound
        return MarketRegime.RANGING, round(agreeing / 4, 2)

    def analyze(self, pair: str) -> MarketState:
        """Run full analysis for a trading pair. Returns a MarketState object."""
        logger.info(f"Analyzing {pair}...")

        timeframe = "15m"  # 15-minute candles for faster regime detection (updates every 15 min vs 1h)
        df = self.fetch_ohlcv(pair, timeframe=timeframe, limit=100)
        ind = self.calculate_indicators(df, timeframe=timeframe)
        regime, confidence = self.determine_regime(ind)

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
            volume_ratio=ind.get("volume_ratio", 1.0),
        )

        market_state = MarketState(
            pair=pair,
            current_price=round(ind["current_price"], 2),
            volume_24h=round(volume_24h, 2),
            indicators=indicators,
            regime=regime,
            regime_confidence=confidence,
            timestamp=datetime.now(timezone.utc),
        )

        logger.info(
            f"{pair} | Price: {market_state.current_price} | Regime: {regime.value} "
            f"| Confidence: {confidence:.0%} | Vol ratio: {ind.get('volume_ratio', 1.0):.2f}"
        )
        return market_state
