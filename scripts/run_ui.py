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

# 같은 프로그램을 두 번 띄우면 나중에 뜬 쪽이 토큰을 새로 발급받아 먼저 돌던 엔진의
# 토큰을 무효화한다 — 키움은 앱키당 토큰을 하나만 유지한다 (PRD 10절 "토큰 무효화와
# 자동 재발급"). 그래서 한 번에 하나만 실행되게 막는다.
SINGLE_INSTANCE_MUTEX = "AutoTrade-SingleInstance"


def _log_uncaught(exc_type, exc, tb) -> None:
    """UI/슬롯에서 처리되지 않은 예외도 파일 로그에 남긴다 (창이 닫히면 화면 로그는 사라진다)."""
    logger.critical("처리되지 않은 예외", exc_info=(exc_type, exc, tb))
    # pythonw.exe로 띄우면 콘솔이 없어 sys.stderr가 None이다 — 기본 훅은 거기에 쓴다
    if sys.stderr is not None:
        sys.__excepthook__(exc_type, exc, tb)


def _show_dialog(title: str, message: str, icon: int) -> None:
    """콘솔 없이 실행될 때 시작 단계의 알림이 조용히 묻히지 않도록 대화상자로 띄운다.

    Qt가 올라오기 전에도 불리므로 QMessageBox가 아니라 Win32 MessageBoxW를 쓴다.
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, icon)
    except Exception:
        pass  # 대화상자를 못 띄워도 로그 파일에는 이미 남아 있다


def _show_fatal(message: str) -> None:
    _show_dialog("AutoTrade 시작 실패", message, 0x10)  # MB_ICONERROR


def _already_running() -> bool:
    """이미 떠 있는 인스턴스가 있으면 True.

    뮤텍스 핸들은 닫지 않는다 — 프로세스가 끝날 때 OS가 놓으므로 비정상 종료해도
    잠금이 남지 않는다. 락 파일과 달리 정리 코드도, stale lock 처리도 필요 없다.

    검사 자체가 실패하면 막지 않는다 — 두 번 뜨는 것보다 아예 못 뜨는 쪽이 더 나쁘다.
    """
    ERROR_ALREADY_EXISTS = 183
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX):
            return False
        return kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        logger.warning("단일 인스턴스 검사에 실패했습니다 — 확인 없이 시작합니다.", exc_info=True)
        return False


def main() -> None:
    # 파일 로그를 먼저 붙인다 — MainWindow가 그 위에 화면 출력 핸들러를 추가한다.
    # 실행 위치와 무관하게 프로젝트 폴더의 logs/ 에 쌓이도록 절대경로를 넘긴다.
    setup_logging(log_dir=str(ROOT / "logs"))
    sys.excepthook = _log_uncaught

    # 두 번째 실행은 여기서 끝낸다. pythonw.exe로 띄우면 콘솔이 없어 그냥 종료하면
    # "눌렀는데 아무 일도 없음"이 되므로, 왜 안 뜨는지 대화상자로 알린다.
    if _already_running():
        logger.warning("AutoTrade가 이미 실행 중입니다 — 두 번째 실행을 중단합니다.")
        _show_dialog(
            "AutoTrade",
            "AutoTrade가 이미 실행 중입니다.\n\n"
            "실행 중인 창을 사용하세요. 두 개를 동시에 띄우면 나중에 뜬 쪽이 "
            "키움 토큰을 새로 발급받아 먼저 돌던 엔진의 감시가 멈춥니다.",
            0x40,  # MB_ICONINFORMATION
        )
        sys.exit(0)

    # --auto-start : 배치파일(예: AutoTrade_AutoStart.bat)로 실행했을 때
    # 화면의 "▶ 시작" 버튼을 직접 누르지 않아도 엔진이 자동으로 시작되도록 한다.
    auto_start = "--auto-start" in sys.argv
    logger.info(
        "AutoTrade UI 시작 (mode=%s, auto_start=%s)",
        os.getenv("TRADE_MODE", "paper"),
        auto_start,
    )

    app = QApplication(sys.argv)
    app.setApplicationName("AutoTrade")
    window = MainWindow(auto_start=auto_start)
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
