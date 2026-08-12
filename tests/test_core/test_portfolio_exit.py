"""합산 익절/손절 — 판정은 보유 종목 전체로 하고, 걸리면 전량 매도한다 (PRD 5.5-B).

엔진 테스트 대부분은 RiskManager를 스텁으로 대체하지만, 여기서는 실물을 그대로 써서
'합산으로 판정한 결과가 실제 주문으로 이어지는가'를 끝까지 확인한다.
"""
from types import SimpleNamespace

from src.api.account import BalanceSnapshot, Position
from src.core.engine import TradingEngine
from src.core.events import MarketData, OrderResult, OrderSide, OrderStatus, Signal
from src.risk.manager import RiskManager


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
            )
            for t, p in self.positions.items()
        }

    def get_cash(self):
        return 1_000_000.0

    def get_balance_snapshot(self):
        return BalanceSnapshot(cash=self.get_cash(), positions=self.get_positions())


def make_engine(positions, simple_take_profit_enabled=False):
    """비용을 0으로 둔 실물 RiskManager를 붙인 엔진 — 가격 변동률이 곧 순손익률이 된다."""
    orders = []
    alerts = []

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
        account=FakeAccount(positions),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: Signal.HOLD),
        risk_manager=RiskManager(
            take_profit_ratio=0.005,
            stop_loss_ratio=0.02,
            commission_rate=0.0,
            tax_rate=0.0,
            slippage_rate=0.0,
            # 기본은 끈다 — 대부분의 테스트가 퍼센트 익절선(+0.5%)에서의 전량 매도를 보는데,
            # 단순익절이 켜져 있으면 이익 난 종목이 먼저 개별 매도되어 검증 대상이 달라진다
            simple_take_profit_enabled=simple_take_profit_enabled,
        ),
        notifier=SimpleNamespace(send=alerts.append),
    )
    engine.start()
    return engine, orders, alerts


def two_holdings(price_a=1010.0, price_b=970.0):
    """각 100주씩 평단 1,000원 — 두 종목의 매입금액이 같아 합산이 단순평균과 일치한다."""
    return {
        "005930": Position(
            ticker="005930", quantity=100, avg_price=1000.0, current_price=price_a, name="삼성전자"
        ),
        "000660": Position(
            ticker="000660", quantity=100, avg_price=1000.0, current_price=price_b, name="SK하이닉스"
        ),
    }


def test_losing_stock_is_not_sold_while_the_rest_offsets_it():
    """한 종목이 손절선(-2%)을 넘겨도 합산이 밴드 안이면 매도하지 않는다 — 합산 판정의 대가다."""
    engine, orders, _ = make_engine(two_holdings())  # +1% / -3% → 합산 -1%

    engine.on_market_data(MarketData(ticker="000660", price=970.0, volume=1))

    assert orders == [], "종목별로 손절이 나갔다 — 판정은 합산이어야 한다"


def test_all_holdings_are_sold_when_the_total_hits_the_stop_loss():
    """합산이 손절선을 넘으면 손실 종목만이 아니라 보유 종목 전량을 판다."""
    engine, orders, _ = make_engine(two_holdings())

    # 000660이 -6%로 밀리면 합산은 (+1% -6%)/2 = -2.5% → 손절선 통과
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert [o.side for o in orders] == [OrderSide.SELL, OrderSide.SELL]
    assert {o.ticker for o in orders} == {"005930", "000660"}
    assert all(o.quantity == 100 for o in orders)
    assert engine.open_tickers == [], "전량 매도 후에도 감시 목록이 남았다"


def test_all_holdings_are_sold_when_the_total_hits_the_take_profit():
    """이익 쪽도 마찬가지다 — 합산이 익절선에 닿으면 손실 종목까지 함께 정리한다."""
    engine, orders, _ = make_engine(two_holdings(price_a=1030.0, price_b=995.0))

    # +3% / -0.5% → 합산 +1.25%
    engine.on_market_data(MarketData(ticker="000660", price=995.0, volume=1))

    assert {o.ticker for o in orders} == {"005930", "000660"}


