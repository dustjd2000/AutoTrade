"""청산 수량 — 미체결 매도가 걸려 있으면 보유수량 전량 주문은 거부된다."""
from types import SimpleNamespace

from src.api.account import BalanceSnapshot, Position
from src.core.engine import TradingEngine
from src.core.events import ExitReason, MarketData, OrderResult, OrderStatus


class FakeAccount:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        # 엔진이 캐시본을 변형(current_price)하므로 매번 새 객체를 준다
        return {
            t: Position(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
                name=p.name,
                sellable_quantity=p.sellable_quantity,
            )
            for t, p in self.positions.items()
        }

    def get_balance_snapshot(self):
        return BalanceSnapshot(cash=0.0, positions=self.get_positions())


def make_engine(account, exit_reason=None):
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
        account=account,
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_exit=lambda p: exit_reason,
        ),
    )
    return engine, orders


def held(sellable):
    return {
        "005930": Position(
            ticker="005930",
            quantity=10,
            avg_price=70000.0,
            current_price=69000.0,
            name="삼성전자",
            sellable_quantity=sellable,
        )
    }


def test_forced_close_orders_the_sellable_quantity():
    engine, orders = make_engine(FakeAccount(held(sellable=4)))
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    assert [o.quantity for o in orders] == [4]


def test_forced_close_falls_back_to_holding_quantity_when_unknown():
    """매도가능수량을 읽지 못했다고 매도를 포기하면 포지션이 조용히 이월된다."""
    engine, orders = make_engine(FakeAccount(held(sellable=None)))
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    assert [o.quantity for o in orders] == [10]


def test_forced_close_skips_when_nothing_is_sellable():
    engine, orders = make_engine(FakeAccount(held(sellable=0)))
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    assert orders == []
    assert engine.open_tickers == ["005930"], "팔지 못한 포지션은 감시 대상으로 남아야 한다"


def test_exit_orders_the_sellable_quantity():
    engine, orders = make_engine(FakeAccount(held(sellable=3)), exit_reason=ExitReason.STOP_LOSS)
    engine.start()

    engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert [o.quantity for o in orders] == [3]


def test_exit_warns_only_once_while_nothing_is_sellable(caplog):
    """틱마다 같은 경고가 쌓이면 실행 로그를 못 읽는다."""
    engine, orders = make_engine(FakeAccount(held(sellable=0)), exit_reason=ExitReason.STOP_LOSS)
    engine.start()

    with caplog.at_level("WARNING"):
        for _ in range(5):
            engine._invalidate_positions()
            engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert orders == []
    assert caplog.text.count("매도가능수량이 0입니다") == 1
