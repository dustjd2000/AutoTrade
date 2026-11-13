"""장중 전환 체크박스(`MainWindow._on_switch_flag_toggled`)의 `.env` 저장·복원.

`test_exit_flag_persistence.py`와 같은 방식 — QApplication 없이 더블에 실제 메서드를 바인딩한다.
실제 `.env`는 건드리지 않는다(`save_env`·`load_env`를 가로챈다).
"""
import inspect
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ui import main_window
from src.ui.engine_thread import EngineThread
from src.ui.main_window import MainWindow


class FakeCheckbox:
    def __init__(self, checked=True):
        self._checked = checked

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = value


class FakeThread:
    def __init__(self, applied=True):
        self._applied = applied
        self.calls = []

    def set_switch_enabled(self, enabled):
        self.calls.append(enabled)
        return self._applied


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(main_window, "save_env", lambda path, values: calls.append(values))
    return calls


def make_fake_window(*, checked=True, thread=None):
    fake = SimpleNamespace(
        _syncing_exit_flags=False,
        _etf_switch_enabled=FakeCheckbox(checked),
        _engine_thread=thread,
    )
    fake._on_switch_flag_toggled = MethodType(MainWindow._on_switch_flag_toggled, fake)
    return fake


def test_toggling_off_saves_and_pushes_to_engine(saved):
    thread = FakeThread()
    fake = make_fake_window(checked=False, thread=thread)

    fake._on_switch_flag_toggled()

    assert saved == [{"ETF_SWITCH_ENABLED": "0"}]
    assert thread.calls == [False]


def test_toggling_on_saves_one(saved):
    fake = make_fake_window(checked=True, thread=None)

    fake._on_switch_flag_toggled()

    assert saved == [{"ETF_SWITCH_ENABLED": "1"}]


def test_no_save_while_restoring(saved):
    thread = FakeThread()
    fake = make_fake_window(checked=False, thread=thread)
    fake._syncing_exit_flags = True

    fake._on_switch_flag_toggled()

    assert saved == []
    assert thread.calls == []


class LoadDouble:
    """`_load_settings`가 만지는 나머지 위젯은 MagicMock으로 받아 넘긴다."""

    def __init__(self):
        self._syncing_exit_flags = False
        self._etf_switch_enabled = FakeCheckbox(True)
        self._ai_exit_enabled = FakeCheckbox(True)
        self._stop_loss_enabled = FakeCheckbox(True)
        # 인덱스를 비교하는 코드가 닿는 위젯은 MagicMock으로는 안 된다
        self._emergency_action = SimpleNamespace(findData=lambda data: 0, setCurrentIndex=lambda i: None)
        self._decision_time = self._buy_fallback_time = SimpleNamespace(setTime=lambda t: None)

    def __getattr__(self, name):
        mock = MagicMock()
        object.__setattr__(self, name, mock)
        return mock


@pytest.mark.parametrize("env, expected", [({"ETF_SWITCH_ENABLED": "0"}, False), ({}, True)])
def test_load_restores_state_without_writing(monkeypatch, saved, env, expected):
    monkeypatch.setattr(main_window, "load_env", lambda path: env)
    double = LoadDouble()
    double._etf_switch_enabled.setChecked(not expected)

    MainWindow._load_settings(double)

    assert double._etf_switch_enabled.isChecked() is expected
    assert saved == []


def test_engine_start_pushes_state(monkeypatch):
    thread = FakeThread()
    fake = make_fake_window(checked=False, thread=thread)
    monkeypatch.setattr(main_window, "save_env", lambda path, values: None)
    fake._on_exit_flag_toggled = lambda: None
    fake._set_actions_enabled = lambda running: None
    fake._holdings_timer = MagicMock()
    fake._refresh_holdings = lambda: None
    # 시작 경로가 뒤에서 건드리는 위젯은 MagicMock으로 채운다 — 예외를 삼키지 않아야 회귀가 드러난다
    fake._status_dot = fake._btn_start = fake._btn_stop = MagicMock()

    MainWindow._set_engine_running(fake, True)

    assert thread.calls == [False]


def test_engine_start_updates_the_screen_even_if_saving_flags_fails(monkeypatch):
    def locked(path, values):
        raise PermissionError("locked")

    thread = FakeThread()
    fake = make_fake_window(checked=False, thread=thread)
    monkeypatch.setattr(main_window, "save_env", locked)
    fake._on_exit_flag_toggled = lambda: locked(None, None)
    fake._set_actions_enabled = lambda running: None
    fake._holdings_timer = MagicMock()
    fake._refresh_holdings = lambda: None
    fake._status_dot = MagicMock()
    fake._btn_start = MagicMock()
    fake._btn_stop = MagicMock()

    MainWindow._set_engine_running(fake, True)

    fake._holdings_timer.start.assert_called_once()
    fake._btn_start.setEnabled.assert_called_with(False)
    fake._btn_stop.setEnabled.assert_called_with(True)
    assert thread.calls == [False]  # 저장은 실패해도 엔진에는 밀어 넣었다


def test_key_is_not_part_of_the_restart_comparison():
    assert "ETF_SWITCH_ENABLED" not in inspect.getsource(MainWindow._save_settings)


def test_engine_thread_set_switch_enabled():
    thread = EngineThread.__new__(EngineThread)
    thread._runtime = None
    assert thread.set_switch_enabled(False) is False

    thread._runtime = SimpleNamespace(engine=SimpleNamespace(etf_switch_enabled=True))
    assert thread.set_switch_enabled(False) is True
    assert thread._runtime.engine.etf_switch_enabled is False
