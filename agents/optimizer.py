"""Agent Reviewer/Optimizer - Meta-agent that analyzes performance and evolves the system.

This agent:
- Analyzes completed trades (wins/losses)
- Identifies patterns (time, regime, pair performance)
- Suggests parameter optimizations
- Generates weekly performance reports
- Feeds improvements back to the strategy
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple
import statistics

from database.db import get_connection

logger = logging.getLogger(__name__)


class OptimizerAgent:
    """Meta-agent that reviews performance and suggests optimizations."""

    def __init__(self):
        pass

    def analyze_performance(self, days: int = 7) -> Dict:
        """
        Analyze trading performance over the past N days.

        Returns:
            Dictionary with performance metrics and insights
        """
        conn = get_connection()
        cursor = conn.cursor()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Get all trades in the period
        cursor.execute("""
            SELECT pair, side, entry_price, exit_price, pnl, entry_time, exit_time
            FROM trades
            WHERE exit_time IS NOT NULL
            AND exit_time >= ?
        """, (cutoff,))

        trades = cursor.fetchall()
        conn.close()

        if not trades:
            return {"error": "No completed trades in period"}

        # Overall metrics
        total_trades = len(trades)
        winning_trades = [t for t in trades if t[4] > 0]  # pnl > 0
        losing_trades = [t for t in trades if t[4] < 0]

        win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0

        total_profit = sum(t[4] for t in winning_trades)
        total_loss = abs(sum(t[4] for t in losing_trades))
        net_pnl = total_profit - total_loss

        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

        # Average trade metrics
        avg_win = total_profit / len(winning_trades) if winning_trades else 0
        avg_loss = total_loss / len(losing_trades) if losing_trades else 0

        # Analyze by pair
        pair_performance = self._analyze_by_pair(trades)

        # Analyze by time of day (session)
        session_performance = self._analyze_by_session(trades)

        # Analyze by trade duration
        duration_analysis = self._analyze_duration(trades)

        return {
            "period_days": days,
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "total_profit": total_profit,
            "total_loss": total_loss,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "pair_performance": pair_performance,
            "session_performance": session_performance,
            "duration_analysis": duration_analysis,
        }

    def _analyze_by_pair(self, trades: List[Tuple]) -> Dict:
        """Analyze performance by trading pair."""
        pair_stats = {}

        for trade in trades:
            pair = trade[0]
            pnl = trade[4]

            if pair not in pair_stats:
                pair_stats[pair] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0,
                    "profit": 0,
                    "loss": 0,
                }

            pair_stats[pair]["trades"] += 1
            pair_stats[pair]["total_pnl"] += pnl

            if pnl > 0:
                pair_stats[pair]["wins"] += 1
                pair_stats[pair]["profit"] += pnl
            else:
                pair_stats[pair]["losses"] += 1
                pair_stats[pair]["loss"] += abs(pnl)

        # Calculate win rate and profit factor for each pair
        for pair, stats in pair_stats.items():
            stats["win_rate"] = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0
            stats["profit_factor"] = (
                stats["profit"] / stats["loss"] if stats["loss"] > 0 else float('inf')
            )

        return pair_stats

    def _analyze_by_session(self, trades: List[Tuple]) -> Dict:
        """
        Analyze performance by trading session.

        Sessions (UTC):
        - Asian: 00:00-08:00
        - European: 08:00-16:00
        - American: 16:00-24:00
        """
        session_stats = {
            "Asian": {"trades": 0, "wins": 0, "pnl": 0},
            "European": {"trades": 0, "wins": 0, "pnl": 0},
            "American": {"trades": 0, "wins": 0, "pnl": 0},
        }

        for trade in trades:
            entry_time = trade[5]  # entry_time
            pnl = trade[4]

            # Parse timestamp
            if isinstance(entry_time, str):
                dt = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
            else:
                dt = entry_time

            hour = dt.hour

            # Determine session
            if 0 <= hour < 8:
                session = "Asian"
            elif 8 <= hour < 16:
                session = "European"
            else:
                session = "American"

            session_stats[session]["trades"] += 1
            session_stats[session]["pnl"] += pnl
            if pnl > 0:
                session_stats[session]["wins"] += 1

        # Calculate win rates
        for session, stats in session_stats.items():
            if stats["trades"] > 0:
                stats["win_rate"] = stats["wins"] / stats["trades"]
            else:
                stats["win_rate"] = 0

        return session_stats

    def _analyze_duration(self, trades: List[Tuple]) -> Dict:
        """Analyze trade duration patterns."""
        durations = []

        for trade in trades:
            entry_time = trade[5]
            exit_time = trade[6]
            pnl = trade[4]

            if entry_time and exit_time:
                if isinstance(entry_time, str):
                    entry_dt = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                    exit_dt = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
                else:
                    entry_dt = entry_time
                    exit_dt = exit_time

                duration_hours = (exit_dt - entry_dt).total_seconds() / 3600
                durations.append({
                    "hours": duration_hours,
                    "pnl": pnl,
                    "profitable": pnl > 0
                })

        if not durations:
            return {"error": "No duration data"}

        avg_duration = statistics.mean([d["hours"] for d in durations])
        winning_durations = [d["hours"] for d in durations if d["profitable"]]
        losing_durations = [d["hours"] for d in durations if not d["profitable"]]

        return {
            "avg_duration_hours": avg_duration,
            "avg_winning_duration": statistics.mean(winning_durations) if winning_durations else 0,
            "avg_losing_duration": statistics.mean(losing_durations) if losing_durations else 0,
        }

    def generate_recommendations(self, performance: Dict) -> List[str]:
        """
        Generate actionable recommendations based on performance analysis.

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Check overall win rate
        win_rate = performance.get("win_rate", 0)
        if win_rate < 0.45:
            recommendations.append(
                f"⚠️ Low win rate ({win_rate*100:.1f}%). Consider widening grid spacing "
                "or reducing number of pairs to focus on best performers."
            )
        elif win_rate > 0.65:
            recommendations.append(
                f"✅ Excellent win rate ({win_rate*100:.1f}%)! System is performing well."
            )

        # Check profit factor
        profit_factor = performance.get("profit_factor", 0)
        if profit_factor < 1.5:
            recommendations.append(
                f"⚠️ Low profit factor ({profit_factor:.2f}). Winning trades aren't "
                "covering losses adequately. Consider tightening stop-loss or widening "
                "grid spacing for larger profits per trade."
            )

        # Analyze pair performance
        pair_perf = performance.get("pair_performance", {})
        if pair_perf:
            # Find best and worst pairs
            sorted_pairs = sorted(
                pair_perf.items(),
                key=lambda x: x[1].get("profit_factor", 0),
                reverse=True
            )

            if len(sorted_pairs) >= 2:
                best_pair = sorted_pairs[0]
                worst_pair = sorted_pairs[-1]

                recommendations.append(
                    f"🏆 Best performer: {best_pair[0]} "
                    f"(PF={best_pair[1]['profit_factor']:.2f}, "
                    f"WR={best_pair[1]['win_rate']*100:.1f}%)"
                )

                if worst_pair[1]["profit_factor"] < 1.0:
                    recommendations.append(
                        f"❌ Worst performer: {worst_pair[0]} "
                        f"(PF={worst_pair[1]['profit_factor']:.2f}, "
                        f"WR={worst_pair[1]['win_rate']*100:.1f}%). "
                        "Consider removing this pair or adjusting its grid parameters."
                    )

        # Analyze session performance
        session_perf = performance.get("session_performance", {})
        if session_perf:
            worst_session = min(
                session_perf.items(),
                key=lambda x: x[1].get("win_rate", 0)
            )

            if worst_session[1]["trades"] > 5 and worst_session[1]["win_rate"] < 0.40:
                recommendations.append(
                    f"⏰ {worst_session[0]} session has low win rate "
                    f"({worst_session[1]['win_rate']*100:.1f}%). "
                    f"Consider reducing trading activity during this period."
                )

        # Check average loss vs average win
        avg_win = performance.get("avg_win", 0)
        avg_loss = abs(performance.get("avg_loss", 0))

        if avg_loss > avg_win * 0.8:
            recommendations.append(
                f"⚠️ Average loss (${avg_loss:.2f}) is close to average win (${avg_win:.2f}). "
                "Consider tightening stop-loss or widening grid spacing to improve risk/reward."
            )

        return recommendations

    def generate_daily_report(self) -> str:
        """Generate quick daily performance summary with key insights."""
        performance = self.analyze_performance(days=1)

        if "error" in performance:
            return "📊 *Daily Performance*\n\nNo completed trades today."

        report = "📊 *DAILY PERFORMANCE*\n"
        report += "=" * 25 + "\n\n"

        # Key metrics
        win_rate = performance['win_rate']
        net_pnl = performance['net_pnl']
        total_trades = performance['total_trades']

        # Emoji based on performance
        if net_pnl > 0:
            emoji = "✅" if win_rate >= 0.5 else "⚠️"
        else:
            emoji = "❌"

        report += f"{emoji} *Today's Results:*\n"
        report += f"• Trades: {total_trades}\n"
        report += f"• Win Rate: {win_rate*100:.1f}%\n"
        report += f"• Net P&L: ${net_pnl:.2f}\n"
        report += f"• Profit Factor: {performance['profit_factor']:.2f}\n\n"

        # Top performing pair today
        pair_perf = performance.get("pair_performance", {})
        if pair_perf:
            best_pair = max(pair_perf.items(), key=lambda x: x[1]["total_pnl"])
            worst_pair = min(pair_perf.items(), key=lambda x: x[1]["total_pnl"])

            report += "*Pair Performance:*\n"
            report += f"🏆 Best: {best_pair[0].split('/')[0]} (${best_pair[1]['total_pnl']:.2f})\n"
            if worst_pair[1]["total_pnl"] < 0:
                report += f"❌ Worst: {worst_pair[0].split('/')[0]} (${worst_pair[1]['total_pnl']:.2f})\n"

        # Quick insight
        report += "\n*Quick Insight:*\n"
        if win_rate >= 0.6:
            report += "✅ Strong performance today!\n"
        elif win_rate >= 0.5:
            report += "✅ Solid performance, keep it up.\n"
        elif win_rate >= 0.4:
            report += "⚠️ Below target, monitor closely.\n"
        else:
            report += "❌ Weak day, review strategy.\n"

        return report

    def generate_weekly_report(self) -> str:
        """Generate comprehensive weekly performance report."""
        performance = self.analyze_performance(days=7)

        if "error" in performance:
            return "❌ No trades to analyze this week."

        report = "📊 *WEEKLY PERFORMANCE REPORT*\n"
        report += "=" * 35 + "\n\n"

        # Overall metrics
        report += "*Overall Performance:*\n"
        report += f"• Total Trades: {performance['total_trades']}\n"
        report += f"• Win Rate: {performance['win_rate']*100:.1f}%\n"
        report += f"• Net P&L: ${performance['net_pnl']:.2f}\n"
        report += f"• Profit Factor: {performance['profit_factor']:.2f}\n"
        report += f"• Avg Win: ${performance['avg_win']:.2f}\n"
        report += f"• Avg Loss: ${performance['avg_loss']:.2f}\n\n"

        # Pair performance
        report += "*Performance by Pair:*\n"
        pair_perf = performance.get("pair_performance", {})
        for pair, stats in sorted(
            pair_perf.items(),
            key=lambda x: x[1]["total_pnl"],
            reverse=True
        ):
            report += f"• {pair.split('/')[0]}: "
            report += f"${stats['total_pnl']:.2f} "
            report += f"({stats['win_rate']*100:.0f}% WR, "
            report += f"PF={stats['profit_factor']:.2f})\n"

        report += "\n"

        # Session analysis
        report += "*Performance by Session:*\n"
        session_perf = performance.get("session_performance", {})
        for session, stats in session_perf.items():
            if stats["trades"] > 0:
                report += f"• {session}: "
                report += f"{stats['trades']} trades, "
                report += f"{stats['win_rate']*100:.0f}% WR, "
                report += f"${stats['pnl']:.2f}\n"

        report += "\n"

        # Recommendations
        recommendations = self.generate_recommendations(performance)
        if recommendations:
            report += "*Recommendations:*\n"
            for i, rec in enumerate(recommendations, 1):
                report += f"{i}. {rec}\n"

        return report
