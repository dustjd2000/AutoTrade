import logging
from typing import Dict, Optional

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

    def start(self) -> None:
        logger.info("Trading engine starting...")
        self.auth.ensure_token()
        snapshot = self.account.get_balance_snapshot()
        self.risk_manager.initialize(snapshot)
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
        positions = self.account.get_positions()
        for position in positions.values():
            if position.quantity <= 0:
                continue
            order_request = OrderRequest(
                ticker=position.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
            )
            result = self.order_client.send_order(order_request)
            self.risk_manager.record_order(result, avg_price=position.avg_price)
            self._record_trade(result, avg_price=position.avg_price)
            logger.warning("Forced day-end exit (%s): %s", reason, result)
            self.notify(f"장마감 강제청산 ({reason}): {position.ticker} x{position.quantity}")

    def on_market_data(self, data: MarketData) -> None:
        """WebSocket 시세 수신 시 호출되는 콜백."""
        if not self._running:
            return

        positions = self.account.get_positions()

        position = positions.get(data.ticker)
        if position is not None:
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
            logger.info("Order rejected by risk manager: %s %s", signal, data.ticker)
            self.notify(f"주문 거부 (리스크 관리): {signal.name} {data.ticker}")
            return

        result = self.order_client.send_order(order_request)
        self.risk_manager.record_order(result)
        self._record_trade(result)
        logger.info("Order sent: %s", result)
        if result.status == OrderStatus.REJECTED:
            self.notify(f"주문 실패: {result.ticker} {result.error_message}")

    def _execute_exit(self, position: Position, reason: ExitReason) -> None:
        """익절/손절 라인 도달 시 전략 신호와 무관하게 즉시 청산한다."""
        order_request = OrderRequest(
            ticker=position.ticker,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=position.quantity,
        )
        result = self.order_client.send_order(order_request)
        self.risk_manager.record_order(result, avg_price=position.avg_price)
        self._record_trade(result, avg_price=position.avg_price)
        logger.warning("Position exit executed (%s): %s", reason.value, result)
        self.notify(f"{reason.value} 청산: {position.ticker} x{position.quantity}")

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
            return OrderRequest(
                ticker=data.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=qty,
            )
        return None

    def _record_trade(self, result: OrderResult, avg_price: Optional[float] = None) -> None:
        if self.trade_store is not None:
            self.trade_store.record_fill(result, avg_price=avg_price)

    def notify(self, message: str) -> None:
        if self.notifier is not None:
            self.notifier.send(message)
