from pathlib import Path

from src.llm.prompt_store import PromptStore, next_version
from src.llm.recommender import (
    DEFAULT_PROMPT_SECTIONS,
    PROMPT_SECTION_ORDER,
    PROMPT_TEMPLATE_VERSION,
)


def test_next_version_increments():
    assert next_version("v11") == "v12"
    assert next_version("v9") == "v10"


def test_next_version_falls_back_on_garbage():
    """버전 파일이 깨져도 멈추지 않는다 — 코드 상수에서 이어 간다."""
    assert next_version("쓰레기") == next_version(PROMPT_TEMPLATE_VERSION)


def test_load_sections_without_files_returns_defaults(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    assert store.load_sections() == DEFAULT_PROMPT_SECTIONS


def test_load_version_without_file_returns_code_constant(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    assert store.load_version() == PROMPT_TEMPLATE_VERSION


def test_save_writes_files_archives_history_and_bumps_version(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    new_version = store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")

    assert new_version == next_version(PROMPT_TEMPLATE_VERSION)
    assert store.load_version() == new_version
    sections = store.load_sections()
    assert sections["outlook"] == "## 오늘 전망 작성 지침\n새 내용"
    # 고치지 않은 절은 기본값 그대로 파일에 쓰인다
    assert sections["reason"] == DEFAULT_PROMPT_SECTIONS["reason"]

    history = tmp_path / "prompt" / "history" / PROMPT_TEMPLATE_VERSION
    assert (history / "outlook.md").read_text(encoding="utf-8") == DEFAULT_PROMPT_SECTIONS["outlook"]
    assert "시험" in (history / "why.md").read_text(encoding="utf-8")


def test_second_save_archives_the_first_version(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    v_first = store.save({"outlook": "## 오늘 전망 작성 지침\n첫 번째 수정본입니다"}, reason="1회")
    store.save({"reason": "## 근거 작성 지침\n두 번째 수정본입니다"}, reason="2회")

    archived = tmp_path / "prompt" / "history" / v_first / "outlook.md"
    assert "첫 번째 수정본입니다" in archived.read_text(encoding="utf-8")


def test_blank_file_falls_back_to_default(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")
    (tmp_path / "prompt" / "outlook.md").write_text("   ", encoding="utf-8")

    assert store.load_sections()["outlook"] == DEFAULT_PROMPT_SECTIONS["outlook"]


def test_invalid_utf8_file_falls_back_to_default(tmp_path):
    """섹션 파일이 깨진 인코딩이어도(쓰다가 죽어 반쪽 바이트가 남는 경우 포함) 그 절만
    기본값으로 폴백하고, 나머지 절은 영향받지 않는다."""
    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")
    (tmp_path / "prompt" / "outlook.md").write_bytes(b"\xff\xfe\xfa")

    sections = store.load_sections()
    assert sections["outlook"] == DEFAULT_PROMPT_SECTIONS["outlook"]
    assert set(sections) == set(PROMPT_SECTION_ORDER)


def test_load_sections_always_has_all_five_keys(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")
    assert set(store.load_sections()) == set(PROMPT_SECTION_ORDER)
