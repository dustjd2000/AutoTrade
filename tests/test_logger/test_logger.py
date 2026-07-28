import logging
from datetime import date, datetime, timedelta

import pytest

from src.logger.logger import ALL_LOG_DIR, ERROR_LOG_DIR, DailyFileHandler, setup_logging


@pytest.fixture
def clean_root():
    """루트 로거를 건드리는 테스트가 서로 간섭하지 않도록 핸들러를 되돌린다."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    yield root
    for handler in root.handlers:
        handler.close()
    root.handlers, root.level = saved_handlers, saved_level


def _record(message: str, level: int = logging.INFO, created: datetime = None) -> logging.LogRecord:
    record = logging.LogRecord("test", level, __file__, 1, message, None, None)
    if created is not None:
        record.created = created.timestamp()
    return record


def test_writes_to_date_named_file(tmp_path):
    handler = DailyFileHandler(tmp_path / ALL_LOG_DIR)
    handler.emit(_record("한 줄"))
    handler.close()

    expected = tmp_path / ALL_LOG_DIR / f"{date.today().isoformat()}.log"
    assert expected.exists()
    assert "한 줄" in expected.read_text(encoding="utf-8")


def test_no_file_until_something_is_logged(tmp_path):
    DailyFileHandler(tmp_path / ALL_LOG_DIR)
    assert list((tmp_path / ALL_LOG_DIR).glob("*.log")) == []


def test_switches_file_when_date_changes(tmp_path):
    """자정을 넘겨 실행 중이면 다음 날 파일로 갈아탄다."""
    handler = DailyFileHandler(tmp_path / ALL_LOG_DIR)
    handler.emit(_record("오늘"))
    tomorrow = datetime.now() + timedelta(days=1)
    handler.emit(_record("내일", created=tomorrow))
    handler.close()

    today_file = tmp_path / ALL_LOG_DIR / f"{date.today().isoformat()}.log"
    tomorrow_file = tmp_path / ALL_LOG_DIR / f"{tomorrow.date().isoformat()}.log"
    assert "오늘" in today_file.read_text(encoding="utf-8")
    assert "내일" in tomorrow_file.read_text(encoding="utf-8")
    assert "내일" not in today_file.read_text(encoding="utf-8")


def test_prune_removes_files_past_retention(tmp_path):
    log_dir = tmp_path / ALL_LOG_DIR
    log_dir.mkdir(parents=True)
    old = log_dir / f"{(date.today() - timedelta(days=40)).isoformat()}.log"
    recent = log_dir / f"{(date.today() - timedelta(days=3)).isoformat()}.log"
    unrelated = log_dir / "메모.log"
    for path in (old, recent, unrelated):
        path.write_text("x", encoding="utf-8")

    DailyFileHandler(log_dir, retention_days=30)

    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()  # 날짜 이름이 아닌 파일은 건드리지 않는다


def test_prune_disabled_when_retention_not_positive(tmp_path):
    log_dir = tmp_path / ALL_LOG_DIR
    log_dir.mkdir(parents=True)
    old = log_dir / f"{(date.today() - timedelta(days=400)).isoformat()}.log"
    old.write_text("x", encoding="utf-8")

    DailyFileHandler(log_dir, retention_days=0)

    assert old.exists()


def test_setup_logging_splits_all_and_error_logs(tmp_path, clean_root):
    setup_logging(log_dir=str(tmp_path))
    logger = logging.getLogger("test.split")
    logger.info("정보")
    logger.error("오류")
    for handler in clean_root.handlers:
        handler.flush()

    today = f"{date.today().isoformat()}.log"
    all_text = (tmp_path / ALL_LOG_DIR / today).read_text(encoding="utf-8")
    error_text = (tmp_path / ERROR_LOG_DIR / today).read_text(encoding="utf-8")

    assert "정보" in all_text and "오류" in all_text
    assert "오류" in error_text
    assert "정보" not in error_text


def test_setup_logging_is_idempotent(tmp_path, clean_root):
    setup_logging(log_dir=str(tmp_path))
    before = len(clean_root.handlers)
    setup_logging(log_dir=str(tmp_path))
    assert len(clean_root.handlers) == before
