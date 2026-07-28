"""전일 이월 포지션 — 아무도 구독하지 않으면 익절/손절 감시가 한 번도 돌지 않는다."""
from types import SimpleNamespace

from src.api.account import BalanceSnapshot, Position
from src.core.engine import TradingEngine
from src.core.events import ExitReason, MarketData, OrderResult, OrderSide, OrderStatus
from src.core.runtime import adopt_carried_over_positions


class FakeWebSocket:
    def __init__(self, connected=True):
        self.subscribed = []
        self.is_connected = connected

    def subscribe(self, tickers):
        self.subscribed.extend(tickers)


def make_engine(positions, exit_reason=None):
    orders = []

    def send_order(request):
        orders.append(request)
        return OrderResult(
            order_id=str(len(orders)),
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.FILLED,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_price=1000.0,
            error_message=None,
        )

    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=send_order),
        account=SimpleNamespace(
            get_positions=lambda: dict(positions),
            get_balance_snapshot=lambda: BalanceSnapshot(cash=0.0, positions=dict(positions)),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_exit=lambda p: exit_reason,
        ),
    )
    return engine, orders


def carried_over():
    """전일 매도되지 못하고 넘어온 포지션."""
    return {
        "005930": Position(
            ticker="005930", quantity=10, avg_price=70000.0, current_price=69000.0, name="삼성전자"
        )
    }


def make_runtime(positions, exit_reason=None, connected=True):
    engine, orders = make_engine(positions, exit_reason=exit_reason)
    runtime = SimpleNamespace(engine=engine, ws_client=FakeWebSocket(connected))
    return runtime, orders


def test_carried_over_positions_are_subscribed_for_quotes():
    """구독하지 않으면 시세가 오지 않아 익절/손절 판정 자체가 돌지 않는다."""
    runtime, _ = make_runtime(carried_over())
    runtime.engine.start()

    adopted = adopt_carried_over_positions(runtime)

    assert adopted == ["005930"]
    assert runtime.ws_client.subscribed == ["005930"]


def test_nothing_to_adopt_when_the_account_is_flat():
    runtime, _ = make_runtime({})
    runtime.engine.start()

    assert adopt_carried_over_positions(runtime) == []
    assert runtime.ws_client.subscribed == []


def test_adopted_position_is_watched_for_stop_loss():
    """편입 후 첫 시세에 손절선을 넘겼다면 청산되어야 한다."""
    runtime, orders = make_runtime(carried_over(), exit_reason=ExitReason.STOP_LOSS)
    runtime.engine.start()
    adopt_carried_over_positions(runtime)

    runtime.engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert [o.side for o in orders] == [OrderSide.SELL]
    assert orders[0].quantity == 10


def test_adopted_position_is_included_in_the_forced_close():
    """당일 매수분이 아니어도 15:20 강제청산 대상이어야 한다."""
    runtime, orders = make_runtime(carried_over())
    runtime.engine.start()
    adopt_carried_over_positions(runtime)

    runtime.engine.force_close_all_positions(reason="day_end")

    assert [o.ticker for o in orders] == ["005930"]
    assert runtime.engine.open_tickers == []


def test_adoption_still_subscribes_while_disconnected():
    """미연결 상태여도 목록에 담아두면 재접속 시 connect가 구독을 복구한다."""
    runtime, _ = make_runtime(carried_over(), connected=False)
    runtime.engine.start()

    adopt_carried_over_positions(runtime)

    assert runtime.ws_client.subscribed == ["005930"]
