"""잔고 조회 캐시 — 틱마다 REST를 때리면 429가 나고 익절/손절 판정이 통째로 멈춘다."""
from types import SimpleNamespace

import pytest

from src.api.account import Position
from src.core.engine import TradingEngine
from src.core.events import ExitReason, MarketData, OrderResult, OrderSide, OrderStatus


class CountingAccount:
    """get_positions 호출 횟수를 세고, 필요하면 실패를 흉내 낸다."""

    def __init__(self, positions, fail_after=None):
        self.positions = positions
        self.calls = 0
        self.fail_after = fail_after

    def get_positions(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("429 Client Error: null for url: .../api/dostk/acnt")
        # 엔진이 캐시본을 변형(current_price)하므로 매번 새 객체를 준다
        return {
            t: Position(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
                name=p.name,
            )
            for t, p in self.positions.items()
        }

    def get_balance_snapshot(self):
        return SimpleNamespace(total_asset=1_000_000, positions=self.get_positions())


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


def held(ticker="005930", quantity=10, avg_price=70000.0):
    return {ticker: Position(ticker=ticker, quantity=quantity, avg_price=avg_price)}


def test_repeated_ticks_do_not_refetch_balance():
    """연속 틱이 매번 REST를 부르면 유량 제한에 걸린다 — TTL 안에서는 캐시를 쓴다."""
    account = CountingAccount(held())
    engine, _ = make_engine(account)
    engine.start()
    calls_after_start = account.calls

    for _ in range(20):
        engine.on_market_data(MarketData(ticker="005930", price=70500.0, volume=1))

    assert account.calls == calls_after_start, "틱마다 잔고를 다시 읽고 있다"


def test_stale_snapshot_keeps_exit_watch_alive_when_refresh_fails():
    """429로 갱신이 실패해도 직전 스냅샷으로 익절/손절 판정을 이어가야 한다."""
    account = CountingAccount(held(), fail_after=1)  # start()의 스냅샷만 성공
    engine, orders = make_engine(account, exit_reason=ExitReason.STOP_LOSS)
    engine.start()
    engine._invalidate_positions()  # 갱신을 유도해 실패를 발생시킨다

    engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert [o.side for o in orders] == [OrderSide.SELL], "갱신 실패로 손절이 건너뛰어졌다"


def test_exit_order_is_not_sent_twice_for_the_same_position():
    """체결이 잔고에 반영되기 전 다음 틱이 와도 같은 포지션을 두 번 팔면 안 된다."""
    account = CountingAccount(held())  # 잔고는 계속 보유 중이라고 답한다
    engine, orders = make_engine(account, exit_reason=ExitReason.STOP_LOSS)
    engine.start()

    for _ in range(5):
        engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert len(orders) == 1, f"청산 주문이 {len(orders)}번 나갔다"


def test_exit_guard_clears_once_balance_confirms_the_sale():
    """잔고에서 종목이 사라지면 중복 매도 가드를 풀어 다음 매수를 막지 않는다."""
    account = CountingAccount(held())
    engine, _ = make_engine(account, exit_reason=ExitReason.STOP_LOSS)
    engine.start()
    engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))
    assert engine._exiting == {"005930"}

    account.positions = {}  # 매도 체결이 잔고에 반영됨
    engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert engine._exiting == set()


def test_force_close_ignores_the_cache():
    """무엇을 파는지가 곧 결과이므로 강제청산은 최신 잔고를 읽어야 한다."""
    account = CountingAccount(held())
    engine, orders = make_engine(account)
    engine.start()
    engine.on_market_data(MarketData(ticker="005930", price=70500.0, volume=1))
    calls_before = account.calls

    engine.force_close_all_positions(reason="day_end")

    assert account.calls == calls_before + 1
    assert [o.side for o in orders] == [OrderSide.SELL]


def test_snapshot_reflects_the_latest_tick_price():
    """UI 표는 잔고 조회 없이 캐시만 읽으므로, 틱 가격이 반영되어야 한다."""
    account = CountingAccount(held(avg_price=70000.0))
    engine, _ = make_engine(account)
    engine.start()

    engine.on_market_data(MarketData(ticker="005930", price=71400.0, volume=1))
    [view] = engine.position_snapshot()

    assert view.current_price == 71400.0
    assert view.pnl == (71400.0 - 70000.0) * 10
    assert round(view.pnl_percent, 2) == 2.0


def test_snapshot_drops_sold_positions():
    """매도된 건은 목록에서 즉시 사라져야 한다 (잔고 반영 전이라도)."""
    account = CountingAccount(held())
    engine, _ = make_engine(account, exit_reason=ExitReason.STOP_LOSS)
    engine.start()
    assert len(engine.position_snapshot()) == 1

    engine.on_market_data(MarketData(ticker="005930", price=68000.0, volume=1))

    assert engine.position_snapshot() == []


def test_snapshot_excludes_zero_quantity_rows():
    account = CountingAccount(held(quantity=0))
    engine, _ = make_engine(account)
    engine.start()

    assert engine.position_snapshot() == []


def test_force_close_surfaces_balance_failure():
    """청산 대상 목록을 못 읽으면 조용히 넘어가선 안 된다."""
    account = CountingAccount(held(), fail_after=1)
    engine, _ = make_engine(account)
    engine.start()

    with pytest.raises(RuntimeError):
        engine.force_close_all_positions(reason="day_end")
