from datetime import datetime

import pytest

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from src.risk.manager import RiskManager, exit_trigger_price, net_return, portfolio_net_return


def make_manager(
    take_profit_ratio=0.005,
    stop_loss_ratio=0.02,
    initial_asset=10_000_000,
    max_total_exposure_ratio=0.7,
    commission_rate=0.0,
    tax_rate=0.0,
    slippage_rate=0.0,
    take_profit_enabled=True,
    stop_loss_enabled=True,
    simple_take_profit_enabled=False,
):
    """대부분의 테스트가 퍼센트 익절선을 검증하므로 단순익절만 기본값을 뒤집어 둔다.

    실제 기본값은 '적용'(True)이다 — `test_flags_default_to_enabled`가 그쪽을 지킨다.
    """
    manager = RiskManager(
        take_profit_ratio=take_profit_ratio,
        stop_loss_ratio=stop_loss_ratio,
        max_total_exposure_ratio=max_total_exposure_ratio,
        commission_rate=commission_rate,
        tax_rate=tax_rate,
        slippage_rate=slippage_rate,
        take_profit_enabled=take_profit_enabled,
        stop_loss_enabled=stop_loss_enabled,
        simple_take_profit_enabled=simple_take_profit_enabled,
    )
    manager.initialize(BalanceSnapshot(cash=initial_asset, positions={}))
    return manager


def held(ticker, quantity, avg_price, current_price):
    return Position(
        ticker=ticker, quantity=quantity, avg_price=avg_price, current_price=current_price
    )


def test_portfolio_exit_triggers_take_profit_at_threshold():
    """기본 익절선은 순손익 +0.5% — 비용이 0인 이 케이스에서는 가격 +0.5%가 곧 그 지점이다.

    정확히 경계값(1,005원)을 쓰지 않는 것은 부동소수 오차 때문이다 — 1005/1000-1이
    0.004999999999999893으로 나와 경계에서는 판정이 갈린다. 호가 단위가 1원 이상이라
    실전에서는 다음 틱에 잡히므로 로직을 손대지 않고 테스트만 경계 위에서 확인한다.
    """
    manager = make_manager(take_profit_ratio=0.005)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1006.0)]) == ExitReason.TAKE_PROFIT


def test_portfolio_exit_triggers_stop_loss_at_threshold():
    manager = make_manager(stop_loss_ratio=0.02)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) == ExitReason.STOP_LOSS


def test_portfolio_exit_returns_none_within_band():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1002.0)]) is None


def test_portfolio_exit_take_profit_reflects_costs():
    """수수료·세금·슬리피지가 있으면 가격 +0.5%만으로는 순손익 +0.5%에 못 미친다."""
    manager = make_manager(
        take_profit_ratio=0.005, commission_rate=0.00015, tax_rate=0.0018, slippage_rate=0.001
    )
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1005.0)]) is None

    trigger_price = exit_trigger_price(1000.0, 0.005, 0.00015, 0.0018, 0.001)
    assert trigger_price > 1005.0  # 비용만큼 익절가가 위로 밀린다
    at_trigger = [held("005930", 10, 1000.0, trigger_price + 1)]
    assert manager.check_portfolio_exit(at_trigger) == ExitReason.TAKE_PROFIT


def test_portfolio_exit_stop_loss_triggers_earlier_with_costs():
    """비용이 있으면 원가 대비 -2%보다 얕은 하락(-1.8%)에서 이미 순손실 -2%에 도달한다."""
    manager = make_manager(
        stop_loss_ratio=0.02, commission_rate=0.00015, tax_rate=0.0018, slippage_rate=0.001
    )
    shallower_drop = [held("005930", 10, 1000.0, 982.0)]
    assert manager.check_portfolio_exit(shallower_drop) == ExitReason.STOP_LOSS

    trigger_price = exit_trigger_price(1000.0, -0.02, 0.00015, 0.0018, 0.001)
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, trigger_price)]) == ExitReason.STOP_LOSS


def test_portfolio_exit_returns_none_without_anything_to_measure():
    """보유가 없거나 평단·수량이 0이면 판정하지 않는다."""
    manager = make_manager()

    assert manager.check_portfolio_exit([]) is None
    assert manager.check_portfolio_exit([held("005930", 0, 0.0, 1000.0)]) is None


def test_single_position_matches_the_per_stock_formula():
    """종목이 하나면 종전의 종목별 순손익률과 같은 값이어야 한다 (PRD 5.5-B)."""
    rates = (0.00015, 0.0018, 0.001)
    position = held("005930", 7, 1000.0, 1012.0)

    assert portfolio_net_return([position], *rates) == pytest.approx(
        net_return(1012.0, 1000.0, *rates)
    )


def test_profit_and_loss_offset_each_other():
    """한 종목이 손절선을 넘겨도 다른 종목이 상쇄하면 매도하지 않는다 (합산 판정의 대가)."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    positions = [
        held("005930", 100, 1000.0, 1010.0),  # +1%
        held("000660", 100, 1000.0, 970.0),   # -3% — 종목별이었다면 손절
    ]

    assert manager.check_portfolio_exit(positions) is None  # 합산 -1%
    assert manager.portfolio_return(positions) == pytest.approx(-0.01)


def test_weighting_follows_invested_amount_not_stock_count():
    """비중은 투입금액을 따른다 — 종목 개수로 나누는 단순평균이면 정반대 결과가 나온다."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    positions = [
        held("005930", 1000, 1000.0, 1010.0),  # 매입 100만원, +1%  → +1만원
        held("000660", 10, 1000.0, 700.0),     # 매입 1만원,  -30% → -3천원
    ]

    # 단순평균이면 (+1% -30%)/2 = -14.5%로 손절이 나가야 하지만, 실제 손익은 +7,000원이다
    assert manager.portfolio_return(positions) == pytest.approx(7_000 / 1_010_000)
    assert manager.check_portfolio_exit(positions) == ExitReason.TAKE_PROFIT


