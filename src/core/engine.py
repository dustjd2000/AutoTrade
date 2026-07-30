import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set

from src.api.auth import AuthClient
from src.api.client import KiwoomAPIError
from src.api.market_data import MarketDataClient
from src.api.order import OrderClient
from src.api.account import AccountClient, Position
from src.strategy.base import BaseStrategy
from src.risk.manager import RiskManager
from src.core.events import (
    format_stock,
    MarketData,
    Signal,
    OrderSide,
    OrderType,
    OrderRequest,
    OrderResult,
    OrderStatus,
    ExitReason,
    UnsellableView,
)
from src.logger.trade_store import TradeStore
from src.notification.alert import AlertNotifier

logger = logging.getLogger(__name__)

# 실시간 체결 틱마다 잔고 REST(kt00005)를 호출하면 키움 유량 제한(429)에 걸린다.
# 그 예외는 콜백 전체를 중단시키므로 익절/손절 판정이 통째로 건너뛰어지는데,
# _last_market_data_at은 이미 갱신된 뒤라 시세 끊김 감시에도 걸리지 않는다 —
# 즉 아무 경고 없이 손절만 멈춘다. TTL 캐시로 호출을 '틱당 1회'에서 'TTL당 1회'로 줄인다.
POSITION_CACHE_TTL_SECONDS = 5.0

# 상장폐지·거래정지 등으로 키움이 종목 자체를 모르는 상태의 거부 사유.
# 몇 번을 시도해도 같은 이유로 거부되는데 잔고에는 계속 남아 있어, 걸러내지 않으면
# 감시 목록과 창 종료 경고, 다음 청산 시도에 매번 다시 올라온다 (2026-07-28 118970/395680).
UNKNOWN_STOCK_ERROR = "종목 정보가 없"


