"""매도 판단 검증의 분류 (스펙 2026-09-22 3.2) — 잣대는 순수익이다.

덜 잃은 것은 성공이 아니다. 분류는 코드가 정하고 LLM은 평가문만 쓴다.
"""
from datetime import date, datetime

import pytest

from src.core.exit_review import (
    CAPTURED,
    MISSED,
    NO_CHANCE,
    UNKNOWN,
    build_exit_review_rows,
    classify_outcome,
)
from src.logger.trade_store import AIExitDecisionRow, TradeRow

DAY = date(2026, 9, 22)


@pytest.mark.parametrize(
    "net, peak, expected",
    [
        (0.012, 0.02, CAPTURED),
        (0.012, None, CAPTURED),
        (-0.029, 0.017, MISSED),
        (0.0, 0.017, MISSED),
        (-0.029, -0.0073, NO_CHANCE),
        (-0.029, 0.0, NO_CHANCE),
        (-0.029, None, UNKNOWN),
        (None, 0.02, UNKNOWN),
    ],
)
def test_classify_outcome(net, peak, expected):
    assert classify_outcome(net, peak) == expected


def decision(ticker, hour, version="v2"):
    return AIExitDecisionRow(
        day=DAY, at=datetime(2026, 9, 22, hour, 0), ticker=ticker, name="", sell=False, ok=True,
        reason="r", net_return=-0.01, peak_return=None, current_price=0.0, exit_prompt_version=version,
    )


def samsung_life():
    """2026-09-22 삼성생명 — 298,000원 2주, 290,000원 강제청산, 수수료·세금 1,320원."""
    return TradeRow(
        ticker="032830", name="삼성생명", quantity=2, buy_price=298_000.0, sell_price=290_000.0,
        pnl=-16_000.0, fees=1_320.0,
    )


def test_rows_use_net_pnl_after_fees():
    [row] = build_exit_review_rows(DAY, [samsung_life()], {"032830": -0.0073}, [], {"032830": "day_end"})

    assert row.net_pnl == -17_320.0
    assert abs(row.net_return - (-17_320.0 / 596_000.0)) < 1e-12
    assert row.outcome == NO_CHANCE
    assert row.exit_reason == "day_end"
    assert row.peak_return == -0.0073


def test_unsold_positions_are_not_reviewed():
    held = TradeRow(ticker="005930", name="", quantity=1, buy_price=1.0, sell_price=None, pnl=None)
    assert build_exit_review_rows(DAY, [held], {}, [], {}) == []


def test_unknown_pnl_is_unknown():
    manual = TradeRow(ticker="005930", name="", quantity=1, buy_price=0.0, sell_price=1.0, pnl=None)
    [row] = build_exit_review_rows(DAY, [manual], {"005930": 0.02}, [], {})
    assert row.net_pnl is None and row.outcome == UNKNOWN


def test_partial_unknown_pnl_forces_outcome_unknown():
    """F2 (2026-09-22 최종 리뷰) — `TradeRow.pnl`이 known 합계라 값이 있어도, 그 종목이
    `unknown_pnl_tickers`에 있으면(매도 중 일부가 realized_pnl 불명) 순손익을 모른다로 떨어진다.
    """
    [row] = build_exit_review_rows(
        DAY,
        [samsung_life()],
        {"032830": -0.0073},
        [],
        {"032830": "day_end"},
        unknown_pnl_tickers={"032830"},
    )
    assert row.net_pnl is None
    assert row.net_return is None
    assert row.outcome == UNKNOWN
    # 고점 등 나머지 수치는 그대로 채워진다 — 모르는 것은 순손익뿐이다
    assert row.peak_return == -0.0073


def test_version_and_count_come_from_that_tickers_decisions():
    decisions = [decision("032830", 10, "v2"), decision("035420", 11, "v1"), decision("032830", 14, "20260929")]
    [row] = build_exit_review_rows(DAY, [samsung_life()], {}, decisions, {})

    assert row.decision_count == 2
    assert row.exit_prompt_version == "20260929"


def test_no_decisions_means_empty_version():
    [row] = build_exit_review_rows(DAY, [samsung_life()], {}, [], {})
    assert row.exit_prompt_version == "" and row.decision_count == 0
