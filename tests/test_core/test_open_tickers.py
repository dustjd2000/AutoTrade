"""보유 종목 추적 — 창 종료 경고와 시세 끊김 감시가 이 값에 의존한다."""
from types import SimpleNamespace

from src.api.account import Position
from src.core.engine import TradingEngine
from src.core.events import MarketData, OrderResult, OrderSide, OrderStatus


def make_engine(positions, sell_status=OrderStatus.FILLED):
    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(
            send_order=lambda r: OrderResult(
                order_id="1",
                ticker=r.ticker,
                side=r.side,
                status=sell_status,
                quantity=r.quantity,
                filled_quantity=r.quantity,
                filled_price=1000.0,
                error_message=None if sell_status == OrderStatus.FILLED else "거부",
            )
        ),
        account=SimpleNamespace(
            get_positions=lambda: positions,
            get_balance_snapshot=lambda: SimpleNamespace(
                total_asset=1_000_000, cash=1_000_000, positions=positions
            ),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_exit=lambda p: None,
        ),
    )


def test_start_flags_carried_over_positions():
    """전일 이월 포지션을 들고 시작하면 감시 대상으로 잡혀야 한다."""
    positions = {"005930": Position(ticker="005930", quantity=10, avg_price=70000.0)}
    engine = make_engine(positions)

    engine.start()

    assert engine.open_tickers == ["005930"]


def test_note_open_position_marks_immediately():
    """체결 통보 전에도 매수 접수만으로 감시 대상이 된다."""
    engine = make_engine({})
    engine.start()
    engine.note_open_position("068270")

    assert engine.open_tickers == ["068270"]


def test_force_close_clears_sold_tickers():
    positions = {
        "005930": Position(ticker="005930", quantity=10, avg_price=70000.0, current_price=71000.0)
    }
    engine = make_engine(positions)
    engine.start()
    assert engine.open_tickers == ["005930"]

    engine.force_close_all_positions(reason="day_end")

    assert engine.open_tickers == []


def test_rejected_close_keeps_ticker_under_watch():
    """청산이 거부됐다면 여전히 보유 중이므로 감시를 놓아선 안 된다."""
    positions = {
        "005930": Position(ticker="005930", quantity=10, avg_price=70000.0, current_price=71000.0)
    }
    engine = make_engine(positions, sell_status=OrderStatus.REJECTED)
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    assert engine.open_tickers == ["005930"]


def test_market_data_refreshes_watch_list_and_timestamp():
    """잔고가 최신 사실이므로 시세 수신 시 감시 목록을 맞춘다."""
    positions = {"000660": Position(ticker="000660", quantity=5, avg_price=200000.0)}
    engine = make_engine(positions)
    engine.start()
    engine.note_open_position("999999")  # 이미 팔린 종목이 남아 있다고 가정

    assert engine.last_market_data_at is None
    engine.on_market_data(MarketData(ticker="000660", price=201000.0, volume=1))

    assert engine.open_tickers == ["000660"]
    assert engine.last_market_data_at is not None
