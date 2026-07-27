from datetime import datetime

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from src.risk.manager import RiskManager


def make_manager(
    take_profit_ratio=0.02,
    stop_loss_ratio=0.02,
    initial_asset=10_000_000,
    max_total_exposure_ratio=0.7,
):
    manager = RiskManager(
        take_profit_ratio=take_profit_ratio,
        stop_loss_ratio=stop_loss_ratio,
        max_total_exposure_ratio=max_total_exposure_ratio,
    )
    manager.initialize(BalanceSnapshot(cash=initial_asset, positions={}))
    return manager


def test_check_exit_triggers_take_profit_at_threshold():
    manager = make_manager(take_profit_ratio=0.02)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=1020.0)

    assert manager.check_exit(position) == ExitReason.TAKE_PROFIT


def test_check_exit_triggers_stop_loss_at_threshold():
    manager = make_manager(stop_loss_ratio=0.02)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=980.0)

    assert manager.check_exit(position) == ExitReason.STOP_LOSS


def test_check_exit_returns_none_within_band():
    manager = make_manager(take_profit_ratio=0.02, stop_loss_ratio=0.02)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=1005.0)

    assert manager.check_exit(position) is None


def test_check_exit_returns_none_for_empty_position():
    manager = make_manager()
    position = Position(ticker="005930", quantity=0, avg_price=0.0, current_price=1000.0)

    assert manager.check_exit(position) is None


def test_record_order_accumulates_realized_loss_only_on_loss():
    manager = make_manager()

    loss_result = OrderResult(
        order_id="1",
        ticker="005930",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity=10,
        filled_quantity=10,
        filled_price=980.0,
        timestamp=datetime.now(),
    )
    manager.record_order(loss_result, avg_price=1000.0)

    assert manager._daily_realized_loss == 200.0

    profit_result = OrderResult(
        order_id="2",
        ticker="005930",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity=10,
        filled_quantity=10,
        filled_price=1050.0,
        timestamp=datetime.now(),
    )
    manager.record_order(profit_result, avg_price=1000.0)

    # 이익 실현은 손실 누적에 영향을 주지 않는다
    assert manager._daily_realized_loss == 200.0


def test_get_holding_quantity():
    manager = make_manager()
    positions = {"005930": Position(ticker="005930", quantity=5, avg_price=1000.0)}

    assert manager.get_holding_quantity("005930", positions) == 5
    assert manager.get_holding_quantity("000660", positions) == 0


def test_approve_rejects_buy_exceeding_total_exposure_ratio():
    # 초기자산 1000만원, 노출한도 70% = 700만원. 기존 보유 600만원 + 신규 200만원 = 800만원 > 700만원
    manager = make_manager(initial_asset=10_000_000, max_total_exposure_ratio=0.7)
    positions = {
        "005930": Position(ticker="005930", quantity=60, avg_price=100_000.0, current_price=100_000.0)
    }
    request = OrderRequest(
        ticker="000660", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=20
    )

    assert manager.approve(request, positions, reference_price=100_000.0) is False


def test_approve_allows_buy_within_total_exposure_ratio():
    # 기존 보유 300만원 + 신규 200만원 = 500만원 <= 700만원 한도
    manager = make_manager(initial_asset=10_000_000, max_total_exposure_ratio=0.7)
    positions = {
        "005930": Position(ticker="005930", quantity=30, avg_price=100_000.0, current_price=100_000.0)
    }
    request = OrderRequest(
        ticker="000660", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=20
    )

    assert manager.approve(request, positions, reference_price=100_000.0) is True
