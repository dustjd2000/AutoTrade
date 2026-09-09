from datetime import datetime

import pytest

from src.api.account import BalanceSnapshot, Position
from src.core.events import ExitReason, OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from src.risk.manager import (
    RiskManager,
    exit_trigger_price,
    net_return,
    portfolio_net_pnl,
    portfolio_net_return,
    position_costs,
    position_net_pnl,
)


def make_manager(
    take_profit_ratio=0.005,
    stop_loss_ratio=0.02,
    initial_asset=10_000_000,
    max_total_exposure_ratio=0.7,
    commission_rate=0.0,
    tax_rate=0.0,
    slippage_rate=0.0,
    stop_loss_enabled=True,
):
    manager = RiskManager(
        take_profit_ratio=take_profit_ratio,
        stop_loss_ratio=stop_loss_ratio,
        max_total_exposure_ratio=max_total_exposure_ratio,
        commission_rate=commission_rate,
        tax_rate=tax_rate,
        slippage_rate=slippage_rate,
        stop_loss_enabled=stop_loss_enabled,
    )
    manager.initialize(BalanceSnapshot(cash=initial_asset, positions={}))
    return manager


def held(ticker, quantity, avg_price, current_price):
    return Position(
        ticker=ticker, quantity=quantity, avg_price=avg_price, current_price=current_price
    )


# 익절 자동 청산(check_portfolio_exit의 TAKE_PROFIT 분기)은 2026-09-09에 걷어냈다 (PRD 10절,
# 실매매 27건 대조 — 어떤 익절선도 "익절 없음"보다 낫지 않았다). 그 분기만 확인하던
# test_portfolio_exit_triggers_take_profit_at_threshold / test_portfolio_exit_take_profit_reflects_costs /
# test_take_profit_can_be_disabled_without_affecting_stop_loss /
# test_stop_loss_can_be_disabled_without_affecting_take_profit / test_both_disabled_leaves_only_the_forced_close는
# 지웠다 — 아래 test_portfolio_exit_no_longer_takes_profit / test_stop_loss_can_still_be_disabled가
# 새 동작을 대신 확인한다.
def test_portfolio_exit_triggers_stop_loss_at_threshold():
    manager = make_manager(stop_loss_ratio=0.02)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 980.0)]) == ExitReason.STOP_LOSS


def test_portfolio_exit_returns_none_within_band():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)

    assert manager.check_portfolio_exit([held("005930", 10, 1000.0, 1002.0)]) is None


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
    # 합산 +0.69%는 옛 익절선(0.5%)을 넘지만, 익절 자동 청산은 걷어냈으므로 팔지 않는다
    assert manager.check_portfolio_exit(positions) is None


def test_positions_without_a_price_are_excluded():
    """현재가 0(조회 실패·장 전)을 그대로 넣으면 -100%로 잡혀 합산이 왜곡된다 — 계산에서 뺀다."""
    manager = make_manager()
    positions = [
        held("005930", 10, 1000.0, 1006.0),
        held("000660", 10, 1000.0, 0.0),  # 현재가를 못 읽은 종목
    ]

    assert manager.portfolio_return(positions) == pytest.approx(0.006)


def test_disabled_flags_do_not_stop_the_return_calculation():
    """판정만 멈추고 합산 순손익률은 계속 계산한다 — UI 표시에 그대로 쓰인다."""
    manager = make_manager(stop_loss_enabled=False)

    assert manager.portfolio_return([held("005930", 10, 1000.0, 1010.0)]) == pytest.approx(0.01)


