"""종목별 손절 — 판정은 종목 단위로 하고, 손절선에 닿은 종목만 판다 (PRD 5.5-B, 2026-09-18).

엔진 테스트 대부분은 RiskManager를 스텁으로 대체하지만, 여기서는 실물을 그대로 써서
'종목별로 판정한 결과가 실제 주문으로 이어지는가'를 끝까지 확인한다.

2026-08-10부터 2026-09-18까지는 합산(보유 종목 전체)으로 판정해 닿으면 전량을 팔았다.
그 시절을 검증하던 테스트는 지우지 않고 새 동작(닿은 종목만 판다)을 확인하도록 고쳤다 —
각 자리에 남긴 주석을 참고.

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


def test_losing_stock_is_sold_while_the_healthy_one_stays():
    """한 종목만 손절선(-2%)을 넘기면 그 종목만 팔고 건강한 종목은 그대로 둔다.

    종목별 판정으로 바뀌면서(2026-09-18) 기대값이 뒤집혔다 — 합산 판정 시절에는 이
    시나리오(+1% / -3% → 합산 -1%)가 밴드 안이라 아무것도 팔리지 않는 것이 정답이었다
    (옛 이름 test_losing_stock_is_not_sold_while_the_rest_offsets_it, 옛 주석 "종목별로
    손절이 나갔다 — 판정은 합산이어야 한다").
    """
    engine, orders, _ = make_engine(two_holdings())  # +1% / -3%

    engine.on_market_data(MarketData(ticker="000660", price=970.0, volume=1))

    assert [o.ticker for o in orders] == ["000660"], "손절선에 닿은 종목만 팔려야 한다"


def test_only_the_broken_stock_is_sold_when_the_stop_loss_hits():
    """손절선을 넘긴 종목만 팔고 넘기지 않은 종목은 감시 목록에 남긴다.

    종목별 판정으로 바뀌면서(2026-09-18) 기대값이 바뀌었다 — 합산 판정 시절에는
    000660이 -6%로 밀려 합산 -2.5%가 손절선을 넘기면 보유 종목 전량(두 종목)이 팔렸다
    (옛 이름 test_all_holdings_are_sold_when_the_total_hits_the_stop_loss). 이제는 -6%인
    000660만 팔리고, +1%인 005930은 손절선에 닿지 않아 그대로 남는다.
    """
    engine, orders, _ = make_engine(two_holdings())

    # 000660만 -6%로 밀린다 — 005930은 +1%로 손절선에 닿지 않는다
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert [o.side for o in orders] == [OrderSide.SELL]
    assert [o.ticker for o in orders] == ["000660"]
    assert orders[0].quantity == 100
    assert engine.open_tickers == ["005930"], "닿지 않은 종목은 감시 목록에 남아야 한다"


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


def test_exit_alert_names_only_the_sold_stock():
    """알림은 이번에 판 종목만 담는다 — 손절선에 닿지 않은 종목까지 끌어들이지 않는다.

    합산 판정 시절에는 000660이 -6%로 밀리면 합산 -2.5%로 두 종목이 함께 팔려 한 통에
    묶였다 (옛 이름 test_exit_alert_is_sent_once_for_the_whole_list). 이제는 000660만
    팔리므로 알림에도 000660의 순손익(-6.00%)과 이름만 실린다.
    """
    engine, _, alerts = make_engine(two_holdings())

    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert len(alerts) == 1, f"청산 알림이 {len(alerts)}통 나갔다"
    [message] = alerts
    assert "-6.00%" in message          # 팔린 종목(000660)만의 순손익
    assert "SK하이닉스" in message and "삼성전자" not in message


def test_sold_positions_leave_the_calculation():
    """체결이 잔고에 반영되기 전 다음 틱이 와도, 이미 판 물량으로 다시 판정하면 안 된다.

    종목별 판정으로 바뀌면서(2026-09-18) 첫 틱에 팔리는 종목이 000660 하나뿐이라
    기대 주문 수를 2에서 1로 바꿨다 — 005930은 애초에 손절선에 닿지 않아 팔리지 않는다.
    """
    engine, orders, _ = make_engine(two_holdings())
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))
    assert len(orders) == 1

    # 잔고는 아직 000660을 보유 중이라고 답한다
    engine._invalidate_positions()
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert len(orders) == 1, "이미 매도한 종목에 청산 주문이 또 나갔다"


def test_snapshot_reports_the_value_the_engine_judges_on():
    """UI 표시값 — 평가손익률이 아니라 판정에 쓰는 합산 순손익률이어야 한다."""
    engine, _, _ = make_engine(two_holdings())  # +1% / -3%

    assert engine.portfolio_return_snapshot() == -0.01


def test_snapshot_drops_sold_positions():
    """판 종목은 판정 대상에서 빠지고, 남은 종목의 값만 스냅샷에 반영된다.

    합산 판정 시절에는 손절선에 닿으면 보유 종목 전량이 팔려 대상이 하나도 안 남았다
    (그래서 옛 기대값은 None). 종목별 판정에서는 000660만 팔리고 005930(+1%)은 남으므로,
    스냅샷도 그 값으로 남는다.
    """
    engine, _, _ = make_engine(two_holdings())
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert engine.portfolio_return_snapshot() == 0.01


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
    """종목별 판정에서는 손절선에 닿은 종목만 사유가 기록된다 — 나머지는 대상이 아니다.

    합산 판정 시절에는 전량이 팔려 005930도 같이 기록됐다. 여기서는 005930(+1%)이
    손절선에 닿지 않으므로 000660 하나만 남는다.
    """
    store = RecordingStore()
    engine, _, _ = make_engine(two_holdings(price_b=940.0), trade_store=store)

    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))

    assert store.reasons == [("000660", "stop_loss")]


# 단순익절 사유("simple_take_profit")를 percent take_profit과 구분해 기록하는지 보던
# test_simple_take_profit_is_recorded_apart_from_the_percent_one은 단순익절과 함께 지웠다.


# ── 종목별 손절 판정 (2026-09-18) ──────────────────────────
def _risk(stop_loss_ratio=0.05, stop_loss_enabled=True):
    return RiskManager(
        stop_loss_ratio=stop_loss_ratio,
        stop_loss_enabled=stop_loss_enabled,
        commission_rate=0.0,
        tax_rate=0.0,
        slippage_rate=0.0,
    )


def _pos(ticker, avg_price, current_price):
    return Position(
        ticker=ticker,
        quantity=1,
        avg_price=avg_price,
        current_price=current_price,
        name=ticker,
    )


def test_position_exits_picks_only_the_broken_one():
    """한 종목만 손절선에 닿으면 그 종목만 돌려준다."""
    positions = [
        _pos("000660", 100_000, 93_000),   # -7.0%
        _pos("005930", 100_000, 101_000),  # +1.0%
    ]

    assert _risk().check_position_exits(positions) == ["000660"]


def test_position_exits_returns_all_broken():
    """여러 종목이 동시에 닿으면 전부 돌려준다."""
    positions = [
        _pos("000660", 100_000, 93_000),
        _pos("005930", 100_000, 94_000),
    ]

    assert sorted(_risk().check_position_exits(positions)) == ["000660", "005930"]


def test_position_exits_spares_the_healthy_one():
    """합산이 손절선을 넘어도 닿지 않은 종목은 남긴다 — 합산 방식이면 전량이 나갔다.

    합산 순손익률은 매입금액 가중평균이라 합산이 손절선에 닿으면 개별 중 최소 하나는
    반드시 닿아 있다. 그래서 "합산은 닿는데 개별은 아무도 안 닿는" 상황은 없고,
    실제 차이는 **닿지 않은 종목을 함께 파느냐**에서 갈린다.
    """
    positions = [
        _pos("000660", 100_000, 50_000),   # -50%, 매입금액 100,000
        _pos("005930", 900_000, 891_000),  # -1%,  매입금액 900,000
    ]                                       # 합산 -5.9% (손절선 -4% 아래)

    assert _risk(stop_loss_ratio=0.04).check_position_exits(positions) == ["000660"]


def test_position_exits_empty_when_disabled():
    """손절을 끄면 빈 목록 — 판정 자체를 하지 않는다."""
    positions = [_pos("000660", 100_000, 90_000)]

    assert _risk(stop_loss_enabled=False).check_position_exits(positions) == []


# ── 엔진이 종목별로 판다 (2026-09-18) ──────────────────────────
def test_engine_sells_only_the_broken_position():
    """손절선에 닿은 종목만 팔고 나머지는 보유한다 (2026-09-18).

    합산 판정이던 시절에는 한 종목이 닿으면 전량이 나갔다.
    """
    engine, orders, _ = make_engine(
        {
            "000660": Position("000660", 1, 100_000, 100_000, name="하이닉스"),
            "005930": Position("005930", 1, 100_000, 100_000, name="삼성전자"),
        }
    )
    engine.risk_manager.stop_loss_ratio = 0.05

    engine.on_market_data(MarketData(ticker="000660", price=93_000, volume=1))

    assert [o.ticker for o in orders] == ["000660"]
