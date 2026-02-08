import os
from dotenv import load_dotenv

load_dotenv()

# --- Exchange ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TESTNET = True  # Set False for live trading

# --- Trading Pairs (USDT-margined futures use :USDT suffix) ---
PAIRS = ["BTC/USDT:USDT"]

# --- Leverage ---
LEVERAGE = 10

# --- Capital Allocation (USDT) ---
TOTAL_CAPITAL = 1000
GRID_CAPITAL = 600
DCA_RESERVE = 250
EMERGENCY_BUFFER = 100
FEE_BUFFER = 50

# --- Risk Limits ---
MAX_POSITION_PCT = 0.50       # 50% of capital per pair (10x leverage)
MAX_OPEN_ORDERS = 10
DAILY_LOSS_LIMIT_PCT = 0.03   # 3% daily loss limit
KILL_SWITCH_DRAWDOWN = 0.10   # 10% total drawdown kills trading

# --- Technical Analysis ---
RSI_PERIOD = 14
EMA_SHORT = 20
EMA_LONG = 50
BB_PERIOD = 20
BB_STD = 2
ADX_PERIOD = 14

# --- Regime Thresholds ---
ADX_TRENDING_THRESHOLD = 25
CRASH_DROP_PCT = 0.05         # 5% drop in 24h
CRASH_RSI_THRESHOLD = 30

# --- Rate Limiting ---
MAX_REQUESTS_PER_MINUTE = 100

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Database ---
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database", "trades.db")
