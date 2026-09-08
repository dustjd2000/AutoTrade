from datetime import date
from types import SimpleNamespace

import pytest

from src.api.market_data import TodayMetrics
from src.core import daily_workflow
from src.core.daily_workflow import DailyWorkflow
from src.core.events import (
    FillRecord,
    MarketData,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.data.collector import DailyStockData
from src.llm.recommender import PROMPT_TEMPLATE_VERSION, StockRecommendation
from src.logger.trade_store import DailySummary, MonthlySummary, RecommendationRow, TradeRow, TradeStore
from src.risk.manager import exit_trigger_price
from src.strategy.llm_momentum import LLMMomentumStrategy


@pytest.fixture(autouse=True)
def report_mark(tmp_path, monkeypatch):
    """최종 리포트 발송 표시를 테스트마다 격리한다 (실제 data/ 를 건드리지 않도록)."""
    path = tmp_path / "final_report_sent"
    monkeypatch.setattr(daily_workflow, "DEFAULT_REPORT_MARK_PATH", path)
    return path


@pytest.fixture(autouse=True)
def buy_records_file(tmp_path, monkeypatch):
    """09:00 매수 기록 파일도 테스트마다 격리한다."""
    path = tmp_path / "buy_records.json"
    monkeypatch.setattr(daily_workflow, "DEFAULT_BUY_RECORDS_PATH", path)
    return path


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None, images=None):
        self.sent.append((subject, message, html))


class FakeOrderClient:
    def __init__(self):
        self.orders = []
        self.fills = []
        self.cancelled = []

    def send_order(self, request):
        self.orders.append(request)
        return OrderResult(
            order_id=str(len(self.orders)),
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.FILLED,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_price=1000.0,
        )

    def get_today_fills(self):
        return self.fills

    def cancel_order(self, order_id, ticker, quantity=0):
        self.cancelled.append((order_id, ticker, quantity))
        return True


def make_workflow(recommendations=None, collected=True, cash=12_000_000):
    strategy = LLMMomentumStrategy()
    email = FakeEmail()
    order_client = FakeOrderClient()
    notifications = []

    daily_data = (
        [
            DailyStockData(
                ticker="005930",
                name="삼성전자",
                prev_close=1000.0,
                prev_high=1020.0,
                prev_low=980.0,
                prev_change_rate=1.0,
                prev_volume=100,
                volume_surge=2.0,
            )
        ]
        if collected
        else []
    )

    engine = SimpleNamespace(
        market_data=SimpleNamespace(
            get_current_price=lambda t: MarketData(ticker=t, price=1000.0, volume=100)
        ),
        order_client=order_client,
        risk_manager=SimpleNamespace(
            approve=lambda *a, **kw: True,
            record_order=lambda *a, **kw: None,
            take_profit_ratio=0.005,
            stop_loss_ratio=0.02,
            commission_rate=0.00015,
            tax_rate=0.0018,
            slippage_rate=0.001,
            # 메일에 익절선(%)이 그대로 적히는지 보는 테스트들이라 퍼센트 익절로 고정한다
            simple_take_profit_enabled=False,
            take_profit_enabled=True,
        ),
        note_open_position=lambda ticker: None,
        last_price=lambda ticker: 0.0,
        # 매수 예정 표가 접수 행을 잔고와 대조한다 (_settled_row) — 기본은 '아직 미체결'
        position_snapshot=lambda: [],
        notify=notifications.append,
        unsellable_snapshot=lambda: [],
    )

    workflow = DailyWorkflow(
        collector=SimpleNamespace(collect=lambda: daily_data),
        recommender=SimpleNamespace(
            recommend=lambda d: recommendations, prompt_version=PROMPT_TEMPLATE_VERSION
        ),
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(
            get_cash=lambda: cash,
            get_positions=lambda: {},
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=cash, cash=cash),
        ),
        trade_store=SimpleNamespace(
            record_fill=lambda *a, **kw: None,
            save_recommendations=lambda *a, **kw: None,
        ),
        email=email,
    )
    return workflow, email, order_client, notifications, strategy


def test_recommendation_email_sent_on_success():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="외국인 순매수")]
    workflow, email, _, _, strategy = make_workflow(recommendations=recs)

    workflow.recommend_and_notify(today=date(2026, 7, 27))

    assert len(email.sent) == 1
    subject, body, _ = email.sent[0]
    assert "2026-07-27" in subject
    assert "005930" in body
    assert "외국인 순매수" in body
    assert strategy._recommendations == recs


def test_llm_failure_skips_buys_and_notifies():
    workflow, email, _, notifications, strategy = make_workflow(recommendations=None)

    workflow.recommend_and_notify(today=date(2026, 7, 27))

    assert email.sent == []
    assert any("LLM 추천 실패" in n for n in notifications)
    assert strategy._recommendations == []


def test_data_collection_failure_skips_recommendation():
    workflow, email, _, notifications, _ = make_workflow(recommendations=[], collected=False)

    workflow.recommend_and_notify(today=date(2026, 7, 27))

    assert email.sent == []
    assert any("데이터 수집 실패" in n for n in notifications)


# ── 일일 리포트 ─────────────────────────────────────────────
REPORT_DAY = date(2026, 7, 29)


def make_report_workflow(fill_error=None):
    workflow, email, order_client, _, _ = make_workflow(recommendations=[])
    applied = []
    summary = DailySummary(
        day=REPORT_DAY,
        buy_count=1,
        sell_count=1,
        realized_pnl=3500.0,
        trades=[TradeRow("035720", "카카오", 5, 36300.0, 37000.0, 3500.0, fees=409.0)],
        cost=181500.0,
        fees=409.0,
    )
    workflow.trade_store = SimpleNamespace(
        apply_fills=lambda fills, day: applied.append((list(fills), day)),
        daily_summary=lambda day: summary,
        monthly_summary=lambda year, month, up_to: MonthlySummary(
            realized_pnl=12340.0, fees=1258.0
        ),
        monthly_cumulative_series=lambda year, month, up_to: [],
        yearly_summary=lambda year, up_to: MonthlySummary(
            realized_pnl=98700.0, fees=7400.0
        ),
        yearly_cumulative_series=lambda year, up_to: [],
    )

    def get_today_fills():
        if fill_error:
            raise fill_error
        return [
            FillRecord(
                order_id="0087730",
                ticker="035720",
                side=OrderSide.SELL,
                filled_quantity=5,
                filled_price=37000.0,
            )
        ]

    order_client.get_today_fills = get_today_fills
    return workflow, email, applied


def test_daily_report_applies_fills_before_summarising():
    """체결을 반영하지 않으면 접수 기록이 pending으로 남아 0건으로 집계된다."""
    workflow, email, applied = make_report_workflow()

    workflow.send_daily_report(today=REPORT_DAY)

    assert len(applied) == 1
    fills, day = applied[0]
    assert fills[0].order_id == "0087730"
    assert day == REPORT_DAY

    subject, body, html = email.sent[0]
    assert "2026-07-29" in subject
    assert "(035720)카카오" in body
    assert "+1.93%" in body
    assert "<table" in html


