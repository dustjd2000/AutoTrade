from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


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


class BuyOutcome(Enum):
    """09:00 매수 한 종목의 결과.

    접수·부분체결·체결은 주문이 살아 있는 상태, 건너뜀·실패는 매수하지 못한 상태다.
    """
    ORDERED = "ordered"                     # 접수됨 — 체결 여부는 아직 확인되지 않았다
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    SKIPPED = "skipped"                     # 1주 가격이 종목당 배정액을 초과
    FAILED = "failed"                       # 리스크 관리 거부 / 주문 거부 / 처리 중 오류

    @property
    def is_ordered(self) -> bool:
        return self in (BuyOutcome.ORDERED, BuyOutcome.PARTIALLY_FILLED, BuyOutcome.FILLED)


@dataclass
class BuyRecord:
    """09:00 매수 실행 결과 한 종목 — 매수 알림 메일의 표 한 줄."""
    ticker: str
    name: Optional[str] = None
    outcome: BuyOutcome = BuyOutcome.ORDERED
    quantity: int = 0                 # 주문 수량
    reference_price: float = 0.0      # 수량 산정에 쓴 현재가 — 체결가를 모를 때의 표기 기준
    filled_quantity: int = 0
    filled_price: Optional[float] = None
    order_id: Optional[str] = None
    note: Optional[str] = None        # 건너뜀·실패 사유

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)

    @property
    def price(self) -> float:
        """표기 단가 — 체결가를 알면 체결가, 아니면 산정 기준가."""
        return self.filled_price or self.reference_price

    @property
    def shares(self) -> int:
        return self.filled_quantity or self.quantity

    @property
    def amount(self) -> float:
        return self.price * self.shares


@dataclass
class BuyExecution:
    """09:00 매수 실행 전체 결과 — 매수 알림 메일의 원본 데이터.

    주문 접수 직후에 만들어지므로 체결가가 비어 있을 수 있다(접수 상태). 체결가·수수료·
    손익의 최종 확정은 15:30 리포트가 담당한다.
    """
    at: datetime
    cash: float                       # 매수 산정에 쓴 예수금
    amount_per_stock: float           # 종목당 배정액
    records: List[BuyRecord] = field(default_factory=list)
    take_profit_percent: float = 0.0
    stop_loss_percent: float = 0.0
    fills_synced: bool = True         # False면 체결 조회에 실패해 접수 기준으로 집계했다

    @property
    def ordered(self) -> List[BuyRecord]:
        return [r for r in self.records if r.outcome.is_ordered]

    @property
    def not_bought(self) -> List[BuyRecord]:
        return [r for r in self.records if not r.outcome.is_ordered]

    @property
    def invested(self) -> float:
        return sum(r.amount for r in self.ordered)


@dataclass(frozen=True)
class UnsellableView:
    """매도하지 못한 종목과 그 사유 — UI '매도 불가' 표와 리포트 메일이 함께 쓴다.

    사유를 로그로만 남기면 보유 목록에서 제외된 종목(TradingEngine._exclude_untradable)이
    보유 종목 표에서도 사라져 계좌에 남아 있다는 사실이 아무 데도 보이지 않는다.
    """

    ticker: str
    label: str
    reason: str
    at: datetime
    # True면 보유 목록에서 제외된 건 (상장폐지·거래정지 추정) — 자동 청산되지 않는다
    excluded: bool = False


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
