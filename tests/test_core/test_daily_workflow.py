from datetime import date
from types import SimpleNamespace

from src.core.daily_workflow import DailyWorkflow
from src.core.events import FillRecord, MarketData, OrderResult, OrderSide, OrderStatus
from src.data.collector import DailyStockData
from src.llm.recommender import StockRecommendation
from src.logger.trade_store import DailySummary, MonthlySummary, TradeRow
from src.risk.manager import exit_trigger_price
from src.strategy.llm_momentum import LLMMomentumStrategy


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None):
        self.sent.append((subject, message, html))


class FakeOrderClient:
    def __init__(self):
        self.orders = []
        self.fills = []

    def send_order(self, request):
        self.orders.append(request)
        return OrderResult(
            order_id="1",
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.FILLED,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_price=1000.0,
        )

    def get_today_fills(self):
        return self.fills


def make_workflow(recommendations=None, collected=True, cash=12_000_000):
    strategy = LLMMomentumStrategy()
    email = FakeEmail()
    order_client = FakeOrderClient()
    notifications = []

    daily_data = (
        [DailyStockData(ticker="005930", name="삼성전자", change_rate=1.0, volume=100, gap_rate=0.5)]
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
            take_profit_ratio=0.02,
            stop_loss_ratio=0.02,
            commission_rate=0.00015,
            tax_rate=0.0018,
            slippage_rate=0.001,
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
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="외국인 순매수")]
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
        StockRecommendation(ticker="005930", name="삼성전자", reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", reason="b"),
        StockRecommendation(ticker="035420", name="NAVER", reason="c"),
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
        StockRecommendation(ticker="005930", name="삼성전자", reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", reason="b"),
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
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
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


def test_buy_resets_final_report_flag():
    """매수로 보유가 다시 생기면 앞서 보낸 최종 리포트는 더 이상 최종이 아니다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow._final_report_on = REPORT_DAY

    workflow.execute_buys()

    assert workflow._final_report_on is None


def test_rejected_buy_keeps_final_report_flag():
    """매수가 전부 거부돼 보유가 생기지 않았다면 앞서 보낸 리포트가 여전히 최종이다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
    workflow, _, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow._final_report_on = REPORT_DAY
    order_client.send_order = lambda request: OrderResult(
        order_id="1",
        ticker=request.ticker,
        side=request.side,
        status=OrderStatus.REJECTED,
        quantity=request.quantity,
        error_message="CB 발동중입니다. 취소주문만 가능합니다.",
    )

    workflow.execute_buys()

    assert workflow._final_report_on == REPORT_DAY


def test_note_open_position_is_called_for_ordered_stock():
    """감시 대상 표시가 빠지면 매수 직후 창을 닫아도 경고가 뜨지 않는다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
    workflow, _, _, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    noted = []
    workflow.engine.note_open_position = noted.append

    workflow.execute_buys()

    assert noted == ["005930"]


# ── 09:00 매수 결과 메일 ────────────────────────────────────
def run_buys(recommendations, cash=12_000_000, fills=None, fill_error=None):
    workflow, email, order_client, notifications, strategy = make_workflow(
        recommendations=recommendations, cash=cash
    )
    strategy.set_recommendations(recommendations)
    order_client.fills = fills or []
    if fill_error:
        order_client.get_today_fills = lambda: (_ for _ in ()).throw(fill_error)

    workflow.execute_buys()
    return email, order_client, notifications


def test_buy_result_email_lists_ordered_stocks():
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", reason="a"),
        StockRecommendation(ticker="000660", name="SK하이닉스", reason="b"),
    ]
    email, _, _ = run_buys(recs)

    assert len(email.sent) == 1
    subject, body, html = email.sent[0]
    assert "매수 실행 결과 2/2종목" in subject
    assert "(005930)삼성전자" in body and "(000660)SK하이닉스" in body
    assert "2,000" in body                    # 수량 2000주
    assert "총 투입금액" in body
    assert "4,000,000원" in body               # 2종목 × 200만
    tp_price = exit_trigger_price(1000.0, 0.02, 0.00015, 0.0018, 0.001)
    sl_price = exit_trigger_price(1000.0, -0.02, 0.00015, 0.0018, 0.001)
    assert f"{tp_price:,.0f}" in body and f"{sl_price:,.0f}" in body  # 순손익 반영 익절/손절 라인
    assert "<table" in html


def test_buy_result_email_shows_filled_price_when_fill_is_already_known():
    """시장가 매수는 곧바로 체결되므로, 조회로 잡히면 접수가 아니라 체결가로 알린다."""
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
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
    assert "접수'는 주문이" not in body  # 접수 상태가 없으면 그 주의 문구도 붙지 않는다


def test_buy_result_email_marks_partial_fill():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
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
    email, _, _ = run_buys(recs, fills=fills)

    _, body, _ = email.sent[0]
    assert "부분체결" in body
    assert "1,500" in body


def test_buy_result_email_is_sent_even_when_fill_query_fails():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
    email, _, _ = run_buys(recs, fill_error=RuntimeError("조회 실패"))

    _, body, _ = email.sent[0]
    assert "체결 내역 조회에 실패" in body
    assert "접수" in body
    assert "1,000" in body  # 체결가를 모르면 산정 기준가로 표기


def test_buy_result_email_lists_stocks_that_were_not_bought():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", reason="a")]
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

    subject, body, html = email.sent[0]
    assert "매수 실행 결과 0/1종목" in subject
    assert "매수하지 못한 종목" in body
    assert "CB 발동중" in body and "CB 발동중" in html


def test_buy_result_email_not_sent_without_buy_plans():
    """추천이 없어 매수 자체를 시도하지 않았으면 08:45 스킵 알림으로 충분하다."""
    email, _, _ = run_buys([])

    assert email.sent == []


def test_buy_skipped_when_one_share_exceeds_allocation():
    """삼성바이오로직스처럼 1주 가격이 종목당 배정액을 넘으면 주문하지 않는다.

    건너뜀은 별도 알림 메일을 보내지 않고 매수 실행 결과 메일에만 싣는다.
    """
    recs = [StockRecommendation(ticker="207940", name="삼성바이오로직스", reason="a")]
    workflow, email, order_client, notifications, strategy = make_workflow(
        recommendations=recs, cash=2_000_000
    )
    strategy.set_recommendations(recs)
    # 종목당 배정 = 2,000,000 × 1/6 ≈ 333,333원 < 1주 1,000,000원
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=1_000_000.0, volume=1)
    )

    workflow.execute_buys()

    assert order_client.orders == []
    assert not any("건너뜀" in n for n in notifications)

    _, body, _ = email.sent[0]
    assert "매수하지 못한 종목" in body
    assert "1주 1,000,000원이 배정액 333,333원을 초과" in body