def test_daily_report_is_sent_even_when_fill_sync_fails():
    workflow, email, applied = make_report_workflow(fill_error=RuntimeError("조회 실패"))

    workflow.send_daily_report(today=REPORT_DAY)

    assert applied == []
    _, body, _ = email.sent[0]
    assert "체결 내역 조회에 실패" in body


def test_execute_buys_sends_orders_with_fixed_one_sixth_amount():
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", target_price=1000, reason="b"),
        StockRecommendation(ticker="035420", name="NAVER", target_price=1000, reason="c"),
    ]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    workflow.execute_buys()

    # 예수금 1200만 → 매수가능 600만 → 종목당 200만 → 주당 1000원이므로 2000주씩
    assert len(order_client.orders) == 3
    assert all(o.quantity == 2000 for o in order_client.orders)
    assert all(o.side == OrderSide.BUY for o in order_client.orders)


def test_execute_buys_with_two_recommendations_keeps_amount_fixed():
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", target_price=1000, reason="b"),
    ]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    workflow.execute_buys()

    # 2종목만 추천돼도 종목당 200만원(=예수금 1/6) 고정, 나머지는 현금 유지
    assert len(order_client.orders) == 2
    assert all(o.quantity == 2000 for o in order_client.orders)


def test_execute_buys_does_nothing_without_recommendations():
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])

    workflow.execute_buys()

    assert order_client.orders == []


def test_rejected_buy_is_not_treated_as_ordered():
    """거부된 주문을 접수로 처리하면 보유하지도 않은 종목을 구독하고 실패를 놓친다.

    실제로 CB 발동 중 주문이 전부 거부됐는데 "Buy order sent"로 남아 원인 파악이 늦어졌다.
    """
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, notifications, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    subscribed = []
    workflow.ws_client = SimpleNamespace(subscribe=subscribed.extend)
    order_client.send_order = lambda request: OrderResult(
        order_id="1",
        ticker=request.ticker,
        side=request.side,
        status=OrderStatus.REJECTED,
        quantity=request.quantity,
        filled_quantity=0,
        error_message="CB 발동중입니다. 취소주문만 가능합니다.",
    )

    workflow.execute_buys()

    assert subscribed == []  # 거부된 종목은 실시간 구독하지 않는다
    assert any("매수 거부" in n and "CB 발동중" in n for n in notifications)
    assert any("한 건도 접수되지 않았습니다" in n for n in notifications)


def test_buy_resets_final_report_flag(report_mark):
    """매수로 보유가 다시 생기면 앞서 보낸 최종 리포트는 더 이상 최종이 아니다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    report_mark.write_text(REPORT_DAY.isoformat(), encoding="utf-8")

    workflow.execute_buys()

    assert not report_mark.exists()


def test_rejected_buy_keeps_final_report_flag(report_mark):
    """매수가 전부 거부돼 보유가 생기지 않았다면 앞서 보낸 리포트가 여전히 최종이다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    report_mark.write_text(REPORT_DAY.isoformat(), encoding="utf-8")
    order_client.send_order = lambda request: OrderResult(
        order_id="1",
        ticker=request.ticker,
        side=request.side,
        status=OrderStatus.REJECTED,
        quantity=request.quantity,
        error_message="CB 발동중입니다. 취소주문만 가능합니다.",
    )

    workflow.execute_buys()

    assert report_mark.read_text(encoding="utf-8") == REPORT_DAY.isoformat()


def test_note_open_position_is_called_for_ordered_stock():
    """감시 대상 표시가 빠지면 매수 직후 창을 닫아도 경고가 뜨지 않는다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    noted = []
    workflow.engine.note_open_position = noted.append

    workflow.execute_buys()

    assert noted == ["005930"]


def test_execute_buys_sends_limit_orders_at_target_price():
    """시장가로 사면 개장 직후 고가를 그대로 따라간다 — 목표가 지정가로 낸다 (확정 2026-08-06)."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1200, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    workflow.execute_buys()

    order = order_client.orders[0]
    assert order.order_type == OrderType.LIMIT
    assert order.price == 1200
    assert order.quantity == 1666  # 종목당 200만 ÷ 1,200원


def test_quantity_is_sized_by_target_price_not_current_price():
    """현재가는 갭 판정에만 쓴다 — 수량은 목표가 기준이다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    # 현재가가 목표가보다 낮아도(=갭 하락) 수량 산정 기준은 목표가다
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=500.0, volume=100)
    )

    workflow.execute_buys()

    assert order_client.orders[0].price == 1000
    assert order_client.orders[0].quantity == 2000  # 종목당 200만 ÷ 1,000원


def test_buy_is_skipped_when_the_open_gaps_above_the_target():
    """전일 종가로 잡은 목표가가 갭 상승으로 무의미해진 날은 참여하지 않는다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.engine.market_data = SimpleNamespace(  # 허용치 2% → 1,020원 초과면 스킵
        get_current_price=lambda t: MarketData(ticker=t, price=1021.0, volume=100)
    )

    workflow.execute_buys()

    assert order_client.orders == []


def test_buy_proceeds_inside_the_gap_tolerance():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=1020.0, volume=100)
    )

    workflow.execute_buys()

    assert len(order_client.orders) == 1


# ── 범위 지정 주문가 (PRD 5.5-B '주문 방식', 확정 2026-08-26) ────────────────────
def band_setup(current_price, target_price=1000, tolerance=0.02):
    """현재가만 바꿔 가며 주문가를 보는 공용 셋업 — 갭 하락 판정은 끄고 위쪽만 본다."""
    recs = [
        StockRecommendation(
            ticker="005930", name="삼성전자", target_price=target_price, reason="a"
        )
    ]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.buy_price_tolerance_ratio = tolerance
    workflow.gap_down_tolerance_ratio = 0.0
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=current_price, volume=100)
    )
    return workflow, order_client


def test_order_price_rises_to_the_band_top_when_the_price_is_above_the_target():
    """밴드 안이면 반드시 체결돼야 한다 — 목표가 한 점에 걸면 되밀리지 않는 한 미체결이다."""
    workflow, order_client = band_setup(current_price=1010.0)

    workflow.execute_buys()

    (order,) = order_client.orders
    assert order.price == 1020  # 1,000 × 1.02, 호가 단위(5원) 내림
    assert order.quantity == 1960  # 종목당 200만 ÷ 1,020원 — 수량도 주문가 기준이다


def test_order_price_stays_at_the_target_when_the_price_is_below_it():
    """아래쪽 절반은 목표가 지정가로도 이미 즉시 체결된다 — 올리면 수량만 준다."""
    workflow, order_client = band_setup(current_price=990.0)

    workflow.execute_buys()

    (order,) = order_client.orders
    assert order.price == 1000
    assert order.quantity == 2000


