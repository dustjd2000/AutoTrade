"""즉시 실행 버튼과 스케줄 실행이 공유하는 단계 정의와 실행 통로.

버튼과 스케줄러가 서로 다른 껍데기로 같은 workflow 메서드를 부르던 것을 하나로 모은
모듈이다. 두 경로가 서로의 실행 여부를 모르면 09:07에 누른 ① 버튼의 LLM 호출이 도는
사이에 09:08 매수가 발동해, 추천이 덜 끝난 채 매수가 나갈 수 있다.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List

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
    }
    # 일괄 실행은 '진입'까지만 — 청산과 리포트는 스케줄에 맡긴다.
    # 청산(③)을 넣으면 매수 직후 곧바로 되팔아 익절/손절 감시 구간이 사라지고 왕복 비용만 남는다.
    # 당일 매도 원칙은 FORCE_CLOSE_TIME(15:15)이 지키고, 리포트는 당일 매매가 끝난 뒤에야
    # 의미가 있는 집계이므로 REPORT_TIME(15:35)에 맡긴다. 지금 당장 필요하면 ③·④ 버튼으로 따로 실행한다.
    step_factories["full"] = lambda: step_factories["recommend"]() + step_factories["buy"]()

    if action not in step_factories:
        raise ValueError(f"Unknown manual action: {action}")
    return step_factories[action]()
