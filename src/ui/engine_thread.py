"""매매 런타임을 UI와 별도 스레드에서 구동한다.

엔진은 단독 실행하지 않고 UI에서만 제어하므로, UI가 살아 있는 동안 이 스레드가
asyncio 이벤트 루프를 소유한다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

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

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._runtime = runtime_module.build_runtime(self._settings)
            # 스케줄 실행과 버튼 실행이 같은 통로를 타므로, 자동 실행 상태도 UI에 그대로 뜬다
            self._runtime.runner.on_started = self.action_started.emit
            self._runtime.runner.on_finished = self.action_finished.emit
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

    def buy_plan_snapshot(self) -> list:
        """오늘의 매수 예정 종목과 진행 상태 (UI 표 갱신용 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return []
        return runtime.workflow.buy_plan_snapshot()

    def portfolio_return(self) -> Optional[float]:
        """익절/손절 판정에 쓰이는 합산 순손익률 (UI 표시용 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return None
        return runtime.engine.portfolio_return_snapshot()

    def portfolio_net_pnl(self) -> tuple:
        """표시용 합산 (순손익 금액, 순손익률). 판정값과 달리 슬리피지가 빠져 있다."""
        runtime = self._runtime
        if runtime is None:
            return (0.0, None)
        return runtime.engine.portfolio_net_pnl_snapshot()

    def cash_snapshot(self) -> Optional[float]:
        """마지막으로 조회한 예수금 (UI 표시용 — API 호출 없이 캐시값만 읽는다)."""
        runtime = self._runtime
        if runtime is None:
            return None
        return runtime.engine.cash_snapshot()

    def set_exit_flags(self, ai_exit: bool, stop_loss: bool) -> bool:
        """AI 매도 판단·손절 적용 여부를 돌고 있는 엔진에 바로 반영한다. 반영했으면 True.

        다른 리스크 설정과 달리 `.env` 저장 → 엔진 재시작 경로를 타지 않는다. 재시작 사이에는
        WebSocket이 끊겨 감시가 멈추는데, 정작 손절을 끄고 싶은 순간에 그 공백이 생긴다.

        bool 대입은 원자적이고 주문을 내지 않으므로 `run_action`처럼 루프 스레드로 넘겨
        직렬화할 필요가 없다 — 손절은 다음 시세 틱의 판정부터, AI 매도 판단은 다음 주기
        게이트(`runtime.ai_exit_due`)부터 바뀐 값이 쓰인다.
        """
        runtime = self._runtime
        if runtime is None:
            return False
        runtime.engine.ai_exit_enabled = ai_exit
        runtime.engine.risk_manager.stop_loss_enabled = stop_loss
        return True

    # ── 즉시 실행 ────────────────────────────────────────────
    @property
    def action_busy(self) -> bool:
        runtime = self._runtime
        return runtime is not None and runtime.runner is not None and runtime.runner.busy

    def run_action(self, action: str, tickers: Iterable[str] = ()) -> bool:
        """스케줄 시각과 무관하게 하루 흐름의 단계를 지금 실행한다.

        접수만 하고 곧바로 돌아온다 — 실행은 ActionRunner의 큐가 순서대로 맡는다.
        이미 다른 단계가 도는 중이면 거부하지 않고 그 뒤에 붙는다. 완료는 action_finished로
        알린다.

        `submit`은 루프 스레드에서만 불러야 하므로 `call_soon_threadsafe`로 넘긴다. 결과를
        기다리지 않는 이유는, 루프가 매수 같은 긴 단계를 돌고 있으면 UI가 그만큼 얼기
        때문이다 — 중복 접수는 러너가 걸러내고 로그로 남긴다.

        `tickers`는 '선택 매도'와 '선택 삭제'만 쓴다 (`manual_steps` 참고).
        """
        loop, runtime = self._loop, self._runtime
        if runtime is None or loop is None or not loop.is_running():
            logger.warning("엔진이 실행 중이 아니어서 즉시 실행할 수 없습니다.")
            return False

        loop.call_soon_threadsafe(runtime.runner.submit, action, tuple(tickers))
        return True

    def _await_action(self) -> None:
        """진행 중인 실행을 중간에 끊지 않도록 큐가 빌 때까지 기다린다.

        별도 스레드로 넘긴 단계는 루프가 살아 있어 정지 요청이 즉시 처리되므로,
        기다려주지 않으면 수집·LLM·메일이 중간에 버려진 채 스레드만 정리된다.
        """
        loop, runtime = self._loop, self._runtime
        if runtime is None or runtime.runner is None or loop is None or not loop.is_running():
            return
        if not runtime.runner.busy:
            return

        logger.warning("실행이 진행 중입니다 — 최대 %d초까지 완료를 기다립니다.", ACTION_WAIT_SECONDS)
        try:
            future = asyncio.run_coroutine_threadsafe(runtime.runner.wait_idle(), loop)
            future.result(timeout=ACTION_WAIT_SECONDS)
        except Exception:
            logger.warning("실행 완료를 기다리지 못했습니다. 정지를 계속 진행합니다.")

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
