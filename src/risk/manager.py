import logging
from typing import Dict, Iterable, Optional

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus

logger = logging.getLogger(__name__)


def net_return(
    current_price: float,
    avg_price: float,
    commission_rate: float,
    tax_rate: float,
    slippage_rate: float,
) -> float:
    """평단가 대비 순손익률 — 왕복 수수료·매도세금·슬리피지를 뺀 실수령 기준.

    슬리피지는 매도 체결가가 현재가보다 불리하게 밀리는 정도로 가정한다(항상 불리한 방향).
    """
    sell_price = current_price * (1 - slippage_rate)
    return sell_price * (1 - commission_rate - tax_rate) / avg_price - (1 + commission_rate)


def exit_trigger_price(
    avg_price: float,
    target_ratio: float,
    commission_rate: float,
    tax_rate: float,
    slippage_rate: float,
) -> float:
    """순손익률이 target_ratio에 도달하는 현재가 — net_return의 역함수 (표시용).

    익절/손절은 보유 종목 합산으로 판정하므로(PRD 5.5-B) 이 가격에 닿아도 그 종목만
    팔리지는 않는다. "이 종목 혼자였다면 조건에 닿는 가격"이라는 참고값이다.
    """
    return (
        avg_price
        * (target_ratio + 1 + commission_rate)
        / ((1 - slippage_rate) * (1 - commission_rate - tax_rate))
    )


def portfolio_net_return(
    positions: Iterable[Position],
    commission_rate: float,
    tax_rate: float,
    slippage_rate: float,
) -> Optional[float]:
    """보유 종목 전체를 합산한 순손익률 (PRD 5.5-B "매도(청산) 조건", 확정 2026-08-10).

        (전체 예상 매도수령액 - 전체 매입원가) / 전체 매입금액

    매입금액으로 가중한 평균이므로 "보유 종목 손익을 전부 더해 투입 총액으로 나눈 값"과
    같다. 종목 개수로 나누는 단순평균이 아니다 — 그러면 투입금액이 다른 종목을 같은 비중으로
    취급해 실제 계좌 손익과 어긋난다. 종목이 하나뿐이면 net_return과 같은 값이 나온다.

    평단·수량·현재가 중 하나라도 0인 종목은 계산에서 뺀다. 특히 현재가 0은 조회 실패나 장 전
    상태인데, 그대로 넣으면 -100%로 잡혀 합산이 즉시 손절선을 넘는다.
    계산에 넣을 종목이 하나도 없으면 None — '판정하지 않는다'는 뜻이다.
    """
    cost = 0.0
    market_value = 0.0
    for position in positions:
        if position.avg_price <= 0 or position.quantity <= 0 or position.current_price <= 0:
            continue
        cost += position.avg_price * position.quantity
        market_value += position.current_price * position.quantity

    if cost <= 0:
        return None

    proceeds = market_value * (1 - slippage_rate) * (1 - commission_rate - tax_rate)
    return (proceeds - cost * (1 + commission_rate)) / cost


class RiskManager:
    def __init__(
        self,
        max_position_ratio: float = 0.1,      # 종목당 최대 계좌 비중
        max_daily_loss_ratio: float = 0.02,   # 일일 최대 손실 비중
        take_profit_ratio: float = 0.005,     # 익절 라인 (순손익률)
        stop_loss_ratio: float = 0.02,        # 손절 라인 (순손익률)
        max_total_exposure_ratio: float = 0.7,  # 전체 계좌 대비 최대 노출 비중
        commission_rate: float = 0.00015,     # 매매수수료 (매수·매도 동일 적용)
        tax_rate: float = 0.0018,             # 증권거래세+농특세 (매도 시만)
        slippage_rate: float = 0.001,         # 시장가 청산 슬리피지 추정치
    ):
        self.max_position_ratio = max_position_ratio
        self.max_daily_loss_ratio = max_daily_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        self.stop_loss_ratio = stop_loss_ratio
        self.max_total_exposure_ratio = max_total_exposure_ratio
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage_rate = slippage_rate

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

    def portfolio_return(self, positions: Iterable[Position]) -> Optional[float]:
        """보유 종목 합산 순손익률 — 설정된 수수료·세금·슬리피지를 적용한다."""
        return portfolio_net_return(
            positions, self.commission_rate, self.tax_rate, self.slippage_rate
        )

    def check_portfolio_exit(self, positions: Iterable[Position]) -> Optional[ExitReason]:
        """보유 종목 **전체**가 익절/손절 라인에 도달했는지 확인한다.

        판정은 종목별이 아니라 합산이다 (확정 2026-08-10, PRD 5.5-B). 조건에 닿으면
        보유 종목을 전량 매도한다. 종목별 익절/손절은 두지 않으므로, 한 종목이 크게
        무너져도 다른 종목이 상쇄하면 매도가 나가지 않고 15:20 강제청산까지 간다.

        키움 REST API에 조건부 예약주문(스탑오더) 엔드포인트가 확인되지 않아, 이 실시간
        모니터링이 **1차이자 사실상 유일한 청산 수단**이다 (PRD 5.5-B, 2026-07-27 확정).
        즉 익절/손절은 증권사 서버가 아니라 이 프로그램이 떠 있는 동안에만 동작한다 —
        앱이 꺼지거나 WebSocket이 끊기면 감시 공백이 생긴다.

        판정은 가격 변동률이 아니라 왕복 수수료·매도세금·슬리피지를 뺀 순손익률 기준이다.
        익절선(기본 0.5%)은 이미 비용을 뺀 값이라, 도달하면 그만큼이 실수령 이익이다.
        """
        ret = self.portfolio_return(positions)
        if ret is None:
            return None
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
        """매도 주문에 실을 수량 — 매도가능수량 기준.

        미결제·미체결 매도 주문이 걸려 있으면 보유수량 전량은 주문이 거부된다.
        """
        position = positions.get(ticker)
        return position.closable_quantity if position else 0

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
