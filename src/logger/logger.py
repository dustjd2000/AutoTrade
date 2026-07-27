import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> None:
    Path(log_dir).mkdir(exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # 일자별 파일 로테이션
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=f"{log_dir}/autotrade.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # 오류 전용 로그
    error_handler = logging.handlers.TimedRotatingFileHandler(
        filename=f"{log_dir}/error.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)
