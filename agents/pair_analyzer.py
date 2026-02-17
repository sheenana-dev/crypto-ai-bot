"""Pair Analyzer - Automatically analyzes and selects best pairs for grid trading."""

import json
import logging
import os
import time
from typing import List, Dict, Tuple
import ccxt

logger = logging.getLogger(__name__)

# Active pairs file path (runtime state)
ACTIVE_PAIRS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "active_pairs.json")


def save_active_pairs(pairs: List[str]) -> None:
    """Save active pairs to JSON file for persistence across bot restarts."""
    try:
        with open(ACTIVE_PAIRS_FILE, 'w') as f:
            json.dump({"pairs": pairs, "updated_at": time.time()}, f, indent=2)
        logger.info(f"Saved active pairs: {pairs}")
    except Exception as e:
        logger.error(f"Failed to save active pairs: {e}")


def load_active_pairs(default_pairs: List[str]) -> List[str]:
    """Load active pairs from JSON file, or return default if file doesn't exist."""
    try:
        if os.path.exists(ACTIVE_PAIRS_FILE):
            with open(ACTIVE_PAIRS_FILE, 'r') as f:
                data = json.load(f)
                pairs = data.get("pairs", default_pairs)
                logger.info(f"Loaded active pairs from file: {pairs}")
                return pairs
        else:
            logger.info(f"No active pairs file found, using default: {default_pairs}")
            save_active_pairs(default_pairs)  # Create file with defaults
            return default_pairs
    except Exception as e:
        logger.error(f"Failed to load active pairs, using default: {e}")
        return default_pairs


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
            'POL/USDT:USDT',
            'DOT/USDT:USDT',
        ]

        results = []
        for symbol in candidates:
            try:
                # Fetch 48h of hourly data
                ohlcv = self.exchange.fetch_ohlcv(symbol, '15m', limit=192)  # 15m × 192 = 48 hours
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

    def auto_rotate_pairs(self, current_pairs: List[str], max_pairs: int = 4) -> Tuple[List[str], Dict]:
        """
        Automatically rotate pairs: drop worst performer, add best new candidate.

        Strategy:
        1. Analyze all candidates and score them
        2. Keep current pairs that are still in top performers
        3. If a current pair scores poorly, replace with best new candidate
        4. Return updated pair list + rotation summary

        Args:
            current_pairs: List of currently active pairs
            max_pairs: Maximum number of pairs to trade (default 4)

        Returns:
            (new_pairs, rotation_info): Updated pair list and info dict
        """
        logger.info(f"Auto-rotating pairs: current={current_pairs}, max={max_pairs}")

        # Analyze all candidates
        all_results = self.analyze_candidates(top_n=12)  # Get top 12 for selection

        # Create score map for current pairs
        current_scores = {}
        for result in all_results:
            if result['symbol'] in current_pairs:
                current_scores[result['symbol']] = result['score']

        # Find lowest-scoring current pair (candidate for removal)
        if current_scores:
            worst_pair = min(current_scores.items(), key=lambda x: x[1])
            worst_symbol, worst_score = worst_pair
        else:
            worst_symbol, worst_score = None, 0

        # Find best new candidate (not in current pairs)
        best_new_candidate = None
        for result in all_results:
            if result['symbol'] not in current_pairs:
                best_new_candidate = result
                break

        # Decide whether to rotate
        rotation_info = {
            "rotated": False,
            "removed": None,
            "added": None,
            "reason": None,
        }

        # Rotation logic: only rotate if best new candidate scores significantly higher than worst current
        ROTATION_THRESHOLD = 1.5  # New candidate must score 1.5+ points higher
        if best_new_candidate and worst_symbol:
            score_diff = best_new_candidate['score'] - worst_score
            if score_diff >= ROTATION_THRESHOLD:
                # Perform rotation
                new_pairs = [p for p in current_pairs if p != worst_symbol]
                new_pairs.append(best_new_candidate['symbol'])

                rotation_info = {
                    "rotated": True,
                    "removed": worst_symbol,
                    "removed_score": worst_score,
                    "added": best_new_candidate['symbol'],
                    "added_score": best_new_candidate['score'],
                    "score_diff": score_diff,
                    "reason": f"New candidate scored {score_diff:.2f} points higher"
                }

                logger.info(
                    f"PAIR ROTATION: Removed {worst_symbol} (score {worst_score:.2f}) → "
                    f"Added {best_new_candidate['symbol']} (score {best_new_candidate['score']:.2f})"
                )

                return new_pairs, rotation_info
            else:
                logger.info(
                    f"No rotation: Best new candidate {best_new_candidate['symbol']} "
                    f"(score {best_new_candidate['score']:.2f}) not significantly better than "
                    f"{worst_symbol} (score {worst_score:.2f}, diff={score_diff:.2f})"
                )

        # No rotation needed
        return current_pairs, rotation_info
