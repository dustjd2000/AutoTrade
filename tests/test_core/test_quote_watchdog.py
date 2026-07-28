"""시세 끊김 감시 — 익절/손절이 실시간 시세에만 의존하므로 공백을 반드시 알려야 한다."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.core.runtime import is_market_hours, quote_stall_seconds


def make_runtime(open_tickers, last_market_data_at):
    return SimpleNamespace(
        engine=SimpleNamespace(
            open_tickers=open_tickers,
            last_market_data_at=last_market_data_at,
        )
    )


# 2026-07-28은 화요일
TUE_1000 = datetime(2026, 7, 28, 10, 0)
TUE_0830 = datetime(2026, 7, 28, 8, 30)
TUE_1600 = datetime(2026, 7, 28, 16, 0)
SAT_1000 = datetime(2026, 8, 1, 10, 0)


def test_market_hours_weekday():
    assert is_market_hours(TUE_1000) is True
    assert is_market_hours(TUE_0830) is False  # 장 시작 전
    assert is_market_hours(TUE_1600) is False  # 장 마감 후


def test_market_hours_weekend():
    assert is_market_hours(SAT_1000) is False


def test_no_stall_without_holdings():
    """보유 종목이 없으면 시세가 안 와도 문제가 아니다."""
    runtime = make_runtime([], TUE_1000 - timedelta(hours=2))
    assert quote_stall_seconds(runtime, TUE_1000) is None


def test_no_stall_outside_market_hours():
    """장 시간 외에는 시세가 없는 것이 정상이다."""
    runtime = make_runtime(["005930"], TUE_1600 - timedelta(hours=2))
    assert quote_stall_seconds(runtime, TUE_1600) is None


def test_stall_measured_from_last_quote():
    runtime = make_runtime(["005930"], TUE_1000 - timedelta(minutes=7))
    assert quote_stall_seconds(runtime, TUE_1000) == 420.0


def test_fresh_quote_is_not_stalled():
    runtime = make_runtime(["005930"], TUE_1000 - timedelta(seconds=10))
    assert quote_stall_seconds(runtime, TUE_1000) == 10.0


def test_never_received_quote_is_infinite_stall():
    """매수는 됐는데 첫 시세조차 못 받은 상태 — 구독 실패 가능성."""
    runtime = make_runtime(["005930"], None)
    assert quote_stall_seconds(runtime, TUE_1000) == float("inf")
