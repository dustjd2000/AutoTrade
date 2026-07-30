"""매매 런타임을 UI와 별도 스레드에서 구동한다.

엔진은 단독 실행하지 않고 UI에서만 제어하므로, UI가 살아 있는 동안 이 스레드가
asyncio 이벤트 루프를 소유한다.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from config.settings import Settings
from src.core import runtime as runtime_module

logger = logging.getLogger(__name__)

STOP_TIMEOUT_MS = 15_000
# 진행 중인 즉시 실행을 중간에 끊지 않도록 완료를 기다려주는 한도
ACTION_WAIT_SECONDS = 60


class EngineThread(QThread):
    """매매 엔진 스레드. 시작 성공/실패와 종료를 시그널로 알린다."""

    started_ok = pyqtSignal()
    failed = pyqtSignal(str)
    finished_run = pyqtSignal()
    action_started = pyqtSignal(str)                # action key
    action_finished = pyqtSignal(str, bool, str)     # action key, 성공 여부, 오류 메시지

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._runtime: Optional[runtime_module.Runtime] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._action_busy = False
        self._action_future: Optional[Future] = None

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

    def open_tickers(self) -> list:
        """감시 중인 보유 종목 (UI 스레드에서 호출 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return []
        return runtime.engine.open_tickers

    def position_snapshot(self) -> list:
        """보유 종목 상세 (UI 표 갱신용 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return []
        return runtime.engine.position_snapshot()

    def unsellable_snapshot(self) -> list:
        """오늘 매도하지 못한 종목과 사유 (UI 표 갱신용 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return []
        return runtime.engine.unsellable_snapshot()

    # ── 즉시 실행 ────────────────────────────────────────────
    @property
    def action_busy(self) -> bool:
        return self._action_busy

    def run_action(self, action: str) -> bool:
        """스케줄 시각과 무관하게 하루 흐름의 단계를 지금 실행한다.

        주문을 내는 단계는 엔진 루프 스레드에서 실행해 실시간 시세 콜백과 직렬화하고
        (같은 종목을 동시에 청산하는 경쟁 상태 방지), 오래 걸리는 수집·LLM·메일은
        별도 스레드로 넘긴다. 예약에 성공하면 True — 완료는 action_finished로 알린다.
        """
        loop, runtime = self._loop, self._runtime
        if runtime is None or loop is None or not loop.is_running():
            logger.warning("엔진이 실행 중이 아니어서 즉시 실행할 수 없습니다.")
            return False
        if self._action_busy:
            logger.warning("이미 즉시 실행이 진행 중입니다. 완료 후 다시 시도하세요.")
            return False

        try:
            steps = runtime_module.manual_steps(runtime, action)
        except ValueError:
            logger.error("알 수 없는 즉시 실행 액션: %s", action)
            return False

        self._action_busy = True
        self._action_future = asyncio.run_coroutine_threadsafe(
            self._run_action(action, steps), loop
        )
        return True

    async def _run_action(self, action: str, steps) -> None:
        self.action_started.emit(action)
        loop = asyncio.get_running_loop()
        try:
            for step in steps:
                logger.info("[즉시 실행] %s — 시작", step.label)
                if step.touches_orders:
                    # 실시간 익절/손절 콜백과 겹치지 않도록 루프 스레드에서 직접 실행한다
                    result = step.run()
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    # 수집·LLM·메일은 수십 초가 걸려 루프를 막으면 WebSocket이 끊긴다
                    await loop.run_in_executor(None, step.run)
                logger.info("[즉시 실행] %s — 완료", step.label)
            self.action_finished.emit(action, True, "")
        except Exception as e:
            logger.exception("[즉시 실행] 실행 중 오류가 발생했습니다: %s", action)
            self.action_finished.emit(action, False, f"{type(e).__name__}: {e}")
        finally:
            self._action_busy = False

    def _await_action(self) -> None:
        """진행 중인 즉시 실행을 중간에 끊지 않도록 완료를 기다린다.

        별도 스레드로 넘긴 단계는 루프가 살아 있어 정지 요청이 즉시 처리되므로,
        기다려주지 않으면 수집·LLM·메일이 중간에 버려진 채 스레드만 정리된다.
        """
        future = self._action_future
        if future is None or future.done():
            return
        logger.warning("즉시 실행이 진행 중입니다 — 최대 %d초까지 완료를 기다립니다.", ACTION_WAIT_SECONDS)
        try:
            future.result(timeout=ACTION_WAIT_SECONDS)
        except Exception:
            logger.warning("즉시 실행 완료를 기다리지 못했습니다. 정지를 계속 진행합니다.")

    def stop(self) -> None:
        """구동 루프에 정지를 요청하고 스레드가 끝날 때까지 기다린다."""
        self._await_action()

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