def test_order_price_stays_at_the_target_when_the_price_equals_it():
    workflow, order_client = band_setup(current_price=1000.0)

    workflow.execute_buys()

    assert order_client.orders[0].price == 1000


def test_order_price_never_falls_below_the_current_price_after_tick_flooring():
    """호가 단위 내림이 현재가 아래로 떨어지면 다시 미체결이 된다 — 그때는 현재가로 낸다.

    목표가 1,000원·허용치 0.6% → 상단 1,006원, 호가 단위(5원) 내림이면 1,005원이라
    현재가 1,006원보다 낮아진다. 현재가는 체결된 값이라 이미 호가 단위에 맞는다.
    """
    workflow, order_client = band_setup(current_price=1006.0, tolerance=0.006)

    workflow.execute_buys()

    assert order_client.orders[0].price == 1006


def test_order_price_falls_back_to_the_target_when_the_quote_fails():
    """밴드를 확인할 수 없는 상태에서 상단에 거는 것은 근거 없이 비싸게 사는 것이다."""
    workflow, order_client = band_setup(current_price=1010.0)

    def boom(ticker):
        raise RuntimeError("quote down")

    workflow.engine.market_data = SimpleNamespace(get_current_price=boom)

    workflow.execute_buys()

    assert order_client.orders[0].price == 1000


def test_korea_electric_power_regression_2026_08_26():
    """2026-08-26 (015760)한국전력 회귀 — 밴드 안인데 종일 미체결로 투입 0원이던 사례.

    목표가 33,200원, 09:05 현재가 33,400원, 상승 허용치 3% → 상한 34,196원.
    갭 판정은 통과했는데 지정가가 33,200원이라 시장가 아래에 놓여 체결되지 않았다.
    """
    workflow, order_client = band_setup(
        current_price=33_400.0, target_price=33_200, tolerance=0.03
    )

    workflow.execute_buys()

    (order,) = order_client.orders
    assert order.price == 34_150  # 34,196 → 호가 단위(50원) 내림
    assert order.price > 33_400  # 현재가 위 = 접수 즉시 체결되는 지정가


def test_gap_skip_is_reported_in_the_buy_result_email():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=1500.0, volume=100)
    )

    workflow.execute_buys()
    workflow.cancel_unfilled_buys()

    _, body, _ = email.sent[0]
    assert "건너뜀" in body
    assert "갭" in body


# ── 갭 하락 판정 (PRD 5.5-B, 확정 2026-08-11, 기준값 변경 2026-08-14) ──────────────
def gap_down_setup(current_price, recommend_price=1000.0, target_price=1000):
    recs = [
        StockRecommendation(
            ticker="005930",
            name="삼성전자",
            target_price=target_price,
            reason="a",
            recommend_price=recommend_price,
        )
    ]
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=current_price, volume=100)
    )
    return workflow, email, order_client


def test_buy_is_skipped_when_the_price_gaps_below_the_recommend_price():
    """추천한 뒤 무너진 종목은 사지 않는다 (허용치 1% → 990원 미만)."""
    workflow, _, order_client = gap_down_setup(current_price=989.0)

    workflow.execute_buys()

    assert order_client.orders == []


def test_buy_proceeds_inside_the_gap_down_tolerance():
    workflow, _, order_client = gap_down_setup(current_price=990.0)

    workflow.execute_buys()

    assert len(order_client.orders) == 1


def test_gap_down_is_measured_against_the_reference_not_the_target():
    """2026-08-11 포스코퓨처엠 회귀 — 목표가 기준으로는 걸리지 않고 기준가로만 걸린다.

    163,300원은 목표가 163,500원 대비 -0.12%라 목표가 기준 ±2% 밴드에는 안 걸리지만,
    기준가 165,500원 대비로는 -1.33%다. 이 종목은 이날 158,800원까지 밀려 손절됐다.
    목표 매수가 자체가 눌림을 노려 기준가보다 낮게 잡히므로 목표가는 기준이 될 수 없다.
    """
    workflow, _, order_client = gap_down_setup(
        current_price=163_300.0, recommend_price=165_500.0, target_price=163_500
    )

    workflow.execute_buys()

    assert order_client.orders == []


def test_gap_down_check_is_off_when_the_tolerance_is_zero():
    """임계값 근거가 약해 언제든 끌 수 있어야 한다 — 갭 상승 쪽(0 = 가장 엄격)과 반대 규약이다."""
    workflow, _, order_client = gap_down_setup(current_price=800.0)
    workflow.gap_down_tolerance_ratio = 0.0

    workflow.execute_buys()

    assert len(order_client.orders) == 1


def test_gap_down_check_is_skipped_when_the_reference_price_is_unknown():
    """기준가를 모르면 판정할 수 없다 — 시세 조회 실패와 같이 매수를 막지 않는다."""
    workflow, _, order_client = gap_down_setup(current_price=800.0, recommend_price=0.0)

    workflow.execute_buys()

    assert len(order_client.orders) == 1


def test_gap_down_skip_is_reported_in_the_buy_result_email():
    workflow, email, _ = gap_down_setup(current_price=989.0)

    workflow.execute_buys()
    workflow.cancel_unfilled_buys()

    _, body, _ = email.sent[0]
    assert "건너뜀" in body
    assert "갭 하락" in body


def test_buy_proceeds_when_the_current_price_cannot_be_read():
    """시세 조회가 실패해도 매수는 낸다 — 지정가라 목표가보다 비싸게 체결되지 않는다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    def boom(ticker):
        raise RuntimeError("시세 조회 실패")

    workflow.engine.market_data = SimpleNamespace(get_current_price=boom)

    workflow.execute_buys()

    assert len(order_client.orders) == 1


# ── 09:30 미체결 취소 + 매수 결과 메일 ──────────────────────
def run_buys(recommendations, cash=12_000_000, fills=None, fill_error=None):
    """09:00 매수 → 09:30 마무리까지 돌린다. 결과 메일은 09:30에 나간다.

    fills를 주지 않으면 접수된 주문이 전부 체결된 것으로 본다 — 실제 API도 체결된 주문은
    당일 체결내역에 실어 주므로, 빈 목록을 기본값으로 두면 '흔적 없는 주문'으로 오인된다.
    """
    workflow, email, order_client, notifications, strategy = make_workflow(
        recommendations=recommendations, cash=cash
    )
    strategy.set_recommendations(recommendations)
    if fill_error:
        order_client.get_today_fills = lambda: (_ for _ in ()).throw(fill_error)

    workflow.execute_buys()
    order_client.fills = (
        fills
        if fills is not None
        else [
            FillRecord(
                order_id=str(i),
                ticker=order.ticker,
                side=OrderSide.BUY,
                filled_quantity=order.quantity,
                filled_price=1000.0,
            )
            for i, order in enumerate(order_client.orders, start=1)
        ]
    )
    workflow.cancel_unfilled_buys()
    return email, order_client, notifications


def unfilled_fill(ticker="005930", order_id="1", filled=0, unfilled=2000, price=0.0):
    return FillRecord(
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.BUY,
        filled_quantity=filled,
        filled_price=price,
        unfilled_quantity=unfilled,
    )


def test_buy_result_email_is_sent_at_cancel_time_not_at_order_time():
    """지정가는 접수 시점에 체결 여부를 모른다 — 메일은 09:30 마무리에서 나간다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    order_client.fills = [unfilled_fill()]

    workflow.execute_buys()
    assert email.sent == []

    workflow.cancel_unfilled_buys()
    assert len(email.sent) == 1


