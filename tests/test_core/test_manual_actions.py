from types import SimpleNamespace

import pytest

from src.core.runtime import MANUAL_ACTIONS, ORDER_ACTIONS, close_out, manual_steps


def make_runtime(calls):
    """호출 순서만 기록하는 가짜 런타임."""
    workflow = SimpleNamespace(
        recommend_and_notify=lambda: calls.append("recommend"),
        execute_buys=lambda: calls.append("buy"),
        cancel_unfilled_buys=lambda: calls.append("cancel_unfilled"),
        send_daily_report=lambda: calls.append("report"),
        drop_buy_plans=lambda tickers: calls.append(f"drop_plan:{','.join(tickers)}"),
    )
    engine = SimpleNamespace(
        force_close_all_positions=lambda reason="day_end": calls.append(f"sell_all:{reason}"),
        close_positions=lambda tickers, reason="manual_selected": calls.append(
            f"sell_selected:{reason}:{','.join(tickers)}"
        ),
    )
    return SimpleNamespace(workflow=workflow, engine=engine)


@pytest.mark.parametrize(
    "action,expected",
    [
        ("recommend", ["recommend"]),
        ("buy", ["buy"]),
        ("cancel_unfilled", ["cancel_unfilled"]),
        ("sell_all", ["sell_all:manual"]),
        ("report", ["report"]),
    ],
)
def test_single_action_runs_matching_step(action, expected):
    calls = []
    for step in manual_steps(make_runtime(calls), action):
        step.run()
    assert calls == expected


def test_full_action_stops_after_buy():
    """일괄 실행은 진입까지만 — 청산은 15:15, 리포트는 15:35 스케줄에 맡긴다."""
    calls = []
    for step in manual_steps(make_runtime(calls), "full"):
        step.run()
    assert calls == ["recommend", "buy"]


def test_full_action_never_liquidates():
    """매수 직후 되파는 일이 없어야 한다 — 익절/손절 감시 구간이 사라지고 왕복 비용만 남는다."""
    calls = []
    for step in manual_steps(make_runtime(calls), "full"):
        step.run()
    assert not any(call.startswith("sell_all") for call in calls)
    assert "report" not in calls


def test_only_order_steps_run_on_the_engine_loop():
    """주문 단계만 루프 스레드에서 직렬화하고, 오래 걸리는 단계는 별도 스레드로 넘긴다."""
    steps = [step.touches_orders for step in manual_steps(make_runtime([]), "full")]
    assert steps == [False, True]


def test_every_action_has_a_label_and_steps():
    calls = []
    runtime = make_runtime(calls)
    for action in MANUAL_ACTIONS:
        assert manual_steps(runtime, action)


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        manual_steps(make_runtime([]), "nope")


def test_order_actions_are_known_actions():
    assert ORDER_ACTIONS <= set(MANUAL_ACTIONS)


def test_cancel_unfilled_needs_confirmation_and_the_engine_loop():
    """주문이 나가는 액션이므로 확인을 받고, 실시간 감시와 직렬화되어야 한다."""
    assert "cancel_unfilled" in ORDER_ACTIONS
    steps = manual_steps(make_runtime([]), "cancel_unfilled")
    assert [step.touches_orders for step in steps] == [True]


# ── 선택 매도 ───────────────────────────────────────────────
def test_sell_selected_passes_the_chosen_tickers():
    calls = []
    for step in manual_steps(make_runtime(calls), "sell_selected", ["005930", "000660"]):
        step.run()
    assert calls == ["sell_selected:manual_selected:005930,000660"]


def test_sell_selected_needs_confirmation_and_the_engine_loop():
    """실제 매도가 나가므로 확인을 받고, 실시간 익절/손절 콜백과 직렬화되어야 한다."""
    assert "sell_selected" in ORDER_ACTIONS
    steps = manual_steps(make_runtime([]), "sell_selected", ["005930"])
    assert [step.touches_orders for step in steps] == [True]


def test_tickers_are_ignored_by_other_actions():
    """대상이 잔고 전체이거나 추천 결과로 정해지는 액션은 선택 목록을 쓰지 않는다."""
    calls = []
    for step in manual_steps(make_runtime(calls), "sell_all", ["005930"]):
        step.run()
    assert calls == ["sell_all:manual"]


# ── 15:15 마감 정리 ─────────────────────────────────────────
def test_closeout_cancels_unfilled_buys_before_liquidating():
    """순서가 뒤집히면 청산 뒤 살아남은 매수 주문이 체결돼 오버나이트 포지션이 된다."""
    calls = []
    runtime = make_runtime(calls)

    close_out(runtime.workflow, runtime.engine)()

    assert calls == ["cancel_unfilled", "sell_all:day_end"]


def test_closeout_liquidates_even_if_cancelling_raises():
    """취소 조회가 실패해도 당일 청산은 반드시 나가야 한다."""
    calls = []
    runtime = make_runtime(calls)

    def boom():
        calls.append("cancel_unfilled")
        raise RuntimeError("체결내역 조회 실패")

    runtime.workflow.cancel_unfilled_buys = boom
    close_out(runtime.workflow, runtime.engine)()

    assert calls == ["cancel_unfilled", "sell_all:day_end"]


def test_drop_plan_targets_the_selected_tickers():
    """'매수 예정' 표의 선택 삭제 — sell_selected와 같은 tickers 경로를 쓴다."""
    calls = []
    steps = manual_steps(make_runtime(calls), "drop_plan", ["005930", "035720"])

    for step in steps:
        step.run()

    assert calls == ["drop_plan:005930,035720"]


def test_drop_plan_runs_on_the_engine_loop():
    """매수 시각 작업과 같은 추천 목록·표를 건드리므로 직렬화되어야 한다."""
    steps = manual_steps(make_runtime([]), "drop_plan", ["005930"])
    assert [step.touches_orders for step in steps] == [True]


def test_drop_plan_is_not_an_order_action():
    """주문이 나가지 않는다 — 오히려 나갈 주문을 막는 쪽이다."""
    assert "drop_plan" not in ORDER_ACTIONS


def test_drop_plan_still_needs_confirmation():
    """주문은 안 나가지만 되돌릴 수 없다 — 추천을 다시 돌려야 복구된다."""
    from src.core.runtime import CONFIRM_ACTIONS

    assert "drop_plan" in CONFIRM_ACTIONS
    assert CONFIRM_ACTIONS <= set(MANUAL_ACTIONS)
