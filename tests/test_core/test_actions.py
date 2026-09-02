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