def test_unfilled_buy_orders_are_cancelled():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    _, order_client, _ = run_buys(recs, fills=[unfilled_fill()])

    assert order_client.cancelled == [("1", "005930", 2000)]


def test_cancelled_order_is_reported_as_not_bought():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    email, _, _ = run_buys(recs, fills=[unfilled_fill()])

    subject, body, _ = email.sent[0]
    assert "매수 실행 결과 0/1종목" in subject
    assert "미체결 취소" in body
    assert "매수하지 못한 종목" in body


def test_unfilled_sell_orders_are_not_cancelled():
    """매도 주문의 미체결 잔량까지 거두면 청산이 취소된다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    sell = FillRecord(
        order_id="9",
        ticker="000660",
        side=OrderSide.SELL,
        filled_quantity=0,
        filled_price=0.0,
        unfilled_quantity=10,
    )
    _, order_client, _ = run_buys(recs, fills=[sell])

    assert not any(order_id == "9" for order_id, _, _ in order_client.cancelled)


def test_cancel_failure_is_notified():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, order_client, notifications, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    order_client.fills = [unfilled_fill()]
    order_client.cancel_order = lambda *a, **kw: False

    workflow.execute_buys()
    workflow.cancel_unfilled_buys()

    assert any("미체결 매수 취소 실패" in n for n in notifications)


def test_buy_result_email_survives_an_engine_restart():
    """09:00~09:30 사이 설정 저장으로 엔진이 재시작돼도 매수 결과는 알려야 한다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    # 엔진 재시작 — DailyWorkflow가 통째로 새로 만들어진다
    restarted, email, order_client, _, _ = make_workflow(recommendations=recs)
    order_client.fills = [unfilled_fill()]
    restarted.cancel_unfilled_buys()

    assert len(email.sent) == 1
    _, body, _ = email.sent[0]
    assert "005930" in body


def test_buy_result_email_is_not_sent_twice():
    """09:30이 보낸 메일을 15:15 마감 정리가 또 보내면 안 된다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    order_client.fills = [unfilled_fill()]

    workflow.execute_buys()
    workflow.cancel_unfilled_buys()
    workflow.cancel_unfilled_buys()

    assert len(email.sent) == 1


def test_buy_records_from_another_day_are_ignored(buy_records_file):
    """어제 남은 기록으로 오늘 매수 결과 메일을 보내면 안 된다."""
    buy_records_file.write_text(
        '{"date": "2020-01-02", "cash": 1000.0, "amount_per_stock": 500.0,'
        ' "records": [{"ticker": "005930", "name": "삼성전자", "outcome": "ordered",'
        ' "quantity": 1, "reference_price": 1000.0, "filled_quantity": 0,'
        ' "filled_price": null, "order_id": "1", "note": null}]}',
        encoding="utf-8",
    )
    workflow, email, order_client, _, _ = make_workflow(recommendations=[])
    order_client.fills = []

    workflow.cancel_unfilled_buys()

    assert email.sent == []


def test_unreadable_buy_records_do_not_stop_cancellation(buy_records_file):
    """기록이 깨져도 미체결 취소는 돌아야 한다 — 취소가 메일보다 중요하다."""
    buy_records_file.write_text("{망가진 json", encoding="utf-8")
    workflow, email, order_client, _, _ = make_workflow(recommendations=[])
    order_client.fills = [unfilled_fill()]

    workflow.cancel_unfilled_buys()

    assert order_client.cancelled == [("1", "005930", 2000)]
    assert email.sent == []


def test_cancel_runs_even_without_in_memory_records():
    """09:00~09:30 사이에 엔진이 재시작되면 주문 기록이 사라진다 — 그래도 취소는 돼야 한다."""
    workflow, email, order_client, _, _ = make_workflow(recommendations=[])
    order_client.fills = [unfilled_fill()]

    workflow.cancel_unfilled_buys()

    assert order_client.cancelled == [("1", "005930", 2000)]
    assert email.sent == []  # 알릴 매수 기록이 없으므로 메일은 보내지 않는다


def test_buy_result_email_lists_ordered_stocks():
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", target_price=1000, reason="b"),
    ]
    email, _, _ = run_buys(recs)

    assert len(email.sent) == 1
    subject, body, html = email.sent[0]
    assert "매수 실행 결과 2/2종목" in subject
    assert "(005930)삼성전자" in body and "(000660)SK하이닉스" in body
    assert "2,000" in body                    # 수량 2000주
    assert "총 투입금액" in body
    assert "4,000,000원" in body               # 2종목 × 200만
    # 익절/손절 라인은 순손익 설정값(기본 +0.5% / -2%) 기준이다
    tp_price = exit_trigger_price(1000.0, 0.005, 0.00015, 0.0018, 0.001)
    sl_price = exit_trigger_price(1000.0, -0.02, 0.00015, 0.0018, 0.001)
    assert f"{tp_price:,.0f}" in body and f"{sl_price:,.0f}" in body
    assert "<table" in html


def test_buy_result_email_shows_filled_price_when_fill_is_already_known():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=2000,
            filled_price=1005.0,
        )
    ]
    email, _, _ = run_buys(recs, fills=fills)

    _, body, _ = email.sent[0]
    assert "체결" in body
    assert "1,005" in body
    # 접수 상태가 없으면 그 주의 문구도 붙지 않는다
    assert "주문이 받아들여진 상태" not in body


def test_buy_result_email_marks_partial_fill():
    """부분체결분은 그대로 보유하고 잔량만 취소한다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=1500,
            filled_price=1005.0,
            unfilled_quantity=500,
        )
    ]
    email, order_client, _ = run_buys(recs, fills=fills)

    _, body, _ = email.sent[0]
    assert "부분체결" in body
    assert "1,500" in body
    assert order_client.cancelled == [("1", "005930", 500)]


def test_buy_result_email_is_sent_even_when_fill_query_fails():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    email, _, notifications = run_buys(recs, fill_error=RuntimeError("조회 실패"))

    _, body, _ = email.sent[0]
    assert "체결 내역 조회에 실패" in body
    assert "접수" in body
    assert "1,000" in body  # 체결가를 모르면 주문에 쓴 목표가로 표기
    assert any("미체결 매수 주문을 조회하지 못했습니다" in n for n in notifications)


