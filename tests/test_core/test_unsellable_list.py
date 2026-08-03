"""매도 불가 목록 — 보유 목록에서 빠진 종목이 UI에서도 사라지면 계좌에 남은 걸 알 수 없다."""
from types import SimpleNamespace

from src.api.account import Position
from src.core.engine import TradingEngine
from src.core.events import OrderResult, OrderStatus


def make_engine(positions, sell_status=OrderStatus.FILLED, error_message="거부"):
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
                error_message=None if sell_status == OrderStatus.FILLED else error_message,
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
        notifier=None,
    )


def held(ticker, quantity=10, sellable_quantity=None):
    return Position(
        ticker=ticker,
        name="테스트",
        quantity=quantity,
        avg_price=70000.0,
        current_price=71000.0,
        sellable_quantity=sellable_quantity,
    )


def test_excluded_position_is_listed_with_reason():
    """상장폐지·거래정지 추정으로 제외된 종목은 사유와 함께 남아야 한다."""
    engine = make_engine({"118970": held("118970")})
    engine.start()

    engine._exclude_untradable("118970", "(118970)테스트", "종목 정보가 조회되지 않습니다")

    listed = engine.unsellable_snapshot()
    assert len(listed) == 1
    assert listed[0].ticker == "118970"
    assert listed[0].reason == "종목 정보가 조회되지 않습니다"
    assert listed[0].excluded is True
    # 보유 목록에서는 빠지므로 보유 종목 표에는 나타나지 않는다
    assert engine.open_tickers == []
    assert engine.position_snapshot() == []


def test_rejected_sell_is_listed_with_error_message():
    engine = make_engine(
        {"005930": held("005930")},
        sell_status=OrderStatus.REJECTED,
        error_message="CB 발동중입니다. 취소주문만 가능합니다.",
    )
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    listed = engine.unsellable_snapshot()
    assert len(listed) == 1
    assert "CB 발동중" in listed[0].reason
    assert listed[0].excluded is False  # 재시도 여지가 있어 보유 목록에는 남는다
    assert engine.open_tickers == ["005930"]


def test_zero_sellable_quantity_is_listed():
    engine = make_engine({"005930": held("005930", sellable_quantity=0)})
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    listed = engine.unsellable_snapshot()
    assert len(listed) == 1
    assert "매도가능수량" in listed[0].reason


def test_successful_sell_clears_the_entry():
    """거부된 뒤 다시 팔렸으면 목록에서 빠져야 한다."""
    positions = {"005930": held("005930")}
    engine = make_engine(positions, sell_status=OrderStatus.REJECTED)
    engine.start()
    engine.force_close_all_positions(reason="day_end")
    assert len(engine.unsellable_snapshot()) == 1

    # 다음 시도에서는 정상 체결
    engine.order_client.send_order = lambda r: OrderResult(
        order_id="2",
        ticker=r.ticker,
        side=r.side,
        status=OrderStatus.FILLED,
        quantity=r.quantity,
        filled_quantity=r.quantity,
        filled_price=71000.0,
    )
    engine.force_close_all_positions(reason="manual")

    assert engine.unsellable_snapshot() == []


def test_new_day_clears_the_list():
    """'오늘' 매도하지 못한 목록이므로 거래일이 바뀌면 비운다."""
    engine = make_engine({"118970": held("118970")})
    engine.start()
    engine._exclude_untradable("118970", "(118970)테스트", "종목 정보가 조회되지 않습니다")

    engine.reset_for_new_day()

    assert engine.unsellable_snapshot() == []
