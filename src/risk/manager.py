import logging
import math
from typing import Dict, Iterable, List, Optional, Tuple

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

    합산으로 판정하는 손절·퍼센트 익절에서는 이 가격에 닿아도 그 종목만 팔리지는 않는다
    (PRD 5.5-B) — "이 종목 혼자였다면 조건에 닿는 가격"이라는 참고값이다. 반면 종목별로
    판정하는 단순익절(target_ratio=0)에서는 이 가격이 그 종목의 실제 매도 지점이다.
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


# --- 표시 전용 (슬리피지 없음) --------------------------------------------
# 위의 net_return / portfolio_net_return은 익절/손절 **판정**용이라 슬리피지를 포함한다.
# 아래 함수들은 화면에 찍는 값이라 슬리피지를 빼고, 대신 키움의 절사 규칙을 재현한다.
# 슬리피지 0.1%는 세율 오차(0.02%p)의 5배라, 표시값에 넣으면 가장 근거가 약한 가정이
# 화면 숫자를 지배한다 (설계 문서 2026-09-01).


def _truncated_fee(amount: float, rate: float) -> float:
    """매매수수료 — 키움은 10원 단위로 절사한다 (매도 49건 실측, 2026-09-01)."""
    return math.floor(amount * rate / 10) * 10


def position_costs(position: Position, commission_rate: float, tax_rate: float) -> float:
    """이 종목을 지금 팔았을 때 나가는 비용 — 매수·매도 수수료와 매도세금의 합.

    키움 잔고 응답이 실제 비용을 주면(`buy_fee`/`sell_cost`) 그 값을 쓰고, 없으면
    절사 규칙으로 계산한다. 세금은 `0.15%+0.05%`로 쪼개지 않고 `tax_rate` 한 값으로
    절사한다 — 쪼개면 1원 더 정확하지만 TAX_PERCENT 설정을 무시하게 된다.
    """
    buy_amount = position.avg_price * position.quantity
    sell_amount = position.current_price * position.quantity

    buy_fee = (
        position.buy_fee
        if position.buy_fee is not None
        else _truncated_fee(buy_amount, commission_rate)
    )
    sell_cost = (
        position.sell_cost
        if position.sell_cost is not None
        else _truncated_fee(sell_amount, commission_rate) + math.floor(sell_amount * tax_rate)
    )
    return buy_fee + sell_cost


def position_net_pnl(position: Position, commission_rate: float, tax_rate: float) -> float:
    """수수료·세금을 뺀 순손익 금액 (원)."""
    gross = (position.current_price - position.avg_price) * position.quantity
    return gross - position_costs(position, commission_rate, tax_rate)


def portfolio_net_pnl(
    positions: Iterable[Position],
    commission_rate: float,
    tax_rate: float,
) -> Tuple[float, Optional[float]]:
    """보유 종목 전체의 (순손익 금액, 순손익률).

    분모는 매입금액이라 `portfolio_net_return`과 같은 기준이다 — 요약줄에서 두 값이
    나란히 읽혀야 한다. 평단·수량·현재가 중 하나라도 0인 종목은 뺀다: 현재가 0은
    조회 실패나 장 전 상태인데 그대로 넣으면 -100%로 잡힌다.
    계산할 종목이 하나도 없으면 비율은 None — '판정하지 않는다'는 뜻이다.
    """
    total = 0.0
    cost = 0.0
    for position in positions:
        if position.avg_price <= 0 or position.quantity <= 0 or position.current_price <= 0:
            continue
        total += position_net_pnl(position, commission_rate, tax_rate)
        cost += position.avg_price * position.quantity

    if cost <= 0:
        return 0.0, None
    return total, total / cost


