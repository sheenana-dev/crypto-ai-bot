from enum import Enum
from datetime import datetime
from pydantic import BaseModel
from typing import Optional


class MarketRegime(str, Enum):
    RANGING = "RANGING"
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    CRASH = "CRASH"


class Indicators(BaseModel):
    rsi: float
    ema_short: float
    ema_long: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    adx: float
    price_change_24h_pct: float


class MarketState(BaseModel):
    pair: str
    current_price: float
    volume_24h: float
    indicators: Indicators
    regime: MarketRegime
    timestamp: datetime


class SignalType(str, Enum):
    GRID_BUY = "GRID_BUY"
    GRID_SELL = "GRID_SELL"
    DCA_BUY = "DCA_BUY"
    DCA_TAKE_PROFIT = "DCA_TAKE_PROFIT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderSignal(BaseModel):
    pair: str
    side: OrderSide
    price: float
    amount: float
    signal_type: SignalType
    timestamp: datetime


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TradeLog(BaseModel):
    order_id: str
    pair: str
    side: OrderSide
    price: float
    amount: float
    filled: float = 0.0
    fee: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    signal_type: SignalType
    timestamp: datetime
    updated_at: Optional[datetime] = None


class PortfolioSnapshot(BaseModel):
    total_value_usdt: float
    available_balance: float
    unrealized_pnl: float
    realized_pnl: float
    open_orders_count: int
    timestamp: datetime