def test_positions_without_a_price_are_excluded():
    """현재가 0(조회 실패·장 전)을 그대로 넣으면 -100%로 잡혀 합산이 즉시 손절선을 넘는다."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    positions = [
        held("005930", 10, 1000.0, 1006.0),
        held("000660", 10, 1000.0, 0.0),  # 현재가를 못 읽은 종목
    ]

    assert manager.check_portfolio_exit(positions) == ExitReason.TAKE_PROFIT


def test_take_profit_can_be_disabled_without_affecting_stop_loss():
    """익절 적용을 끄면 익절선에 닿아도 팔지 않는다 — 손절은 그대로 동작한다."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02, take_profit_enabled=False)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1006.0)]) is None
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) == ExitReason.STOP_LOSS


def test_stop_loss_can_be_disabled_without_affecting_take_profit():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02, stop_loss_enabled=False)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) is None
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1006.0)]) == ExitReason.TAKE_PROFIT


def test_both_disabled_leaves_only_the_forced_close():
    """둘 다 끄면 실시간 청산이 사라진다 — 15:15 강제청산만 남는다 (PRD 5.5-B)."""
    manager = make_manager(take_profit_enabled=False, stop_loss_enabled=False)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1006.0)]) is None
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) is None


def test_disabled_flags_do_not_stop_the_return_calculation():
    """판정만 멈추고 합산 순손익률은 계속 계산한다 — UI 표시에 그대로 쓰인다."""
    manager = make_manager(take_profit_enabled=False, stop_loss_enabled=False)

    assert manager.portfolio_return([held("005930", 10, 1000.0, 1010.0)]) == pytest.approx(0.01)


def test_simple_take_profit_picks_each_winner_on_its_own():
    """판정은 합산이 아니라 종목별이다 — 이익 난 종목만 골라 돌려준다 (확정 2026-08-12)."""
    manager = make_manager(simple_take_profit_enabled=True)
    positions = [
        held("005930", 100, 1000.0, 1001.0),  # +0.1%
        held("000660", 100, 1000.0, 970.0),   # -3%
    ]

    # 합산은 -1.45%로 마이너스지만, 그것과 무관하게 이익 난 종목은 대상이 된다
    assert manager.portfolio_return(positions) < 0
    assert [p.ticker for p in manager.check_simple_take_profits(positions)] == ["005930"]


def test_simple_take_profit_holds_at_break_even():
    """0을 '넘어야' 판다 — 본전(0)에서는 팔지 않는다. 비용만 내고 끝나는 매매를 막는다."""
    manager = make_manager(simple_take_profit_enabled=True)

    assert manager.check_simple_take_profits([held("005930", 10, 1000.0, 1000.0)]) == []


def test_simple_take_profit_still_measures_net_return_not_price():
    """'단순'은 기준선이 0이라는 뜻이지, 비용을 무시한다는 뜻이 아니다."""
    manager = make_manager(
        simple_take_profit_enabled=True,
        commission_rate=0.00015,
        tax_rate=0.0018,
        slippage_rate=0.001,
    )

    # 가격은 +0.2%로 올랐지만 왕복 비용을 빼면 아직 손실이다
    assert manager.check_simple_take_profits([held("005930", 10, 1000.0, 1002.0)]) == []

    break_even = exit_trigger_price(1000.0, 0.0, 0.00015, 0.0018, 0.001)
    assert break_even > 1002.0
    at_profit = [held("005930", 10, 1000.0, break_even + 1)]
    assert [p.ticker for p in manager.check_simple_take_profits(at_profit)] == ["005930"]


def test_simple_take_profit_returns_nothing_when_disabled():
    manager = make_manager(simple_take_profit_enabled=False)

    assert manager.check_simple_take_profits([held("005930", 10, 1000.0, 1050.0)]) == []


def test_simple_take_profit_skips_positions_without_a_price():
    """현재가 0(조회 실패·장 전)인 종목은 판정하지 않는다."""
    manager = make_manager(simple_take_profit_enabled=True)

    assert manager.check_simple_take_profits([held("000660", 10, 1000.0, 0.0)]) == []


def test_simple_take_profit_does_not_reach_the_portfolio_judgement():
    """단순익절은 합산 판정에 끼지 않는다 — 손절과 퍼센트 익절만 거기서 본다."""
    manager = make_manager(
        take_profit_ratio=0.005, stop_loss_ratio=0.02, simple_take_profit_enabled=True
    )

    # 합산 +0.1%는 퍼센트 익절선에 못 미치므로 합산 판정은 아무것도 내지 않는다
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1001.0)]) is None
    # 손절은 단순익절과 무관하게 그대로 동작한다
    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) == ExitReason.STOP_LOSS


def test_flags_default_to_stop_loss_and_percent_take_profit():
    """기본값은 **손절 + 합산 퍼센트 익절**이다 (확정 2026-08-18).

    익절 기본값은 세 번 바뀌었다: 단순익절(도입) → 둘 다 해제(2026-08-12) →
    단순익절(2026-08-13) → 퍼센트 익절(2026-08-18). 두 익절 방식은 배타적이라
    한쪽이 켜지면 다른 쪽은 꺼진다. 손절은 계좌를 지키는 쪽이라 계속 기본 적용이다.
    """
    manager = RiskManager()

    assert manager.stop_loss_enabled is True
    assert manager.take_profit_enabled is True
    assert manager.simple_take_profit_enabled is False


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
