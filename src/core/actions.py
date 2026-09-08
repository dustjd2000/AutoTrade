"""즉시 실행 버튼과 스케줄 실행이 공유하는 단계 정의와 실행 통로.

버튼과 스케줄러가 서로 다른 껍데기로 같은 workflow 메서드를 부르던 것을 하나로 모은
모듈이다. 두 경로가 서로의 실행 여부를 모르면 09:07에 누른 ① 버튼의 LLM 호출이 도는
사이에 09:08 매수가 발동해, 추천이 덜 끝난 채 매수가 나갈 수 있다.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


def close_out(workflow, engine) -> Callable[[], None]:
    """15:15 마감 정리 — 미체결 매수를 먼저 거두고 나서 보유 포지션을 청산한다.

    시각이 15:20이 아닌 이유는 `runtime.FORCE_CLOSE_TIME` 주석 참고 (장마감 동시호가 회피).

    순서를 뒤집으면 안 된다. 청산 뒤에도 매수 주문이 살아 있으면 그 주문이 장 마감 직전에
    체결되어 오버나이트 포지션이 남고, 당일 매도 원칙이 깨진다.

    10:10 `cancel_unfilled_buys`가 정상적으로 돌았다면 여기서 거둘 주문은 없다. 이 호출은
    10:10을 놓친 경우를 위한 그물이다 — 엔진을 10:10 이후에 켜면 `TimeScheduler`가 지난
    트리거를 소급 실행하지 않아 그날 취소가 통째로 빠진다.
    """

    def run() -> None:
        try:
            workflow.cancel_unfilled_buys()
        except Exception:
            # 취소가 실패해도 청산은 반드시 나가야 한다 — 당일 매도의 마지막 방어선이다
            logger.exception("마감 정리 중 미체결 매수 취소에 실패했습니다. 청산은 계속합니다.")
        engine.force_close_all_positions(reason="day_end")

    return run


# ── 즉시 실행(점검) 액션 ────────────────────────────────────
# 스케줄 시각을 기다리지 않고 UI에서 바로 하루 흐름의 각 단계를 실행하기 위한 목록.
# 실행 주체는 스케줄러와 동일한 workflow/engine 메서드이므로 동작이 갈리지 않는다.
MANUAL_ACTIONS: Dict[str, str] = {
    "recommend": "① LLM 추천 + 메일 발송",
    "buy": "② 매수 실행",
    "cancel_unfilled": "③ 미체결 매수 취소 + 결과 메일",
    "sell_all": "④ 전량 매도 (청산)",
    "report": "⑤ 최종 리포트 메일",
    # 매도 '설정'은 별도 단계가 아니다 — 익절/손절 라인은 엔진 시작 시 적용되어 있고,
    # 매수로 포지션이 생기는 순간 RiskManager.check_portfolio_exit 감시가 자동으로 붙는다.
    "full": "매수 및 매도설정까지 일괄 수행",
    # ①~⑤ 버튼 그리드에는 넣지 않는다 — 대상 종목을 보유 종목 표에서 골라야 하므로
    # 버튼도 그 표 아래에 둔다. 여기 두는 것은 라벨과 잠금·확인 처리를 공유하기 위함이다.
    "sell_selected": "선택 매도",
    # 같은 이유로 '매수 예정' 표 아래에 둔다. 접수된 행을 고르면 미체결 주문 취소가
    # 나가므로 ORDER_ACTIONS에 넣는다 (확대 2026-08-26, PRD 5.10 '선택 삭제').
    "drop_plan": "선택 삭제",
}

# 버튼에는 없고 스케줄러만 부르는 액션. 사용자에게 노출하지 않으므로 MANUAL_ACTIONS와
# 분리한다 — UI는 MANUAL_ACTIONS만 보고 버튼 그리드를 만든다.
# 'daily_report'가 버튼 ⑤('report')와 다른 함수를 부르는 것은 의도된 설계다. 버튼은
# 사용자가 직접 누른 것이므로 발송 표시와 무관하게 항상 보내고(send_daily_report),
# 15:35 스케줄은 청산 즉시 발송과 표시를 공유해 하루 한 번만 보낸다(send_final_report).
SCHEDULED_ACTIONS: Dict[str, str] = {
    "daily_reset": "일일 상태 초기화",
    "close_out": "마감 정리 (미체결 취소 + 전량 청산)",
    "daily_report": "최종 리포트 메일 (스케줄)",
    "review_recommendations": "추천 검증 메일 (스케줄)",
    "tune_prompt": "추천 프롬프트 자동 수정 (스케줄)",
}

ACTION_LABELS: Dict[str, str] = {**MANUAL_ACTIONS, **SCHEDULED_ACTIONS}

# 실제 주문이 나가는 액션 — UI가 실행 전 확인을 받고, 실전 계좌 경고도 함께 띄운다
ORDER_ACTIONS = frozenset(
    {"buy", "cancel_unfilled", "sell_all", "sell_selected", "full", "drop_plan"}
)

# 확인 팝업이 필요한 액션. '선택 삭제'는 되돌릴 수 없다는 이유로 주문 액션이 되기 전부터
# 여기 있었다 — 되돌리려면 LLM 추천을 다시 돌려야 하고, 그러면 추천 종목 자체가 달라진다.
CONFIRM_ACTIONS = ORDER_ACTIONS


@dataclass(frozen=True)
class ManualStep:
    label: str
    run: Callable[[], None]
    # True면 엔진 루프 스레드에서 실행해 실시간 익절·손절 감시와 직렬화한다
    # (같은 종목을 동시에 청산하는 경쟁 상태 방지). False면 별도 스레드 — ActionRunner 참고.
    touches_orders: bool = False


def manual_steps(runtime, action: str, tickers: Iterable[str] = ()) -> List[ManualStep]:
    """액션 이름을 실행 단계 목록으로 바꾼다. '전체'는 하루 흐름의 진입 단계를 순서대로 이어 붙인다.

    `tickers`는 '선택 매도'와 '선택 삭제'만 쓴다 — 다른 액션은 대상이 잔고 전체이거나
    추천 결과로 정해진다.
    """
    selected = tuple(tickers)
    # 값을 람다로 감싸 요청된 액션만 지연 평가한다 — 딕셔너리를 즉시 구성하면 요청하지
    # 않은 액션의 속성(예: 스케줄 전용 액션이 쓰는 runtime.engine.reset_for_new_day)까지
    # 미리 참조해, 그 속성이 없는 호출자(예: 스케줄 전용 액션을 모르는 옛 테스트의 가짜
    # 런타임)가 관계없는 액션을 요청해도 AttributeError로 깨진다.
    step_factories: Dict[str, Callable[[], List[ManualStep]]] = {
        "recommend": lambda: [
            ManualStep(MANUAL_ACTIONS["recommend"], runtime.workflow.recommend_and_notify)
        ],
        "buy": lambda: [
            ManualStep(MANUAL_ACTIONS["buy"], runtime.workflow.execute_buys, touches_orders=True)
        ],
        "cancel_unfilled": lambda: [
            ManualStep(
                MANUAL_ACTIONS["cancel_unfilled"],
                runtime.workflow.cancel_unfilled_buys,
                touches_orders=True,
            )
        ],
        "sell_all": lambda: [
            ManualStep(
                MANUAL_ACTIONS["sell_all"],
                lambda: runtime.engine.force_close_all_positions(reason="manual"),
                touches_orders=True,
            )
        ],
        "sell_selected": lambda: [
            ManualStep(
                MANUAL_ACTIONS["sell_selected"],
                lambda: runtime.engine.close_positions(selected, reason="manual_selected"),
                touches_orders=True,
            )
        ],
        "report": lambda: [
            ManualStep(MANUAL_ACTIONS["report"], runtime.workflow.send_daily_report)
        ],
        "drop_plan": lambda: [
            ManualStep(
                MANUAL_ACTIONS["drop_plan"],
                lambda: runtime.workflow.drop_buy_plans(selected),
                touches_orders=True,
            )
        ],
        # ── 스케줄 전용 ──
        # 주문을 내지는 않지만 현행 스케줄러가 루프 스레드에서 그대로 돌리므로 동작을
        # 보존한다 — 즉시 끝나는 상태 초기화라 루프를 막지 않는다.
        "daily_reset": lambda: [
            ManualStep(
                SCHEDULED_ACTIONS["daily_reset"],
                runtime.engine.reset_for_new_day,
                touches_orders=True,
            )
        ],
        "close_out": lambda: [
            ManualStep(
                SCHEDULED_ACTIONS["close_out"],
                close_out(runtime.workflow, runtime.engine),
                touches_orders=True,
            )
        ],
        "daily_report": lambda: [
            ManualStep(SCHEDULED_ACTIONS["daily_report"], runtime.workflow.send_final_report)
        ],
        # 일봉 조회와 LLM 호출이 걸리므로 루프 스레드를 쓰지 않는다 (touches_orders=False).
        # 주문을 내지 않으므로 실시간 감시와 직렬화할 이유도 없다.
        "review_recommendations": lambda: [
            ManualStep(
                SCHEDULED_ACTIONS["review_recommendations"],
                runtime.workflow.review_recommendations,
            )
        ],
        # LLM 호출과 파일 쓰기가 걸리므로 루프 스레드를 쓰지 않는다. 주문을 내지 않는다.
        "tune_prompt": lambda: [
            ManualStep(SCHEDULED_ACTIONS["tune_prompt"], runtime.workflow.tune_prompt)
        ],
    }
    # 일괄 실행은 '진입'까지만 — 청산과 리포트는 스케줄에 맡긴다.
    # 청산(③)을 넣으면 매수 직후 곧바로 되팔아 익절/손절 감시 구간이 사라지고 왕복 비용만 남는다.
    # 당일 매도 원칙은 FORCE_CLOSE_TIME(15:15)이 지키고, 리포트는 당일 매매가 끝난 뒤에야
    # 의미가 있는 집계이므로 REPORT_TIME(15:35)에 맡긴다. 지금 당장 필요하면 ③·④ 버튼으로 따로 실행한다.
    step_factories["full"] = lambda: step_factories["recommend"]() + step_factories["buy"]()

    if action not in step_factories:
        raise ValueError(f"Unknown manual action: {action}")
    return step_factories[action]()


class ActionRunner:
    """하루 흐름의 각 단계를 한 줄로 세워 실행하는 통로.

    스케줄러 잡과 UI '즉시 실행' 버튼이 이 큐 하나를 공유한다. 두 경로가 각자 workflow
    메서드를 직접 부르던 때는 서로의 실행 여부를 몰라, 09:07에 누른 ① 버튼의 LLM 호출이
    도는 사이 09:08 매수가 발동할 수 있었다.

    충돌하면 **거부가 아니라 순차 대기**다. 거부하면 그날 매수나 청산이 통째로 빠진다.
    다만 같은 액션이 이미 큐에 있으면 넣지 않는다 — 같은 메일이 두 번 나가거나 취소가
    되풀이되는 것을 막는다.

    `submit`은 이벤트 루프 스레드에서만 부른다. UI 스레드는 `loop.call_soon_threadsafe`로
    넘긴다 (EngineThread.run_action 참고).
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self._queue: List[Tuple[str, tuple]] = []
        # 큐에 있거나 지금 실행 중인 액션 — 중복 접수를 막는 열쇠다
        self._pending: set = set()
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        # UI 스레드가 정지 전에 읽는다 (MainWindow._stop_engine) — 단순 bool이라 잠금이 없다
        self.busy = False
        self.on_started: Optional[Callable[[str], None]] = None
        self.on_finished: Optional[Callable[[str, bool, str], None]] = None

    def submit(self, action: str, tickers: Iterable[str] = ()) -> bool:
        """실행을 큐에 넣는다. 접수했으면 True.

        모르는 액션이거나 같은 액션이 이미 대기·실행 중이면 False다.
        """
        if action not in ACTION_LABELS:
            logger.error("알 수 없는 실행 액션: %s", action)
            return False
        if action in self._pending:
            logger.info("이미 대기 중이라 건너뜁니다: %s", ACTION_LABELS[action])
            return False

        self._pending.add(action)
        self._queue.append((action, tuple(tickers)))
        self.busy = True
        self._idle.clear()
        self._wakeup.set()
        logger.info("실행 접수: %s (대기 %d건)", ACTION_LABELS[action], len(self._queue))
        return True

    async def run(self) -> None:
        """큐를 소비한다. `runtime.run`이 태스크로 띄우고 종료할 때 취소한다."""
        while True:
            if not self._queue:
                self.busy = False
                self._idle.set()
                await self._wakeup.wait()
                self._wakeup.clear()
                continue

            action, tickers = self._queue.pop(0)
            try:
                await self._execute(action, tickers)
            finally:
                # 실행이 끝난 뒤에 풀어야 도는 중에 같은 액션이 또 들어오지 않는다
                self._pending.discard(action)

    async def wait_idle(self) -> None:
        """큐가 비고 실행 중인 것이 없을 때까지 기다린다 (정지 전에 쓴다)."""
        await self._idle.wait()

    async def _execute(self, action: str, tickers: tuple) -> None:
        try:
            steps = manual_steps(self._runtime, action, tickers)
        except ValueError:
            logger.error("알 수 없는 실행 액션: %s", action)
            return

        self._notify(self.on_started, action)
        loop = asyncio.get_running_loop()
        try:
            for step in steps:
                logger.info("[실행] %s — 시작", step.label)
                if step.touches_orders:
                    # 실시간 익절/손절 콜백과 겹치지 않도록 루프 스레드에서 직접 실행한다
                    result = step.run()
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    # 수집·LLM·메일은 수십 초가 걸려 루프를 막으면 WebSocket이 끊긴다
                    await loop.run_in_executor(None, step.run)
                logger.info("[실행] %s — 완료", step.label)
            self._notify(self.on_finished, action, True, "")
        except Exception as e:
            logger.exception("[실행] 실행 중 오류가 발생했습니다: %s", action)
            self._notify(self.on_finished, action, False, f"{type(e).__name__}: {e}")

    @staticmethod
    def _notify(callback, *args) -> None:
        """생명주기 알림. 콜백이 터져도 실행 흐름을 끊지 않는다."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("실행 상태 알림에 실패했습니다.")