def test_buy_result_email_lists_stocks_that_were_not_bought():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    order_client.send_order = lambda request: OrderResult(
        order_id="1",
        ticker=request.ticker,
        side=request.side,
        status=OrderStatus.REJECTED,
        quantity=request.quantity,
        error_message="CB 발동중입니다. 취소주문만 가능합니다.",
    )

    workflow.execute_buys()
    workflow.cancel_unfilled_buys()

    subject, body, html = email.sent[0]
    assert "매수 실행 결과 0/1종목" in subject
    assert "매수하지 못한 종목" in body
    assert "CB 발동중" in body and "CB 발동중" in html


def test_buy_result_email_not_sent_without_buy_plans():
    """추천이 없어 매수 자체를 시도하지 않았으면 추천 스킵 알림으로 충분하다."""
    email, _, _ = run_buys([])

    assert email.sent == []


def test_buy_skipped_when_one_share_exceeds_allocation():
    """삼성바이오로직스처럼 1주 목표가가 종목당 배정액을 넘으면 주문하지 않는다.

    건너뜀은 별도 알림 메일을 보내지 않고 매수 실행 결과 메일에만 싣는다.
    """
    # 종목당 배정 = 2,000,000 × 1/6 ≈ 333,333원 < 1주 1,000,000원
    recs = [
        StockRecommendation(
            ticker="207940", name="삼성바이오로직스", target_price=1_000_000, reason="a"
        )
    ]
    workflow, email, order_client, notifications, strategy = make_workflow(
        recommendations=recs, cash=2_000_000
    )
    strategy.set_recommendations(recs)

    workflow.execute_buys()

    assert order_client.orders == []
    assert not any("건너뜀" in n for n in notifications)


def test_pending_order_missing_from_fills_is_still_cancelled():
    """체결내역 TR이 대기 주문을 싣지 않으면 1순위 경로로는 못 잡는다 — 접수 기록으로 보완한다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    _, order_client, _ = run_buys(recs, fills=[])

    assert order_client.cancelled == [("1", "005930", 0)]  # 0 = 잔량 전부 취소


def test_filled_order_present_in_fills_is_not_cancelled_again():
    """조회에 잡힌 체결 완료 주문까지 취소하면 헛된 실패 알림이 나간다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="a")]
    fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=2000,
            filled_price=1000.0,
            unfilled_quantity=0,
        )
    ]
    _, order_client, _ = run_buys(recs, fills=fills)

    assert order_client.cancelled == []


def test_gap_down_ignores_the_previous_close():
    """전일 종가 대비 판정은 09:05 후보 선정이 맡는다 — 주문 직전은 추천 시점만 본다.

    전일 종가 10,000 / 추천 시점 10,500 / 현재가 10,300이면 전일 종가 기준으로는 +3%라
    통과하지만, 추천 시점 기준으로는 -1.9%라 걸러야 한다.
    """
    workflow, _, order_client = gap_down_setup(
        current_price=10_300.0, recommend_price=10_500.0, target_price=10_400
    )

    workflow.execute_buys()

    assert order_client.orders == []


# ── UI '매수 예정' 표 (PRD 5.10) ────────────────────────────
BOARD_DAY = date(2026, 8, 24)


def board_recs():
    return [
        StockRecommendation(
            ticker="005930",
            name="삼성전자",
            target_price=1000,
            reason="a",
            target_sell_price=1100,
        )
    ]


def test_buy_plan_snapshot_is_filled_at_recommendation_time():
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())

    workflow.recommend_and_notify(today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.status == daily_workflow.BUY_PENDING_STATUS
    assert plan.buy_price == 1000
    assert plan.quantity == 0  # 아직 주문 전이라 수량을 모른다
    # 매도예상가는 LLM 목표 매도가(1,100)가 아니라 익절선에 닿는 가격이다
    assert plan.sell_price == pytest.approx(
        exit_trigger_price(1000, 0.005, 0.00015, 0.0018, 0.001)
    )


def test_buy_plan_sell_price_follows_simple_take_profit():
    """단순익절이 켜져 있으면 익절선은 0이다 — 표도 그 가격을 보여줘야 한다."""
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    workflow.engine.risk_manager.simple_take_profit_enabled = True

    workflow.recommend_and_notify(today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.sell_price == pytest.approx(
        exit_trigger_price(1000, 0.0, 0.00015, 0.0018, 0.001)
    )


def test_buy_plan_sell_price_is_blank_when_take_profit_is_off():
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    workflow.engine.risk_manager.take_profit_enabled = False

    workflow.recommend_and_notify(today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.sell_price == 0.0


def test_buy_plan_snapshot_shows_order_status_after_buying():
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)

    workflow.execute_buys()

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "접수"
    assert plan.quantity == 2000  # 종목당 200만 ÷ 1,000원


def test_buy_plan_snapshot_keeps_the_result_after_the_records_file_is_cleared():
    """기록 파일은 10:10에 지워진다 — 표는 그 뒤에도 결과를 계속 보여줘야 한다."""
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    order_client.fills = [unfilled_fill()]

    workflow.cancel_unfilled_buys()

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "미체결 취소"
    assert plan.quantity == 0        # 사지 못했으므로 수량을 비운다
    assert plan.sell_price == 0.0    # 팔 것이 없으니 매도예상가도 없다
    assert "목표 매수가에 닿지 않아" in plan.note


def test_buy_plan_snapshot_is_empty_on_a_different_day():
    """전날 행이 남으면 오늘 매수한 것처럼 보인다 — 날짜가 다르면 비운다."""
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    workflow.recommend_and_notify(today=BOARD_DAY)

    assert workflow.buy_plan_snapshot(today=date(2026, 8, 25)) == []


# ── 현재가 표시와 선택 삭제 (PRD 5.10) ──────────────────────
def test_buy_plan_snapshot_carries_the_live_price():
    """표의 현재가는 엔진이 받아둔 마지막 시세다 — UI가 API를 부르지 않는다."""
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    workflow.engine.last_price = lambda ticker: 1_050.0 if ticker == "005930" else 0.0

    workflow.recommend_and_notify(today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.current_price == 1_050.0


def test_buy_plan_current_price_is_blank_before_the_first_tick():
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())

    workflow.recommend_and_notify(today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.current_price == 0.0


def test_recommendation_subscribes_the_recommended_tickers():
    """아직 보유가 아니라 아무도 구독하지 않는다 — 추천 시점에 걸어야 현재가가 들어온다."""
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    subscribed = []
    workflow.ws_client = SimpleNamespace(
        subscribe=subscribed.append, is_connected=True
    )

    workflow.recommend_and_notify(today=BOARD_DAY)

    assert subscribed == [["005930"]]


def test_filled_rows_leave_the_buy_plan_board():
    """체결된 종목은 '매수 예정'이 아니다 — 보유 종목 표로 넘어간다."""
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    order_client.fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=2000,
            filled_price=1000.0,
            unfilled_quantity=0,
        )
    ]

    workflow.cancel_unfilled_buys()

    assert workflow.buy_plan_snapshot() == []


def test_partially_filled_rows_stay_on_the_board():
    """부분체결은 남은 수량이 아직 미체결이라 진행 중인 건이다."""
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    order_client.fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=1000,
            filled_price=1000.0,
            unfilled_quantity=1000,
        )
    ]

    workflow.cancel_unfilled_buys()

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "부분체결"


