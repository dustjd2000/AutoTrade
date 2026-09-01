"""단일 인스턴스 판정 — 잘못 걸리면 그날 매매가 통째로 빠진다."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def run_ui(monkeypatch, tmp_path):
    """scripts/run_ui.py를 불러온다.

    import 시점의 load_dotenv를 막는다 — 실제 `.env`가 os.environ에 들어가면
    기본값을 검증하는 다른 테스트(test_settings)가 실행 순서에 따라 흔들린다.
    PID 경로도 tmp로 돌려 실제 실행 중인 인스턴스를 건드리지 않는다.
    """
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    spec = importlib.util.spec_from_file_location(
        "run_ui_under_test", ROOT / "scripts" / "run_ui.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PID_PATH", tmp_path / "autotrade.pid")
    return module


def write_pid_file(module, pid, mtime=None):
    module.PID_PATH.write_text(str(pid), encoding="utf-8")
    if mtime is not None:
        os.utime(module.PID_PATH, (mtime, mtime))


def test_missing_pid_file_is_not_stale(run_ui):
    assert run_ui._pid_file_is_stale() is False


def test_pid_file_written_before_the_last_boot_is_stale(run_ui, monkeypatch):
    """재부팅하면 PID가 재사용된다 — 그 전에 쓰인 파일은 믿을 수 없다."""
    monkeypatch.setattr(run_ui, "_boot_epoch", lambda: 2_000.0)
    write_pid_file(run_ui, 4321, mtime=1_000.0)

    assert run_ui._pid_file_is_stale() is True


def test_pid_file_written_after_the_last_boot_is_live(run_ui, monkeypatch):
    monkeypatch.setattr(run_ui, "_boot_epoch", lambda: 2_000.0)
    write_pid_file(run_ui, 4321, mtime=3_000.0)

    assert run_ui._pid_file_is_stale() is False


def test_unreadable_boot_time_does_not_discard_the_pid_file(run_ui, monkeypatch):
    """부팅 시각을 못 읽었다고 stale로 몰면 중복 실행을 막지 못한다 — 안전한 쪽으로 붙는다."""
    def broken():
        raise OSError("GetTickCount64 failed")

    monkeypatch.setattr(run_ui, "_boot_epoch", broken)
    write_pid_file(run_ui, 4321, mtime=1_000.0)

    assert run_ui._pid_file_is_stale() is False


def test_stale_pid_file_does_not_report_a_running_instance(run_ui, monkeypatch):
    """액세스 거부로 '살아 있다'고 읽히는 PID라도, 부팅 이전 기록이면 무시해야 한다.

    이것이 없으면 재부팅 후 그 번호를 SYSTEM 서비스가 쓰는 순간 --auto-start가
    영구히 막힌다 (대체도 실패하고 sys.exit(1)).
    """
    write_pid_file(run_ui, 4321)
    monkeypatch.setattr(run_ui, "_pid_file_is_stale", lambda: True)

    assert run_ui._running_instance_pid() == 0


def test_clear_pid_removes_only_our_own_record(run_ui):
    """--auto-start 대체로 다음 인스턴스가 이미 자기 PID를 써 놓았을 수 있다."""
    write_pid_file(run_ui, os.getpid())
    run_ui._clear_pid()
    assert not run_ui.PID_PATH.exists()

    write_pid_file(run_ui, os.getpid() + 1)
    run_ui._clear_pid()
    assert run_ui.PID_PATH.exists()


def test_clear_pid_survives_a_missing_file(run_ui):
    run_ui._clear_pid()   # 예외가 나면 종료 경로가 깨진다
