# 실행 통로 통합과 매수 결과 메일 조기 발송 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스케줄 실행과 UI 버튼 실행이 하나의 큐(`ActionRunner`)를 공유하게 하고, 접수한 매수가 전부 체결되면 10:10을 기다리지 않고 매수 결과 메일을 보낸다.

**Architecture:** `src/core/actions.py`를 새로 만들어 단계 정의(`ManualStep`, 액션 dict, `manual_steps`)와 실행 통로(`ActionRunner`)를 모은다. `ActionRunner`는 엔진 이벤트 루프에서 도는 태스크 하나가 중복 없는 FIFO 큐를 소비하며, `touches_orders` 값에 따라 루프 스레드/executor로 단계를 배분한다. 스케줄러 잡과 `EngineThread.run_action`이 모두 `runner.submit(action)`만 부른다. 매수 조기 발송은 체결내역 조회(주문번호 대조)로 전량 체결을 확인한 뒤 같은 큐에 `cancel_unfilled`을 넣는다.

**Tech Stack:** Python 3.14, asyncio, PyQt6, pytest. 새 의존성 없음.

## Global Constraints

- 설계 원본은 `docs/superpowers/specs/2026-09-02-unified-action-runner-design.md`다. 동작 규칙의 단일 소스는 `주식자동매매_PRD.md`이며 Task 8에서 갱신한다.
- 주석·로그·문서는 한국어, 코드 식별자는 영어 — 기존 파일의 스타일과 주석 밀도를 그대로 따른다.
- 셸은 PowerShell 5.1이다. `&&`는 동작하지 않는다. 명령을 이어 쓰려면 `;`를 쓰거나 한 줄씩 실행한다.
- 기존 `tests/test_core/test_manual_actions.py`는 **한 줄도 고치지 않고** 통과해야 한다. `MANUAL_ACTIONS` / `ORDER_ACTIONS` / `CONFIRM_ACTIONS` / `manual_steps` / `close_out`은 `src.core.runtime`에서 계속 import 가능해야 한다.
- 버튼 ⑤(`report`)는 `workflow.send_daily_report`, 15:35 스케줄(`daily_report`)은 `workflow.send_final_report`다. 이 갈림은 의도된 설계이므로 통합하지 않는다.
- `touches_orders=True`인 단계는 반드시 이벤트 루프 스레드에서 실행한다 (실시간 익절·손절 콜백과 직렬화). `False`는 executor로 넘긴다 (WebSocket PING 차단 방지).
- 실계좌 API를 부르는 검증은 하지 않는다. 엔진이 떠 있는 동안 `scripts/` 진단 스크립트를 돌리지 않는다 (토큰 무효화).
- 테스트 실행: `pytest` (프로젝트 루트에서). 가상환경이 있으면 그 인터프리터를 쓴다.

---

### Task 1: `src/core/actions.py` — 단계 정의를 옮기고 스케줄 전용 액션을 더한다

**Files:**
- Create: `src/core/actions.py`
- Modify: `src/core/runtime.py` (`close_out` 함수와 "즉시 실행(점검) 액션" 블록 삭제 + 재수출)
- Test: `tests/test_core/test_actions.py` (신규)

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - `ManualStep(label: str, run: Callable[[], None], touches_orders: bool = False)` — frozen dataclass
  - `MANUAL_ACTIONS: Dict[str, str]` — 버튼에 뜨는 액션 키 → 라벨
  - `SCHEDULED_ACTIONS: Dict[str, str]` — 스케줄 전용 액션 키 → 라벨
  - `ACTION_LABELS: Dict[str, str]` — 위 둘의 합집합
  - `ORDER_ACTIONS: frozenset`, `CONFIRM_ACTIONS: frozenset`
  - `manual_steps(runtime, action: str, tickers: Iterable[str] = ()) -> List[ManualStep]`
  - `close_out(workflow, engine) -> Callable[[], None]`

- [ ] **Step 1: 새 액션이 올바른 단계로 풀리는지 확인하는 실패 테스트를 쓴다**

`tests/test_core/test_actions.py`를 만든다. 가짜 런타임은 기존 `test_manual_actions.py`의 것을 확장한 것이다 — `send_final_report`와 `reset_for_new_day`가 추가됐다.

```python
from types import SimpleNamespace

import pytest

from src.core.actions import (
    ACTION_LABELS,
    MANUAL_ACTIONS,
    SCHEDULED_ACTIONS,
    manual_steps,
)


def make_runtime(calls):
    """호출 순서만 기록하는 가짜 런타임 (스케줄 전용 액션까지 다룬다)."""
    workflow = SimpleNamespace(
        recommend_and_notify=lambda: calls.append("recommend"),
        execute_buys=lambda: calls.append("buy"),
        cancel_unfilled_buys=lambda: calls.append("cancel_unfilled"),
        send_daily_report=lambda: calls.append("report"),
        send_final_report=lambda: calls.append("final_report"),
        drop_buy_plans=lambda tickers: calls.append(f"drop_plan:{','.join(tickers)}"),
    )
    engine = SimpleNamespace(
        force_close_all_positions=lambda reason="day_end": calls.append(f"sell_all:{reason}"),
        close_positions=lambda tickers, reason="manual_selected": calls.append(
            f"sell_selected:{reason}:{','.join(tickers)}"
        ),
        reset_for_new_day=lambda: calls.append("daily_reset"),
    )
    return SimpleNamespace(workflow=workflow, engine=engine)


# ── 스케줄 전용 액션 ────────────────────────────────────────
@pytest.mark.parametrize(
    "action,expected",
    [
        ("daily_reset", ["daily_reset"]),
        ("close_out", ["cancel_unfilled", "sell_all:day_end"]),
        ("daily_report", ["final_report"]),
    ],
)
def test_scheduled_action_runs_matching_step(action, expected):
    calls = []
    for step in manual_steps(make_runtime(calls), action):
        step.run()
    assert calls == expected


def test_scheduled_actions_are_not_buttons():
    """버튼 그리드에 뜨면 안 된다 — 사용자에게 노출하지 않는 내부 액션이다."""
    assert not (set(SCHEDULED_ACTIONS) & set(MANUAL_ACTIONS))
    assert set(ACTION_LABELS) == set(MANUAL_ACTIONS) | set(SCHEDULED_ACTIONS)


def test_button_report_and_scheduled_report_call_different_functions():
    """⑤ 버튼은 표시와 무관하게 항상 보내고, 15:35는 하루 한 번만 보낸다."""
    calls = []
    runtime = make_runtime(calls)
    for step in manual_steps(runtime, "report"):
        step.run()
    for step in manual_steps(runtime, "daily_report"):
        step.run()
    assert calls == ["report", "final_report"]


def test_daily_reset_and_close_out_run_on_the_engine_loop():
    """상태 초기화와 마감 정리는 현행 스케줄러처럼 루프 스레드에서 돈다."""
    runtime = make_runtime([])
    assert [s.touches_orders for s in manual_steps(runtime, "daily_reset")] == [True]
    assert [s.touches_orders for s in manual_steps(runtime, "close_out")] == [True]


def test_daily_report_runs_off_the_loop():
    """리포트는 집계·메일로 수십 초가 걸린다 — 루프를 막으면 WebSocket이 끊긴다."""
    runtime = make_runtime([])
    assert [s.touches_orders for s in manual_steps(runtime, "daily_report")] == [False]


def test_every_action_has_a_label_and_steps():
    runtime = make_runtime([])
    for action in ACTION_LABELS:
        if action in ("sell_selected", "drop_plan"):
            assert manual_steps(runtime, action, ["005930"])
        else:
            assert manual_steps(runtime, action)
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_actions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.actions'`