def test_dropping_a_plan_removes_it_from_today_s_orders():
    """표에서 지운 종목은 매수 시각에 주문이 나가면 안 된다."""
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    workflow.recommend_and_notify(today=BOARD_DAY)

    workflow.drop_buy_plans(["005930"], today=BOARD_DAY)

    assert workflow.buy_plan_snapshot(today=BOARD_DAY) == []
    workflow.execute_buys()
    assert order_client.orders == []


def test_dropping_keeps_the_other_stocks_and_their_allocation():
    """남은 종목의 배정액은 그대로다 — 지운 몫은 현금으로 남는다 (PRD 10절 2026-07-27)."""
    recs = board_recs() + [
        StockRecommendation(ticker="035720", name="카카오", target_price=1000, reason="b")
    ]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    workflow.recommend_and_notify(today=BOARD_DAY)

    workflow.drop_buy_plans(["005930"], today=BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.ticker == "035720"
    workflow.execute_buys()
    (order,) = order_client.orders
    assert order.ticker == "035720"
    assert order.quantity == 2000, "종목당 배정액은 지운 종목 수와 무관하게 고정이다"


# ── 체결 반영 시차를 잔고로 메움 (PRD 5.10, 확정 2026-08-26) ──────────────────
def hold(ticker, quantity):
    """엔진 잔고 캐시 한 줄 — position_snapshot이 돌려주는 모양만 흉내낸다."""
    return SimpleNamespace(ticker=ticker, quantity=quantity)


def test_fully_filled_row_leaves_the_board_before_the_ten_ten_sync():
    """2026-08-26 회귀 — 전량 체결됐는데 표가 15:15까지 '접수'로 남아 두 표가 겹쳤다.

    체결 반영(_fill_buy_prices)은 10:10·15:15에만 도는데, 밴드 상단 지정가는 접수 직후
    체결되는 것이 보통이라 그 시차가 그대로 드러난다.
    """
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    assert workflow.buy_plan_snapshot()[0].status == "접수"

    workflow.engine.position_snapshot = lambda: [hold("005930", 2000)]

    assert workflow.buy_plan_snapshot() == [], "보유 종목 표로 옮겨갔으므로 겹쳐 실리면 안 된다"


def test_partially_held_row_becomes_partially_filled():
    """보유 수량이 주문 수량에 못 미치면 아직 진행 중이다 — 남은 수량은 미체결분이다."""
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    workflow.engine.position_snapshot = lambda: [hold("005930", 1200)]

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "부분체결"
    assert plan.quantity == 1200


def test_ordered_row_stays_while_nothing_is_held():
    """잔고에 없으면 아직 미체결이다 — 이때는 표가 종전대로 '접수'다."""
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "접수"


def test_filled_row_does_not_return_after_the_position_is_sold():
    """2026-09-01 회귀 — 전량 매도했더니 그 종목이 매수예정 표에 '접수'로 되살아났다.

    체결 판정을 현재 보유 수량에서 매번 다시 유도하기 때문이었다. 판 뒤에는 잔고가
    0이라 '아직 체결 안 됨'과 구분되지 않는다. 한 번 본 체결은 되돌아가면 안 된다.
    """
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    workflow.engine.position_snapshot = lambda: [hold("005930", 2000)]
    assert workflow.buy_plan_snapshot() == []   # 체결을 한 번 관찰한다

    workflow.engine.position_snapshot = lambda: []   # 전량 매도
    assert workflow.buy_plan_snapshot() == [], "판 종목이 매수예정으로 돌아왔다"


def test_never_filled_row_still_stays_after_a_sell_of_another_stock():
    """되돌아가지 않게 만든 것이 '미체결도 숨긴다'가 되면 안 된다."""
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    workflow.engine.position_snapshot = lambda: []

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "접수"


def test_settled_tickers_do_not_leak_into_another_day():
    """표가 날짜로 비워지듯, 체결을 봤다는 기억도 그날 것이라야 한다."""
    recs = board_recs()
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    workflow.engine.position_snapshot = lambda: [hold("005930", 2000)]
    workflow.buy_plan_snapshot()

    workflow._set_buy_board(date(2026, 9, 2), workflow._buy_board[1])
    workflow.engine.position_snapshot = lambda: []

    (plan,) = workflow.buy_plan_snapshot(today=date(2026, 9, 2))
    assert plan.status == "접수", "전날의 체결 기억이 다음 날 행을 숨겼다"


def test_settling_does_not_touch_the_saved_records():
    """표시만 바꾼다 — 기록과 결과 메일을 확정하는 것은 10:10의 체결내역 조회다."""
    recs = board_recs()
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    workflow.engine.position_snapshot = lambda: [hold("005930", 2000)]
    workflow.buy_plan_snapshot()

    order_client.fills = [
        FillRecord(
            order_id="1",
            ticker="005930",
            side=OrderSide.BUY,
            filled_quantity=2000,
            filled_price=1000.0,
            unfilled_quantity=0,
        )
    ]
    workflow.cancel_unfilled_buys()

    _, body, _ = email.sent[0]
    assert "체결" in body


# ── 접수 행 선택 삭제 (PRD 5.10, 확대 2026-08-26) ────────────────────────────
def test_dropping_an_ordered_row_cancels_the_unfilled_order():
    """접수 행을 빼면 10:10을 기다리지 않고 그 자리에서 미체결 주문을 취소한다.

    종전에는 '매수 대기' 행만 지울 수 있었는데, 그 상태가 추천~매수 3분 동안만 존재해
    기능을 쓸 수 있는 시간이 사실상 없었다.
    """
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()

    assert workflow.drop_buy_plans(["005930"]) == ["005930"]

    assert order_client.cancelled == [("1", "005930", 0)]  # 잔량 전부
    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "미체결 취소", "행은 지우지 않는다 — 주문이 있었다는 사실이 기록이다"


def test_cancelled_row_is_not_cancelled_again_at_ten_ten():
    """기록 파일을 갈아써야 10:10이 같은 주문을 다시 취소하려 들지 않는다."""
    recs = board_recs()
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    workflow.drop_buy_plans(["005930"])

    workflow.cancel_unfilled_buys()

    assert order_client.cancelled == [("1", "005930", 0)], "취소는 한 번뿐이어야 한다"


def test_dropping_an_ordered_row_leaves_it_alone_when_the_cancel_fails():
    """취소에 실패했는데 표만 정리되면, 주문이 살아 있는 줄 모른 채 하루가 간다."""
    recs = board_recs()
    workflow, _, order_client, notifications, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    order_client.cancel_order = lambda order_id, ticker, quantity=0: False

    assert workflow.drop_buy_plans(["005930"]) == []

    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "접수"
    assert any("취소 실패" in n for n in notifications)


def test_cancel_rejected_on_a_filled_order_is_not_reported_as_failure():
    """키움은 이미 체결된 주문의 취소를 거절한다 — '주문이 살아 있다'는 정반대 안내였다.

    2026-08-26 13:49 실측: 취소가능수량이 없습니다(=전량 체결)인데 실패 알림이 나갔다.
    """
    recs = board_recs()
    workflow, _, order_client, notifications, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()
    order_client.cancel_order = lambda order_id, ticker, quantity=0: False
    workflow.engine.position_snapshot = lambda: [hold("005930", 2000)]

    assert workflow.drop_buy_plans(["005930"]) == []

    assert not any("취소 실패" in n for n in notifications)


def test_dropping_ignores_rows_with_no_live_order():
    """건너뜀·실패 행은 취소할 주문이 없다 — 체크 열도 비어 있다 (MainWindow._can_drop)."""
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", target_price=9_000_000, reason="a")
    ]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.execute_buys()  # 1주 가격이 배정액을 넘어 '건너뜀'
    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "건너뜀"

    assert workflow.drop_buy_plans(["005930"]) == []

    assert order_client.cancelled == []


