"""매수 확인 팝업의 청산 감시 안내 문구 (`MainWindow._exit_watch_text` 등).

익절 자동 청산과 단순익절은 Task 1(2026-09-09)에서 엔진에서 걷어냈다 (PRD 10절). 그런데
`MainWindow`의 체크박스와 팝업 문구는 Task 6이 정리하기 전까지 그대로 남아 있어,
`_take_profit_watched()`를 고치지 않으면 손절을 꺼 둔 채 '익절' 체크박스만 켜져 있을 때
"자동 감시되며 닿으면 전량 매도합니다"라는, 실제로는 아무 보호도 없는 문구가 매수 확인
팝업에 그대로 뜬다 — Task 1 수정 라운드에서 발견됨.

이 저장소에는 PyQt6 위젯을 실제로 띄우는 테스트가 없다(QApplication 기반 테스트 인프라
부재). 여기서는 QApplication 없이도 되도록, 위젯이 필요로 하는 최소 인터페이스
(`.isChecked()` / `.text()`)만 흉내낸 순수 파이썬 더블에 `MainWindow`의 실제 메서드를
바인딩해서 부른다 — 문구 생성 로직 자체(프로덕션 코드)는 그대로 실행된다.
"""
from types import MethodType, SimpleNamespace

from src.ui.main_window import MainWindow


class FakeCheckable:
    """QCheckBox 더블 — `.isChecked()`만 있으면 된다."""

    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class FakeTextField:
    """QLineEdit 더블 — `.text()`만 있으면 된다."""

    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


def make_fake_window(
    *, take_profit=False, simple_take_profit=False, stop_loss=True, exit_percent="2"
):
    """`_exit_watch_text` 계열 메서드가 읽는 위젯만 흉내낸 가짜 `MainWindow`."""
    fake = SimpleNamespace(
        _take_profit_enabled=FakeCheckable(take_profit),
        _simple_take_profit_enabled=FakeCheckable(simple_take_profit),
        _stop_loss_enabled=FakeCheckable(stop_loss),
        _exit_percent=FakeTextField(exit_percent),
    )
    fake._take_profit_watched = MethodType(MainWindow._take_profit_watched, fake)
    fake._take_profit_label = MethodType(MainWindow._take_profit_label, fake)
    fake._exit_watch_text = MethodType(MainWindow._exit_watch_text, fake)
    return fake


def test_take_profit_watched_is_always_false():
    """익절 자동 청산은 걷어냈다 — 체크박스 상태와 무관하게 항상 False다."""
    assert make_fake_window(take_profit=True)._take_profit_watched() is False
    assert make_fake_window(simple_take_profit=True)._take_profit_watched() is False
    assert make_fake_window(take_profit=False, simple_take_profit=False)._take_profit_watched() is False


def test_exit_watch_text_does_not_claim_take_profit_is_watched_when_stop_loss_is_off():
    """손절 끔 + 익절 켬 — 실시간 보호가 전혀 없는데 '자동 감시'라고 말하면 안 된다."""
    fake = make_fake_window(take_profit=True, stop_loss=False)

    text = fake._exit_watch_text()

    assert "자동 감시" not in text
    assert "실시간 청산이 동작하지 않습니다" in text


def test_exit_watch_text_only_mentions_stop_loss_when_it_alone_is_on():
    """손절만 켜져 있으면(익절 체크박스가 어떤 상태든) 안내는 손절만 말해야 한다."""
    fake = make_fake_window(take_profit=True, stop_loss=True, exit_percent="3")

    text = fake._exit_watch_text()

    assert "손절 -3%" in text
    assert "익절" not in text