- [ ] **Step 3: `src/core/actions.py`를 만든다**

`src/core/runtime.py`의 `close_out` 함수와 "즉시 실행(점검) 액션" 블록 전체를 **그대로 옮기고**, 스케줄 전용 액션만 더한다. 옮기는 코드의 주석은 한 글자도 바꾸지 않는다.

```python
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
    steps: Dict[str, List[ManualStep]] = {
        "recommend": [
            ManualStep(MANUAL_ACTIONS["recommend"], runtime.workflow.recommend_and_notify)
        ],
        "buy": [
            ManualStep(MANUAL_ACTIONS["buy"], runtime.workflow.execute_buys, touches_orders=True)
        ],
        "cancel_unfilled": [
            ManualStep(
                MANUAL_ACTIONS["cancel_unfilled"],
                runtime.workflow.cancel_unfilled_buys,
                touches_orders=True,
            )
        ],
        "sell_all": [
            ManualStep(
                MANUAL_ACTIONS["sell_all"],
                lambda: runtime.engine.force_close_all_positions(reason="manual"),
                touches_orders=True,
            )
        ],
        "sell_selected": [
            ManualStep(
                MANUAL_ACTIONS["sell_selected"],
                lambda: runtime.engine.close_positions(selected, reason="manual_selected"),
                touches_orders=True,
            )
        ],
        "report": [ManualStep(MANUAL_ACTIONS["report"], runtime.workflow.send_daily_report)],
        "drop_plan": [
            ManualStep(
                MANUAL_ACTIONS["drop_plan"],
                lambda: runtime.workflow.drop_buy_plans(selected),
                touches_orders=True,
            )
        ],
        # ── 스케줄 전용 ──
        # 주문을 내지는 않지만 현행 스케줄러가 루프 스레드에서 그대로 돌리므로 동작을
        # 보존한다 — 즉시 끝나는 상태 초기화라 루프를 막지 않는다.
        "daily_reset": [
            ManualStep(
                SCHEDULED_ACTIONS["daily_reset"],
                runtime.engine.reset_for_new_day,
                touches_orders=True,
            )
        ],
        "close_out": [
            ManualStep(
                SCHEDULED_ACTIONS["close_out"],
                close_out(runtime.workflow, runtime.engine),
                touches_orders=True,
            )
        ],
        "daily_report": [
            ManualStep(SCHEDULED_ACTIONS["daily_report"], runtime.workflow.send_final_report)
        ],
    }
    # 일괄 실행은 '진입'까지만 — 청산과 리포트는 스케줄에 맡긴다.
    # 청산(③)을 넣으면 매수 직후 곧바로 되팔아 익절/손절 감시 구간이 사라지고 왕복 비용만 남는다.
    # 당일 매도 원칙은 FORCE_CLOSE_TIME(15:15)이 지키고, 리포트는 당일 매매가 끝난 뒤에야
    # 의미가 있는 집계이므로 REPORT_TIME(15:35)에 맡긴다. 지금 당장 필요하면 ③·④ 버튼으로 따로 실행한다.
    steps["full"] = steps["recommend"] + steps["buy"]

    if action not in steps:
        raise ValueError(f"Unknown manual action: {action}")
    return steps[action]
```

- [ ] **Step 4: `src/core/runtime.py`에서 옮긴 코드를 지우고 재수출한다**

`runtime.py`에서 `close_out` 함수 정의와 `# ── 즉시 실행(점검) 액션 ──` 블록부터 `manual_steps` 함수 끝까지를 삭제한다. 파일 상단 import 근처에 다음을 넣는다.

```python
# 액션 정의는 src/core/actions.py로 옮겼다. UI(main_window)와 기존 테스트가 이 모듈에서
# import하고 있으므로 이름을 그대로 다시 내보낸다.
from src.core.actions import (  # noqa: F401  (재수출)
    ACTION_LABELS,
    CONFIRM_ACTIONS,
    MANUAL_ACTIONS,
    ORDER_ACTIONS,
    SCHEDULED_ACTIONS,
    ActionRunner,
    ManualStep,
    close_out,
    manual_steps,
)
```

`ActionRunner`는 Task 2에서 만든다. Task 1 단계에서는 그 이름을 빼고 import한 뒤, Task 2에서 다시 넣는다.

`runtime.py`에서 더 이상 쓰지 않게 된 import(`Iterable` 등)가 남았는지 확인하고 지운다. `dataclass`와 `Dict`는 다른 곳에서 쓰이는지 보고 판단한다.

- [ ] **Step 5: 새 테스트와 기존 테스트가 모두 통과하는지 확인한다**

