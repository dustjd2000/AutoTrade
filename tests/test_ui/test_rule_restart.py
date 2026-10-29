"""규칙 자동 변경 뒤 엔진 재시작 처리 (`MainWindow._on_action_finished` 외).

금요일 평가가 `.env`를 바꾸면 UI가 설정을 다시 읽고 엔진을 재시작한다. 그 재시작이 실패하면 엔진이
주말 내내 멈춘 채로 아무도 모르므로 메일로 알리고, 사용자가 정지하는 중이라면 되살리지 않는다.

`test_exit_flag_persistence.py`와 같은 이유로 QApplication 없이 더블에 실제 메서드를 바인딩한다.
"""
from types import MethodType, SimpleNamespace

from src.ui import main_window
from src.ui.main_window import MainWindow


class FakeThread:
    def __init__(self, changed=True):
        self.changed = changed

    def consume_rule_change(self):
        changed, self.changed = self.changed, False
        return changed


class FakeStatusBar:
    def showMessage(self, *args):
        pass


def make_window(thread=None, *, stopping=False, restart_pending=False, check=False):
    fake = SimpleNamespace(
        _engine_thread=thread,
        _stopping=stopping,
        _restart_pending=restart_pending,
        _rule_restart_pending_check=check,
        _statusbar=FakeStatusBar(),
        loads=0,
        restarts=0,
        _set_actions_enabled=lambda enabled: None,
        _set_engine_running=lambda running: None,
    )
    fake._load_settings = lambda: setattr(fake, "loads", fake.loads + 1)
    fake._restart_engine = lambda: setattr(fake, "restarts", fake.restarts + 1)
    for name in (
        "_on_action_finished", "_alert_rule_restart_failure", "_clear_rule_restart_check",
        "_on_engine_failed", "_on_engine_finished",
    ):
        setattr(fake, name, MethodType(getattr(MainWindow, name), fake))
    return fake


def test_rule_change_reloads_and_restarts_and_arms_the_alert():
    fake = make_window(FakeThread())
    fake._on_action_finished("evaluate_rules", True, "")
    assert (fake.loads, fake.restarts, fake._rule_restart_pending_check) == (1, 1, True)


def test_rule_change_while_user_is_stopping_only_reloads():
    fake = make_window(FakeThread(), stopping=True)
    fake._on_action_finished("evaluate_rules", True, "")
    assert (fake.loads, fake.restarts, fake._rule_restart_pending_check) == (1, 0, False)


def test_engine_failure_after_auto_restart_sends_one_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(main_window, "send_restart_failure_alert", sent.append)
    fake = make_window(check=True)

    fake._on_engine_failed("RuntimeError: boom")
    fake._on_engine_failed("again")

    assert sent == ["RuntimeError: boom"]
    assert fake._rule_restart_pending_check is False


def test_engine_failure_without_auto_restart_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(main_window, "send_restart_failure_alert", sent.append)
    make_window()._on_engine_failed("boom")
    assert sent == []


def test_clean_finish_clears_the_check_without_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(main_window, "send_restart_failure_alert", sent.append)
    fake = make_window(check=True)
    fake._on_engine_finished()
    assert fake._rule_restart_pending_check is False and sent == []


def test_finish_while_stopping_reloads_unconsumed_rule_change():
    fake = make_window(FakeThread(), stopping=True)
    fake._on_engine_finished()
    assert fake.loads == 1
    assert fake._stopping is False and fake._engine_thread is None


def test_finish_without_rule_change_does_not_reload():
    fake = make_window(FakeThread(changed=False))
    fake._on_engine_finished()
    assert fake.loads == 0


def test_alert_mail_has_subject_and_error_and_never_raises(monkeypatch):
    mails = []

    class FakeNotifier:
        def __init__(self, settings):
            pass

        def send(self, subject, message, html=None, images=None):
            mails.append((subject, message))

    monkeypatch.setattr(main_window, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_window, "Settings", lambda: SimpleNamespace())
    monkeypatch.setattr(main_window, "EmailNotifier", FakeNotifier)

    main_window.send_restart_failure_alert("설정 오류: bad")

    assert "재시작 실패" in mails[0][0] and "설정 오류: bad" in mails[0][1]

    def explode(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(main_window, "EmailNotifier", explode)
    main_window.send_restart_failure_alert("x")  # 예외 없이 끝나야 한다
