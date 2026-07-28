import logging
from typing import Dict, Optional

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        max_position_ratio: float = 0.1,      # 종목당 최대 계좌 비중
        max_daily_loss_ratio: float = 0.02,   # 일일 최대 손실 비중
        take_profit_ratio: float = 0.02,      # 익절 라인 (매수 대비 수익률)
        stop_loss_ratio: float = 0.02,        # 손절 라인 (매수 대비 손실률)
        max_total_exposure_ratio: float = 0.7,  # 전체 계좌 대비 최대 노출 비중
    ):
        self.max_position_ratio = max_position_ratio
        self.max_daily_loss_ratio = max_daily_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        self.stop_loss_ratio = stop_loss_ratio
        self.max_total_exposure_ratio = max_total_exposure_ratio

        self._initial_asset: float = 0.0
        self._daily_realized_loss: float = 0.0
        self._halted: bool = False

    def initialize(self, snapshot: BalanceSnapshot) -> None:
        self._initial_asset = snapshot.total_asset
        self._daily_realized_loss = 0.0
        self._halted = False
        logger.info("RiskManager initialized. Total asset: %.0f", self._initial_asset)

    def approve(
        self,
        request: OrderRequest,
        positions: Dict[str, Position],
        reference_price: float = 0.0,
    ) -> bool:
        if self._halted:
            logger.warning("Trading halted. Order rejected: %s", request.ticker)
            return False

        if request.side == OrderSide.BUY:
            if self._is_daily_loss_exceeded():
                logger.warning("Daily loss limit reached. Halting new buys.")
                self._halted = True
                return False
            if self._is_exposure_exceeded(request, positions, reference_price):
                logger.warning("Max total exposure ratio exceeded. Order rejected: %s", request.ticker)
                return False
        return True

    def check_exit(self, position: Position) -> Optional[ExitReason]:
        """보유 종목이 익절/손절 라인에 도달했는지 확인한다.

        키움 REST API에 조건부 예약주문(스탑오더) 엔드포인트가 확인되지 않아, 이 실시간
        모니터링이 **1차이자 사실상 유일한 청산 수단**이다 (PRD 5.5-B, 2026-07-27 확정).
        즉 익절/손절은 증권사 서버가 아니라 이 프로그램이 떠 있는 동안에만 동작한다 —
        앱이 꺼지거나 WebSocket이 끊기면 감시 공백이 생긴다.
        """
        if position.avg_price <= 0 or position.quantity <= 0:
            return None

        ret = (position.current_price - position.avg_price) / position.avg_price
        if ret >= self.take_profit_ratio:
            return ExitReason.TAKE_PROFIT
        if ret <= -self.stop_loss_ratio:
            return ExitReason.STOP_LOSS
        return None

    def record_order(self, result: OrderResult, avg_price: Optional[float] = None) -> None:
        if (
            result.side == OrderSide.SELL
            and result.status == OrderStatus.FILLED
            and result.filled_price is not None
            and avg_price is not None
        ):
            pnl = (result.filled_price - avg_price) * result.filled_quantity
            if pnl < 0:
                self._daily_realized_loss += -pnl
            logger.info("Realized PnL for %s: %.0f", result.ticker, pnl)

    def calc_buy_quantity(self, ticker: str, price: float) -> int:
        max_amount = self._initial_asset * self.max_position_ratio
        return int(max_amount // price)

    def get_holding_quantity(self, ticker: str, positions: Dict[str, Position]) -> int:
        position = positions.get(ticker)
        return position.quantity if position else 0

    def _is_daily_loss_exceeded(self) -> bool:
        limit = self._initial_asset * self.max_daily_loss_ratio
        return self._daily_realized_loss >= limit

    def _is_exposure_exceeded(
        self, request: OrderRequest, positions: Dict[str, Position], reference_price: float
    ) -> bool:
        """전체 계좌 대비 노출 비중이 한도를 넘는지 확인한다 (PRD 5.6).

        전략 로직 버그로 과도한 금액이 계산되는 경우를 잡아내는 상위 안전장치이며,
        시장가 주문의 실제 체결가를 알 수 없으므로 신호 시점의 참조가(reference_price)로 근사한다.
        """
        current_exposure = sum(p.quantity * p.current_price for p in positions.values())
        unit_price = request.price if request.price is not None else reference_price
        order_value = request.quantity * unit_price
        limit = self._initial_asset * self.max_total_exposure_ratio
        return (current_exposure + order_value) > limit
