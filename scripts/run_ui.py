"""UI 진입점."""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from PyQt6.QtWidgets import QApplication
from src.logger.logger import setup_logging
from src.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _log_uncaught(exc_type, exc, tb) -> None:
    """UI/슬롯에서 처리되지 않은 예외도 파일 로그에 남긴다 (창이 닫히면 화면 로그는 사라진다)."""
    logger.critical("처리되지 않은 예외", exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)


def main() -> None:
    # 파일 로그를 먼저 붙인다 — MainWindow가 그 위에 화면 출력 핸들러를 추가한다.
    # 실행 위치와 무관하게 프로젝트 폴더의 logs/ 에 쌓이도록 절대경로를 넘긴다.
    setup_logging(log_dir=str(ROOT / "logs"))
    sys.excepthook = _log_uncaught
    logger.info("AutoTrade UI 시작 (mode=%s)", os.getenv("TRADE_MODE", "paper"))

    app = QApplication(sys.argv)
    app.setApplicationName("AutoTrade")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