def test_portfolio_exit_no_longer_takes_profit():
    """익절은 자동 청산에서 빠졌다 — 이익이 아무리 커도 여기서는 팔지 않는다."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    profitable = [held("005930", 10, 1000.0, 1100.0)]
    assert manager.check_portfolio_exit(profitable) is None


def test_portfolio_exit_still_stops_loss():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    losing = [held("005930", 10, 1000.0, 900.0)]
    assert manager.check_portfolio_exit(losing) is ExitReason.STOP_LOSS


def test_stop_loss_can_still_be_disabled():
    manager = make_manager(stop_loss_ratio=0.02, stop_loss_enabled=False)
    losing = [held("005930", 10, 1000.0, 900.0)]
    assert manager.check_portfolio_exit(losing) is None


def test_take_profit_ratio_survives_as_a_reference_line():
    """자동 청산에는 안 쓰지만 AI에게 넘길 기준선이라 설정은 남는다."""
    manager = make_manager(take_profit_ratio=0.005)
    assert manager.take_profit_ratio == 0.005


def test_simple_take_profit_is_gone():
    """단순익절(종목별 0% 익절) 모드는 2026-09-09에 걷어냈다 (PRD 10절).

    이 자리에 있던 test_simple_take_profit_picks_each_winner_on_its_own /
    test_simple_take_profit_holds_at_break_even / test_simple_take_profit_still_measures_net_return_not_price /
    test_simple_take_profit_returns_nothing_when_disabled / test_simple_take_profit_skips_positions_without_a_price /
    test_simple_take_profit_does_not_reach_the_portfolio_judgement 여섯 개는 모두
    check_simple_take_profits를 직접 호출했다 — 메서드 자체가 없어졌으므로 지웠고, 이 테스트가
    "그 메서드도 그 플래그도 더는 없다"는 것만 확인한다.
    """
    manager = make_manager()
    assert not hasattr(manager, "check_simple_take_profits")
    assert not hasattr(manager, "simple_take_profit_enabled")


def test_stop_loss_defaults_to_enabled():
    """손절 기본값은 계속 적용이다 — 계좌를 지키는 마지막 안전장치라 기본을 유지한다.

    익절 적용 플래그(take_profit_enabled)와 단순익절(simple_take_profit_enabled)은
    2026-09-09에 걷어냈다 (PRD 10절) — 그 전까지 기본값이 네 번 바뀐 내력은 PRD 10절에 남아 있다.
    """
    manager = RiskManager()

    assert manager.stop_loss_enabled is True


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


# ── 표시용 순손익 (슬리피지 없음, 절사 규칙 재현) ──────────────
# 2026-09-01 실매매 3건 — (평단, 현재가, 수량, 실제 순손익)
# 실제 순손익 = 실현손익 - 실제 매수수수료 - 실제 매도수수료 - 실제 세금
REAL_FILLS = [
    ("047050", 55600.0, 56400.0, 15, 10068.0),   # 12000 - 120 - 120 - 1692
    ("012330", 453000.0, 447500.0, 1, -6514.0),  # -5500 - 60 - 60 - 894
    ("161390", 67100.0, 66900.0, 12, -4245.0),   # -2400 - 120 - 120 - 1605
]


def fill_position(ticker, avg, cur, qty):
    return Position(ticker=ticker, quantity=qty, avg_price=avg, current_price=cur)


@pytest.mark.parametrize("ticker,avg,cur,qty,actual", REAL_FILLS)
def test_net_pnl_matches_real_fills_within_one_won(ticker, avg, cur, qty, actual):
    """절사 규칙 폴백이 실제 체결 결과와 1원 이내로 맞아야 한다."""
    pnl = position_net_pnl(fill_position(ticker, avg, cur, qty), 0.00015, 0.002)
    assert abs(pnl - actual) <= 1.0, f"{ticker}: {pnl} vs {actual}"


def test_commission_is_truncated_to_ten_won():
    """키움은 매매수수료를 10원 단위로 절사한다 — 453,000원이면 67.95원이 아니라 60원."""
    position = fill_position("012330", 453000.0, 453000.0, 1)
    # 매수 60 + 매도 60 + 세금 ⌊453000*0.002⌋=906
    assert position_costs(position, 0.00015, 0.002) == 1026.0


def test_kiwoom_values_take_precedence_over_the_formula():
    """키움이 실제 비용을 주면 절사 계산 대신 그 값을 쓴다."""
    position = fill_position("047050", 55600.0, 56400.0, 15)
    position.buy_fee = 100.0
    position.sell_cost = 200.0
    assert position_costs(position, 0.00015, 0.002) == 300.0


def test_portfolio_net_pnl_sums_positions_and_divides_by_cost():
    """합산 비율의 분모는 매입금액 — portfolio_net_return과 같은 기준이라야 나란히 읽힌다."""
    positions = [fill_position(t, a, c, q) for t, a, c, q, _ in REAL_FILLS]

    amount, ratio = portfolio_net_pnl(positions, 0.00015, 0.002)

    assert amount == pytest.approx(-692.0, abs=1.0)
    cost = 55600 * 15 + 453000 * 1 + 67100 * 12
    assert ratio == pytest.approx(amount / cost)


def test_portfolio_net_pnl_skips_positions_without_a_price():
    """현재가 0을 그대로 넣으면 -100%로 잡힌다 — portfolio_net_return과 같은 방어다."""
    positions = [
        fill_position("047050", 55600.0, 56400.0, 15),
        fill_position("000000", 10000.0, 0.0, 10),
    ]

    amount, ratio = portfolio_net_pnl(positions, 0.00015, 0.002)

    assert amount == pytest.approx(10068.0, abs=1.0)
    assert ratio == pytest.approx(10068.0 / (55600 * 15), abs=1e-6)


def test_portfolio_net_pnl_returns_none_ratio_when_nothing_to_measure():
    assert portfolio_net_pnl([], 0.00015, 0.002) == (0.0, None)


def test_manager_exposes_net_pnl_with_its_own_rates():
    manager = make_manager(commission_rate=0.00015, tax_rate=0.002)
    position = fill_position("047050", 55600.0, 56400.0, 15)

    assert manager.position_net_pnl(position) == pytest.approx(10068.0, abs=1.0)
    assert manager.portfolio_net_pnl([position])[0] == pytest.approx(10068.0, abs=1.0)


def test_net_pnl_ignores_slippage():
    """표시용이라 슬리피지를 빼지 않는다 — 판정(portfolio_net_return)과 다른 점이다."""
    manager = make_manager(commission_rate=0.0, tax_rate=0.0, slippage_rate=0.5)
    position = fill_position("047050", 1000.0, 1100.0, 10)

    assert manager.position_net_pnl(position) == pytest.approx(1000.0)
