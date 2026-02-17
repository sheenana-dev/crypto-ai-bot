import os
from dotenv import load_dotenv

load_dotenv()

# --- Exchange ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TESTNET = False  # Set False for live trading

# --- Trading Pairs (USDT-margined futures use :USDT suffix) ---
PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]  # DOGE removed (10% WR dead weight)

# --- Leverage ---
LEVERAGE = 10

# --- Capital Allocation (USDT) ---
TOTAL_CAPITAL = 900  # Reset from 1000 — accumulated losses already absorbed, kill switch protects from here
GRID_CAPITAL = 540        # 60% for grid trading
DCA_RESERVE = 225         # 25% for DCA reserve
EMERGENCY_BUFFER = 90     # 10% emergency buffer
FEE_BUFFER = 45           # 5% fee buffer

# --- Risk Limits ---
MAX_POSITION_PCT = 0.80       # 80% of capital per pair (10x leverage)
MAX_OPEN_ORDERS = 36          # 4 pairs × 6 grids + 4 emergency stops + 8 buffer
DAILY_LOSS_LIMIT_PCT = 0.05   # 5% daily loss limit
KILL_SWITCH_DRAWDOWN = 0.10   # 10% total drawdown kills trading
# Per-position stop loss REMOVED (Feb 16) — grid needs room for round trips
# Protection layers: kill switch (10% drawdown) + daily loss limit (5%) + close-only mode (position bias ≥2x)
EMERGENCY_STOP_PCT = 0.03     # 3% from entry — exchange-side safety net that survives bot crashes

# --- Technical Analysis ---
RSI_PERIOD = 14
EMA_SHORT = 20
EMA_LONG = 50
BB_PERIOD = 20
BB_STD = 2
ADX_PERIOD = 14

# --- Regime Thresholds ---
ADX_TRENDING_THRESHOLD = 25  # Feb 13: raised from 20 (trade more, pause less)
CRASH_DROP_PCT = 0.05         # 5% drop in 24h
CRASH_RSI_THRESHOLD = 30

# --- Rate Limiting ---
MAX_REQUESTS_PER_MINUTE = 100

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Database ---
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database", "trades.db")
