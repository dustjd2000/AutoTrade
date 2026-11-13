"""장애 알림 메일은 환경변수 값 대신 화면에 보이는 대응 이름을 쓴다."""
import pytest

from src.core.engine import TradingEngine


def _engine(action):
    engine = TradingEngine.__new__(TradingEngine)
    engine.emergency_action = action
    engine.messages = []
    engine.notify = engine.messages.append
    engine.stop = lambda: None
    engine.force_close_all_positions = lambda reason="": None
    return engine


@pytest.mark.parametrize(
    "action, label", [("hold", "보유 유지"), ("close_all", "전량 청산"), ("weird", "weird")]
)
def test_alert_uses_korean_label(action, label):
    engine = _engine(action)

    engine.handle_fatal_error("boom")

    assert engine.messages == [f"[긴급] 장애 발생: boom (대응: {label})"]
