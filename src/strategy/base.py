from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.core.events import MarketData, Signal


@dataclass
class StockRecommendation:
    """오늘 매수하기로 정한 종목 — 전략이 아침에 정하고 매수·매수 예정 표·AI 매도 판단이 읽는다.

    1호 전략(LLM 추천) 시절의 이름을 그대로 쓴다 — 2호 전략도 같은 매수 경로를 탄다 (PRD 5.5-C).
    """

    ticker: str
    name: str
    target_price: int          # 결정 시점 기준가 (2호는 그 시점 현재가)
    reason: str                # 결정 근거 — AI 매도 판단 프롬프트의 '아침 근거'로 실린다
    # 목표 매도가 — 2호는 쓰지 않는다(0 = 없음). AI 매도 판단 프롬프트가 읽는 칸이라 남긴다
    target_sell_price: int = 0
    # 결정 시점 현재가 — 0은 '모름'
    recommend_price: float = 0.0
    setup: str = ""
    # 결정 시나리오 — AI 매도 판단 프롬프트의 '아침 전망'으로 실린다
    outlook: str = ""


@dataclass
class BuyPlan:
    ticker: str
    name: str
    amount: float        # 종목당 배정액
    target_price: int    # 결정 시점 기준가
    reason: str
    recommend_price: float = 0.0


class BaseStrategy(ABC):
    """모든 전략이 구현해야 하는 공통 인터페이스."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate_signal(self, data: MarketData) -> Signal:
        """시세 데이터를 받아 BUY / SELL / HOLD 신호를 반환한다."""
        ...
