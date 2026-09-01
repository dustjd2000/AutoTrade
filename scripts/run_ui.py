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
#
# **이름 앞의 `Global` 접두사가 핵심이다.** 접두사가 없으면 뮤텍스가 세션별로 따로 만들어져,
# 작업 스케줄러가 세션 0에 띄운 인스턴스를 세션 1의 실행이 보지 못한다 — 2026-09-01에
# 이것 때문에 인스턴스 3개가 동시에 돌았다 (PRD 10절 "인스턴스가 세션을 넘어 쌓인 사고").
SINGLE_INSTANCE_MUTEX = r"Global\AutoTrade-SingleInstance"

# 인스턴스 대체(--auto-start)가 상대 프로세스를 찾는 경로. 뮤텍스는 "누군가 떠 있다"만
# 알려줄 뿐 PID를 주지 않는다. 비정상 종료로 남은 값은 지우지 않는다 — 대체는 살아 있는
# 프로세스인지, 파이썬인지까지 확인하므로 stale 처리가 필요 없다.
PID_PATH = ROOT / "data" / "autotrade.pid"

# 정상 종료(WM_CLOSE)를 기다리는 시간. 보유 종목이 있으면 MainWindow.closeEvent가 확인
# 팝업에서 멈추는데, 무인 실행에는 누를 사람이 없어 이 시간이 지나면 강제 종료한다.
INSTANCE_REPLACE_TIMEOUT_SECONDS = 15


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


def _mutex_says_running() -> bool:
    """뮤텍스가 "이미 떠 있다"고 하면 True.

    뮤텍스 핸들은 닫지 않는다 — 프로세스가 끝날 때 OS가 놓으므로 비정상 종료해도
    잠금이 남지 않는다. 락 파일과 달리 정리 코드도, stale lock 처리도 필요 없다.

    **`Global` 네임스페이스 객체를 만드는 데는 SeCreateGlobalPrivilege가 필요하다.** 일반 권한
    세션에서는 생성이 거부될 수 있어, 그때는 열기만 시도해 존재 여부를 본다 — 잠금을
    소유하지는 못하지만 그 경우는 PID 대조(`_running_instance_pid`)가 받아낸다.

    검사 자체가 실패하면 막지 않는다 — 두 번 뜨는 것보다 아예 못 뜨는 쪽이 더 나쁘다.
    """
    ERROR_ALREADY_EXISTS = 183
    SYNCHRONIZE = 0x00100000
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX):
            return kernel32.GetLastError() == ERROR_ALREADY_EXISTS

        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        logger.warning("단일 인스턴스 검사에 실패했습니다 — 확인 없이 시작합니다.", exc_info=True)
        return False


def _write_pid() -> None:
    """자기 PID를 남긴다 — 다음 `--auto-start` 실행이 이 프로세스를 찾는 유일한 단서다."""
    try:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning("PID 파일을 남기지 못했습니다 (%s).", PID_PATH, exc_info=True)


def _running_pid() -> int:
    """PID 파일에 적힌 값. 없거나 깨졌거나 자기 자신이면 0."""
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return pid if pid > 0 and pid != os.getpid() else 0


def _running_instance_pid() -> int:
    """지금도 살아 있는 인스턴스의 PID. 없으면 0.

    **열지 못한 프로세스는 살아 있는 것으로 본다.** 무인 실행(작업 스케줄러)의 인스턴스는
    세션 0에서 전체 토큰으로 돌아 일반 권한 세션에서는 열리지 않는데, 그것을 '죽었다'로
    처리하면 중복 실행을 그대로 허용하게 된다 — 2026-09-01 사고의 절반이 이것이다.
    """
    pid = _running_pid()
    if pid <= 0:
        return 0

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 액세스 거부 = 프로세스는 있는데 권한이 모자란 것이다. 그 외(대개 '없는 PID')는 죽었다
        return pid if kernel32.GetLastError() == ERROR_ACCESS_DENIED else 0
    try:
        # PID가 재사용돼 다른 프로그램이 그 번호를 쓰고 있으면 우리 인스턴스가 아니다
        return pid if _is_python_process(handle) else 0
    finally:
        kernel32.CloseHandle(handle)


