from src.core.events import MarketData, Signal
from src.strategy.base import BaseStrategy


class DummyStrategy(BaseStrategy):
    """프레임워크 검증용 더미 전략. 항상 HOLD를 반환한다."""

    @property
    def name(self) -> str:
        return "DummyStrategy"

    def generate_signal(self, data: MarketData) -> Signal:
        return Signal.HOLD
