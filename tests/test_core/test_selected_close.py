"""선택 매도 — 보유 종목 표에서 고른 종목만 판다 (PRD 5.10).

전량 청산(`force_close_all_positions`)과 주문 루프를 공유하므로, 여기서는 "고른 것만
팔리는가"와 "고르지 않은 종목이 그대로 남는가"를 확인한다.
"""
from types import SimpleNamespace

from src.api.account import Position
from src.core.engine import TradingEngine
from src.core.events import OrderResult, OrderStatus


def make_engine(positions, sent, sell_status=OrderStatus.FILLED):
    def send_order(request):
        sent.append(request)
        return OrderResult(
            order_id="1",
            ticker=request.ticker,
            side=request.side,
            status=sell_status,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_price=1000.0,
            error_message=None if sell_status == OrderStatus.FILLED else "거부",
        )

    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=send_order),
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
            check_portfolio_exit=lambda ps: None,
            commission_rate=0.0,
            tax_rate=0.0,
            position_net_pnl=lambda p: (p.current_price - p.avg_price) * p.quantity,
            portfolio_net_pnl=lambda ps: (0.0, None),
            check_simple_take_profits=lambda ps: [],
        ),
    )


def held(ticker, quantity=10, avg_price=1000.0, current_price=1010.0):
    return Position(
        ticker=ticker,
        quantity=quantity,
        avg_price=avg_price,
        current_price=current_price,
    )


def make_three(sent):
    positions = {t: held(t) for t in ("005930", "000660", "035420")}
    engine = make_engine(positions, sent)
    engine.start()
    return engine


def test_only_selected_tickers_are_sold():
    sent = []
    engine = make_three(sent)

    engine.close_positions(["000660"])

    assert [r.ticker for r in sent] == ["000660"]


def test_unselected_positions_stay_under_watch():
    """고르지 않은 종목은 그대로 보유하며 익절/손절 감시도 이어져야 한다."""
    sent = []
    engine = make_three(sent)

    engine.close_positions(["000660"])

    assert engine.open_tickers == ["005930", "035420"]


def test_multiple_selected_tickers_are_all_sold():
    sent = []
    engine = make_three(sent)

    engine.close_positions(["005930", "035420"])

    assert sorted(r.ticker for r in sent) == ["005930", "035420"]
    assert engine.open_tickers == ["000660"]


def test_ticker_missing_from_balance_is_skipped():
    """고른 뒤 팔렸거나 매도 불가로 제외된 종목은 주문을 내지 않는다."""
    sent = []
    engine = make_three(sent)

    engine.close_positions(["999999"])

    assert sent == []
    assert engine.open_tickers == ["000660", "005930", "035420"]


def test_empty_selection_sends_nothing():
    sent = []
    engine = make_three(sent)

    engine.close_positions([])

    assert sent == []
    assert engine.open_tickers == ["000660", "005930", "035420"]


def test_rejected_selected_close_keeps_ticker_under_watch():
    """매도가 거부됐다면 여전히 보유 중이므로 감시를 놓아선 안 된다."""
    sent = []
    positions = {"005930": held("005930")}
    engine = make_engine(positions, sent, sell_status=OrderStatus.REJECTED)
    engine.start()

    engine.close_positions(["005930"])

    assert engine.open_tickers == ["005930"]


def test_selected_close_reads_fresh_balance():
    """무엇을 파는지가 곧 결과이므로 캐시가 아니라 최신 잔고를 읽어야 한다."""
    sent = []
    calls = []
    positions = {"005930": held("005930")}
    engine = make_engine(positions, sent)
    engine.start()

    original = engine.account.get_positions

    def counted():
        calls.append(1)
        return original()

    engine.account.get_positions = counted
    engine.close_positions(["005930"])

    assert calls, "선택 매도가 잔고를 다시 읽지 않았다"
