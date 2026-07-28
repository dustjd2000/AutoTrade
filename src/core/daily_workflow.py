import logging
from datetime import date
from typing import Optional

from src.api.account import AccountClient
from src.core.engine import TradingEngine
from src.core.events import OrderRequest, OrderSide, OrderStatus, OrderType, format_stock
from src.data.collector import DataCollector
from src.llm.recommender import LLMRecommender
from src.logger.trade_store import TradeStore
from src.notification.email import EmailNotifier
from src.notification import templates
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)


class DailyWorkflow:
    """1호 전략의 하루 흐름을 스케줄러 트리거에 연결한다 (PRD 5.5-B, 5.11).

    08:45 recommend_and_notify → 09:00 execute_buys → 15:30 send_daily_report
    """

    def __init__(
        self,
        collector: DataCollector,
        recommender: LLMRecommender,
        strategy: LLMMomentumStrategy,
        engine: TradingEngine,
        account: AccountClient,
        trade_store: TradeStore,
        email: EmailNotifier,
        ws_client=None,
    ):
        self.collector = collector
        self.recommender = recommender
        self.strategy = strategy
        self.engine = engine
        self.account = account
        self.trade_store = trade_store
        self.email = email
        self.ws_client = ws_client

    def recommend_and_notify(self, today: Optional[date] = None) -> None:
        """08:45 — 당일 데이터 수집 → LLM 추천 → 결과를 이메일로 발송."""
        today = today or date.today()
        daily_data = self.collector.collect()
        if not daily_data:
            logger.error("No daily data collected. Skipping today's recommendation.")
            self.engine.notify("[경고] 당일 데이터 수집 실패 — 오늘 매수를 스킵합니다.")
            return

        recommendations = self.recommender.recommend(daily_data)
        if not recommendations:
            logger.error("LLM recommendation unavailable. Skipping today's buys.")
            self.engine.notify("[경고] LLM 추천 실패/타임아웃 — 오늘 매수를 스킵합니다.")
            return

        self.strategy.set_recommendations(recommendations)
        subject, body = templates.recommendation_email(recommendations, today)
        self.email.send(subject, body)
        logger.info("Recommendation email sent for %s", today)

    def execute_buys(self) -> None:
        """09:00 — 예수금 기준으로 자금을 배분해 추천 종목을 시장가 매수."""
        cash = self.account.get_cash()
        plans = self.strategy.build_buy_plans(cash)
        if not plans:
            logger.warning("매수 계획이 없습니다 (추천 종목 없음). 예수금 %s원", f"{cash:,.0f}")
            return

        logger.info(
            "매수 시작 — 예수금 %s원, 대상 %d종목, 종목당 배정 %s원",
            f"{cash:,.0f}",
            len(plans),
            f"{plans[0].amount:,.0f}",
        )

        positions = self.account.get_positions()
        ordered, skipped, failed = [], [], []
        for plan in plans:
            label = format_stock(plan.ticker, plan.name)
            try:
                price = self.engine.market_data.get_current_price(plan.ticker).price
                quantity = int(plan.amount // price)
                if quantity <= 0:
                    logger.warning(
                        "매수 건너뜀: %s — 1주 %s원이 종목당 배정액 %s원을 초과합니다.",
                        label,
                        f"{price:,.0f}",
                        f"{plan.amount:,.0f}",
                    )
                    self.engine.notify(f"매수 건너뜀: {label} — 1주 가격이 배정액을 초과")
                    skipped.append(plan.ticker)
                    continue

                logger.info(
                    "매수 산정: %s 현재가 %s원 × %d주 = %s원 (배정 %s원)",
                    label,
                    f"{price:,.0f}",
                    quantity,
                    f"{price * quantity:,.0f}",
                    f"{plan.amount:,.0f}",
                )

                request = OrderRequest(
                    ticker=plan.ticker,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=quantity,
                    name=plan.name,
                )
                if not self.engine.risk_manager.approve(request, positions, reference_price=price):
                    logger.warning("매수 거부 (리스크 관리): %s x%d주", label, quantity)
                    self.engine.notify(f"매수 거부 (리스크 관리): {label}")
                    failed.append(plan.ticker)
                    continue

                result = self.engine.order_client.send_order(request)
                self.engine.risk_manager.record_order(result)
                self.trade_store.record_fill(result)

                # 거부된 주문을 접수된 것처럼 남기면 매수 실패를 알아챌 수 없다
                if result.status == OrderStatus.REJECTED:
                    logger.error(
                        "매수 주문 거부됨: %s x%d주 — %s", label, quantity, result.error_message
                    )
                    self.engine.notify(f"[실패] 매수 거부: {label} x{quantity}주 — {result.error_message}")
                    failed.append(plan.ticker)
                    continue

                ordered.append(plan.ticker)
                # 체결 통보를 기다리지 않고 감시 대상으로 표시한다 (창 종료 경고의 근거)
                self.engine.note_open_position(plan.ticker)
                logger.info(
                    "매수 주문 접수: %s x%d주 (상태 %s, 주문번호 %s)",
                    label,
                    quantity,
                    result.status.value,
                    result.order_id,
                )
            except Exception:
                logger.exception("매수 처리 중 오류: %s", label)
                self.engine.notify(f"[오류] 매수 실패: {label}")
                failed.append(plan.ticker)

        logger.info(
            "매수 종료 — 접수 %d종목%s, 건너뜀 %d종목%s, 실패 %d종목%s",
            len(ordered),
            f" {ordered}" if ordered else "",
            len(skipped),
            f" {skipped}" if skipped else "",
            len(failed),
            f" {failed}" if failed else "",
        )
        if not ordered:
            self.engine.notify("[경고] 매수가 한 건도 접수되지 않았습니다. 로그를 확인하세요.")

        # 접수된 종목만 실시간 시세를 구독한다 — 익절/손절 감시(RiskManager.check_exit)의 전제.
        # 거부된 종목까지 구독하면 보유하지도 않은 종목의 시세를 받는다.
        if ordered and self.ws_client is not None:
            self.ws_client.subscribe(ordered)
            if getattr(self.ws_client, "is_connected", True):
                logger.info("실시간 시세 구독: %s", ordered)
            else:
                # 구독 목록에는 담기지만 재접속까지 시세가 오지 않는다 = 그동안 익절/손절 공백
                logger.error(
                    "실시간 시세 구독 보류 — WebSocket 미연결 상태입니다. "
                    "재접속까지 익절/손절 감시가 동작하지 않습니다: %s",
                    ordered,
                )
                self.engine.notify(
                    "[경고] 실시간 시세 미연결 — 재접속까지 익절/손절 감시가 멈춥니다. "
                    f"대상: {ordered}"
                )

    def send_daily_report(self, today: Optional[date] = None) -> None:
        """15:30 — 당일 매매 결과와 월간 누적 실적을 이메일로 발송."""
        today = today or date.today()
        summary = self.trade_store.daily_summary(today)
        monthly_pnl = self.trade_store.monthly_realized_pnl(today.year, today.month, up_to=today)

        total_asset = self.account.get_balance_snapshot().total_asset
        base_asset = total_asset - monthly_pnl
        monthly_return_pct = (monthly_pnl / base_asset * 100) if base_asset else 0.0

        subject, body = templates.daily_report_email(summary, monthly_pnl, monthly_return_pct)
        self.email.send(subject, body)
        logger.info("Daily report email sent for %s", today)