Run: `pytest tests/test_core/test_actions.py tests/test_core/test_manual_actions.py -v`
Expected: 전부 PASS. `test_manual_actions.py`는 한 줄도 고치지 않은 채 통과해야 한다.

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest`
Expected: 전부 PASS (기존과 동일한 결과)

- [ ] **Step 7: 커밋**

```bash
git add src/core/actions.py src/core/runtime.py tests/test_core/test_actions.py
git commit -m "즉시 실행 단계 정의를 별도 모듈로 옮기고 스케줄 전용 액션을 더한다"
```

---

### Task 2: `ActionRunner` — 중복 없는 FIFO 큐로 실행을 직렬화한다

**Files:**
- Modify: `src/core/actions.py` (`ActionRunner` 추가), `src/core/runtime.py` (재수출 목록에 `ActionRunner` 추가)
- Test: `tests/test_core/test_actions.py` (추가)

**Interfaces:**
- Consumes: Task 1의 `ManualStep`, `ACTION_LABELS`, `manual_steps`
- Produces:
  - `ActionRunner(runtime)` — 생성자는 런타임 하나만 받는다
  - `runner.on_started: Optional[Callable[[str], None]]` — 대입 가능한 속성, 기본 `None`
  - `runner.on_finished: Optional[Callable[[str, bool, str], None]]` — 기본 `None`
  - `runner.busy: bool` — 실행 중이거나 큐에 남은 것이 있으면 True (다른 스레드에서 읽어도 안전한 단순 bool)
  - `runner.submit(action: str, tickers: Iterable[str] = ()) -> bool` — **루프 스레드에서만 부른다**
  - `async runner.run() -> None` — 큐 소비 루프 (`runtime.run`이 태스크로 띄운다)
  - `async runner.wait_idle() -> None` — 큐가 비고 실행 중인 것이 없을 때까지 기다린다

- [ ] **Step 1: 큐 동작을 검증하는 실패 테스트를 쓴다**

`tests/test_core/test_actions.py` 끝에 붙인다.

```python
# ── ActionRunner ────────────────────────────────────────────
import asyncio
import threading

from src.core.actions import ActionRunner


