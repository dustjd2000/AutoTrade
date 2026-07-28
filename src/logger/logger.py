"""로그 설정 — 날짜별 파일로 분리해 기록한다.

    logs/autotrade/2026-07-28.log   전체 로그
    logs/error/2026-07-28.log       ERROR 이상만

파일명 자체가 날짜이므로 며칠 전 로그를 열어보지 않고 바로 고를 수 있다.
"""
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# 날짜 파일을 며칠까지 보관할지 (지난 파일은 자동 삭제)
DEFAULT_RETENTION_DAYS = 40

ALL_LOG_DIR = "autotrade"
ERROR_LOG_DIR = "error"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class DailyFileHandler(logging.FileHandler):
    """`<디렉터리>/YYYY-MM-DD.log` 에 기록하고, 날짜가 바뀌면 다음 날 파일로 갈아탄다.

    표준 TimedRotatingFileHandler는 현재 파일 이름이 항상 같고 지난 파일에만 날짜가
    붙어서 원하는 구성(파일명 = 날짜)이 되지 않으므로 직접 처리한다.
    """

    def __init__(
        self,
        directory: Path,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        encoding: str = "utf-8",
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._day = date.today()
        # delay=True — 기록할 내용이 생길 때 파일을 만든다 (빈 파일이 쌓이지 않도록)
        super().__init__(self._path_for(self._day), encoding=encoding, delay=True)
        self.prune()

    def _path_for(self, day: date) -> str:
        return str(self.directory / f"{day.isoformat()}.log")

    @property
    def current_path(self) -> Path:
        return Path(self.baseFilename)

    def emit(self, record: logging.LogRecord) -> None:
        # 자정을 넘겨 계속 실행 중인 경우에도 기록 시각의 날짜 파일로 들어가게 한다
        record_day = datetime.fromtimestamp(record.created).date()
        if record_day != self._day:
            self._switch_day(record_day)
        super().emit(record)

    def _switch_day(self, day: date) -> None:
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self._day = day
            self.baseFilename = os.path.abspath(self._path_for(day))
        finally:
            self.release()
        self.prune()

    def prune(self) -> None:
        """보관 기간이 지난 날짜 파일을 삭제한다."""
        if self.retention_days <= 0:
            return
        cutoff = self._day - timedelta(days=self.retention_days)
        for path in self.directory.glob("*.log"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue  # 날짜 이름이 아닌 파일은 우리 것이 아니므로 건드리지 않는다
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    # 삭제 실패로 로깅 자체가 막히면 안 된다 (여기서 로그를 남기면 재귀 위험)
                    pass


def setup_logging(
    log_dir: str = "logs",
    level: int = logging.INFO,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """루트 로거에 콘솔 + 날짜별 파일 핸들러를 붙인다. 이미 붙어 있으면 아무것도 하지 않는다."""
    root = logging.getLogger()
    if any(isinstance(handler, DailyFileHandler) for handler in root.handlers):
        return

    root.setLevel(level)
    fmt = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # pythonw.exe로 띄우면 콘솔이 없어 sys.stderr가 None이다. 그 상태로 StreamHandler를
    # 붙이면 로그를 남길 때마다 실패하므로, 쓸 스트림이 있을 때만 콘솔 출력을 건다.
    # 화면 표시는 UI의 '실행 로그' 뷰가 대신한다.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    base = Path(log_dir)

    all_handler = DailyFileHandler(base / ALL_LOG_DIR, retention_days)
    all_handler.setFormatter(fmt)
    root.addHandler(all_handler)

    # 오류 전용 — 문제가 생겼을 때 이 폴더의 당일 파일만 보면 된다
    error_handler = DailyFileHandler(base / ERROR_LOG_DIR, retention_days)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)