def _is_python_process(handle) -> bool:
    """대상이 파이썬 프로세스인지 — PID 재사용으로 엉뚱한 프로그램을 죽이지 않기 위한 확인."""
    import ctypes
    from ctypes import wintypes

    size = wintypes.DWORD(260)
    buf = ctypes.create_unicode_buffer(size.value)
    if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
        handle, 0, buf, ctypes.byref(size)
    ):
        return False
    return Path(buf.value).name.lower() in ("python.exe", "pythonw.exe")


def _close_windows_of(pid: int) -> bool:
    """대상 프로세스의 최상위 창에 WM_CLOSE — closeEvent를 태워 엔진을 정상 정지시킨다.

    창을 하나라도 찾았으면 True. **다른 세션의 창은 열거되지 않으므로 False가 된다** —
    무인 실행은 세션 0에서 돌아 WM_CLOSE가 닿지 않는다. 호출측은 그때 기다리지 않는다.
    """
    import ctypes
    from ctypes import wintypes

    WM_CLOSE = 0x0010
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    found = False

    def on_window(hwnd, _lparam):
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            found = True
        return True

    user32.EnumWindows(callback_type(on_window), 0)
    return found


def _replace_running_instance(pid: int) -> bool:
    """떠 있는 인스턴스를 종료한다 (PRD 10절 "무인 실행과 인스턴스 대체"). 성공하면 True.

    `--auto-start`로 뜬 경우에만 불린다. 수동 실행에서 기존 인스턴스를 죽이면 살아 있는
    익절/손절 감시를 끊는 것이라, 그쪽은 종전대로 안내 후 종료한다.

    정상 종료(WM_CLOSE)를 먼저 시도하고, 보유 종목 확인 팝업에 걸려 시간 안에 끝나지
    않으면 강제 종료한다. **창을 찾지 못하면(= 다른 세션) 기다리지 않고 바로 강제
    종료한다** — 종전에는 닿지도 않는 WM_CLOSE를 보내고 15초를 그냥 버렸다.

    대상을 잘못 고르지 않도록 실행 파일명을 확인하며, 어긋나면 죽이지 않고 False를
    돌려준다. **False면 호출측이 새 인스턴스를 띄우지 않는다** — 대체에 실패했는데 그대로
    뜨면 그것이 곧 중복 실행이다.
    """
    import ctypes

    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0x0
    kernel32 = ctypes.windll.kernel32

    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        logger.warning("PID %d 프로세스를 열 수 없어 대체하지 않습니다.", pid)
        return False

    try:
        if not _is_python_process(handle):
            # PID가 재사용돼 다른 프로그램이 그 번호를 쓰고 있는 경우다
            logger.warning("PID %d는 파이썬 프로세스가 아닙니다 — 대체하지 않습니다.", pid)
            return False

        logger.warning("실행 중인 AutoTrade(PID %d)를 종료하고 새로 시작합니다.", pid)
        if _close_windows_of(pid):
            if kernel32.WaitForSingleObject(
                handle, INSTANCE_REPLACE_TIMEOUT_SECONDS * 1000
            ) == WAIT_OBJECT_0:
                logger.info("기존 인스턴스가 정상 종료됐습니다 (PID %d).", pid)
                return True

            # 보유 종목이 있으면 closeEvent가 확인 팝업에서 멈춘다 — 무인 실행에는 누를 사람이 없다
            logger.warning(
                "기존 인스턴스가 %d초 안에 닫히지 않아 강제 종료합니다 (PID %d) — "
                "보유 종목 확인 팝업에 걸렸을 수 있습니다. 주문 도중이었다면 "
                "data/buy_records.json과 체결내역을 대조하세요.",
                INSTANCE_REPLACE_TIMEOUT_SECONDS,
                pid,
            )
        else:
            logger.warning(
                "기존 인스턴스(PID %d)의 창을 찾지 못해 바로 강제 종료합니다 — "
                "다른 세션(무인 실행)에서 도는 인스턴스입니다.",
                pid,
            )
        if not kernel32.TerminateProcess(handle, 1):
            logger.error("기존 인스턴스를 강제 종료하지 못했습니다 (PID %d).", pid)
            return False
        kernel32.WaitForSingleObject(handle, 5000)
        return True
    finally:
        kernel32.CloseHandle(handle)