def test_exit_alert_is_sent_once_for_the_whole_list():
    """종목마다 보내면 보유 3종목에 메일 3통이 나간다 — 합산 손익과 함께 한 통으로 묶는다."""
    engine, _, alerts = make_engine(two_holdings())

    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert len(alerts) == 1, f"청산 알림이 {len(alerts)}통 나갔다"
    [message] = alerts
    assert "-2.50%" in message          # 합산 순손익
    assert "삼성전자" in message and "SK하이닉스" in message


def test_sold_positions_leave_the_calculation():
    """체결이 잔고에 반영되기 전 다음 틱이 와도, 이미 판 물량으로 다시 판정하면 안 된다."""
    engine, orders, _ = make_engine(two_holdings())
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))
    assert len(orders) == 2

    # 잔고는 아직 두 종목을 보유 중이라고 답한다
    engine._invalidate_positions()
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert len(orders) == 2, "이미 매도한 종목에 청산 주문이 또 나갔다"


def test_snapshot_reports_the_value_the_engine_judges_on():
    """UI 표시값 — 평가손익률이 아니라 판정에 쓰는 합산 순손익률이어야 한다."""
    engine, _, _ = make_engine(two_holdings())  # +1% / -3%

    assert engine.portfolio_return_snapshot() == -0.01


def test_snapshot_drops_sold_positions():
    """전량 매도한 뒤에는 판정 대상이 없다 — 잔고 반영 전이라도 값이 남으면 안 된다."""
    engine, _, _ = make_engine(two_holdings())
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert engine.portfolio_return_snapshot() is None


def test_position_without_a_price_does_not_trigger_a_selloff():
    """현재가 0(조회 실패·장 전)을 손실로 읽으면 아무 이유 없이 전량 매도가 나간다."""
    positions = two_holdings(price_a=1002.0)
    positions["000660"].current_price = 0.0
    engine, orders, _ = make_engine(positions)

    engine.on_market_data(MarketData(ticker="005930", price=1002.0, volume=1))

    assert orders == [], "현재가를 못 읽은 종목이 합산에 -100%로 들어갔다"


# ── 단순익절 (종목별, 확정 2026-08-12) ──────────────────────────
def test_simple_take_profit_sells_only_the_winning_stock():
    """이익 난 종목만 팔고 나머지는 그대로 둔다 — 합산 판정과 달리 전량이 아니다."""
    engine, orders, _ = make_engine(two_holdings(), simple_take_profit_enabled=True)  # +1% / -3%

    engine.on_market_data(MarketData(ticker="005930", price=1010.0, volume=1))

    assert [(o.ticker, o.side) for o in orders] == [("005930", OrderSide.SELL)]
    assert engine.open_tickers == ["000660"], "손실 종목까지 함께 팔렸다"


def test_simple_take_profit_alert_names_the_sold_stock():
    engine, _, alerts = make_engine(two_holdings(), simple_take_profit_enabled=True)

    engine.on_market_data(MarketData(ticker="005930", price=1010.0, volume=1))

    [message] = alerts
    assert "단순익절" in message
    assert "삼성전자" in message and "SK하이닉스" not in message


def test_stop_loss_beats_simple_take_profit():
    """계좌 전체가 손절선 아래면 이익 난 종목 하나를 파는 것보다 전량 청산이 우선이다."""
    engine, orders, _ = make_engine(
        two_holdings(price_a=1010.0, price_b=940.0), simple_take_profit_enabled=True
    )  # +1% / -6% → 합산 -2.5%

    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert {o.ticker for o in orders} == {"005930", "000660"}


def test_remaining_loser_can_hit_the_stop_loss_after_the_winner_leaves():
    """종목별 익절이 만드는 순서 의존성 — 이익 종목이 빠지면 남은 손실이 상쇄를 잃는다.

    합산만 볼 때는 -1%로 아무것도 팔리지 않던 조합이, 단순익절로 +1% 종목이 먼저 나가면
    남은 -3% 하나가 손절선(-2%)을 넘겨 결국 둘 다 정리된다 (PRD 5.5-B, 사용자가 택한 동작).
    """
    engine, orders, _ = make_engine(two_holdings(), simple_take_profit_enabled=True)

    engine.on_market_data(MarketData(ticker="005930", price=1010.0, volume=1))
    assert [o.ticker for o in orders] == ["005930"]

    engine._invalidate_positions()
    engine.on_market_data(MarketData(ticker="000660", price=970.0, volume=1))

    assert [o.ticker for o in orders] == ["005930", "000660"]