@dataclass(frozen=True)
class PositionView:
    """UI 표시용 보유 종목 사본. 엔진 내부 상태와 분리해 다른 스레드에서 읽는다."""

    ticker: str
    name: Optional[str]
    quantity: int
    avg_price: float
    current_price: float

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)

    @property
    def pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def pnl_percent(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price * 100


def _position_summary(position: Position) -> str:
    """종목 표기와 평단 대비 손익을 로그에서 바로 읽을 수 있게 요약한다."""
    pct = (
        (position.current_price - position.avg_price) / position.avg_price * 100
        if position.avg_price
        else 0.0
    )
    return (
        f"{position.label} {position.quantity}주, 평단 {position.avg_price:,.0f}원 → "
        f"현재가 {position.current_price:,.0f}원 "
        f"(평가손익 {position.unrealized_pnl:+,.0f}원, {pct:+.2f}%)"
    )


class TradingEngine:
    def __init__(
        self,
        auth: AuthClient,
        market_data: MarketDataClient,
        order_client: OrderClient,
        account: AccountClient,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        trade_store: Optional[TradeStore] = None,
        notifier: Optional[AlertNotifier] = None,
        emergency_action: str = "hold",
    ):
        self.auth = auth
        self.market_data = market_data
        self.order_client = order_client
        self.account = account
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.trade_store = trade_store
        self.notifier = notifier
        self.emergency_action = emergency_action
        self._running = False
        # 익절/손절 감시가 필요한 종목과 마지막 시세 수신 시각.
        # 감시는 이 프로그램이 떠 있는 동안에만 동작하므로(키움 REST는 스탑오더 미지원),
        # 창 종료 경고와 시세 끊김 감지에 이 두 값을 쓴다.
        self._open_tickers: Set[str] = set()
        self._last_market_data_at: Optional[datetime] = None
        # 잔고 스냅샷 캐시 — `POSITION_CACHE_TTL_SECONDS` 참고
        self._positions: Dict[str, Position] = {}
        self._positions_fetched_at: Optional[datetime] = None
        # 청산 주문을 이미 낸 종목. 체결이 잔고에 반영되기 전에 다음 틱이 들어와도
        # 같은 포지션을 두 번 매도하지 않도록 막는다.
        self._exiting: Set[str] = set()
        # 매도할 수 없다고 판명된 종목 — 보유 목록에서 제외한다 (_screen_positions).
        # 거래일이 바뀌면 비워서 다시 확인한다(거래정지 해제 대비).
        self._untradable: Set[str] = set()
        # 종목 정보 확인을 마친 종목. 같은 종목을 잔고 조회마다 다시 묻지 않기 위한 표시.
        self._verified: Set[str] = set()
        # 매도가능수량 0으로 청산을 건너뛴 종목. 틱마다 같은 경고를 반복하지 않기 위한 표시.
        self._zero_sellable: Set[str] = set()
        # 오늘 매도하지 못한 종목과 사유 (UI 표시용) — `UnsellableView` 참고
        self._unsellable: Dict[str, UnsellableView] = {}
        # 보유 목록이 매도로 비워진 시각. 15:30을 기다리지 않고 결과 리포트를 보내는
        # 근거가 된다 (runtime.closeout_report_due).
        self._closed_out_at: Optional[datetime] = None

    @property
    def open_tickers(self) -> List[str]:
        """감시 중인(=청산되지 않은) 보유 종목. 다른 스레드에서 읽어도 안전한 사본을 준다."""
        return sorted(self._open_tickers)

    @property
    def last_market_data_at(self) -> Optional[datetime]:
        return self._last_market_data_at

    @property
    def closed_out_at(self) -> Optional[datetime]:
        """보유 목록이 매도로 비워진 시각. 아직 비지 않았거나 다시 매수했으면 None."""
        return self._closed_out_at

    def unsellable_snapshot(self) -> List[UnsellableView]:
        """오늘 매도하지 못한 종목 사본 (UI 스레드에서 호출 — API를 호출하지 않는다).

        `_unsellable`은 통째로 갈아끼우기만 하므로 참조를 한 번 집어두면 일관된 사본이 된다.
        """
        return sorted(self._unsellable.values(), key=lambda u: u.ticker)

    def note_open_position(self, ticker: str) -> None:
        """매수 주문이 접수되면 즉시 감시 대상으로 표시한다.

        체결 통보나 첫 시세가 오기 전에도 '보유 중'으로 간주해야, 매수 직후 창을 닫는
        상황에서 경고를 띄울 수 있다.
        """
        self._open_tickers.add(ticker)
        # 다시 보유가 생겼으므로 '전량 매도 완료'가 아니다 — 리포트 발송 근거를 되돌린다
        self._closed_out_at = None
        # 새 포지션의 평단가는 잔고를 다시 읽어야 알 수 있다 — 익절/손절 판정의 기준값이다
        self._invalidate_positions()

    def _get_positions(self, force: bool = False) -> Dict[str, Position]:
        """보유 포지션 스냅샷. 틱마다 REST를 때리지 않도록 짧은 TTL로 캐시한다.

        갱신에 실패해도 직전 스냅샷으로 감시를 이어간다 — 평단가는 보유 중 변하지 않으므로
        낡은 스냅샷으로도 익절/손절 판정은 유효하고, 한 번의 429로 감시가 멈추는 편이 더 위험하다.
        `force=True`는 청산처럼 최신 잔고가 반드시 필요한 경우로, 실패를 그대로 올린다.
        """
        now = datetime.now()
        if (
            not force
            and self._positions_fetched_at is not None
            and (now - self._positions_fetched_at).total_seconds() < POSITION_CACHE_TTL_SECONDS
        ):
            return self._positions

        try:
            self._positions = self._screen_positions(self.account.get_positions())
        except Exception:
            if force:
                raise
            # 실패해도 시각을 갱신해 매 틱 재시도로 429를 키우지 않는다
            self._positions_fetched_at = now
            logger.warning("잔고 갱신 실패 — 직전 스냅샷으로 감시를 이어갑니다.", exc_info=True)
            return self._positions

        self._positions_fetched_at = now
        # 잔고가 최신 사실이므로 감시 목록을 여기서 맞춘다 (체결·수동매도 반영)
        self._open_tickers = {t for t, p in self._positions.items() if p.quantity > 0}
        # 잔고에서 사라진 종목은 청산이 확정된 것 — 중복 매도 가드를 푼다
        self._exiting &= set(self._positions)
        return self._positions

    def _invalidate_positions(self) -> None:
        """다음 조회에서 잔고를 새로 읽도록 캐시를 버린다 (주문 직후 호출)."""
        self._positions_fetched_at = None

    def _screen_positions(self, positions: Dict[str, Position]) -> Dict[str, Position]:
        """보유 목록을 확정하기 전에 매도할 수 없는 종목을 걸러낸다.

        상장폐지·거래정지 종목은 잔고에 계속 실려 오지만 주문은 '종목 정보가 없습니다'로
        영구 거부된다. 주문 거부를 기다렸다 빼면 그때까지 감시·창 종료 경고·청산 시도가
        전부 헛돈다. 종목당 한 번만 확인하고 결과를 기억한다.
        """
        for ticker, position in positions.items():
            if ticker in self._untradable or ticker in self._verified:
                continue
            reason = self._untradable_reason(position)
            self._verified.add(ticker)
            if reason is not None:
                self._exclude_untradable(ticker, position.label, reason)

        if not self._untradable:
            return positions
        return {t: p for t, p in positions.items() if t not in self._untradable}

    def _untradable_reason(self, position: Position) -> Optional[str]:
        """종목 정보를 조회해 매도 불가 사유를 돌려준다. 정상이면 None.

        일시적인 조회 실패(유량 제한 등)로 보유 종목을 지우면 청산이 조용히 멈추므로,
        판단이 서지 않으면 정상으로 취급한다 — 그 경우는 주문 거부(_note_untradable)로 걸린다.
        """
        if self.market_data is None:
            return None

        try:
            master = self.market_data.get_stock_master(position.ticker)
        except KiwoomAPIError as e:
            if UNKNOWN_STOCK_ERROR in str(e):
                return "종목 정보가 조회되지 않습니다"
            logger.warning("종목 정보 조회 실패 — 보유 목록에 그대로 둡니다: %s (%s)", position.label, e)
            return None
        except Exception as e:
            logger.warning("종목 정보 조회 실패 — 보유 목록에 그대로 둡니다: %s (%s)", position.label, e)
            return None

        if not master.name:
            return "종목명이 조회되지 않습니다"
        # 마스터 현재가만 0인 경우는 장 전 시간대일 수 있어, 잔고 현재가까지 0일 때만 제외한다.
        # 정상 종목을 잘못 빼면 청산이 통째로 멈추는 쪽이 더 위험하다.
        if master.price <= 0 and position.current_price <= 0:
            return "현재가가 조회되지 않습니다"
        return None

    def _note_unsellable(
        self, ticker: str, label: str, reason: str, excluded: bool = False
    ) -> None:
        """매도하지 못한 사유를 기록한다 (UI '매도 불가' 표시용).

        제자리에서 고치지 않고 새 딕셔너리로 갈아끼운다 — UI 스레드가 사본을 만드는
        도중에 크기가 바뀌면 순회가 깨진다 (unsellable_snapshot 참고).
        """
        entry = UnsellableView(
            ticker=ticker, label=label, reason=reason, at=datetime.now(), excluded=excluded
        )
        self._unsellable = {**self._unsellable, ticker: entry}

    def _clear_unsellable(self, ticker: str) -> None:
        """매도에 성공했거나 사유가 풀린 종목을 매도 불가 목록에서 뺀다."""
        if ticker in self._unsellable:
            self._unsellable = {t: e for t, e in self._unsellable.items() if t != ticker}

    def _exclude_untradable(self, ticker: str, label: str, reason: str) -> None:
        """매도할 수 없는 종목을 보유 목록·감시 대상에서 뺀다."""
        self._untradable.add(ticker)
        self._open_tickers.discard(ticker)
        # 제자리에서 지우지 않고 새 딕셔너리로 갈아끼운다 (position_snapshot 참고)
        self._positions = {t: p for t, p in self._positions.items() if t != ticker}
        # 보유 목록에서는 빠지지만 계좌에는 남아 있다 — UI가 사유와 함께 보여준다
        self._note_unsellable(ticker, label, reason, excluded=True)
        # 종목정보를 못 찾은 건은 메일로 알리지 않는다 — 로그로만 남긴다
        logger.warning(
            "보유 목록에서 제외합니다 — %s (상장폐지·거래정지 추정): %s", reason, label
        )

    def _note_untradable(self, result: OrderResult) -> bool:
        """매도가 '종목 정보 없음'으로 거부됐으면 보유 목록에서 제외한다.

        사전 확인(_screen_positions)이 통과시킨 종목이라도 장중에 거래정지될 수 있다.
        재시도해도 결과가 같으므로 목록에 남겨두면 청산할 때마다 무의미한 주문이 나간다.

        True면 제외 처리를 마쳤다는 뜻 — 호출부는 거부 메일을 보내지 않는다.
        """
        if result.side != OrderSide.SELL:
            return False
        if UNKNOWN_STOCK_ERROR not in (result.error_message or ""):
            return False

        self._exclude_untradable(result.ticker, result.label, "주문이 종목 정보 없음으로 거부되었습니다")
        return True

    def _closable_or_skip(self, position: Position, context: str) -> int:
        """청산 주문에 실을 수량. 0이면 경고를 남기고 건너뛰라는 뜻이다.

        매도가능수량 0은 보통 미체결 매도 주문이 걸려 있다는 뜻이라 잠시 뒤 풀린다.
        실시간 틱마다 같은 경고가 쌓이지 않도록 종목당 한 번만 남긴다.
        """
        quantity = position.closable_quantity
        if quantity <= 0:
            if position.ticker not in self._zero_sellable:
                self._zero_sellable.add(position.ticker)
                logger.warning(
                    "청산 건너뜀 (%s): %s — 매도가능수량이 0입니다 (미체결 매도 주문 확인 필요).",
                    context,
                    position.label,
                )
                self.notify(f"청산 불가 ({context}): {position.label} — 매도가능수량 0")
                self._note_unsellable(
                    position.ticker,
                    position.label,
                    "매도가능수량이 0입니다 (미체결 매도 주문 확인 필요)",
                )
            return 0

        self._zero_sellable.discard(position.ticker)
        # 수량이 다시 잡혔으므로 직전 사유는 지운다 — 주문이 또 거부되면 다시 기록된다
        self._clear_unsellable(position.ticker)
        if quantity < position.quantity:
            logger.warning(
                "매도가능수량 기준으로 청산합니다 (%s): %s 보유 %d주 중 %d주.",
                context,
                position.label,
                position.quantity,
                quantity,
            )
        return quantity

    def position_snapshot(self) -> List[PositionView]:
        """보유 종목 사본 (UI 스레드에서 호출 — API를 호출하지 않고 캐시만 읽는다).

        `_positions`는 통째로 갈아끼우기만 하고 제자리에서 고치지 않으므로, 참조를 한 번
        집어두면 엔진 루프가 그 사이 잔고를 갱신해도 일관된 사본을 만들 수 있다.
        매도된 종목은 잔고에서 빠지거나 _mark_exited가 지우므로 여기 나타나지 않는다.
        """
        positions = self._positions
        return [
            PositionView(
                ticker=p.ticker,
                name=p.name,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
            )
            for p in positions.values()
            if p.quantity > 0
        ]

    def reset_for_new_day(self) -> None:
        """장 시작 전 일일 리스크 카운터를 초기화한다.

        앱을 여러 날 연속으로 켜두면 initialize()가 엔진 시작 시 한 번만 돌아, '일일'
        손실 한도가 사실상 누적 한도로 굳는다. 한도를 넘겨 _halted가 서면 재시작 전까지
        신규 매수가 영구 차단되므로(RiskManager.approve), 매 거래일 아침에 다시 세운다.
        """
        self.auth.ensure_token()
        snapshot = self.account.get_balance_snapshot()
        self.risk_manager.initialize(snapshot)
        self._invalidate_positions()
        # 거래정지가 풀렸을 수 있으므로 매도 불가 판정을 지우고 오늘 다시 확인한다
        self._untradable.clear()
        self._verified.clear()
        # 매도 불가 목록은 '오늘' 기준이므로 함께 비운다. 경고 억제 표시(_zero_sellable)까지
        # 지워야 같은 종목이 오늘 다시 걸렸을 때 목록에 올라온다.
        self._unsellable = {}
        self._zero_sellable.clear()
        self._closed_out_at = None
        logger.info("새 거래일 준비 — 일일 손실 한도와 매매 중지 상태를 초기화했습니다.")

    def start(self) -> None:
        logger.info("Trading engine starting...")
        self.auth.ensure_token()
        snapshot = self.account.get_balance_snapshot()
        self.risk_manager.initialize(snapshot)
        self._positions = self._screen_positions(snapshot.positions)
        self._positions_fetched_at = datetime.now()
        self._open_tickers = {t for t, p in self._positions.items() if p.quantity > 0}
        if self._open_tickers:
            logger.warning(
                "시작 시점에 이미 보유 중인 종목이 있습니다 (전일 이월 가능): %s",
                sorted(self._open_tickers),
            )
        self._running = True
        logger.info("Trading engine started. Strategy: %s", self.strategy.name)

    def stop(self) -> None:
        self._running = False
        logger.info("Trading engine stopped.")

    def handle_fatal_error(self, reason: str) -> None:
        """장중 치명적 예외 발생 시 설정된 장애 대응 방식에 따라 처리한다 (6절 비기능요구사항).

        emergency_action == "close_all": 보유 포지션을 정리한 뒤 정지
        emergency_action == "hold" (기본값): 포지션은 그대로 두고 신규 매매만 정지
        """
        logger.error("Fatal error encountered (%s). Emergency action: %s", reason, self.emergency_action)
        self.notify(f"[긴급] 장애 발생: {reason} (대응: {self.emergency_action})")
        if self.emergency_action == "close_all":
            self.force_close_all_positions(reason=f"emergency:{reason}")
        self.stop()

    def force_close_all_positions(self, reason: str = "day_end") -> None:
        """당일 매도(데이트레이딩) 원칙에 따라 장 마감 전 미청산 포지션을 전량 정리한다.

        스케줄러가 장 마감 직전(예: 15:20)에 호출하는 것을 전제로 한다.
        """
        # 무엇을 파는지가 곧 결과이므로 캐시를 쓰지 않고 최신 잔고를 읽는다
        positions = self._get_positions(force=True)
        holdings = [p for p in positions.values() if p.quantity > 0]
        self._open_tickers = {p.ticker for p in holdings}
        if not holdings:
            logger.info("청산할 보유 포지션이 없습니다 (%s).", reason)
            return

        logger.info("강제청산 시작 (%s) — 대상 %d종목", reason, len(holdings))
        for position in holdings:
            summary = _position_summary(position)
            quantity = self._closable_or_skip(position, reason)
            if quantity <= 0:
                continue

            order_request = OrderRequest(
                ticker=position.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=quantity,
                name=position.name,
            )
            result = self.order_client.send_order(order_request)
            self.risk_manager.record_order(result, avg_price=position.avg_price)
            self._record_trade(result, avg_price=position.avg_price)

            if result.status == OrderStatus.REJECTED:
                logger.error(
                    "강제청산 거부됨 (%s): %s — %s", reason, summary, result.error_message
                )
                # 종목정보 없음으로 제외한 건은 로그로 충분하다 (_exclude_untradable이 사유를 기록한다)
                if not self._note_untradable(result):
                    self._note_unsellable(
                        position.ticker,
                        position.label,
                        f"매도 주문 거부: {result.error_message}",
                    )
                    self.notify(
                        f"[실패] 강제청산 거부: {position.label} — {result.error_message}"
                    )
                continue

            self._mark_exited(position.ticker)
            logger.warning(
                "장마감 강제청산 (%s): %s, 주문번호 %s", reason, summary, result.order_id
            )
            self.notify(f"장마감 강제청산 ({reason}): {summary}")

    def on_market_data(self, data: MarketData) -> None:
        """WebSocket 시세 수신 시 호출되는 콜백."""
        if not self._running:
            return

        self._last_market_data_at = datetime.now()
        positions = self._get_positions()

        position = positions.get(data.ticker)
        if position is not None and data.ticker not in self._exiting:
            position.current_price = data.price
            exit_reason = self.risk_manager.check_exit(position)
            if exit_reason is not None:
                self._execute_exit(position, exit_reason)
                return

        signal = self.strategy.generate_signal(data)

        if signal == Signal.HOLD:
            return

        order_request = self._signal_to_order(signal, data, positions)
        if order_request is None:
            return

        approved = self.risk_manager.approve(order_request, positions, reference_price=data.price)
        if not approved:
            logger.warning(
                "주문 거부 (리스크 관리): %s %s x%d주",
                signal.name,
                order_request.label,
                order_request.quantity,
            )
            self.notify(f"주문 거부 (리스크 관리): {signal.name} {order_request.label}")
            return

        result = self.order_client.send_order(order_request)
        self.risk_manager.record_order(result)
        self._record_trade(result)

        if result.status == OrderStatus.REJECTED:
            logger.error(
                "주문 거부됨: %s %s x%d주 — %s",
                result.side.value,
                result.label,
                result.quantity,
                result.error_message,
            )
            # 종목정보 없음으로 제외한 건은 로그로 충분하다
            if not self._note_untradable(result):
                self.notify(f"[실패] 주문 거부: {result.label} — {result.error_message}")
            return

        # 잔고가 바뀌었으므로 다음 틱에서 다시 읽는다
        self._invalidate_positions()
        logger.info(
            "주문 접수: %s %s x%d주 @ %s원 (상태 %s, 주문번호 %s)",
            result.side.value,
            result.label,
            result.quantity,
            f"{data.price:,.0f}",
            result.status.value,
            result.order_id,
        )

    def _execute_exit(self, position: Position, reason: ExitReason) -> None:
        """익절/손절 라인 도달 시 전략 신호와 무관하게 즉시 청산한다."""
        summary = _position_summary(position)
        quantity = self._closable_or_skip(position, reason.value)
        if quantity <= 0:
            return

        logger.info("청산 조건 도달 (%s): %s", reason.value, summary)

        order_request = OrderRequest(
            ticker=position.ticker,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            name=position.name,
        )
        result = self.order_client.send_order(order_request)
        self.risk_manager.record_order(result, avg_price=position.avg_price)
        self._record_trade(result, avg_price=position.avg_price)

        if result.status == OrderStatus.REJECTED:
            logger.error(
                "청산 주문 거부됨 (%s): %s — %s", reason.value, summary, result.error_message
            )
            # 종목정보 없음으로 제외한 건은 로그로 충분하다 (_exclude_untradable이 사유를 기록한다)
            if not self._note_untradable(result):
                self._note_unsellable(
                    position.ticker,
                    position.label,
                    f"매도 주문 거부: {result.error_message}",
                )
                self.notify(
                    f"[실패] {reason.value} 청산 거부: {position.label} — {result.error_message}"
                )
            return

        self._mark_exited(position.ticker)
        logger.warning(
            "%s 청산 주문 접수: %s, 주문번호 %s", reason.value, summary, result.order_id
        )
        self.notify(f"{reason.value} 청산: {summary}")

    def _mark_exited(self, ticker: str) -> None:
        """청산 주문이 접수된 종목을 감시 대상에서 빼고 중복 매도를 막는다.

        체결이 잔고에 반영되기까지 시차가 있어, 캐시에서 지우지 않으면 다음 틱이
        같은 포지션을 다시 손절 대상으로 보고 매도 주문을 한 번 더 낸다.
        """
        self._open_tickers.discard(ticker)
        self._exiting.add(ticker)
        # 제자리에서 지우지 않고 새 딕셔너리로 갈아끼운다 — UI 스레드가 사본을 만드는
        # 도중에 크기가 바뀌면 순회가 깨진다 (position_snapshot 참고).
        self._positions = {t: p for t, p in self._positions.items() if t != ticker}
        self._invalidate_positions()
        self._clear_unsellable(ticker)
        # 마지막 보유 종목까지 팔렸으면 그 시각을 남긴다 — 15:30을 기다리지 않고 결과
        # 리포트를 보내는 근거다 (runtime.watch_closeout_report). 매도할 수 없어 제외된
        # 종목(_exclude_untradable)은 보유 목록에 없으므로 판단에 끼어들지 않는다.
        if not self._open_tickers:
            self._closed_out_at = datetime.now()

    def _signal_to_order(
        self, signal: Signal, data: MarketData, positions: Dict[str, Position]
    ) -> Optional[OrderRequest]:
        if signal == Signal.BUY:
            qty = self.risk_manager.calc_buy_quantity(data.ticker, data.price)
            if qty <= 0:
                return None
            return OrderRequest(
                ticker=data.ticker,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=qty,
            )
        if signal == Signal.SELL:
            qty = self.risk_manager.get_holding_quantity(data.ticker, positions)
            if qty <= 0:
                return None
            held = positions.get(data.ticker)
            return OrderRequest(
                ticker=data.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=qty,
                name=held.name if held is not None else None,
            )
        return None

    def _record_trade(self, result: OrderResult, avg_price: Optional[float] = None) -> None:
        if self.trade_store is not None:
            self.trade_store.record_fill(result, avg_price=avg_price)

    def notify(self, message: str) -> None:
        if self.notifier is not None:
            self.notifier.send(message)
