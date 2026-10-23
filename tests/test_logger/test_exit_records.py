"""AI 매도 판단 기록과 당일 고점 테이블.

판단·고점이 메모리에만 있으면 엔진 재시작으로 사라진다 — 2026-09-22에 09:17·10:59
재시작으로 고점 추적이 초기화됐다. 여기서는 저장과 조회 계약만 본다.
"""
import sqlite3
from datetime import date, datetime

from src.logger.trade_store import AIExitDecisionRow, TradeStore

DAY = date(2026, 9, 22)


def decision(ticker="032830", at=datetime(2026, 9, 22, 10, 20), sell=False, ok=True, version="v2"):
    return AIExitDecisionRow(
        day=DAY,
        at=at,
        ticker=ticker,
        name="삼성생명",
        sell=sell,
        ok=ok,
        reason="손실 구간",
        net_return=-0.0274,
        peak_return=-0.0073,
        current_price=290_500.0,
        exit_prompt_version=version,
    )


def _decision_count(store) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM ai_exit_decisions").fetchone()[0]


def test_decisions_are_saved_one_row_each(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_ai_exit_decisions([decision(), decision(ticker="114800", sell=True)])
    assert _decision_count(store) == 2


def test_saving_no_decisions_is_a_no_op(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_ai_exit_decisions([])
    assert _decision_count(store) == 0


def test_peak_only_moves_up(tmp_path):
    """재시작 직후 더 낮은 고점이 들어와도 기존 고점을 덮지 않는다."""
    store = TradeStore(tmp_path / "t.db")
    at = datetime(2026, 9, 22, 9, 10)
    store.save_position_peak(DAY, "032830", 0.017, at, 304_000.0)
    store.save_position_peak(DAY, "032830", -0.0291, at, 289_500.0)
    store.save_position_peak(DAY, "035420", 0.0002, at, 203_500.0)

    assert store.position_peaks_for(DAY) == {"032830": 0.017, "035420": 0.0002}


def test_peaks_are_per_day(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(DAY, "032830", 0.01, datetime(2026, 9, 22, 9, 10), None)
    assert store.position_peaks_for(date(2026, 9, 23)) == {}
