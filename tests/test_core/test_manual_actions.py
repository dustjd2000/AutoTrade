from types import SimpleNamespace

import pytest

from src.core.runtime import MANUAL_ACTIONS, ORDER_ACTIONS, manual_steps


def make_runtime(calls):
    """호출 순서만 기록하는 가짜 런타임."""
    workflow = SimpleNamespace(
        recommend_and_notify=lambda: calls.append("recommend"),
        execute_buys=lambda: calls.append("buy"),
        send_daily_report=lambda: calls.append("report"),
    )
    engine = SimpleNamespace(
        force_close_all_positions=lambda reason="day_end": calls.append(f"sell_all:{reason}")
    )
    return SimpleNamespace(workflow=workflow, engine=engine)


@pytest.mark.parametrize(
    "action,expected",
    [
        ("recommend", ["recommend"]),
        ("buy", ["buy"]),
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
    """일괄 실행은 진입까지만 — 청산은 15:20, 리포트는 15:30 스케줄에 맡긴다."""
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
