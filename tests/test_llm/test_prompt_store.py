from src.llm.prompt_store import PromptStore

ORDER = ("a", "b")
DEFAULTS = {"a": "## A 기본", "b": "## B 기본"}


def make_store(tmp_path):
    return PromptStore(tmp_path / "prompt", ORDER, DEFAULTS, "v0", label="테스트")


def test_falls_back_to_code_defaults_without_files(tmp_path):
    store = make_store(tmp_path)
    assert store.load_sections() == DEFAULTS
    assert store.load_version() == "v0"


def test_reads_sections_and_version_from_files(tmp_path):
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "a.md").write_text("## A 파일", encoding="utf-8")
    (folder / "version").write_text("20261001\n", encoding="utf-8")

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
