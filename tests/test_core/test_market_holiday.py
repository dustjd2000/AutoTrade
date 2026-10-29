"""휴장일 자동 판정 (확정 2026-09-24, PRD 10절).

2026-09-24(공휴일)에 `is_trading_day`가 요일만 보는 탓에 그날 흐름이 그대로 돌았다 —
추천 메일이 나가고 매수 주문 2건이 접수됐다(거래소가 "장이 열리지않는 날입니다"로 거부).
결정 시각의 판정(KODEX 200 당일 거래 여부)은 `test_etf_workflow.py`가 보고, 여기서는
판정 뒤 스케줄 차단과 주문 거부 판정, 엔진 표시를 본다.
"""
from types import SimpleNamespace

import pytest

from src.core.actions import ACTION_LABELS
from src.core.daily_workflow import _is_market_closed_rejection
from src.core.runtime import HOLIDAY_ALWAYS_ACTIONS, _submit

from tests.test_core.test_portfolio_exit import make_engine, two_holdings


# ── 판정 뒤 스케줄 차단 ──────────────────────────────────────
def make_runtime(market_closed: bool, submitted: list):
    engine = SimpleNamespace(market_closed_today=market_closed)
    runner = SimpleNamespace(submit=lambda action: submitted.append(action))
    return SimpleNamespace(engine=engine, runner=runner)


def test_scheduled_actions_are_skipped_on_a_holiday():
    submitted = []
    runtime = make_runtime(True, submitted)

    for action in ("recommend", "buy", "cancel_unfilled", "daily_report", "review_exits"):
        _submit(runtime, action)()

    assert submitted == []


def test_daily_reset_and_close_out_still_run_on_a_holiday():
    """상태 초기화는 다음 거래일을 위해, 마감 정리는 판정이 틀렸을 때를 위해 남긴다."""
    submitted = []
    runtime = make_runtime(True, submitted)

    for action in HOLIDAY_ALWAYS_ACTIONS:
        _submit(runtime, action)()

    assert submitted == list(HOLIDAY_ALWAYS_ACTIONS)


def test_nothing_is_skipped_on_a_normal_day():
    submitted = []
    runtime = make_runtime(False, submitted)

    _submit(runtime, "recommend")()

    assert submitted == ["recommend"]


def test_every_holiday_always_action_is_a_known_action():
    assert set(HOLIDAY_ALWAYS_ACTIONS) <= set(ACTION_LABELS)


# ── 두 번째 안전장치 — 주문 거부 문구 ────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "[kt10000] return_code=20: [2000](571489:장이 열리지않는 날입니다.)",
        "장이 열리지 않는 날입니다.",
    ],
)
def test_holiday_rejection_is_recognised(message):
    assert _is_market_closed_rejection(message) is True


@pytest.mark.parametrize("message", ["주문가능금액 부족", "", None])
def test_other_rejections_are_not_holidays(message):
    assert _is_market_closed_rejection(message) is False


# ── 엔진 표시 — 하루치이고 알림은 한 번 ──────────────────────
def test_engine_flag_notifies_once_and_clears_next_day():
    engine, _, alerts = make_engine(two_holdings())

    engine.note_market_closed("사유 A")
    engine.note_market_closed("사유 B")

    assert engine.market_closed_today is True
    assert len([a for a in alerts if "휴장" in a]) == 1

    engine.reset_for_new_day()

    assert engine.market_closed_today is False
