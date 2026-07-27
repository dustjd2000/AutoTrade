from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class Signal(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ExitReason(Enum):
    """전략과 무관하게 시스템이 강제 청산하는 사유."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


@dataclass
class MarketData:
    ticker: str
    price: float
    volume: int
    timestamp: datetime = field(default_factory=datetime.now)
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass
class OrderRequest:
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None  # None이면 시장가


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    side: OrderSide
    status: OrderStatus
    quantity: int
    filled_quantity: int = 0
    filled_price: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
