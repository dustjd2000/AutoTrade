from datetime import datetime

from src.core.events import ExitReason
from src.core.exit_trace import ExitTrace


def test_append_and_read_back():
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {"005930": 0.006}, {"005930": 71000.0})

    points = trace.points()
    assert len(points) == 1
    assert points[0].at == datetime(2026, 9, 9, 9, 20)
    assert points[0].portfolio_return == 0.004
    assert points[0].per_ticker["005930"] == 0.006
    assert points[0].prices["005930"] == 71000.0


def test_points_keep_their_order():
    """되돌림 판단은 순서가 전부다 — 쌓은 순서 그대로 나와야 한다."""
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.009, {}, {})
    trace.append(datetime(2026, 9, 9, 9, 35), 0.004, {}, {})

    assert [p.portfolio_return for p in trace.points()] == [0.009, 0.004]


def test_clear_empties_the_trace():
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})

    trace.clear()

    assert trace.points() == []
    assert trace.count == 0
    assert trace.partial is True


def test_partial_is_true_until_the_first_point():
    """엔진을 장중에 다시 켜면 궤적이 비어 시작한다 — 없는 것을 있는 척하면 안 된다."""
    trace = ExitTrace()
    assert trace.partial is True

    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})
    assert trace.partial is False


def test_points_returns_a_copy():
    """호출측이 리스트를 건드려도 내부 상태가 흔들리지 않는다."""
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})

    trace.points().clear()

    assert trace.count == 1


def test_appended_dicts_are_copied():
    """호출측이 재사용하는 dict를 그대로 들면 과거 지점까지 바뀐다."""
    trace = ExitTrace()
    per_ticker = {"005930": 0.006}
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, per_ticker, {})

    per_ticker["005930"] = 99.0

    assert trace.points()[0].per_ticker["005930"] == 0.006


def test_ai_judgment_exit_reason_exists_alongside_the_old_ones():
    """과거 기록이 읽히도록 TAKE_PROFIT을 남긴 채 새 사유를 더한다."""
    assert ExitReason.AI_JUDGMENT.value == "ai_judgment"
    assert ExitReason.TAKE_PROFIT.value == "take_profit"
    assert ExitReason.STOP_LOSS.value == "stop_loss"
