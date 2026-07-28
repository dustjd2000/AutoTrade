import logging
from datetime import datetime
from typing import Dict, List, Optional, Set

from src.api.auth import AuthClient
from src.api.market_data import MarketDataClient
from src.api.order import OrderClient
from src.api.account import AccountClient, Position
from src.strategy.base import BaseStrategy
from src.risk.manager import RiskManager
from src.core.events import (
    MarketData,
    Signal,
    OrderSide,
    OrderType,
    OrderRequest,
    OrderResult,
    OrderStatus,
    ExitReason,
)
from src.logger.trade_store import TradeStore
from src.notification.alert import AlertNotifier

logger = logging.getLogger(__name__)

# 실시간 체결 틱마다 잔고 REST(kt00005)를 호출하면 키움 유량 제한(429)에 걸린다.
# 그 예외는 콜백 전체를 중단시키므로 익절/손절 판정이 통째로 건너뛰어지는데,
# _last_market_data_at은 이미 갱신된 뒤라 시세 끊김 감시에도 걸리지 않는다 —
# 즉 아무 경고 없이 손절만 멈춘다. TTL 캐시로 호출을 '틱당 1회'에서 'TTL당 1회'로 줄인다.
POSITION_CACHE_TTL_SECONDS = 5.0


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

    @property
    def open_tickers(self) -> List[str]:
        """감시 중인(=청산되지 않은) 보유 종목. 다른 스레드에서 읽어도 안전한 사본을 준다."""
        return sorted(self._open_tickers)

    @property
    def last_market_data_at(self) -> Optional[datetime]:
        return self._last_market_data_at

    def note_open_position(self, ticker: str) -> None:
        """매수 주문이 접수되면 즉시 감시 대상으로 표시한다.

        체결 통보나 첫 시세가 오기 전에도 '보유 중'으로 간주해야, 매수 직후 창을 닫는
        상황에서 경고를 띄울 수 있다.
        """
        self._open_tickers.add(ticker)
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
            self._positions = self.account.get_positions()
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
        logger.info("새 거래일 준비 — 일일 손실 한도와 매매 중지 상태를 초기화했습니다.")

    def start(self) -> None:
        logger.info("Trading engine starting...")
        self.auth.ensure_token()
        snapshot = self.account.get_balance_snapshot()
        self.risk_manager.initialize(snapshot)
        self._positions = snapshot.positions
        self._positions_fetched_at = datetime.now()
        self._open_tickers = {t for t, p in snapshot.positions.items() if p.quantity > 0}
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
            order_request = OrderRequest(
                ticker=position.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
                name=position.name,
            )
            result = self.order_client.send_order(order_request)
            self.risk_manager.record_order(result, avg_price=position.avg_price)
            self._record_trade(result, avg_price=position.avg_price)

            if result.status == OrderStatus.REJECTED:
                logger.error(
                    "강제청산 거부됨 (%s): %s — %s", reason, summary, result.error_message
                )
                self.notify(f"[실패] 강제청산 거부: {position.label} — {result.error_message}")
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
        logger.info("청산 조건 도달 (%s): %s", reason.value, summary)

        order_request = OrderRequest(
            ticker=position.ticker,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            name=position.name,
        )
        result = self.order_client.send_order(order_request)
        self.risk_manager.record_order(result, avg_price=position.avg_price)
        self._record_trade(result, avg_price=position.avg_price)

        if result.status == OrderStatus.REJECTED:
            logger.error(
                "청산 주문 거부됨 (%s): %s — %s", reason.value, summary, result.error_message
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
        self._positions.pop(ticker, None)
        self._invalidate_positions()

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
