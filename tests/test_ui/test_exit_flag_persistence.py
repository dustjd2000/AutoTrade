"""청산 체크박스(`AI 매도 판단`/`손절`) 상태의 `.env` 저장 (`MainWindow._on_exit_flag_toggled`).

2026-09-09까지 두 체크박스는 `.env`에 저장되지 않아, 프로그램을 다시 켤 때는 물론 **설정
저장에 따른 엔진 재시작만으로도** 항상 켬으로 돌아갔다. 손절을 꺼 두고 매매하다 재시작
한 번에 손절이 되살아나는(또는 그 반대인) 상태를 사용자가 알아챌 방법이 없었다.

지금은 켜고 끄는 즉시 저장하고 `Settings`가 엔진 시작 때 그 값을 읽는다. 다만 **저장이
재시작을 부르면 안 된다** — 재시작 사이에는 WebSocket이 끊겨 손절 감시가 멈추는데, 정작
손절을 끄고 싶은 순간이 그때다. 여기서 지키는 것은 그 두 가지다: 저장은 항상 일어나고,
`_save_settings`가 다루는 값(= 재시작 판정 대상)에는 들어가지 않는다.

`test_exit_watch_text.py`와 같은 이유로 QApplication 없이 더블에 실제 메서드를 바인딩해
부른다.
"""
from types import MethodType, SimpleNamespace

import pytest

from src.ui import main_window
from src.ui.main_window import MainWindow


class FakeCheckable:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class FakeThread:
    """`EngineThread` 더블 — `set_exit_flags`가 받은 값만 기억한다."""

    def __init__(self, applied=True):
        self._applied = applied
        self.calls = []

    def set_exit_flags(self, ai_exit, stop_loss):
        self.calls.append((ai_exit, stop_loss))
        return self._applied


@pytest.fixture
def saved(monkeypatch):
    """`save_env` 호출을 가로채 (경로, 값) 목록으로 모은다 — 실제 .env는 건드리지 않는다."""
    calls = []
    monkeypatch.setattr(main_window, "save_env", lambda path, values: calls.append((path, values)))
    return calls


def make_fake_window(*, ai_exit=True, stop_loss=True, thread=None):
    fake = SimpleNamespace(
        _syncing_exit_flags=False,
        _ai_exit_enabled=FakeCheckable(ai_exit),
        _stop_loss_enabled=FakeCheckable(stop_loss),
        _engine_thread=thread,
        # 화면 연동(호출 주기 활성화·경고 문구)은 여기서 확인하려는 대상이 아니다
        _sync_exit_flag_widgets=lambda: None,
    )
    fake._on_exit_flag_toggled = MethodType(MainWindow._on_exit_flag_toggled, fake)
    return fake


@pytest.mark.parametrize(
    "ai_exit, stop_loss, expected",
    [
        (True, True, {"AI_EXIT_ENABLED": "1", "STOP_LOSS_ENABLED": "1"}),
        (False, True, {"AI_EXIT_ENABLED": "0", "STOP_LOSS_ENABLED": "1"}),
        (True, False, {"AI_EXIT_ENABLED": "1", "STOP_LOSS_ENABLED": "0"}),
        (False, False, {"AI_EXIT_ENABLED": "0", "STOP_LOSS_ENABLED": "0"}),
    ],
)
def test_toggling_writes_both_flags_to_env(saved, ai_exit, stop_loss, expected):
    """네 조합 모두 그대로 저장된다 — 다음에 엔진이 뜰 때 `Settings`가 이 값을 읽는다."""
    fake = make_fake_window(ai_exit=ai_exit, stop_loss=stop_loss)

    fake._on_exit_flag_toggled()

    assert [values for _path, values in saved] == [expected]


def test_saved_keys_are_not_part_of_the_restart_comparison():
    """저장은 하되 재시작은 시키지 않는다 — `_save_settings`가 두 키를 다루면 안 된다.

    `_needs_restart_for_changed_settings`는 `_save_settings`가 돌려준 값과 엔진이 읽어간
    값을 통째로 비교한다. 두 키가 거기에 섞이면 체크박스를 건드린 뒤 설정을 저장할 때마다
    엔진이 재시작되고, 그 사이 손절 감시가 멈춘다.
    """
    import inspect

    source = inspect.getsource(MainWindow._save_settings)

    assert "AI_EXIT_ENABLED" not in source
    assert "STOP_LOSS_ENABLED" not in source


def test_flags_are_saved_even_while_the_engine_is_stopped(saved):
    """엔진이 꺼져 있어도 저장한다 — 시작 전에 미리 끄는 것이 가장 흔한 사용이다."""
    fake = make_fake_window(stop_loss=False, thread=None)

    fake._on_exit_flag_toggled()

    assert saved[0][1]["STOP_LOSS_ENABLED"] == "0"


def test_running_engine_gets_the_flags_before_the_file_is_written(monkeypatch):
    """파일 쓰기가 실패해도 방금 켜고 끈 것은 이미 엔진에 들어가 있어야 한다."""
    order = []

    class RecordingThread(FakeThread):
        def set_exit_flags(self, ai_exit, stop_loss):
            order.append("engine")
            return super().set_exit_flags(ai_exit, stop_loss)

    monkeypatch.setattr(main_window, "save_env", lambda path, values: order.append("save"))
    thread = RecordingThread()
    fake = make_fake_window(ai_exit=False, thread=thread)

    fake._on_exit_flag_toggled()

    assert order == ["engine", "save"]
    assert thread.calls == [(False, True)]


def test_sync_is_skipped_while_flags_are_being_restored(saved):
    """`_load_settings`가 복원하는 동안에는 저장하지 않는다 — 방금 읽은 값이다."""
    fake = make_fake_window()
    fake._syncing_exit_flags = True

    fake._on_exit_flag_toggled()

    assert saved == []