class RiskManager:
    def __init__(
        self,
        max_position_ratio: float = 0.1,      # 종목당 최대 계좌 비중
        max_daily_loss_ratio: float = 0.02,   # 일일 최대 손실 비중
        take_profit_ratio: float = 0.005,     # 익절 라인 (순손익률)
        stop_loss_ratio: float = 0.02,        # 손절 라인 (순손익률)
        max_total_exposure_ratio: float = 0.7,  # 전체 계좌 대비 최대 노출 비중
        commission_rate: float = 0.00015,     # 매매수수료 (매수·매도 동일 적용)
        tax_rate: float = 0.002,              # 증권거래세+농특세 (매도 시만)
        slippage_rate: float = 0.001,         # 시장가 청산 슬리피지 추정치
        take_profit_enabled: bool = True,     # 합산 퍼센트 익절 적용 여부 (UI 체크박스, 기본 적용)
        stop_loss_enabled: bool = True,       # 손절 적용 여부 (UI 체크박스)
        simple_take_profit_enabled: bool = False,  # 단순익절 적용 여부 (UI 체크박스, 기본 해제)
    ):
        self.max_position_ratio = max_position_ratio
        self.max_daily_loss_ratio = max_daily_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        self.stop_loss_ratio = stop_loss_ratio
        self.max_total_exposure_ratio = max_total_exposure_ratio
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage_rate = slippage_rate
        # 다른 설정과 달리 이 둘은 엔진이 도는 중에도 UI가 그대로 바꾼다 (PRD 5.5-B
        # "익절/손절 적용 여부"). `.env`에 저장하지 않으므로 재시작하면 항상 기본값
        # (손절 + 합산 퍼센트 익절)으로 돌아간다 — 감시 공백을 만들지 않으려면 끄고 켜는
        # 데 엔진 재시작이 끼어들면 안 된다.
        self.take_profit_enabled = take_profit_enabled
        self.stop_loss_enabled = stop_loss_enabled
        # 익절선을 `take_profit_ratio` 대신 0으로 두고 **종목별로** 판정하는 모드
        # (PRD 5.5-B "단순익절"). 위 둘과 같이 `.env`에 저장하지 않고 앱을 다시 켜야
        # 기본값(해제)으로 되돌아간다 — 평소 익절은 합산 퍼센트 쪽이 맡는다 (확정
        # 2026-08-18, PRD 5.5-B "퍼센트 익절 기본 적용"). UI에서는 `take_profit_enabled`와
        # 배타적이지만, 여기서는 서로 독립으로 다룬다 — 둘 다 켜져 있으면 합산 판정이
        # 먼저 돈다(엔진 호출 순서).
        self.simple_take_profit_enabled = simple_take_profit_enabled

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

    def position_net_pnl(self, position: Position) -> float:
        """종목 순손익 금액 (표시용 — 슬리피지 없음)."""
        return position_net_pnl(position, self.commission_rate, self.tax_rate)

    def portfolio_net_pnl(self, positions: Iterable[Position]) -> Tuple[float, Optional[float]]:
        """보유 종목 합산 (순손익 금액, 순손익률) — 표시용이라 슬리피지를 빼지 않는다.

        판정에 쓰는 `portfolio_return`과는 슬리피지만큼 다르다. 표시값이 익절선에
        닿아도 실제 매도는 조금 뒤에 일어난다 — 요약줄 툴팁이 이걸 알린다.
        """
        return portfolio_net_pnl(positions, self.commission_rate, self.tax_rate)

    def check_portfolio_exit(self, positions: Iterable[Position]) -> Optional[ExitReason]:
        """보유 종목 **전체**가 익절/손절 라인에 도달했는지 확인한다.

        판정은 종목별이 아니라 합산이다 (확정 2026-08-10, PRD 5.5-B). 조건에 닿으면
        보유 종목을 전량 매도한다. 종목별 익절/손절은 두지 않으므로, 한 종목이 크게
        무너져도 다른 종목이 상쇄하면 매도가 나가지 않고 15:15 강제청산까지 간다.

        키움 REST API에 조건부 예약주문(스탑오더) 엔드포인트가 확인되지 않아, 이 실시간
        모니터링이 **1차이자 사실상 유일한 청산 수단**이다 (PRD 5.5-B, 2026-07-27 확정).
        즉 익절/손절은 증권사 서버가 아니라 이 프로그램이 떠 있는 동안에만 동작한다 —
        앱이 꺼지거나 WebSocket이 끊기면 감시 공백이 생긴다.

        판정은 가격 변동률이 아니라 왕복 수수료·매도세금·슬리피지를 뺀 순손익률 기준이다.
        익절선(기본 0.5%)은 이미 비용을 뺀 값이라, 도달하면 그만큼이 실수령 이익이다.

        `take_profit_enabled`/`stop_loss_enabled`가 꺼져 있으면 그쪽 라인은 건너뛴다. 둘 다
        꺼면 실시간 청산이 사라지고 15:15 강제청산만 남는다 — 합산 순손익률 계산 자체는
        멈추지 않으므로 UI에는 그대로 표시된다.

        단순익절은 여기 끼지 않는다 — 종목별 판정이라 `check_simple_take_profits`가 따로
        본다 (확정 2026-08-12). UI에서 익절 '적용'과 '단순익절적용'은 배타적이라 둘 중
        하나만 켜지므로, 실제로는 이 메서드의 익절과 단순익절이 같은 날 함께 돌지 않는다.
        """
        ret = self.portfolio_return(positions)
        if ret is None:
            return None
        if self.take_profit_enabled and ret >= self.take_profit_ratio:
            return ExitReason.TAKE_PROFIT
        if self.stop_loss_enabled and ret <= -self.stop_loss_ratio:
            return ExitReason.STOP_LOSS
        return None

    def check_simple_take_profits(self, positions: Iterable[Position]) -> List[Position]:
        """단순익절 대상 — **종목별** 순손익률이 0을 넘은 종목만 골라 돌려준다 (PRD 5.5-B).

        합산이 아니라 종목마다 따로 본다 (확정 2026-08-12). 돌려주는 것은 '지금 팔아야 하는
        종목'이지 전량 청산 신호가 아니다 — 고르지 못한 종목은 그대로 보유한다.

        비교가 `>=`가 아니라 `>`인 것은 본전(0)에서 팔지 않기 위해서다. '단순'은 기준선이
        0이라는 뜻일 뿐, 순손익률에서 수수료·세금·슬리피지를 빼는 것은 합산 판정과 같다.

        **이익 난 종목이 먼저 빠져나가면 남은 보유는 손실 종목 위주가 되어 합산 손절이 더
        쉽게 걸린다** — 종목별 청산이 만드는 순서 의존성이며, 사용자가 알고 택한 동작이다.

        평단·수량·현재가 중 하나라도 0인 종목은 판정하지 않는다 (현재가 0은 조회 실패나
        장 전 상태다).
        """
        if not self.simple_take_profit_enabled:
            return []
        return [
            position
            for position in positions
            if position.avg_price > 0
            and position.quantity > 0
            and position.current_price > 0
            and net_return(
                position.current_price,
                position.avg_price,
                self.commission_rate,
                self.tax_rate,
                self.slippage_rate,
            )
            > 0
        ]

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
