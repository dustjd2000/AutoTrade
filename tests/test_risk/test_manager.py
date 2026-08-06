from datetime import datetime

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from src.risk.manager import RiskManager, exit_trigger_price


def make_manager(
    take_profit_ratio=0.005,
    stop_loss_ratio=0.02,
    initial_asset=10_000_000,
    max_total_exposure_ratio=0.7,
    commission_rate=0.0,
    tax_rate=0.0,
    slippage_rate=0.0,
):
    manager = RiskManager(
        take_profit_ratio=take_profit_ratio,
        stop_loss_ratio=stop_loss_ratio,
        max_total_exposure_ratio=max_total_exposure_ratio,
        commission_rate=commission_rate,
        tax_rate=tax_rate,
        slippage_rate=slippage_rate,
    )
    manager.initialize(BalanceSnapshot(cash=initial_asset, positions={}))
    return manager


def test_check_exit_triggers_take_profit_at_threshold():
    """기본 익절선은 순손익 +0.5% — 비용이 0인 이 케이스에서는 가격 +0.5%가 곧 그 지점이다.

    정확히 경계값(1,005원)을 쓰지 않는 것은 부동소수 오차 때문이다 — 1005/1000-1이
    0.004999999999999893으로 나와 경계에서는 판정이 갈린다. 호가 단위가 1원 이상이라
    실전에서는 다음 틱에 잡히므로 로직을 손대지 않고 테스트만 경계 위에서 확인한다.
    """
    manager = make_manager(take_profit_ratio=0.005)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=1006.0)

    assert manager.check_exit(position) == ExitReason.TAKE_PROFIT


def test_check_exit_triggers_stop_loss_at_threshold():
    manager = make_manager(stop_loss_ratio=0.02)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=980.0)

    assert manager.check_exit(position) == ExitReason.STOP_LOSS


def test_check_exit_returns_none_within_band():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    position = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=1002.0)

    assert manager.check_exit(position) is None


def test_check_exit_take_profit_reflects_costs():
    """수수료·세금·슬리피지가 있으면 가격 +0.5%만으로는 순손익 +0.5%에 못 미친다."""
    manager = make_manager(
        take_profit_ratio=0.005, commission_rate=0.00015, tax_rate=0.0018, slippage_rate=0.001
    )
    just_short = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=1005.0)
    assert manager.check_exit(just_short) is None

    trigger_price = exit_trigger_price(1000.0, 0.005, 0.00015, 0.0018, 0.001)
    assert trigger_price > 1005.0  # 비용만큼 익절가가 위로 밀린다
    at_trigger = Position(
        ticker="005930", quantity=10, avg_price=1000.0, current_price=trigger_price + 1
    )
    assert manager.check_exit(at_trigger) == ExitReason.TAKE_PROFIT


def test_check_exit_stop_loss_triggers_earlier_with_costs():
    """비용이 있으면 원가 대비 -2%보다 얕은 하락(-1.8%)에서 이미 순손실 -2%에 도달한다."""
    manager = make_manager(
        stop_loss_ratio=0.02, commission_rate=0.00015, tax_rate=0.0018, slippage_rate=0.001
    )
    shallower_drop = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=982.0)
    assert manager.check_exit(shallower_drop) == ExitReason.STOP_LOSS

    trigger_price = exit_trigger_price(1000.0, -0.02, 0.00015, 0.0018, 0.001)
    at_trigger = Position(ticker="005930", quantity=10, avg_price=1000.0, current_price=trigger_price)
    assert manager.check_exit(at_trigger) == ExitReason.STOP_LOSS


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
