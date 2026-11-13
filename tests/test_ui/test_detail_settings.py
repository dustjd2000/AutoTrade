"""설정 탭 새로 UI에 올라온 `.env` 값들의 불러오기·저장·저장 전 검사.

`test_switch_flag_persistence.py`와 같은 방식 — QApplication 없이 더블에 실제 메서드를 바인딩한다.
실제 `.env`는 건드리지 않는다(`save_env`·`load_env`를 가로챈다).
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QTime

from config.settings import (
    DEFAULT_BUY_TIME_HHMM,
    DEFAULT_RECOMMEND_TIME_HHMM,
    TUNE_SKIP_MONTHLY_RETURN_CHOICES,
)
from src.ui import main_window
from src.ui.main_window import MainWindow, _validate_env_values


class Text:
    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text

    def setText(self, value):
        self._text = value


class Check:
    def __init__(self, checked=True):
        self._checked = checked

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = value


class TimeBox:
    def __init__(self, hhmm="00:00"):
        self._time = QTime.fromString(hhmm, "HH:mm")

    def time(self):
        return self._time

    def setTime(self, value):
        self._time = value


class Combo:
    """`_NoScrollComboBox`의 data 기반 선택만 흉내 낸다."""

    def __init__(self, datas, current=0):
        self._datas = list(datas)
        self._index = current

    def findData(self, data):
        return self._datas.index(data) if data in self._datas else -1

    def setCurrentIndex(self, index):
        self._index = index

    def currentData(self):
        return self._datas[self._index]


TUNE_DATAS = [f"{v:g}" for v in TUNE_SKIP_MONTHLY_RETURN_CHOICES]


class WindowDouble:
    """`_load_settings`/`_save_settings`가 만지는 위젯을 가짜로 채운다."""

    def __init__(self):
        self._syncing_exit_flags = False
        self._app_key = Text("k")
        self._app_secret = Text("s")
        self._account = Text("123")
        self._anthropic_key = Text("")
        self._llm_model = Text("")
        self._email_from = Text("a@b.c")
        self._email_to = Text("d@e.f")
        self._smtp_host = Text("")
        self._smtp_port = Text("")
        self._smtp_password = Text("")
        self._exit_percent = Text("2")
        self._take_profit_percent = Text("2")
        self._trail_activate_percent = Text("0.5")
        self._trail_giveback_percent = Text("0.25")
        self._ai_exit_step = Combo(["0.5", "1"])
        self._investable_ratio = Combo([50])
        self._ai_exit_enabled = Check()
        self._stop_loss_enabled = Check()
        self._etf_switch_enabled = Check()
        self._radio_live = Check(False)
        self._radio_paper = Check(True)
        self._decision_time = TimeBox("09:05")
        self._buy_fallback_time = TimeBox("09:10")
        self._etf_signal_band = Text("")
        self._etf_flip_enabled = Check(True)
        self._rule_auto_apply = Check(True)
        self._etf_start_date = Text("")
        self._etf_kill_loss = Text("")
        self._switch_index_percent = Text("")
        self._switch_strength_minutes = Text("")
        self._ai_exit_drawdown = Text("")
        self._tune_skip_percent = Combo(TUNE_DATAS, TUNE_DATAS.index("5"))
        self._commission_percent = Text("")
        self._tax_percent = Text("")
        self._slippage_percent = Text("")
        self._emergency_action = Combo(["hold", "close_all"])
        self._statusbar = MagicMock()
        self._engine_thread = None
        self._applied_env = None
        self._update_mode_hint = lambda: None
        self._select_step_value = lambda raw: None
        self._select_combo_value = lambda combo, raw, default: None
        self._needs_restart_for_changed_settings = lambda values: False
        self._restart_engine = lambda: None

    def _select_tune_skip_value(self, raw):
        MainWindow._select_tune_skip_value(self, raw)


@pytest.fixture
def env_io(monkeypatch):
    """`load_env`가 돌려줄 내용을 바꿀 수 있고, `save_env` 호출을 모은다."""
    state = SimpleNamespace(env={}, saved=[])
    monkeypatch.setattr(main_window, "load_env", lambda path: dict(state.env))
    monkeypatch.setattr(main_window, "save_env", lambda path, values: state.saved.append(values))
    monkeypatch.setattr(main_window.QMessageBox, "warning", MagicMock())
    return state


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch):
    """검사가 `os.environ`을 잠시 덮어쓰므로, 실제 값이 섞이지 않게 관련 키를 비운다."""
    for key in (
        "DECISION_TIME", "BUY_FALLBACK_TIME", "ETF_START_DATE", "ETF_SWITCH_INDEX_PERCENT",
        "TRADE_MODE", "LIVE_TRADE_CONFIRMED", "KIWOOM_APP_KEY", "KIWOOM_APP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_puts_env_values_into_new_widgets(env_io):
    env_io.env = {
        "DECISION_TIME": "09:20",
        "BUY_FALLBACK_TIME": "09:30",
        "ETF_SIGNAL_BAND_PERCENT": "0.3",
        "ETF_FLIP_ENABLED": "0",
        "RULE_AUTO_APPLY_ENABLED": "0",
        "ETF_START_DATE": " 2026-11-13 ",
        "ETF_KILL_LOSS_PERCENT": "7",
        "ETF_SWITCH_INDEX_PERCENT": "0.8",
        "ETF_SWITCH_STRENGTH_MINUTES": "3",
        "AI_EXIT_DRAWDOWN_PERCENT": "2",
        "TUNE_SKIP_MONTHLY_RETURN_PERCENT": "7.50",
        "COMMISSION_PERCENT": "0.02",
        "TAX_PERCENT": "0.18",
        "SLIPPAGE_PERCENT": "0.2",
        "EMERGENCY_ACTION": "close_all",
        "LLM_MODEL": "claude-x",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "465",
        "SMTP_PASSWORD": "pw",
    }
    w = WindowDouble()

    MainWindow._load_settings(w)

    assert w._decision_time.time().toString("HH:mm") == "09:20"
    assert w._buy_fallback_time.time().toString("HH:mm") == "09:30"
    assert w._etf_signal_band.text() == "0.3"
    assert w._etf_flip_enabled.isChecked() is False
    assert w._rule_auto_apply.isChecked() is False
    assert w._etf_start_date.text() == "2026-11-13"
    assert w._etf_kill_loss.text() == "7"
    assert w._switch_index_percent.text() == "0.8"
    assert w._switch_strength_minutes.text() == "3"
    assert w._ai_exit_drawdown.text() == "2"
    assert w._tune_skip_percent.currentData() == "7.5"
    assert w._commission_percent.text() == "0.02"
    assert w._tax_percent.text() == "0.18"
    assert w._slippage_percent.text() == "0.2"
    assert w._emergency_action.currentData() == "close_all"
    assert w._llm_model.text() == "claude-x"
    assert (w._smtp_host.text(), w._smtp_port.text(), w._smtp_password.text()) == (
        "smtp.example.com", "465", "pw",
    )
    assert env_io.saved == []


def test_load_falls_back_to_settings_defaults(env_io):
    env_io.env = {"DECISION_TIME": "oops", "EMERGENCY_ACTION": "bogus"}
    w = WindowDouble()
    w._tune_skip_percent.setCurrentIndex(0)

    MainWindow._load_settings(w)

    assert w._decision_time.time().toString("HH:mm") == DEFAULT_RECOMMEND_TIME_HHMM
    assert w._buy_fallback_time.time().toString("HH:mm") == DEFAULT_BUY_TIME_HHMM
    assert w._etf_signal_band.text() == "0"
    assert w._etf_flip_enabled.isChecked() is True
    assert w._rule_auto_apply.isChecked() is True
    assert w._etf_start_date.text() == ""
    assert w._etf_kill_loss.text() == "5"
    assert w._switch_index_percent.text() == "0.5"
    assert w._switch_strength_minutes.text() == "5"
    assert w._ai_exit_drawdown.text() == "1"
    assert w._tune_skip_percent.currentData() == "5"
    assert w._commission_percent.text() == "0.015"
    assert w._tax_percent.text() == "0.20"
    assert w._slippage_percent.text() == "0.1"
    assert w._emergency_action.currentData() == "hold"
    assert w._llm_model.text() == "claude-sonnet-5"
    assert (w._smtp_host.text(), w._smtp_port.text(), w._smtp_password.text()) == (
        "smtp.gmail.com", "587", "",
    )


def test_save_writes_every_new_key_in_expected_form(env_io):
    w = WindowDouble()
    w._decision_time = TimeBox("09:07")
    w._buy_fallback_time = TimeBox("09:12")
    w._etf_signal_band.setText("0.4")
    w._etf_flip_enabled.setChecked(False)
    w._rule_auto_apply.setChecked(False)
    w._etf_start_date.setText(" 2026-11-13 ")
    w._etf_kill_loss.setText("6")
    w._switch_index_percent.setText("0.7")
    w._switch_strength_minutes.setText("4")
    w._ai_exit_drawdown.setText("1.5")
    w._tune_skip_percent.setCurrentIndex(TUNE_DATAS.index("7.5"))
    w._commission_percent.setText("0.02")
    w._tax_percent.setText("0.18")
    w._slippage_percent.setText("0.15")
    w._emergency_action.setCurrentIndex(1)
    w._llm_model.setText("claude-x")
    w._smtp_host.setText("smtp.example.com")
    w._smtp_port.setText("465")
    w._smtp_password.setText("pw")

    values = MainWindow._save_settings(w)

    assert env_io.saved == [values]
    assert values["DECISION_TIME"] == "09:07"
    assert values["BUY_FALLBACK_TIME"] == "09:12"
    assert values["ETF_SIGNAL_BAND_PERCENT"] == "0.4"
    assert values["ETF_FLIP_ENABLED"] == "0"
    assert values["RULE_AUTO_APPLY_ENABLED"] == "0"
    assert values["ETF_START_DATE"] == "2026-11-13"
    assert values["ETF_KILL_LOSS_PERCENT"] == "6"
    assert values["ETF_SWITCH_INDEX_PERCENT"] == "0.7"
    assert values["ETF_SWITCH_STRENGTH_MINUTES"] == "4"
    assert values["AI_EXIT_DRAWDOWN_PERCENT"] == "1.5"
    assert values["TUNE_SKIP_MONTHLY_RETURN_PERCENT"] == "7.5"
    assert values["COMMISSION_PERCENT"] == "0.02"
    assert values["TAX_PERCENT"] == "0.18"
    assert values["SLIPPAGE_PERCENT"] == "0.15"
    assert values["EMERGENCY_ACTION"] == "close_all"
    assert values["LLM_MODEL"] == "claude-x"
    assert values["SMTP_HOST"] == "smtp.example.com"
    assert values["SMTP_PORT"] == "465"
    assert values["SMTP_PASSWORD"] == "pw"


def test_save_empty_numeric_fields_fall_back_to_defaults(env_io):
    w = WindowDouble()

    values = MainWindow._save_settings(w)

    assert values["ETF_SIGNAL_BAND_PERCENT"] == "0"
    assert values["ETF_FLIP_ENABLED"] == "1"
    assert values["ETF_KILL_LOSS_PERCENT"] == "5"
    assert values["ETF_SWITCH_INDEX_PERCENT"] == "0.5"
    assert values["ETF_SWITCH_STRENGTH_MINUTES"] == "5"
    assert values["AI_EXIT_DRAWDOWN_PERCENT"] == "1"
    assert values["COMMISSION_PERCENT"] == "0.015"
    assert values["TAX_PERCENT"] == "0.20"
    assert values["SLIPPAGE_PERCENT"] == "0.1"
    assert values["TUNE_SKIP_MONTHLY_RETURN_PERCENT"] == "5"
    assert values["EMERGENCY_ACTION"] == "hold"
    assert values["LLM_MODEL"] == "claude-sonnet-5"
    assert values["SMTP_HOST"] == "smtp.gmail.com"
    assert values["SMTP_PORT"] == "587"
    assert values["SMTP_PASSWORD"] == ""


def test_save_blocks_buy_time_too_close_to_decision(env_io):
    w = WindowDouble()
    w._buy_fallback_time = TimeBox("09:06")  # 결정 09:05 + 1분
    before = dict(os.environ)

    result = MainWindow._save_settings(w, show_popup=True)

    assert result is None
    assert env_io.saved == []
    assert dict(os.environ) == before
    assert "매수 안전망 시각" in w._statusbar.showMessage.call_args.args[0]
    main_window.QMessageBox.warning.assert_called_once()


def test_save_blocks_bad_start_date_without_popup_when_unattended(env_io):
    w = WindowDouble()
    w._etf_start_date.setText("2026/11/13")

    result = MainWindow._save_settings(w)  # show_popup=False — 시작 경로

    assert result is None
    assert env_io.saved == []
    main_window.QMessageBox.warning.assert_not_called()


def test_validate_restores_environ_exactly(monkeypatch):
    monkeypatch.setenv("TAX_PERCENT", "0.3")
    monkeypatch.delenv("SLIPPAGE_PERCENT", raising=False)
    before = dict(os.environ)

    assert _validate_env_values({"TAX_PERCENT": "0.2", "SLIPPAGE_PERCENT": "0.1"}) is None
    assert dict(os.environ) == before
    assert _validate_env_values({"ETF_SWITCH_INDEX_PERCENT": "0"}) is not None
    assert dict(os.environ) == before


def test_validate_reports_garbage_number_as_message():
    assert _validate_env_values({"COMMISSION_PERCENT": "abc"}) is not None


def test_start_engine_does_not_start_when_save_rejects(monkeypatch):
    started = []
    w = SimpleNamespace(
        _engine_thread=None,
        _save_settings=lambda: None,
        _alert_rule_restart_failure=lambda msg: None,
    )
    monkeypatch.setattr(main_window, "EngineThread", lambda *a, **k: started.append(1))

    MainWindow._start_engine(w)

    assert started == []
