from abc import ABC, abstractmethod

from src.core.events import MarketData, Signal


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
