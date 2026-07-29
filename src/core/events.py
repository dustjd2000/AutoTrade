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


def format_stock(ticker: str, name: Optional[str] = None) -> str:
    """로그·알림에서 종목을 표기하는 공통 형식 — `(종목코드)종목명`.

    종목명을 모르는 경로(주문 응답 등)에서는 코드만 표기한다.
    """
    return f"({ticker}){name}" if name else f"({ticker})"


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
    # 로그·알림에 종목명을 남기기 위한 참고값 — 주문 전송 본문에는 쓰이지 않는다
    name: Optional[str] = None

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)


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
    name: Optional[str] = None

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)


@dataclass
class FillRecord:
    """당일 체결내역 조회(ka10076) 한 건.

    주문 접수 응답(OrderResult)은 주문번호만 알려주고 체결 여부를 모르므로,
    주문번호를 열쇠로 이 체결 결과를 기존 주문 기록에 덮어써야 손익 집계가 된다.
    """
    order_id: str
    ticker: str
    side: OrderSide
    filled_quantity: int
    filled_price: float
    unfilled_quantity: int = 0
    commission: float = 0.0
    tax: float = 0.0
    name: Optional[str] = None

    @property
    def status(self) -> OrderStatus:
        if self.filled_quantity <= 0:
            return OrderStatus.PENDING
        if self.unfilled_quantity > 0:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.FILLED

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)
