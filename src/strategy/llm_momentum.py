import logging
from dataclasses import dataclass
from typing import List

from src.core.events import MarketData, Signal
from src.llm.recommender import StockRecommendation
from src.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

# 1호 전략 자금 규칙 (PRD 5.5-B 4단계)
INVESTABLE_RATIO = 0.5  # 매수가능금액 = 예수금 × 1/2 (나머지 1/2은 항상 미투입)
TARGET_STOCK_COUNT = 3  # 매수가능금액을 3등분 → 종목당 예수금의 1/6


@dataclass
class BuyPlan:
    ticker: str
    name: str
    amount: float
    reason: str


class LLMMomentumStrategy(BaseStrategy):
    """1호 전략 — LLM 기반 급등 예상 대형주 매수 (PRD 5.5-B).

    08:45 LLM 추천 → 09:00 매수를 스케줄러가 트리거하는 시간 기반 전략이므로,
    실시간 시세 콜백(generate_signal)에서는 신규 진입 신호를 내지 않는다.
    보유 포지션의 청산은 RiskManager.check_exit(익절/손절)와 장 마감 강제청산이 담당한다.
    """

    def __init__(self):
        self._recommendations: List[StockRecommendation] = []

    @property
    def name(self) -> str:
        return "LLMMomentumStrategy"

    def set_recommendations(self, recommendations: List[StockRecommendation]) -> None:
        self._recommendations = recommendations

    def build_buy_plans(self, cash: float) -> List[BuyPlan]:
        """예수금과 추천 목록으로 종목별 매수 계획을 만든다.

        종목당 투입금액은 추천 개수와 무관하게 예수금 × 1/6으로 고정한다.
        추천이 3개 미만이면 모자란 몫은 매수하지 않고 현금으로 남긴다 (PRD 10절 확정, 2026-07-27).
        """
        if not self._recommendations:
            return []

        amount_per_stock = cash * INVESTABLE_RATIO / TARGET_STOCK_COUNT
        plans = [
            BuyPlan(ticker=r.ticker, name=r.name, amount=amount_per_stock, reason=r.reason)
            for r in self._recommendations[:TARGET_STOCK_COUNT]
        ]

        if len(plans) < TARGET_STOCK_COUNT:
            idle = amount_per_stock * (TARGET_STOCK_COUNT - len(plans))
            logger.warning(
                "Only %d recommendation(s) received. Keeping %.0f KRW idle as cash.",
                len(plans),
                idle,
            )
        return plans

    def generate_signal(self, data: MarketData) -> Signal:
        return Signal.HOLD
