from datetime import date
from types import SimpleNamespace

import pytest

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
from src.llm.recommender import StockRecommendation
from src.logger.trade_store import DailySummary, MonthlySummary, TradeRow
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

    def send(self, subject, message, html=None):
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
        notify=notifications.append,
        unsellable_snapshot=lambda: [],
    )

    workflow = DailyWorkflow(
        collector=SimpleNamespace(collect=lambda: daily_data),
        recommender=SimpleNamespace(recommend=lambda d: recommendations),
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(
            get_cash=lambda: cash,
            get_positions=lambda: {},
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=cash, cash=cash),
        ),
        trade_store=SimpleNamespace(record_fill=lambda *a, **kw: None),
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
