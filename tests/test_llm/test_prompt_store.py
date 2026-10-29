import logging
from datetime import date

import pytest

from src.llm.prompt_store import PromptStore, next_version

ORDER = ("a", "b")
DEFAULTS = {"a": "## A 기본", "b": "## B 기본"}


def make_store(tmp_path, version="v0"):
    return PromptStore(tmp_path / "prompt", ORDER, DEFAULTS, version, label="테스트")


def test_falls_back_to_code_defaults_without_files(tmp_path):
    store = make_store(tmp_path)
    assert store.load_sections() == DEFAULTS
    assert store.load_version() == "v0"


def test_reads_sections_and_version_from_files(tmp_path):
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "a.md").write_text("## A 파일", encoding="utf-8")
    (folder / "version").write_text("20261001\n", encoding="utf-8")
    (folder / "base").write_text("v0", encoding="utf-8")

    store = make_store(tmp_path)

    assert store.load_sections() == {"a": "## A 파일", "b": "## B 기본"}
    assert store.load_version() == "20261001"


def test_blank_or_broken_files_fall_back(tmp_path):
    """손상 파일 하나가 매도 판단을 멈추면 안 된다 — 공백·디코딩 실패는 기본값으로 떨어진다."""
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "a.md").write_text("   \n", encoding="utf-8")
    (folder / "b.md").write_bytes(b"\xff\xfe\xfa")

    assert make_store(tmp_path).load_sections() == DEFAULTS


def test_next_version_is_the_day_it_was_changed():
    assert next_version("20260903", date(2026, 9, 10)) == "20260910"


@pytest.mark.parametrize(
    "current, expected",
    [("20260910", "20260910-2"), ("20260910-2", "20260910-3")],
)
def test_next_version_tags_a_second_change_on_the_same_day(current, expected):
    """이력이 `history/<version>/`에 쌓이므로 같은 날 이름이 겹치면 앞 이력을 덮어쓴다."""
    assert next_version(current, date(2026, 9, 10)) == expected


@pytest.mark.parametrize("current", ["v11", "쓰레기", "", "2026-09-10"])
def test_next_version_ignores_old_or_broken_values(current):
    assert next_version(current, date(2026, 9, 10)) == "20260910"


def test_save_writes_files_archives_history_and_bumps_version(tmp_path):
    store = make_store(tmp_path)
    new_version = store.save({"b": "## B 새 내용"}, reason="시험", today=date(2026, 9, 29))

    assert new_version == "20260929"
    assert store.load_version() == "20260929"
    # 고치지 않은 절도 기본값 그대로 파일에 쓰인다
    assert store.load_sections() == {"a": "## A 기본", "b": "## B 새 내용"}
    assert sorted(p.name for p in (tmp_path / "prompt").glob("*.md")) == ["a.md", "b.md"]
    history = tmp_path / "prompt" / "history" / "v0"
    assert (history / "a.md").read_text(encoding="utf-8") == "## A 기본"
    assert "시험" in (history / "why.md").read_text(encoding="utf-8")


def test_second_save_archives_the_first_version(tmp_path):
    store = make_store(tmp_path)
    first = store.save({"a": "## A 첫 번째"}, reason="1회", today=date(2026, 9, 29))
    store.save({"b": "## B 두 번째"}, reason="2회", today=date(2026, 9, 30))

    archived = tmp_path / "prompt" / "history" / first / "a.md"
    assert archived.read_text(encoding="utf-8") == "## A 첫 번째"


def test_why_history_lists_recent_reasons(tmp_path):
    store = make_store(tmp_path, "x1")
    store.save({"b": "## B 1"}, reason="첫 이유", today=date(2026, 9, 29))
    store.save({"b": "## B 2"}, reason="둘째 이유", today=date(2026, 9, 30))

    text = store.why_history()
    assert "- x1: 첫 이유" in text
    assert "- 20260929: 둘째 이유" in text


def test_why_history_is_empty_before_any_change(tmp_path):
    assert make_store(tmp_path).why_history() == ""


def test_save_records_the_code_version_it_was_based_on(tmp_path):
    store = make_store(tmp_path, "v6")
    store.save({"a": "## A 수정"}, reason="시험", today=date(2026, 10, 30))

    assert (tmp_path / "prompt" / "base").read_text(encoding="utf-8") == "v6"


def test_files_are_ignored_when_the_code_version_moved_on(tmp_path, caplog):
    """사람이 코드 프롬프트를 고치면(v6) 그보다 오래된 자동 수정 파일이 그것을 덮어쓰면 안 된다."""
    make_store(tmp_path, "v5").save({"a": "## A 자동 수정"}, reason="시험", today=date(2026, 10, 28))

    newer = make_store(tmp_path, "v6")
    with caplog.at_level(logging.WARNING):
        assert newer.load_sections() == DEFAULTS
        assert newer.load_version() == "v6"
        newer.load_sections()

    # 경고는 인스턴스당 한 번
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_files_without_base_are_ignored(tmp_path, caplog):
    """base 파일이 없는 옛 파일(자동 수정이 폐지되기 전 것)도 코드 버전을 모르므로 쓰지 않는다."""
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "a.md").write_text("## A 옛 파일", encoding="utf-8")
    (folder / "version").write_text("20260929", encoding="utf-8")

    store = make_store(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert store.load_sections() == DEFAULTS
        assert store.load_version() == "v0"
    assert any("무시" in r.getMessage() for r in caplog.records)


def test_save_after_version_change_starts_from_code_defaults(tmp_path):
    make_store(tmp_path, "v5").save({"a": "## A 자동 수정"}, reason="옛", today=date(2026, 10, 28))

    store = make_store(tmp_path, "v6")
    store.save({"b": "## B 새 수정"}, reason="새", today=date(2026, 10, 30))

    # 낡은 파일의 a가 아니라 코드 기본값 a 위에서 고친다
    assert store.load_sections() == {"a": "## A 기본", "b": "## B 새 수정"}
    assert store.load_version() == "20261030"
    assert make_store(tmp_path, "v6").load_sections() == store.load_sections()


def test_no_warning_without_any_files(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        make_store(tmp_path).load_sections()
    assert not caplog.records
