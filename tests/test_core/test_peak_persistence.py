"""당일 고점 영속화·복원 (스펙 2026-09-22 2.2).

2026-09-22 09:17·10:59 재시작으로 DrawdownTracker가 초기화돼, 11:00 판단이 실제 고점
-0.73% 대신 -2.91%를 봤다. 새 고점은 DB에 남기고, 엔진 시작 때 되살린다.
"""
from datetime import datetime, timedelta

from src.api.account import Position
from src.core.events import MarketData
from src.logger.trade_store import TradeStore

from tests.test_core.test_portfolio_exit import make_engine

TICKER = "005930"


def one_holding(price=1000.0):
    return {
        TICKER: Position(
            ticker=TICKER, quantity=100, avg_price=1000.0, current_price=price, name="삼성전자"
        )
    }


def today():
    return datetime.now().date()


def test_a_new_peak_is_written_to_the_store(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)

    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.03) < 1e-9


def test_writes_are_batched_within_the_flush_interval(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))

    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))
    assert abs(store.position_peaks_for(today())[TICKER] - 0.01) < 1e-9

    engine.flush_exit_peaks(force=True)
    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_peak_is_flushed_once_the_interval_elapses(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))
    engine._peaks_flushed_at = datetime.now() - timedelta(seconds=6)

    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_engine_start_restores_todays_peak(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(today(), TICKER, 0.0446, datetime.now(), 1044.6)

    engine, _, _ = make_engine(one_holding(), trade_store=store)

    assert engine.exit_drawdown.retracement(TICKER, 0.0).peak == 0.0446


def test_yesterdays_peak_is_not_restored(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(today() - timedelta(days=1), TICKER, 0.0446, datetime.now(), None)

    engine, _, _ = make_engine(one_holding(), trade_store=store)

    assert engine.exit_drawdown.retracement(TICKER, 0.0) is None


def test_peak_is_tracked_even_when_ai_exit_is_off(tmp_path):
    """"순이익 기회"는 AI를 끈 날에도 의미가 있다. 앞당김 표시는 남기지 않는다."""
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.ai_exit_enabled = False
    engine.exit_drawdown.threshold_ratio = 0.01

    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))
    engine.on_market_data(MarketData(ticker=TICKER, price=1015.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.03) < 1e-9
    assert engine.exit_drawdown.urgent_pending is False


def test_forced_close_flushes_pending_peaks(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))
    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))

    engine.force_close_all_positions(reason="day_end")

    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_store_failure_does_not_break_the_tick(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    store.save_position_peak = boom
    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))  # 예외가 새지 않는다


def test_a_failed_write_is_retried_on_the_next_flush(tmp_path):
    """기록이 실패한 고점은 유실되지 않고 다음 flush에서 재시도된다."""
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)

    real_save = store.save_position_peak
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("disk full")
        return real_save(*args, **kwargs)

    store.save_position_peak = flaky
    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))  # 첫 기록 실패

    engine.flush_exit_peaks(force=True)

    assert abs(store.position_peaks_for(today())[TICKER] - 0.03) < 1e-9
