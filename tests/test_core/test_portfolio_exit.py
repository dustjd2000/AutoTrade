"""합산 손절 — 판정은 보유 종목 전체로 하고, 걸리면 전량 매도한다 (PRD 5.5-B).

엔진 테스트 대부분은 RiskManager를 스텁으로 대체하지만, 여기서는 실물을 그대로 써서
'합산으로 판정한 결과가 실제 주문으로 이어지는가'를 끝까지 확인한다.

익절 자동 청산과 단순익절(종목별 0% 익절)은 2026-09-09에 걷어냈다 (PRD 10절, 실매매 27건
대조 — 어떤 익절선도 "익절 없음"보다 낫지 않았다). 그 둘을 검증하던 테스트는 지우거나
새 동작("이제 팔지 않는다")을 확인하는 쪽으로 바꿨다 — 각 자리에 남긴 주석을 참고.
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


class RecordingStore:
    """record_fill에 넘어온 청산 사유만 모아 두는 스텁."""

    def __init__(self):
        self.reasons = []

    def record_fill(self, result, avg_price=None, exit_reason=None):
        self.reasons.append((result.ticker, exit_reason))


def make_engine(positions, trade_store=None):
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
            # take_profit_ratio는 자동 청산에 더는 쓰이지 않지만(2026-09-09, PRD 10절),
            # 옛 익절선 근방에서도 매도가 나가지 않는지 확인하는 테스트를 위해 그대로 둔다
            take_profit_ratio=0.005,
            stop_loss_ratio=0.02,
            commission_rate=0.0,
            tax_rate=0.0,
            slippage_rate=0.0,
        ),
        notifier=SimpleNamespace(send=alerts.append),
        trade_store=trade_store,
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


def test_take_profit_no_longer_triggers_a_selloff():
    """합산이 옛 익절선(+0.5%)에 닿아도 이제는 팔지 않는다 (2026-09-09, PRD 10절).

    익절 자동 청산을 걷어내기 전에는 이 시나리오(+3% / -0.5% → 합산 +1.25%)가 전량 매도로
    이어졌다 — test_all_holdings_are_sold_when_the_total_hits_the_take_profit이 그걸 확인하는
    테스트였다. 지금은 같은 가격으로 정반대(아무것도 팔리지 않음)를 확인한다.
    """
    engine, orders, _ = make_engine(two_holdings(price_a=1030.0, price_b=995.0))

    # +3% / -0.5% → 합산 +1.25% (예전 익절선 통과 지점)
    engine.on_market_data(MarketData(ticker="000660", price=995.0, volume=1))

    assert orders == []


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


# 단순익절(종목별, 확정 2026-08-12)은 2026-09-09에 걷어냈다 (PRD 10절) — check_simple_take_profits /
# _execute_simple_take_profit이 없어지면서 이 자리에 있던
# test_simple_take_profit_sells_only_the_winning_stock / test_simple_take_profit_alert_names_the_sold_stock /
# test_stop_loss_beats_simple_take_profit / test_remaining_loser_can_hit_the_stop_loss_after_the_winner_leaves
# 네 테스트를 지웠다 — 종목별로 골라 파는 경로 자체가 없어져 고쳐 쓸 대상이 남지 않았다.


# ── 청산 사유 기록 (확정 2026-08-12) ────────────────────────────
def test_stop_loss_is_recorded_with_its_reason():
    store = RecordingStore()
    engine, _, _ = make_engine(two_holdings(price_b=940.0), trade_store=store)

    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert sorted(store.reasons) == [("000660", "stop_loss"), ("005930", "stop_loss")]


# 단순익절 사유("simple_take_profit")를 percent take_profit과 구분해 기록하는지 보던
# test_simple_take_profit_is_recorded_apart_from_the_percent_one은 단순익절과 함께 지웠다.
