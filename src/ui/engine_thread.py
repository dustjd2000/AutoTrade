"""매매 런타임을 UI와 별도 스레드에서 구동한다.

엔진은 단독 실행하지 않고 UI에서만 제어하므로, UI가 살아 있는 동안 이 스레드가
asyncio 이벤트 루프를 소유한다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from config.settings import Settings
from src.core import runtime as runtime_module

logger = logging.getLogger(__name__)

STOP_TIMEOUT_MS = 15_000


class EngineThread(QThread):
    """매매 엔진 스레드. 시작 성공/실패와 종료를 시그널로 알린다."""

    started_ok = pyqtSignal()
    failed = pyqtSignal(str)
    finished_run = pyqtSignal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._runtime: Optional[runtime_module.Runtime] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._runtime = runtime_module.build_runtime(self._settings)
            self.started_ok.emit()
            loop.run_until_complete(runtime_module.run(self._runtime))
        except Exception as e:
            logger.exception("엔진 실행 중 오류가 발생했습니다.")
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            try:
                loop.close()
            finally:
                self._loop = None
                self.finished_run.emit()

    def stop(self) -> None:
        """구동 루프에 정지를 요청하고 스레드가 끝날 때까지 기다린다."""
        if self._runtime is not None:
            # 루프 스레드에서 안전하게 실행되도록 예약한다
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(runtime_module.request_stop, self._runtime)
            else:
                runtime_module.request_stop(self._runtime)

        if not self.wait(STOP_TIMEOUT_MS):
            logger.warning("엔진 스레드가 제한 시간 내에 종료되지 않아 강제 종료합니다.")
            self.terminate()
            self.wait()
