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
    # pythonw.exe로 띄우면 콘솔이 없어 sys.stderr가 None이다 — 기본 훅은 거기에 쓴다
    if sys.stderr is not None:
        sys.__excepthook__(exc_type, exc, tb)


def _show_fatal(message: str) -> None:
    """콘솔 없이 실행될 때 시작 실패가 조용히 묻히지 않도록 대화상자로 알린다."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, "AutoTrade 시작 실패", 0x10  # MB_ICONERROR
        )
    except Exception:
        pass  # 대화상자를 못 띄워도 로그 파일에는 이미 남아 있다


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
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # 창이 뜨기 전에 죽으면 콘솔도 UI도 없어 아무 흔적이 남지 않는다
        logging.getLogger(__name__).critical("시작 중 오류", exc_info=True)
        _show_fatal(f"{type(e).__name__}: {e}\n\n자세한 내용은 logs/error/ 폴더를 확인하세요.")
        sys.exit(1)
