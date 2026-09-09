"""매수 확인 팝업의 청산 감시 안내 문구 (`MainWindow._exit_watch_text`, `_refresh_exit_flag_hint`).

Task 1(2026-09-09)이 익절 자동 청산과 단순익절을 엔진에서 걷어냈고, Task 6이 그 자리의
체크박스(`익절`/`단순익절적용`)를 `AI 매도 판단` 하나로 정리했다(PRD 10절). 이제 청산
경로는 두 갈래다 — 손절은 그대로 실시간 감시되는 자동 매도선이고, AI 매도 판단은 켜져
있으면 호출 주기(`_ai_exit_interval` 콤보)마다 보유 종목 전체를 보고 전량 매도 여부를
LLM이 판단한다. 입력값(%)은 손절선(자동)이자 AI에게 넘기는 참고선(비자동)이라는 이중
의미를 갖는데, 이 문구가 그 구분을 흐리면 "AI 매도 판단을 껐는데도 이익 쪽 청산이 되는
줄 알았다"거나 "손절이 꺼진 줄 몰랐다" 같은 사고로 이어진다 — 실계좌로 도는 프로그램이라
이 문구의 정확성이 곧 안전장치다.

이 저장소에는 PyQt6 위젯을 실제로 띄우는 테스트가 없다(QApplication 기반 테스트 인프라
부재). 여기서는 QApplication 없이도 되도록, 위젯이 필요로 하는 최소 인터페이스
(`.isChecked()` / `.text()` / `.currentText()`)만 흉내낸 순수 파이썬 더블에 `MainWindow`의
실제 메서드를 바인딩해서 부른다 — 문구 생성 로직 자체(프로덕션 코드)는 그대로 실행된다.
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


class FakeCombo:
    """QComboBox 더블 — `_exit_watch_text`가 읽는 `.currentText()`만 있으면 된다."""

    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text


class FakeLabel:
    """QLabel 더블 — `_refresh_exit_flag_hint`가 쓰는 `.setVisible()`/`.setText()`만 있으면 된다."""

    def __init__(self):
        self.visible = None
        self.text = None

    def setVisible(self, value):
        self.visible = value

    def setText(self, value):
        self.text = value


def make_fake_window(*, ai_exit=True, stop_loss=True, exit_percent="2", interval_text="15분"):
    """`_exit_watch_text`가 읽는 위젯만 흉내낸 가짜 `MainWindow`."""
    fake = SimpleNamespace(
        _ai_exit_enabled=FakeCheckable(ai_exit),
        _stop_loss_enabled=FakeCheckable(stop_loss),
        _exit_percent=FakeTextField(exit_percent),
        _ai_exit_interval=FakeCombo(interval_text),
    )
    fake._exit_watch_text = MethodType(MainWindow._exit_watch_text, fake)
    return fake


def make_fake_hint_window(*, ai_exit=True, stop_loss=True):
    """`_refresh_exit_flag_hint`가 읽고 쓰는 위젯만 흉내낸 가짜 `MainWindow`."""
    fake = SimpleNamespace(
        _ai_exit_enabled=FakeCheckable(ai_exit),
        _stop_loss_enabled=FakeCheckable(stop_loss),
        _exit_flag_hint=FakeLabel(),
    )
    fake._refresh_exit_flag_hint = MethodType(MainWindow._refresh_exit_flag_hint, fake)
    return fake


# ── _exit_watch_text ────────────────────────────────────────


def test_exit_watch_text_warns_when_both_are_off():
    """AI 매도 판단·손절이 모두 꺼져 있으면 실시간 청산이 전혀 없다고 분명히 말해야 한다."""
    fake = make_fake_window(ai_exit=False, stop_loss=False)

    text = fake._exit_watch_text()

    assert "실시간 청산이 동작하지 않습니다" in text


def test_exit_watch_text_flags_stop_loss_being_off_even_when_ai_exit_is_on():
    """손절 끔 + AI 매도 판단 켬 — 손실 쪽 실시간 보호가 없다는 사실을 숨기면 안 된다.

    (이 화면에서 가장 위험한 오해가 "손절이 꺼진 줄 모르는 것"이다.)
    """
    fake = make_fake_window(ai_exit=True, stop_loss=False)

    text = fake._exit_watch_text()

    assert "손절이 꺼져 있어" in text
    assert "손실 쪽 실시간 청산이 없습니다" in text


def test_exit_watch_text_flags_ai_exit_being_off_even_when_stop_loss_is_on():
    """AI 매도 판단 끔 + 손절 켬 — 이익 쪽 청산이 통째로 없다는 사실이 드러나야 한다."""
    fake = make_fake_window(ai_exit=False, stop_loss=True, exit_percent="3")

    text = fake._exit_watch_text()

    assert "손절 -3%" in text
    assert "AI 매도 판단이 꺼져 있어" in text
    assert "이익 실현 쪽 실시간 청산이 없습니다" in text


def test_exit_watch_text_describes_ai_exit_as_periodic_judgement_not_an_auto_sell_line():
    """익절 목표선은 참고선일 뿐이다 — AI가 켜져 있어도 '닿으면 자동으로 팔린다'는 인상을 주면 안 된다."""
    fake = make_fake_window(ai_exit=True, stop_loss=True, exit_percent="2", interval_text="15분")

    text = fake._exit_watch_text()

    assert "손절 -2%" in text
    assert "15분마다" in text
    assert "참고선" in text
    assert "자동으로 걸리지" in text or "자동으로 팔리지" in text


# ── _refresh_exit_flag_hint ─────────────────────────────────


def test_refresh_exit_flag_hint_hides_when_both_are_on():
    fake = make_fake_hint_window(ai_exit=True, stop_loss=True)

    fake._refresh_exit_flag_hint()

    assert fake._exit_flag_hint.visible is False


def test_refresh_exit_flag_hint_warns_about_stop_loss_specifically():
    """손절이 꺼진 줄 모르는 것이 가장 위험한 오해다 — 문구가 손절을 콕 집어야 한다."""
    fake = make_fake_hint_window(ai_exit=True, stop_loss=False)

    fake._refresh_exit_flag_hint()

    assert fake._exit_flag_hint.visible is True
    assert "손절" in fake._exit_flag_hint.text


def test_refresh_exit_flag_hint_warns_about_ai_exit_specifically():
    fake = make_fake_hint_window(ai_exit=False, stop_loss=True)

    fake._refresh_exit_flag_hint()

    assert fake._exit_flag_hint.visible is True
    assert "AI 매도 판단" in fake._exit_flag_hint.text