def drain(runner, submissions):
    """submit들을 넣고 큐가 빌 때까지 러너를 돌린 뒤 멈춘다."""

    async def scenario():
        task = asyncio.create_task(runner.run())
        results = [
            runner.submit(*s) if isinstance(s, tuple) else runner.submit(s)
            for s in submissions
        ]
        await asyncio.wait_for(runner.wait_idle(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return results

    return asyncio.run(scenario())


def test_queued_actions_run_in_order():
    calls = []
    runner = ActionRunner(make_runtime(calls))

    assert drain(runner, ["recommend", "buy"]) == [True, True]
    assert calls == ["recommend", "buy"]


def test_duplicate_submit_is_refused():
    """같은 액션이 큐에 남아 있으면 두 번 넣지 않는다 — 메일·주문이 중복된다."""
    calls = []
    runner = ActionRunner(make_runtime(calls))

    assert drain(runner, ["cancel_unfilled", "cancel_unfilled"]) == [True, False]
    assert calls == ["cancel_unfilled"]


def test_unknown_action_is_refused_without_queueing():
    calls = []
    runner = ActionRunner(make_runtime(calls))

    assert drain(runner, ["nope"]) == [False]
    assert calls == []


def test_failing_action_does_not_kill_the_runner():
    """한 액션이 예외로 끝나도 다음 액션은 돌아야 한다 — 15:15 청산이 걸려 있다."""
    calls = []
    runtime = make_runtime(calls)

    def boom():
        calls.append("recommend")
        raise RuntimeError("LLM 실패")

    runtime.workflow.recommend_and_notify = boom
    runner = ActionRunner(runtime)

    assert drain(runner, ["recommend", "buy"]) == [True, True]
    assert calls == ["recommend", "buy"]


def test_lifecycle_callbacks_report_success_and_failure():
    calls = []
    runtime = make_runtime(calls)

    def boom():
        raise RuntimeError("LLM 실패")

    runtime.workflow.recommend_and_notify = boom
    runner = ActionRunner(runtime)
    started, finished = [], []
    runner.on_started = started.append
    runner.on_finished = lambda a, ok, msg: finished.append((a, ok, msg))

    drain(runner, ["recommend", "buy"])

    assert started == ["recommend", "buy"]
    assert finished[0][0] == "recommend" and finished[0][1] is False
    assert "LLM 실패" in finished[0][2]
    assert finished[1] == ("buy", True, "")


def test_order_steps_run_on_the_loop_thread():
    """touches_orders=True는 루프 스레드에서, False는 executor에서 돌아야 한다."""
    threads = {}
    runtime = make_runtime([])
    runtime.workflow.recommend_and_notify = lambda: threads.update(
        recommend=threading.get_ident()
    )
    runtime.workflow.execute_buys = lambda: threads.update(buy=threading.get_ident())
    runner = ActionRunner(runtime)

    async def scenario():
        task = asyncio.create_task(runner.run())
        threads["loop"] = threading.get_ident()
        runner.submit("full")
        await asyncio.wait_for(runner.wait_idle(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert threads["buy"] == threads["loop"]
    assert threads["recommend"] != threads["loop"]


def test_tickers_are_carried_through_the_queue():
    calls = []
    runner = ActionRunner(make_runtime(calls))

    drain(runner, [("sell_selected", ["005930", "000660"])])

    assert calls == ["sell_selected:manual_selected:005930,000660"]


def test_busy_is_false_once_the_queue_drains():
    calls = []
    runner = ActionRunner(make_runtime(calls))

    assert runner.busy is False
    drain(runner, ["recommend"])
    assert runner.busy is False
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_actions.py -k "runner or queue or busy or callbacks or loop_thread" -v`
Expected: FAIL — `ImportError: cannot import name 'ActionRunner'`

- [ ] **Step 3: `ActionRunner`를 구현한다**

`src/core/actions.py` 끝에 붙인다.

```python
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
```

- [ ] **Step 4: `runtime.py`의 재수출 목록에 `ActionRunner`를 넣는다**

Task 1 Step 4에서 만든 import 블록에 `ActionRunner`를 더한다 (알파벳 순서상 `ACTION_LABELS` 다음, `ManualStep` 앞).

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_core/test_actions.py -v`
Expected: 전부 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/core/actions.py src/core/runtime.py tests/test_core/test_actions.py
git commit -m "실행 단계를 한 줄로 세우는 ActionRunner를 더한다"
```

---

### Task 3: 스케줄러가 `ActionRunner`에 접수하게 한다

**Files:**
- Modify: `src/core/runtime.py` (`Runtime` dataclass, `build_runtime`, `run`, `_off_loop` 제거)
- Test: `tests/test_core/test_actions.py` (추가)

**Interfaces:**
- Consumes: Task 2의 `ActionRunner`
- Produces:
  - `Runtime.runner: Optional[ActionRunner]` — `build_runtime`이 채운다
  - `_submit(runtime, action) -> Callable[[], None]` — 스케줄 잡 어댑터
  - `runtime.run`이 `runner.run()`을 태스크로 띄우고 종료 시 취소한다

- [ ] **Step 1: 스케줄러가 넣는 여섯 키가 모두 접수되는지 보는 테스트를 쓴다**

`tests/test_core/test_actions.py` 끝에 붙인다. `build_runtime`은 실 API 클라이언트를 만들므로 부르지 않는다 — 키 이름이 액션 키와 일치하는지만 본다. 오타가 나면 그날 흐름 한 단계가 통째로 빠지므로 회귀 방지 가치가 있다.

```python
# ── 스케줄 등록 ─────────────────────────────────────────────
SCHEDULED_KEYS = [
    "daily_reset",
    "recommend",
    "buy",
    "cancel_unfilled",
    "close_out",
    "daily_report",
]


def test_every_scheduled_key_is_a_known_action():
    assert set(SCHEDULED_KEYS) <= set(ACTION_LABELS)


def test_submitting_every_scheduled_key_runs_the_daily_flow():
    """스케줄러가 넣는 여섯 키가 전부 접수되고 하루 흐름 순서대로 돈다."""
    calls = []
    runner = ActionRunner(make_runtime(calls))

    assert drain(runner, SCHEDULED_KEYS) == [True] * len(SCHEDULED_KEYS)
    assert calls == [
        "daily_reset",
        "recommend",
        "buy",
        "cancel_unfilled",
        "cancel_unfilled",
        "sell_all:day_end",
        "final_report",
    ]
```

- [ ] **Step 2: 테스트를 돌려 현재 상태를 확인한다**

Run: `pytest tests/test_core/test_actions.py -k scheduled -v`
Expected: PASS — Task 2 구현만으로 통과한다. 회귀 방지용이므로 그대로 두고 Step 3으로 간다.

- [ ] **Step 3: `Runtime`에 `runner` 필드를 더한다**

`src/core/runtime.py`의 `Runtime` dataclass를 바꾼다.

```python
@dataclass
class Runtime:
    settings: Settings
    engine: TradingEngine
    scheduler: TimeScheduler
    ws_client: WebSocketClient
    workflow: DailyWorkflow
    # 스케줄 잡과 UI 버튼이 공유하는 실행 통로. build_runtime이 Runtime을 만든 뒤에
    # 채운다 — ActionRunner가 runtime을 참조해야 해서 순서를 뒤집을 수 없다.
    runner: Optional[ActionRunner] = None
```

- [ ] **Step 4: `build_runtime`의 스케줄 등록을 액션 키로 바꾼다**

현재 `scheduler = TimeScheduler()` 이후의 등록 루프와 `return Runtime(...)`를 다음으로 바꾼다.

```python
    # 매수/강제청산은 실시간 익절·손절 감시와 직렬화되도록 루프 스레드에서 그대로 실행하고,
    # 오래 걸리는 수집·LLM·리포트는 루프를 막지 않도록 별도 스레드로 넘긴다 — 이 배분은
    # 이제 ActionRunner가 ManualStep.touches_orders를 보고 결정한다.
    # 모든 작업은 거래일에만 돌도록 감싼다 (_trading_days_only 참고). 거래일 가드는
    # 스케줄 쪽에만 있다 — UI 버튼은 지금처럼 요일과 무관하게 눌린다.
    scheduler = TimeScheduler()
    runtime = Runtime(
        settings=settings,
        engine=engine,
        scheduler=scheduler,
        ws_client=ws_client,
        workflow=workflow,
    )
    runtime.runner = ActionRunner(runtime)

    for trigger_time, action in (
        (DAILY_RESET_TIME, "daily_reset"),
        (settings.recommend_time, "recommend"),
        (settings.buy_time, "buy"),
        (CANCEL_UNFILLED_TIME, "cancel_unfilled"),
        (FORCE_CLOSE_TIME, "close_out"),
        (REPORT_TIME, "daily_report"),
    ):
        scheduler.add_job(
            trigger_time,
            _trading_days_only(_submit(runtime, action), action),
            name=action,
        )

    return runtime
```

`_off_loop` 함수는 더 이상 쓰이지 않으므로 **지우고, 그 자리에** 어댑터를 넣는다 (스레드 배분은 이제 `ActionRunner`가 `touches_orders`를 보고 맡는다).

```python
def _submit(runtime: "Runtime", action: str) -> Callable[[], None]:
    """스케줄 잡을 실행 통로 접수로 바꾼다 — 버튼과 같은 큐를 탄다."""

    def job() -> None:
        runtime.runner.submit(action)

    return job
```

- [ ] **Step 5: `runtime.run`이 러너 태스크를 띄우게 한다**

```python
async def run(runtime: Runtime) -> None:
    """엔진을 시작하고 스케줄러·실시간 시세를 함께 구동한다."""
    runtime.engine.start()
    # 구독은 연결 전에 걸어도 된다 — WebSocketClient.connect가 접속 직후 복구해 보낸다
    adopt_carried_over_positions(runtime)
    runner_task = asyncio.create_task(runtime.runner.run())
    watchdog = asyncio.create_task(watch_quote_stall(runtime))
    closeout_watch = asyncio.create_task(watch_closeout_report(runtime))
    cash_watch = asyncio.create_task(watch_cash_refresh(runtime))
    try:
        await asyncio.gather(runtime.ws_client.connect(), runtime.scheduler.run())
    except asyncio.CancelledError:
        pass
    finally:
        runner_task.cancel()
        watchdog.cancel()
        closeout_watch.cancel()
        cash_watch.cancel()
        request_stop(runtime)
        await runtime.ws_client.disconnect()
        logger.info("매매 런타임이 종료되었습니다.")
```

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest`
Expected: 전부 PASS.

`tests/test_core/test_multiday_run.py`가 스케줄 잡 이름이나 `_off_loop`를 참조하고 있으면 깨진다. 깨지면 새 이름(`daily_reset`, `close_out`, `daily_report`)과 `_submit` 구조에 맞게 테스트를 고친다 — 테스트가 검증하려던 동작(연속 실행 시 매일 트리거가 다시 발동하는지)은 그대로 유지한다.

- [ ] **Step 7: 커밋**

```bash
git add src/core/runtime.py tests/test_core/test_actions.py
git commit -m "스케줄 실행이 버튼과 같은 실행 통로를 타게 한다"
```

---

### Task 4: `EngineThread`가 실행을 `ActionRunner`에 위임한다

**Files:**
- Modify: `src/ui/engine_thread.py` (`__init__` 필드, `run`의 콜백 연결, `action_busy`, `run_action`, `_run_action` 삭제, `_await_action`)
- Test: 수동 확인 (PyQt 스레드 코드라 단위 테스트를 두지 않는다 — 기존에도 없다)

**Interfaces:**
- Consumes: Task 3의 `runtime.runner`
- Produces: `EngineThread.run_action(action, tickers=()) -> bool`, `EngineThread.action_busy -> bool` (기존 시그니처 유지)

- [ ] **Step 1: 필드와 콜백 연결을 바꾼다**

`__init__`에서 `self._action_busy`와 `self._action_future`를 지운다.

```python
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._runtime: Optional[runtime_module.Runtime] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
```

`run()`에서 런타임을 만든 직후 러너의 생명주기 콜백을 시그널로 잇는다.

```python
        try:
            self._runtime = runtime_module.build_runtime(self._settings)
            # 스케줄 실행과 버튼 실행이 같은 통로를 타므로, 자동 실행 상태도 UI에 그대로 뜬다
            self._runtime.runner.on_started = self.action_started.emit
            self._runtime.runner.on_finished = self.action_finished.emit
            self.started_ok.emit()
            loop.run_until_complete(runtime_module.run(self._runtime))
```

- [ ] **Step 2: `action_busy`를 러너에 위임한다**

```python
    # ── 즉시 실행 ────────────────────────────────────────────
    @property
    def action_busy(self) -> bool:
        runtime = self._runtime
        return runtime is not None and runtime.runner is not None and runtime.runner.busy
```

- [ ] **Step 3: `run_action`을 접수만 하도록 줄인다**

```python
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
```

기존 `async def _run_action(self, action, steps)` 메서드는 통째로 지운다 — `ActionRunner._execute`가 같은 일을 한다.

- [ ] **Step 4: `_await_action`이 큐가 빌 때까지 기다리게 한다**

```python
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
```

`from concurrent.futures import Future` import가 더 이상 쓰이지 않으면 지운다.

- [ ] **Step 5: 전체 테스트를 돌린다**

Run: `pytest`
Expected: 전부 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/ui/engine_thread.py
git commit -m "엔진 스레드가 실행을 ActionRunner에 위임한다"
```

---

### Task 5: 자동 실행 중에도 버튼을 잠근다

**Files:**
- Modify: `src/ui/main_window.py` (import, `_on_action_started`, `_on_action_finished`)
- Test: 수동 확인 (PyQt UI 코드 — 기존에도 단위 테스트가 없다)

**Interfaces:**
- Consumes: Task 4의 `action_started` 시그널이 스케줄 액션 키로도 올라온다
- Produces: 없음 (UI 종단)

- [ ] **Step 1: 라벨 조회를 `ACTION_LABELS`로 넓힌다**

import를 바꾼다.

```python
from src.core.runtime import ACTION_LABELS, CONFIRM_ACTIONS, MANUAL_ACTIONS, ORDER_ACTIONS
```

`_on_action_started`와 `_on_action_finished`의 `MANUAL_ACTIONS.get(action, action)`을 `ACTION_LABELS.get(action, action)`으로 바꾼다. 버튼 생성(`_make_action_button`)과 확인 팝업(`_confirm_action`)은 `MANUAL_ACTIONS`를 그대로 쓴다 — 스케줄 전용 액션에는 버튼도 팝업도 없다.

- [ ] **Step 2: 실행이 시작되면 버튼을 잠근다**

```python
    def _on_action_started(self, action: str) -> None:
        # 스케줄 실행도 이 시그널을 쏘므로, 09:05 추천이 도는 동안에도 버튼이 잠긴다 —
        # 자동 실행과 겹쳐 누르는 것을 막는다 (버튼 경로의 잠금은 _run_action에도 있다)
        self._set_actions_enabled(False)
        self._statusbar.showMessage(f"실행 중: {ACTION_LABELS.get(action, action)} …")
```

- [ ] **Step 3: `_run_action`의 낙관적 잠금은 그대로 둔다**

`_run_action` 끝의 다음 부분은 바꾸지 않는다 — `action_started`가 오기 전 잠깐의 공백에도 두 번 눌리지 않게 하는 장치다.

```python
        if thread.run_action(action, tickers):
            self._set_actions_enabled(False)
            self._statusbar.showMessage(f"즉시 실행 요청: {MANUAL_ACTIONS[action]}")
```

- [ ] **Step 4: UI를 띄워 눈으로 확인한다**

Run: `python scripts/run_ui.py`

모의투자(`paper`) 모드로만 확인한다:
1. "▶ 시작"을 누르면 ①~⑤ 버튼이 열린다.
2. ① LLM 추천을 누르면 실행 중에 모든 버튼이 잠기고 상태바에 "실행 중: ① LLM 추천 + 메일 발송 …"이 뜬다.
3. 끝나면 버튼이 다시 열리고 "즉시 실행 완료"가 뜬다.
4. "■ 정지"가 정상 동작한다.

- [ ] **Step 5: 커밋**

```bash
git add src/ui/main_window.py
git commit -m "자동 실행 중에도 즉시 실행 버튼을 잠근다"
```

---

### Task 6: `DailyWorkflow.buy_orders_filled` — 체결내역으로 전량 체결을 판정한다

**Files:**
- Modify: `src/core/daily_workflow.py` (`cancel_unfilled_buys` 바로 위에 추가)
- Test: `tests/test_core/test_buy_result_timing.py` (신규)

**Interfaces:**
- Consumes: 없음 (`DailyWorkflow` 내부의 `_read_buy_records`, `engine.order_client.get_today_fills`)
- Produces: `DailyWorkflow.buy_orders_filled(today: Optional[date] = None) -> bool`

- [ ] **Step 1: 실패 테스트를 쓴다**

`tests/test_core/test_buy_result_timing.py`를 만든다. `make_workflow`와 autouse fixture는 기존 테스트에서 재사용한다.

```python
"""매수 결과 메일 조기 발송의 판정 — 체결내역 주문번호 대조 (PRD 5.11).

2026-09-01에 되돌린 잔고 대조 방식(cb93b2b)과 다르다. 잔고는 '이 종목을 들고 있는가'에
답할 뿐 '내 주문이 체결됐는가'에 답하지 못해, 이월 보유분이나 다른 인스턴스가 산 물량을
자기 체결로 오인하고 살아 있는 주문을 취소했다.
"""
from datetime import date

from src.core.events import BuyOutcome, BuyRecord, FillRecord, OrderSide

from tests.test_core.test_daily_workflow import (  # noqa: F401  (autouse fixture 재사용)
    buy_records_file,
    make_workflow,
    report_mark,
)

TODAY = date.today()


def ordered(ticker, order_id, quantity=10):
    return BuyRecord(
        ticker=ticker,
        name=ticker,
        outcome=BuyOutcome.ORDERED,
        quantity=quantity,
        reference_price=1000.0,
        order_id=order_id,
    )


def fill(order_id, ticker, filled=10, unfilled=0):
    return FillRecord(
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.BUY,
        filled_quantity=filled,
        filled_price=1000.0,
        unfilled_quantity=unfilled,
    )


def prepare(records, fills):
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    workflow._write_buy_records(12_000_000, 1_000_000, records)
    order_client.fills = fills
    return workflow, order_client


def test_all_orders_fully_filled_is_true():
    workflow, _ = prepare(
        [ordered("005930", "1"), ordered("000660", "2")],
        [fill("1", "005930"), fill("2", "000660")],
    )
    assert workflow.buy_orders_filled(TODAY) is True


def test_partial_fill_is_false():
    """부분체결이면 남은 잔량을 10:10에 취소해야 한다 — 지금 보내면 결과가 확정되지 않는다."""
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [fill("1", "005930", filled=6, unfilled=4)],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_order_missing_from_fills_is_false():
    """체결내역에 흔적조차 없는 주문은 '모르면 기다린다'로 판정한다."""
    workflow, _ = prepare(
        [ordered("005930", "1"), ordered("000660", "2")],
        [fill("1", "005930")],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_zero_filled_quantity_is_false():
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [fill("1", "005930", filled=0, unfilled=10)],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_sell_fill_with_the_same_order_id_is_ignored():
    """매도 체결이 같은 주문번호로 잡혀도 매수 판정에 쓰지 않는다."""
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [
            FillRecord(
                order_id="1",
                ticker="005930",
                side=OrderSide.SELL,
                filled_quantity=10,
                filled_price=1000.0,
                unfilled_quantity=0,
            )
        ],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_lookup_failure_is_false():
    """조회가 실패하면 10:10에 맡긴다 — 추측으로 메일을 앞당기지 않는다."""
    workflow, order_client = prepare([ordered("005930", "1")], [])

    def boom():
        raise RuntimeError("체결내역 조회 실패")

    order_client.get_today_fills = boom
    assert workflow.buy_orders_filled(TODAY) is False


def test_no_records_is_false_without_calling_the_api():
    """메일이 나가면 기록이 지워진다 — 그 뒤로 하루 종일 조회가 도는 일이 없어야 한다."""
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    called = []
    order_client.get_today_fills = lambda: called.append(1) or []

    assert workflow.buy_orders_filled(TODAY) is False
    assert called == []


def test_records_without_ordered_rows_are_false_without_calling_the_api():
    """전부 건너뛴 날은 접수한 주문이 없다 — 보낼 결과도, 조회할 것도 없다."""
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    workflow._write_buy_records(
        12_000_000,
        1_000_000,
        [
            BuyRecord(
                ticker="005930",
                name="삼성전자",
                outcome=BuyOutcome.SKIPPED,
                reference_price=1000.0,
                note="시가 갭",
            )
        ],
    )
    called = []
    order_client.get_today_fills = lambda: called.append(1) or []

    assert workflow.buy_orders_filled(TODAY) is False
    assert called == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_buy_result_timing.py -v`
Expected: FAIL — `AttributeError: 'DailyWorkflow' object has no attribute 'buy_orders_filled'`

- [ ] **Step 3: `buy_orders_filled`를 구현한다**

`src/core/daily_workflow.py`의 `cancel_unfilled_buys` 바로 위에 넣는다.

```python
    def buy_orders_filled(self, today: Optional[date] = None) -> bool:
        """오늘 접수한 매수 주문이 남김없이 전량 체결됐는지 — 결과 메일을 앞당길 근거.

        판정은 **당일 체결내역 조회의 주문번호 대조**다. 2026-09-01에 되돌린 구현(cb93b2b)은
        잔고를 대조했는데, 잔고는 "이 종목을 들고 있는가"에 답할 뿐 "내 주문이 체결됐는가"에
        답하지 못한다 — 이월 보유 종목이나 다른 인스턴스가 산 물량을 자기 체결로 오인해
        살아 있는 주문을 1시간 4분 일찍 취소했다 (PRD 10절).

        `_fill_buy_prices`와 같은 조회를 같은 방식으로 읽으므로, 여기서 True가 서면
        `cancel_unfilled_buys` 안에서 체결가가 그대로 채워진다.

        기록이 없거나(메일이 이미 나갔거나 주문이 없던 날) 접수 행이 없으면 **API를 부르지
        않고** False다. 부분체결·미체결이 남았거나, 조회 결과에 흔적조차 없는 주문이 있거나,
        조회가 실패하면 역시 False — 그런 날은 종전대로 10:10이 마무리한다.
        """
        state = self._read_buy_records(today or date.today())
        if state is None:
            return False

        pending = {r.order_id for r in state.records if r.order_id and r.outcome.is_ordered}
        if not pending:
            return False

        try:
            fills = self.engine.order_client.get_today_fills()
        except Exception:
            logger.warning(
                "매수 체결 확인 조회 실패 — 결과 메일을 10:10에 맡깁니다.", exc_info=True
            )
            return False

        settled = {
            fill.order_id
            for fill in fills
            if fill.side == OrderSide.BUY
            and fill.filled_quantity > 0
            and fill.unfilled_quantity == 0
        }
        return pending <= settled
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_core/test_buy_result_timing.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 전체 테스트를 돌린다**

Run: `pytest`
Expected: 전부 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/core/daily_workflow.py tests/test_core/test_buy_result_timing.py
git commit -m "접수한 매수의 전량 체결을 체결내역으로 판정한다"
```

---

### Task 7: `watch_buy_result` — 전량 체결이 확인되면 결과 메일을 앞당긴다

**Files:**
- Modify: `src/core/runtime.py` (상수 추가, `watch_buy_result` 추가, `run`에 태스크 등록)
- Test: `tests/test_core/test_buy_result_timing.py` (추가)

**Interfaces:**
- Consumes: Task 6의 `workflow.buy_orders_filled`, Task 2의 `runner.submit`
- Produces: `async watch_buy_result(runtime, interval_seconds=BUY_RESULT_CHECK_INTERVAL_SECONDS) -> None`, `BUY_RESULT_CHECK_INTERVAL_SECONDS = 60`

- [ ] **Step 1: 실패 테스트를 쓴다**

`tests/test_core/test_buy_result_timing.py` 끝에 붙인다.

```python
# ── 감시 태스크 ─────────────────────────────────────────────
import asyncio
from types import SimpleNamespace

from src.core.runtime import watch_buy_result


def run_watch(filled_sequence, submitted, cycles=5):
    """buy_orders_filled가 순서대로 값을 돌려주게 하고 감시를 몇 바퀴 돌린다."""
    answers = list(filled_sequence)

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(
            buy_orders_filled=lambda: answers.pop(0) if answers else False
        ),
        runner=SimpleNamespace(submit=lambda action: submitted.append(action) or True),
    )

    async def scenario():
        task = asyncio.create_task(watch_buy_result(runtime, interval_seconds=0))
        for _ in range(cycles):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_watch_submits_cancel_unfilled_when_all_orders_are_filled():
    submitted = []
    run_watch([True], submitted)
    assert submitted == ["cancel_unfilled"]


def test_watch_stays_quiet_while_orders_are_unfilled():
    submitted = []
    run_watch([False, False, False], submitted)
    assert submitted == []


def test_watch_survives_a_failing_check():
    """판정이 터져도 감시는 계속 돌아야 한다 — 다음 바퀴에 다시 본다."""
    submitted = []
    answers = [None, True]  # None이면 예외를 던진다

    def check():
        value = answers.pop(0) if answers else False
        if value is None:
            raise RuntimeError("조회 실패")
        return value

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(buy_orders_filled=check),
        runner=SimpleNamespace(submit=lambda action: submitted.append(action) or True),
    )

    async def scenario():
        task = asyncio.create_task(watch_buy_result(runtime, interval_seconds=0))
        for _ in range(6):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert submitted == ["cancel_unfilled"]
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_buy_result_timing.py -k watch -v`
Expected: FAIL — `ImportError: cannot import name 'watch_buy_result'`

- [ ] **Step 3: 상수와 감시 태스크를 더한다**

`src/core/runtime.py`의 `CLOSEOUT_SETTLE_SECONDS` 아래에 상수를 넣는다.

```python
# 매수 체결 완료 감시 — 접수한 매수가 전부 체결됐으면 10:10을 기다리지 않고 결과 메일을 보낸다.
# 판정이 체결내역 조회를 부르므로 시세 감시보다 넉넉한 주기를 쓴다. 결과 메일이 나가면
# 기록 파일이 지워져 판정이 API를 부르지 않게 되므로, 하루 호출은 09:08~체결확인 구간에 그친다.
BUY_RESULT_CHECK_INTERVAL_SECONDS = 60
```

`watch_closeout_report` 아래에 감시 태스크를 넣는다.

```python
async def watch_buy_result(
    runtime: Runtime,
    interval_seconds: float = BUY_RESULT_CHECK_INTERVAL_SECONDS,
) -> None:
    """접수한 매수가 전부 체결되면 10:10을 기다리지 않고 결과 메일을 보낸다.

    주문 지정가를 허용 밴드 상단으로 올린 뒤로(2026-08-26) 접수 직후 전량 체결되는 날이
    대부분인데, 결과 메일만 `CANCEL_UNFILLED_TIME`에 묶여 한 시간 늦게 나갔다. 매도 쪽
    `watch_closeout_report`와 같은 구조다.

    판정은 `DailyWorkflow.buy_orders_filled` — 체결내역 조회의 주문번호 대조다. 2026-09-01에
    되돌린 잔고 대조와 달리 남의 물량에 속지 않는다 (PRD 10절).

    발송은 직접 부르지 않고 실행 통로에 접수한다. 09:08 매수가 아직 도는 중이면 그 뒤에서
    기다리고, 실시간 익절·손절 감시와도 직렬화된다. 이미 큐에 있으면 러너가 걸러낸다.
    메일이 나가면 기록 파일이 지워지므로 10:10·15:15가 같은 메일을 다시 보내지 않는다.
    """
    while True:
        await asyncio.sleep(interval_seconds)

        try:
            if not runtime.workflow.buy_orders_filled():
                continue
        except Exception:
            logger.exception("매수 체결 확인에 실패했습니다 — 10:10 마무리에 맡깁니다.")
            continue

        logger.info(
            "접수한 매수가 전부 체결됐습니다 — %s을 기다리지 않고 매수 결과 메일을 발송합니다.",
            CANCEL_UNFILLED_TIME.strftime("%H:%M"),
        )
        runtime.runner.submit("cancel_unfilled")
```

- [ ] **Step 4: `runtime.run`에 태스크를 등록한다**

```python
    runner_task = asyncio.create_task(runtime.runner.run())
    watchdog = asyncio.create_task(watch_quote_stall(runtime))
    closeout_watch = asyncio.create_task(watch_closeout_report(runtime))
    buy_result_watch = asyncio.create_task(watch_buy_result(runtime))
    cash_watch = asyncio.create_task(watch_cash_refresh(runtime))
```

`finally` 블록에 취소를 더한다.

```python
        runner_task.cancel()
        watchdog.cancel()
        closeout_watch.cancel()
        buy_result_watch.cancel()
        cash_watch.cancel()
```

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

Run: `pytest tests/test_core/test_buy_result_timing.py -v`
Expected: 전부 PASS

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `pytest`
Expected: 전부 PASS

- [ ] **Step 7: 커밋**

```bash
git add src/core/runtime.py tests/test_core/test_buy_result_timing.py
git commit -m "접수한 매수가 전부 체결되면 그 시점에 결과 메일을 보낸다"
```

---

### Task 8: 문서를 갱신한다

**Files:**
- Modify: `주식자동매매_PRD.md` (5.5-B, 5.10, 5.11, 10절)
- Modify: `CLAUDE.md` (스레드 / 이벤트 루프 구조 절)
- Test: 없음 (문서)

**Interfaces:**
- Consumes: Task 1~7의 최종 동작
- Produces: 없음

- [ ] **Step 1: PRD에서 고칠 자리를 찾는다**

Run: `python -c "import pathlib; t=pathlib.Path('주식자동매매_PRD.md').read_text(encoding='utf-8'); [print(i+1, l) for i,l in enumerate(t.splitlines()) if l.startswith('#')]"`

5.5-B, 5.10, 5.11, 10절의 줄 번호를 확인한다. 10절에서 "매수 결과 메일 조기 발송"(2026-09-01 철회 기록) 항목의 위치도 찾는다.

- [ ] **Step 2: 5.5-B 6단계에 조기 발송을 적는다**

매수 결과 메일 발송 시점 설명에 다음 취지를 더한다. 문장은 주변 서술 톤에 맞춘다.

> 접수한 매수 주문이 **전부 전량 체결**되면 10:10을 기다리지 않고 그 시점에 결과 메일이
> 나간다. 판정은 당일 체결내역 조회의 주문번호 대조이며, 한 건이라도 확인되지 않으면
> 종전대로 10:10 `cancel_unfilled_buys`가 마무리한다.

- [ ] **Step 3: 5.10(UI)에 실행 큐와 버튼 잠금을 적는다**

> 스케줄 실행과 '즉시 실행' 버튼은 같은 실행 큐를 쓴다. 자동 실행이 도는 동안에는 ①~⑤
> 버튼이 잠기고 상태바에 진행 중인 단계가 뜬다. 실행이 겹치면 거부하지 않고 순서대로
> 기다린다 — 스케줄 작업이 조용히 빠지지 않게 하기 위해서다.

- [ ] **Step 4: 5.11에 조기 발송 조건을 적는다**

매도 쪽 '전량 매도 즉시 리포트' 서술과 나란히, 매수 쪽 조기 발송 조건(전량 체결, 주문번호 대조, 확인 실패 시 10:10 유지)을 적는다.

- [ ] **Step 5: 10절에 재도입 경위를 남긴다**

기존 "매수 결과 메일 조기 발송" 항목(2026-09-01 철회 기록) 뒤에 이어 쓴다. 철회 기록은 지우지 않는다.

> **조기 발송 재도입 (2026-09-02).** 철회한 기능을 판정 근거만 바꿔 다시 넣었다. 잔고
> 대조 대신 당일 체결내역 조회의 주문번호를 대조해, 접수한 주문번호가 전부
> `미체결 잔량 0`일 때만 앞당긴다. 이월 보유 종목이나 다른 인스턴스가 산 물량은 주문번호가
> 다르므로 판정에 섞이지 않는다. 확인되지 않으면 기다리는 쪽으로 틀리므로, 실패해도
> 10:10 흐름이 그대로 남는다.

- [ ] **Step 6: `CLAUDE.md`의 실행 경로 서술을 고친다**

"### 스레드 / 이벤트 루프 구조" 절의 마지막 항목("UI의 '즉시 실행' 버튼(①~④)은 스케줄러가 호출하는 것과 **동일한 함수**를 그 자리에서 호출한다 — 별도 코드 경로가 아니다.")을 다음으로 바꾼다.

```markdown
- 스케줄 실행과 UI의 "즉시 실행" 버튼(①~⑤)은 **같은 실행 큐**(`src/core/actions.py`의
  `ActionRunner`)를 탄다 — 별도 코드 경로가 아니다. 큐는 중복 없는 FIFO라, 실행이 겹치면
  거부하지 않고 순서대로 기다린다. 단계별로 `touches_orders`를 보고 루프 스레드/별도
  스레드를 정한다. 단, 버튼 ⑤와 15:35 스케줄은 **다른 함수**를 부른다 — 버튼은
  `send_daily_report`(항상 발송), 스케줄은 `send_final_report`(하루 한 번).
```

같은 절의 `runtime._off_loop` 언급도 `ActionRunner`가 `touches_orders`로 배분한다는 서술로 고친다.

- [ ] **Step 7: 전체 테스트를 마지막으로 돌린다**

Run: `pytest`
Expected: 전부 PASS

- [ ] **Step 8: 커밋**

```bash
git add 주식자동매매_PRD.md CLAUDE.md
git commit -m "실행 통로 통합과 매수 결과 조기 발송을 문서에 반영한다"
```
