"""Pair Analyzer - Automatically analyzes and selects best pairs for grid trading."""

import logging
import time
from typing import List, Dict
import ccxt

logger = logging.getLogger(__name__)


class PairAnalyzer:
    """Analyzes crypto pairs to find the best candidates for grid trading."""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    def analyze_candidates(self, top_n: int = 5) -> List[Dict]:
        """
        Analyze candidate pairs and return top N by grid trading potential.

        Scoring criteria:
        1. Volatility (higher is better for grid)
        2. Volume (higher means better liquidity)
        3. Established coins (prefer top market cap)

        Returns:
            List of dicts with symbol, volatility, volume, score
        """
        # Candidate pairs - established coins with good liquidity
        candidates = [
            'BTC/USDT:USDT',
            'ETH/USDT:USDT',
            'SOL/USDT:USDT',
            'XRP/USDT:USDT',
            'DOGE/USDT:USDT',
            'AVAX/USDT:USDT',
            'LINK/USDT:USDT',
            'ARB/USDT:USDT',
            'OP/USDT:USDT',
            'ADA/USDT:USDT',
            'MATIC/USDT:USDT',
            'DOT/USDT:USDT',
        ]

        results = []
        for symbol in candidates:
            try:
                # Fetch 48h of hourly data
                ohlcv = self.exchange.fetch_ohlcv(symbol, '1h', limit=48)
                prices = [x[4] for x in ohlcv]  # Close prices

                # Calculate volatility (48h range as % of price)
                high_48h = max(prices)
                low_48h = min(prices)
                current = prices[-1]
                volatility_pct = ((high_48h - low_48h) / current) * 100

                # Get 24h volume
                ticker = self.exchange.fetch_ticker(symbol)
                volume_24h = ticker.get('quoteVolume', 0)

                # Calculate grid trading score
                # Volatility weight: 60% (more volatility = more grid opportunities)
                # Volume weight: 40% (higher volume = better fills)
                volatility_score = min(volatility_pct / 10, 10)  # Cap at 10%
                volume_score = min(volume_24h / 100_000_000, 10)  # $100M = max score

                score = (volatility_score * 0.6) + (volume_score * 0.4)

                results.append({
                    'symbol': symbol,
                    'price': current,
                    'volatility': volatility_pct,
                    'volume': volume_24h,
                    'score': score,
                })

                logger.info(
                    f"{symbol}: vol={volatility_pct:.2f}%, "
                    f"vol24h=${volume_24h/1e6:.1f}M, score={score:.2f}"
                )

                time.sleep(0.2)  # Rate limit

            except Exception as e:
                logger.warning(f"Failed to analyze {symbol}: {e}")
                continue

        # Sort by score (highest first)
        results.sort(key=lambda x: x['score'], reverse=True)

        return results[:top_n]

    def recommend_grid_spacing(self, volatility_pct: float) -> float:
        """
        Recommend grid spacing based on volatility.

        Args:
            volatility_pct: 48h volatility as percentage

        Returns:
            Recommended grid spacing as decimal (e.g., 0.005 for 0.5%)
        """
        # Higher volatility -> wider spacing
        if volatility_pct >= 8:
            return 0.008  # 0.8%
        elif volatility_pct >= 6:
            return 0.007  # 0.7%
        elif volatility_pct >= 5:
            return 0.006  # 0.6%
        elif volatility_pct >= 4:
            return 0.005  # 0.5%
        elif volatility_pct >= 3:
            return 0.004  # 0.4%
        else:
            return 0.003  # 0.3%
