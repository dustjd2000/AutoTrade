"""휴장일 자동 판정 (확정 2026-09-24, PRD 10절).

2026-09-24(공휴일)에 `is_trading_day`가 요일만 보는 탓에 그날 흐름이 그대로 돌았다 —
LLM 추천과 추천 메일이 나가고 매수 주문 2건이 접수됐다(거래소가 "장이 열리지않는
날입니다"로 거부). 여기서는 판정과 그 뒤 차단만 본다.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from src.core.actions import ACTION_LABELS
from src.core.daily_workflow import _is_market_closed_rejection
from src.core.runtime import HOLIDAY_ALWAYS_ACTIONS, _submit
from src.data.collector import DailyStockData, market_looks_closed

from tests.test_core.test_daily_workflow import _recommendation, make_workflow
from tests.test_core.test_portfolio_exit import make_engine, two_holdings


def stock(ticker="005930", prev_close=70_000.0, today_price=70_000.0, change_rate=0.0):
    return DailyStockData(
        ticker=ticker,
        name="삼성전자",
        prev_close=prev_close,
        prev_high=prev_close,
        prev_low=prev_close,
        prev_change_rate=1.0,
        prev_volume=1_000,
        volume_surge=1.5,
        today_price=today_price,
        today_change_rate=change_rate,
    )


def closed_market(count=8):
    """휴장일 모습 — 현재가가 전일 종가 그대로이고 등락률이 0이다."""
    return [stock(ticker=f"00593{i}") for i in range(count)]


# ── 판정 ────────────────────────────────────────────────────
def test_all_stocks_unchanged_reads_as_closed():
    assert market_looks_closed(closed_market()) is True


def test_one_moving_stock_means_the_market_is_open():
    """한 종목이라도 움직였으면 거래일이다 — 보합이 섞이는 것은 흔하다."""
    candidates = closed_market()
    candidates[3] = stock(ticker="000660", today_price=70_700.0, change_rate=1.0)

    assert market_looks_closed(candidates) is False


def test_small_sample_is_not_judged():
    """표본이 적으면 판정하지 않는다 — 모르면 평소대로 도는 쪽이 기본이다."""
    assert market_looks_closed(closed_market(count=4)) is False


def test_unquoted_stocks_are_left_out_of_the_sample():
    """조회 실패(당일 지표 0)는 '모름'이지 '보합'이 아니다."""
    candidates = closed_market(count=5) + [
        stock(ticker="000660", today_price=0.0),
        stock(ticker="000270", today_price=0.0),
    ]

    assert market_looks_closed(candidates) is True


def test_sample_shrinks_below_the_floor_when_quotes_fail():
    candidates = closed_market(count=3) + [stock(ticker="000660", today_price=0.0)]

    assert market_looks_closed(candidates) is False


def test_empty_candidates_are_not_judged():
    assert market_looks_closed([]) is False


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


# ── 추천 경로 — LLM을 부르기 전에 막는다 ─────────────────────
def holiday_workflow():
    """수집 결과가 휴장 모습인 워크플로 — 엔진의 판정 호출을 기록한다."""
    workflow, email, _, notifications, _ = make_workflow(recommendations=[_recommendation()])
    workflow.collector = SimpleNamespace(collect=lambda: closed_market())
    called = []
    workflow.engine.note_market_closed = lambda reason: called.append(reason)
    llm_calls = []
    workflow.recommender = SimpleNamespace(
        recommend=lambda data: llm_calls.append(data) or [_recommendation()],
        prompt_version="20260917",
    )
    return workflow, email, called, llm_calls


def test_holiday_stops_the_recommendation_before_the_llm_call():
    workflow, email, called, llm_calls = holiday_workflow()

    workflow.recommend_and_notify(date(2026, 9, 24))

    assert llm_calls == [], "휴장일에는 LLM을 부르지 않는다 (호출 비용)"
    assert email.sent == []
    assert len(called) == 1 and "당일 지표" in called[0]


def test_normal_day_still_recommends():
    workflow, email, called, llm_calls = holiday_workflow()
    open_market = closed_market()
    open_market[0] = stock(ticker="000660", today_price=70_700.0, change_rate=1.0)
    workflow.collector = SimpleNamespace(collect=lambda: open_market)

    workflow.recommend_and_notify(date(2026, 9, 24))

    assert len(llm_calls) == 1
    assert called == []
    assert email.sent, "거래일에는 추천 메일이 그대로 나간다"


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
