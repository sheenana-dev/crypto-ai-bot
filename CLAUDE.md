## Agent Pipeline
MarketAnalyst → PairAnalyzer → Strategy → RiskManager → Executor → Notifier
Portfolio tracks positions. Optimizer tunes parameters. Telegram for alerts.

## Project Structure
- /agents/ — executor.py, market_analyst.py, notifier.py, optimizer.py, pair_analyzer.py, portfolio.py, risk_manager.py, strategy.py, telegram_handler.py
- /config/ — settings.py (API keys, pairs, capital), grid_config.py (per-pair grid params)
- /database/ — db.py + trades.db (SQLite)
- /models/ — schemas.py (Pydantic models)

## Commands
- python scheduler.py — start the bot (runs every 3 min)
- python check_account.py — check Binance account balance/positions
- python main.py — single cycle test (legacy, for debugging)
- python -m pytest — run tests

## When Improving Strategy
1. Read strategy.py AND risk_manager.py first
2. Show the math and expected impact BEFORE coding
3. Write a test that validates the improvement
4. Never weaken RiskManager to make a strategy work
5. Compare backtest results before/after

## Continuous Improvement Rules
- After every code change, check if it breaks existing agent communication flows
- When adding a new strategy, always include backtesting logic alongside it
- Log every trade decision with reasoning — this data feeds future optimization
- Track win rate, Sharpe ratio, max drawdown per strategy and suggest improvements when patterns emerge
- When refactoring, preserve all existing risk management guardrails — never weaken safety checks
- Suggest performance optimizations or architectural improvements when you notice repeated patterns or bottlenecks

## Critical Rules
- Decimal for all price/quantity — never float
- All orders through risk_manager.py — no bypass
- No API keys in code — use config/
- Do not modify trades.db schema without migration plan
- Keep Telegram alerts working
- Profitable bot = don't touch what's working
- If the bot is profitable, default answer to "should we change X?" is "don't touch what's working."

## Active Development
Modified: executor.py, risk_manager.py, strategy.py
New/untracked: optimizer.py, pair_analyzer.py, telegram_handler.py
These may have incomplete integration — be careful.

## Proactive Improvement Mode
- When touching any agent, check if its input/output schema in schemas.py is still accurate
- If any agent lacks proper error handling or logging, flag it and fix it in the same session
- When a pattern repeats across 2+ agents, suggest extracting it into a shared utility
- After fixing a bug, add a comment explaining WHY it broke — future context matters
- If optimizer.py or pair_analyzer.py are referenced but incomplete, remind me to finish integration before moving on
- When reviewing strategy performance data, suggest parameter tweaks with expected impact

## Learning Log (append as we go)
<!-- Add one-liners here when we discover something useful -->
- GTX rejects on spread cross = expected, not a bug
- DOGE: whole numbers only, 5 decimal price precision
- Grid spacing < 0.3% = dangerous in trends
## Known Issues
- GTX (post-only) orders will reject if they'd cross the spread — this is expected behavior to ensure maker fees
- Grid spacing too tight (<0.3%) causes position accumulation in trending markets
- DOGE needs 5 decimals for price, XRP needs 4 — use exchange.price_to_precision()
- DOGE amount must be whole numbers (precision=1)