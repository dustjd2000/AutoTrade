"""AI 매도 판단 기록·매도 판단 검증 기록과 당일 고점 테이블.

판단·고점이 메모리에만 있으면 엔진 재시작으로 사라진다 — 2026-09-22에 09:17·10:59
재시작으로 고점 추적이 초기화됐다. 여기서는 저장과 조회 계약만 본다.
"""
import sqlite3
from datetime import date, datetime

from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow, TradeStore

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


def review(ticker="069500", day=DAY, version="v6", outcome="no_chance"):
    return ExitReviewRow(
        day=day,
        ticker=ticker,
        name="KODEX 200",
        exit_prompt_version=version,
        net_pnl=-17_240.0,
        net_return=-0.0289,
        peak_return=-0.0073,
        outcome=outcome,
        exit_reason="day_end",
        decision_count=11,
    )


def insert_trade(store, ticker, side, timestamp, exit_reason=None, status="filled"):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp, exit_reason)
               VALUES ('1', ?, ?, ?, 2, 2, 290000, 298000, -16000, ?, ?)""",
            (ticker, side, status, timestamp, exit_reason),
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


def test_decisions_round_trip_in_time_order(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    late = decision(at=datetime(2026, 9, 22, 14, 31), sell=True)
    early = decision(at=datetime(2026, 9, 22, 9, 20))
    store.save_ai_exit_decisions([late, early])

    rows = store.ai_exit_decisions_for(DAY)

    assert [r.at for r in rows] == [early.at, late.at]
    assert rows[1].sell is True and rows[1].ok is True
    assert rows[0].peak_return == -0.0073
    assert rows[0].exit_prompt_version == "v2"


def test_decisions_of_another_day_are_not_returned(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_ai_exit_decisions([decision()])
    assert store.ai_exit_decisions_for(date(2026, 9, 23)) == []
    assert store.ai_exit_decisions_for(DAY) != []


def test_last_exit_reason_wins_per_ticker(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    insert_trade(store, "069500", "buy", "2026-09-22T09:05:00")
    insert_trade(store, "069500", "sell", "2026-09-22T10:00:00", "ai_judgment")
    insert_trade(store, "069500", "sell", "2026-09-22T15:15:00", "day_end")
    insert_trade(store, "114800", "sell", "2026-09-22T14:00:46", "ai_judgment")
    insert_trade(store, "005930", "sell", "2026-09-22T14:00:00", "stop_loss", status="rejected")

    assert store.last_exit_reasons(DAY) == {"069500": "day_end", "114800": "ai_judgment"}


def test_tickers_with_unknown_pnl_only_includes_null_realized_sells(tmp_path):
    """realized_pnl이 NULL인 체결 매도가 있는 종목만 걸린다 (2026-09-22 최종 리뷰 F2)."""
    store = TradeStore(tmp_path / "t.db")
    insert_trade(store, "069500", "sell", "2026-09-22T10:00:00")  # realized_pnl = -16000 (앎)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp)
               VALUES ('2', '114800', 'sell', 'filled', 1, 1, 100000, NULL, NULL, '2026-09-22T11:00:00')"""
        )
        # 매수는 realized_pnl이 원래 NULL이라 여기 걸리면 안 된다 (side=SELL만 본다)
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp)
               VALUES ('3', '005930', 'buy', 'filled', 1, 1, 70000, 70000, NULL, '2026-09-22T09:05:00')"""
        )

    assert store.tickers_with_unknown_pnl(DAY) == {"114800"}


def test_partial_unknown_pnl_still_flags_the_ticker(tmp_path):
    """같은 종목의 매도 두 건 중 한 건만 realized_pnl을 몰라도 그 종목 전체가 걸린다."""
    store = TradeStore(tmp_path / "t.db")
    insert_trade(store, "069500", "sell", "2026-09-22T10:00:00")  # 앎 (-16000)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp)
               VALUES ('2', '069500', 'sell', 'filled', 1, 1, 290000, NULL, NULL, '2026-09-22T14:00:00')"""
        )

    assert store.tickers_with_unknown_pnl(DAY) == {"069500"}


def test_exit_review_upserts_per_day_and_ticker(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review())
    updated = review()
    updated.review = "10:20 하방선 이탈 뒤 보유"
    store.save_exit_review(updated)

    rows = store.exit_reviews_for(DAY)
    assert len(rows) == 1
    assert rows[0].review == "10:20 하방선 이탈 뒤 보유"
    assert rows[0].outcome == "no_chance"


def test_recent_exit_reviews_are_the_last_n_days_oldest_first(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    for day in (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)):
        store.save_exit_review(review(day=day))

    rows = store.recent_exit_reviews(day_count=2)

    assert [r.day for r in rows] == [date(2026, 9, 21), date(2026, 9, 22)]


def test_count_exit_reviews_ignores_unknown_and_other_versions(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review(ticker="069500", version="v6"))
    store.save_exit_review(review(ticker="114800", version="v6", outcome="unknown"))
    store.save_exit_review(review(ticker="069500", day=date(2026, 9, 23), version="20260929"))

    assert store.count_exit_reviews("v6") == 1


ETF = ("069500", "114800")


def test_ticker_filter_keeps_old_stock_rows_out_of_the_count(tmp_path):
    """exit_reviews에는 옛 1호 주식 행이 남아 있다 — ETF 행만 표본으로 센다 (2026-10-29)."""
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review(ticker="032830", version="v6"))
    store.save_exit_review(review(ticker="005930", version="v6"))
    store.save_exit_review(review(ticker="069500", version="v6"))

    assert store.count_exit_reviews("v6") == 3
    assert store.count_exit_reviews("v6", ETF) == 1
    assert store.count_exit_reviews("v6", ()) == 0


def test_ticker_filter_applies_to_recent_reviews_and_to_the_day_window(tmp_path):
    """거래일 수도 ETF 행이 있는 날로 센다 — 주식 행만 있는 날이 창을 차지하면 안 된다."""
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review(ticker="069500", day=date(2026, 9, 18)))
    store.save_exit_review(review(ticker="032830", day=date(2026, 9, 21)))
    store.save_exit_review(review(ticker="114800", day=date(2026, 9, 22)))
    store.save_exit_review(review(ticker="005930", day=date(2026, 9, 22)))

    rows = store.recent_exit_reviews(day_count=2, tickers=ETF)

    assert [(r.day, r.ticker) for r in rows] == [
        (date(2026, 9, 18), "069500"),
        (date(2026, 9, 22), "114800"),
    ]
    assert len(store.recent_exit_reviews(day_count=2)) == 3  # 거르지 않으면 9/21·9/22의 세 행


def test_exit_reviews_table_is_created_on_a_fresh_db_and_kept_on_reopen(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    with sqlite3.connect(store.db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "exit_reviews" in names
    # 같은 파일을 다시 열어도 기존 행이 그대로다 (CREATE IF NOT EXISTS)
    store.save_exit_review(review())
    assert len(TradeStore(tmp_path / "t.db").exit_reviews_for(DAY)) == 1
