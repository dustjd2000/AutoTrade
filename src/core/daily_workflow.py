import logging
from datetime import date
from typing import Optional

from src.api.account import AccountClient
from src.core.engine import TradingEngine
from src.core.events import OrderRequest, OrderSide, OrderType
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
            logger.warning("No buy plans for today (no recommendations).")
            return

        positions = self.account.get_positions()
        ordered_tickers = []
        for plan in plans:
            try:
                price = self.engine.market_data.get_current_price(plan.ticker).price
                quantity = int(plan.amount // price)
                if quantity <= 0:
                    logger.warning("계산된 수량이 0입니다. 건너뜁니다: %s", plan.ticker)
                    continue

                request = OrderRequest(
                    ticker=plan.ticker,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=quantity,
                )
                if not self.engine.risk_manager.approve(request, positions, reference_price=price):
                    logger.warning("Buy order rejected by risk manager: %s", plan.ticker)
                    self.engine.notify(f"매수 거부 (리스크 관리): {plan.name} ({plan.ticker})")
                    continue

                result = self.engine.order_client.send_order(request)
                self.engine.risk_manager.record_order(result)
                self.trade_store.record_fill(result)
                ordered_tickers.append(plan.ticker)
                logger.info("Buy order sent: %s x%d", plan.ticker, quantity)
            except Exception:
                logger.exception("Failed to execute buy for %s", plan.ticker)
                self.engine.notify(f"[오류] 매수 실패: {plan.name} ({plan.ticker})")

        # 매수한 종목의 실시간 시세를 구독해야 익절/손절 감시(RiskManager.check_exit)가 동작한다
        if ordered_tickers and self.ws_client is not None:
            self.ws_client.subscribe(ordered_tickers)
            logger.info("Subscribed to real-time quotes: %s", ordered_tickers)

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