def main() -> None:
    # 파일 로그를 먼저 붙인다 — MainWindow가 그 위에 화면 출력 핸들러를 추가한다.
    # 실행 위치와 무관하게 프로젝트 폴더의 logs/ 에 쌓이도록 절대경로를 넘긴다.
    setup_logging(log_dir=str(ROOT / "logs"))
    sys.excepthook = _log_uncaught

    # --auto-start : 배치파일(예: AutoTrade_AutoStart.bat)로 실행했을 때
    # 화면의 "▶ 시작" 버튼을 직접 누르지 않아도 엔진이 자동으로 시작되도록 한다.
    # 인스턴스 대체 여부도 이 값으로 갈리므로 중복 검사보다 먼저 읽는다.
    auto_start = "--auto-start" in sys.argv

    # 두 번째 실행은 여기서 끝낸다. pythonw.exe로 띄우면 콘솔이 없어 그냥 종료하면
    # "눌렀는데 아무 일도 없음"이 되므로, 왜 안 뜨는지 대화상자로 알린다.
    # 단 무인 실행(--auto-start)은 그 대화상자를 닫을 사람이 없어 그날 매매가 통째로
    # 빠진다 — 그쪽만 기존 인스턴스를 대체한다 (PRD 10절 "무인 실행과 인스턴스 대체").
    # 검사는 두 겹이다 — 뮤텍스는 PID를 주지 않고, PID 파일은 기록에 실패했을 수 있다.
    # 어느 한쪽이라도 "떠 있다"고 하면 떠 있는 것으로 본다.
    #
    # 뮤텍스 검사는 **조건과 무관하게 먼저 부른다** — 이 호출이 곧 뮤텍스를 잡는 일이라,
    # 건너뛰면 이 인스턴스가 소유자가 되지 못해 다음 실행이 아무것도 보지 못한다.
    # 대체 경로에서도 앞서 잡은 핸들이 남아, 기존 인스턴스가 죽어도 잠금은 이어진다.
    mutex_running = _mutex_says_running()
    running_pid = _running_instance_pid()
    if running_pid or mutex_running:
        logger.warning(
            "이미 실행 중으로 판정했습니다 — PID %s, 뮤텍스 %s",
            running_pid or "확인 불가",
            "있음" if mutex_running else "없음",
        )
        if not auto_start:
            logger.warning("AutoTrade가 이미 실행 중입니다 — 두 번째 실행을 중단합니다.")
            _show_dialog(
                "AutoTrade",
                "AutoTrade가 이미 실행 중입니다.\n\n"
                "실행 중인 창을 사용하세요. 두 개를 동시에 띄우면 나중에 뜬 쪽이 "
                "키움 토큰을 새로 발급받아 먼저 돌던 엔진의 감시가 멈춥니다.\n\n"
                "창이 보이지 않으면 작업 스케줄러가 띄운 인스턴스입니다 — "
                "관리자 권한 명령 프롬프트에서 종료해야 합니다.",
                0x40,  # MB_ICONINFORMATION
            )
            sys.exit(0)

        # 여기부터는 무인 실행이다. 대체하지 못하면 **띄우지 않는다** — 그대로 뜨면 중복이다
        if not running_pid:
            logger.error(
                "인스턴스가 떠 있는데 PID를 알 수 없어 대체하지 못했습니다 (%s) — "
                "시작을 중단합니다.",
                PID_PATH,
            )
            sys.exit(1)
        if not _replace_running_instance(running_pid):
            logger.error("기존 인스턴스(PID %d)를 종료하지 못해 시작을 중단합니다.", running_pid)
            _show_dialog(
                "AutoTrade",
                "실행 중인 AutoTrade를 종료하지 못해 시작을 중단했습니다.\n\n"
                "무인 실행(작업 스케줄러)으로 뜬 인스턴스는 관리자 권한에서만 "
                "종료할 수 있습니다.",
                0x30,  # MB_ICONWARNING
            )
            sys.exit(1)
        logger.info("기존 인스턴스를 대체하고 시작합니다 (--auto-start).")

    # 대체를 마친 뒤에 남긴다 — 앞서 죽인 인스턴스의 PID를 덮어써야 한다
    _write_pid()
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