def _recommendation(
    ticker="005930",
    name="삼성전자",
    target_price=70_000,
    target_sell_price=71_400,
    outlook="오전 중 회복 시도",
):
    return StockRecommendation(
        ticker=ticker,
        name=name,
        target_price=target_price,
        target_sell_price=target_sell_price,
        reason="전일 등락률 +2.15%",
        setup="rebound",
        outlook=outlook,
        recommend_price=70_500.0,
    )


class FakeMarketData:
    """collector가 들고 있는 시세 클라이언트의 가짜 — 15:35 검증의 당일 봉 조회 대상."""

    def __init__(self):
        self.today_metrics = {}
        # 이 집합에 든 종목코드는 today_metrics를 보지 않고 조회 자체가 터진 것처럼 군다
        # (candle-lookup exception path — None을 돌려주는 것과는 다른 분기다).
        self.raise_for = set()

    def get_today_metrics(self, ticker, today=None):
        if ticker in self.raise_for:
            raise RuntimeError("candle lookup failed")
        return self.today_metrics.get(ticker)


class FakeReviewer:
    """LLM 검증 평가 모듈의 가짜 — review 호출 인자를 기록하고 미리 정한 결과를 돌려준다."""

    def __init__(self):
        self.result = None
        self.calls = []

    def review(self, items, timeout_seconds=120.0):
        self.calls.append(items)
        return self.result


def build_workflow(tmp_path):
    """recommend_and_notify가 진짜 TradeStore에 저장하는지 보는 테스트 전용 조립.

    make_workflow와 달리 trade_store는 SimpleNamespace가 아니라 실제 TradeStore다 —
    save_recommendations로 남긴 값을 recommendations_for로 그대로 읽어 확인해야 한다.
    """
    strategy = LLMMomentumStrategy()
    email = FakeEmail()
    daily_data = [
        DailyStockData(
            ticker="005930",
            name="삼성전자",
            prev_close=1000.0,
            prev_high=1020.0,
            prev_low=980.0,
            prev_change_rate=1.0,
            prev_volume=100,
            volume_surge=2.0,
        )
    ]
    notifications = []
    engine = SimpleNamespace(
        market_data=SimpleNamespace(
            get_current_price=lambda t: MarketData(ticker=t, price=1000.0, volume=100)
        ),
        order_client=FakeOrderClient(),
        risk_manager=SimpleNamespace(
            approve=lambda *a, **kw: True,
            record_order=lambda *a, **kw: None,
            take_profit_ratio=0.005,
            stop_loss_ratio=0.02,
            commission_rate=0.00015,
            tax_rate=0.0018,
            slippage_rate=0.001,
            simple_take_profit_enabled=False,
            take_profit_enabled=True,
        ),
        note_open_position=lambda ticker: None,
        last_price=lambda ticker: 0.0,
        position_snapshot=lambda: [],
        notify=notifications.append,
        unsellable_snapshot=lambda: [],
    )
    return DailyWorkflow(
        collector=SimpleNamespace(collect=lambda: daily_data, market_data=FakeMarketData()),
        recommender=SimpleNamespace(
            recommend=lambda d: [_recommendation()], prompt_version=PROMPT_TEMPLATE_VERSION
        ),
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(
            get_cash=lambda: 12_000_000,
            get_positions=lambda: {},
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=12_000_000, cash=12_000_000),
        ),
        trade_store=TradeStore(db_path=tmp_path / "t.db"),
        email=email,
        reviewer=FakeReviewer(),
    )


def test_recommend_and_notify_saves_recommendations(tmp_path):
    """추천 메일에 실린 것과 같은 목록이 DB에 남는다."""
    workflow = build_workflow(tmp_path)
    workflow.recommend_and_notify(date(2026, 9, 3))

    rows = workflow.trade_store.recommendations_for(date(2026, 9, 3))
    assert [row.ticker for row in rows] == ["005930"]
    assert rows[0].prompt_version == PROMPT_TEMPLATE_VERSION
    assert rows[0].outlook == "오전 중 회복 시도"


def test_recommend_and_notify_survives_save_failure(tmp_path):
    """기록 저장이 실패해도 추천 메일은 나가고 매수 흐름이 멈추지 않는다."""
    workflow = build_workflow(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    workflow.trade_store.save_recommendations = boom
    workflow.recommend_and_notify(date(2026, 9, 3))

    assert workflow.email.sent, "추천 메일이 나가야 한다"


def test_recommend_and_notify_saves_the_file_prompt_version(tmp_path):
    """저장되는 prompt_version은 코드 상수가 아니라 recommender가 실제로 쓴 버전이다."""
    workflow = build_workflow(tmp_path)
    workflow.recommender.prompt_version = "v99"

    workflow.recommend_and_notify(date(2026, 9, 8))

    rows = workflow.trade_store.recommendations_for(date(2026, 9, 8))
    assert rows[0].prompt_version == "v99"


def _metrics():
    return TodayMetrics(
        ticker="005930", high=72_000.0, low=69_500.0, close=71_000.0, change_rate=1.43
    )


def test_review_recommendations_saves_outcome_and_sends_mail(tmp_path):
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.reviewer.result = {"005930": "오전 회복 시도는 맞았습니다."}

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close == 71_000.0
    assert row.buy_target_hit is True     # 저가 69,500 <= 목표 70,000
    assert row.sell_target_hit is True    # 고가 72,000 >= 목표 71,400
    assert row.review == "오전 회복 시도는 맞았습니다."
    assert "추천 검증" in workflow.email.sent[-1][0]


def test_review_recommendations_does_nothing_without_recommendations(tmp_path):
    workflow = build_workflow(tmp_path)
    workflow.review_recommendations(date(2026, 9, 3))
    assert workflow.email.sent == []


def test_review_recommendations_survives_candle_lookup_failure(tmp_path):
    """당일 봉을 못 받은 종목은 실제값을 비워 두고 나머지 흐름은 계속한다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {}   # 전부 None

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close is None
    assert "당일 봉 조회 실패" in workflow.email.sent[-1][1]


def test_review_recommendations_sends_mail_when_llm_fails(tmp_path):
    """평가 호출이 실패해도 수치까지는 저장하고 메일은 나간다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.reviewer.result = None

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close == 71_000.0
    assert row.review == ""
    assert workflow.email.sent, "평가가 없어도 검증 메일은 나가야 한다"


def test_review_recommendations_skips_llm_for_unverified_stock(tmp_path):
    """실제값이 없는 종목은 대조할 것이 없으므로 평가 입력에서 빠진다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {}

    workflow.review_recommendations(day)

    assert workflow.reviewer.calls == [], "평가 호출 자체가 없어야 한다"


def test_review_recommendations_survives_review_call_raising(tmp_path):
    """평가 호출 자체가 예외로 죽어도(예: 포맷 문자열이 None을 만나 TypeError) 검증
    메일은 나가야 한다 — LLMReviewer.review는 build_review_user_prompt를 try 밖에서
    부르므로 그 안에서 던진 예외가 그대로 새어나올 수 있다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}

    def boom(items, timeout_seconds=120.0):
        raise TypeError("unsupported format string passed to NoneType.__format__")

    workflow.reviewer.review = boom
    workflow.review_recommendations(day)

    assert workflow.email.sent, "평가 호출이 실패해도 검증 메일은 나가야 한다"


