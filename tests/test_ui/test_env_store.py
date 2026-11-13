import pytest

from src.ui import env_store
from src.ui.env_store import load_env, save_env


def _flaky_replace(monkeypatch, failures):
    """처음 `failures`번은 PermissionError(파일 잠김), 그 뒤로는 진짜 os.replace."""
    real = env_store.os.replace
    calls = []

    def replace(src, dst):
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError("locked")
        real(src, dst)

    monkeypatch.setattr(env_store.os, "replace", replace)
    monkeypatch.setattr(env_store.time, "sleep", lambda s: None)
    return calls


def test_save_env_retries_while_the_file_is_locked(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    calls = _flaky_replace(monkeypatch, failures=2)

    save_env(path, {"A": "2"})

    assert load_env(path) == {"A": "2"}
    assert len(calls) == 3


def test_save_env_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    calls = _flaky_replace(monkeypatch, failures=99)

    with pytest.raises(PermissionError):
        save_env(path, {"A": "2"})

    assert len(calls) == env_store.REPLACE_ATTEMPTS
    assert load_env(path) == {"A": "1"}


def test_save_env_round_trips_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 주석\nA=1\nB=2\n", encoding="utf-8")

    save_env(path, {"B": "3", "C": "4"})

    assert load_env(path) == {"A": "1", "B": "3", "C": "4"}
    assert "# 주석" in path.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_save_env_creates_a_missing_file(tmp_path):
    path = tmp_path / ".env"

    save_env(path, {"A": "1"})

    assert load_env(path) == {"A": "1"}
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