def test_fill_reviews_skips_when_any_actual_field_missing(tmp_path):
    """actual_close만 있고 나머지 실제값이 없는 행은 평가 입력에서 빠져야 한다 —
    ReviewInput이 네 수치를 모두 쓰므로, 하나라도 없으면 build_review_user_prompt의
    포맷 문자열이 TypeError로 죽는다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    row = RecommendationRow(
        day=day,
        ticker="005930",
        name="삼성전자",
        prompt_version="v11",
        recommend_price=70_500.0,
        target_price=70_000,
        target_sell_price=71_400,
        setup="rebound",
        reason="전일 등락률 +2.15%",
        outlook="오전 중 회복 시도",
        actual_high=None,
        actual_low=None,
        actual_close=71_000.0,
        actual_change_rate=None,
    )

    workflow._fill_reviews(day, [row])

    assert workflow.reviewer.calls == [], "실제값이 일부만 있는 행은 평가 대상이 아니다"


def test_review_recommendations_survives_candle_lookup_exception(tmp_path):
    """한 종목의 일봉 조회가 예외로 터져도 나머지 종목의 검증은 계속된다.

    today_metrics를 비워 None을 돌려주는 것(존재하는 다른 테스트)과는 다른 분기다 —
    여기서는 get_today_metrics 자체가 raise한다.
    """
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    boom = _recommendation(ticker="000660", name="SK하이닉스")
    ok = _recommendation(ticker="005930", name="삼성전자")
    workflow.trade_store.save_recommendations(day, [boom, ok], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.collector.market_data.raise_for = {"000660"}

    workflow.review_recommendations(day)

    rows = {row.ticker: row for row in workflow.trade_store.recommendations_for(day)}
    assert rows["000660"].actual_close is None
    assert rows["005930"].actual_close == 71_000.0
    assert workflow.email.sent, "조회 실패 종목이 있어도 메일은 나가야 한다"


def test_review_recommendations_leaves_zero_close_unfilled(tmp_path):
    """당일 봉 종가가 0으로 온 경우도 조회 실패와 같이 취급해 값을 채우지 않는다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {
        "005930": TodayMetrics(ticker="005930", high=72_000.0, low=69_500.0, close=0.0, change_rate=0.0)
    }

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close is None


def test_review_recommendations_leaves_blank_high_or_low_unfilled(tmp_path):
    """고가·저가가 빈 응답(0.0)으로 와도 종가가 멀쩡하면 그 0.0을 실제 저가처럼 저장해서는
    안 된다 — market_data.to_float("")가 0.0을 돌려주는 것과 조회 실패를 구분하지 못하면
    '미도달 (매수 무산)'이 사실인 것처럼 메일에 찍힌다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {
        "005930": TodayMetrics(ticker="005930", high=0.0, low=69_500.0, close=71_000.0, change_rate=1.43)
    }

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close is None


def test_review_recommendations_survives_outcome_save_failure(tmp_path):
    """실제 움직임 저장이 실패해도 흐름이 멈추지 않고 메일은 나간다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    workflow.trade_store.save_recommendation_outcome = boom
    workflow.review_recommendations(day)

    assert workflow.email.sent, "저장이 실패해도 검증 메일은 나가야 한다"


def test_review_recommendations_survives_review_save_failure(tmp_path):
    """평가문 저장이 실패해도 흐름이 멈추지 않고 메일은 나간다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.reviewer.result = {"005930": "오전 회복 시도는 맞았습니다."}

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    workflow.trade_store.save_recommendation_review = boom
    workflow.review_recommendations(day)

    assert workflow.email.sent, "평가문 저장이 실패해도 검증 메일은 나가야 한다"


def test_review_recommendations_without_reviewer_saves_numbers_and_sends_mail(tmp_path):
    """reviewer가 None이면 LLM을 부르지 않고도 수치는 저장하고 메일은 나간다."""
    workflow = build_workflow(tmp_path)
    workflow.reviewer = None
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close == 71_000.0
    assert row.review == ""
    assert workflow.email.sent, "reviewer가 없어도 검증 메일은 나가야 한다"


def test_review_recommendations_leaves_unmatched_ticker_review_blank(tmp_path):
    """reviewer가 일부 종목만 평가해 돌려주면, 나머지 종목의 review는 빈 채로 남는다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    first = _recommendation(ticker="005930", name="삼성전자")
    second = _recommendation(ticker="000660", name="SK하이닉스", target_price=100_000, target_sell_price=102_000)
    workflow.trade_store.save_recommendations(day, [first, second], "v11")
    workflow.collector.market_data.today_metrics = {
        "005930": _metrics(),
        "000660": TodayMetrics(ticker="000660", high=103_000.0, low=99_000.0, close=101_000.0, change_rate=1.0),
    }
    workflow.reviewer.result = {"005930": "오전 회복 시도는 맞았습니다."}  # 000660은 빠져 있다

    original_save_review = workflow.trade_store.save_recommendation_review
    saved_tickers = []

    def spy_save_review(day, ticker, review):
        saved_tickers.append(ticker)
        original_save_review(day, ticker, review)

    workflow.trade_store.save_recommendation_review = spy_save_review

    workflow.review_recommendations(day)

    rows = {row.ticker: row for row in workflow.trade_store.recommendations_for(day)}
    assert rows["005930"].review == "오전 회복 시도는 맞았습니다."
    assert rows["000660"].review == ""
    assert saved_tickers == ["005930"], "평가를 받지 못한 종목은 저장을 부르지 않는다"
