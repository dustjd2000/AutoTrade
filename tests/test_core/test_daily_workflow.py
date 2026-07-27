from datetime import date
from types import SimpleNamespace

from src.core.daily_workflow import DailyWorkflow
from src.core.events import MarketData, OrderResult, OrderSide, OrderStatus
from src.data.collector import DailyStockData
from src.llm.recommender import StockRecommendation
from src.strategy.llm_momentum import LLMMomentumStrategy


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message):
        self.sent.append((subject, message))


class FakeOrderClient:
    def __init__(self):
        self.orders = []

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
        ),
        notify=notifications.append,
    )

    workflow = DailyWorkflow(
        collector=SimpleNamespace(collect=lambda: daily_data),
        recommender=SimpleNamespace(recommend=lambda d: recommendations),
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(
            get_cash=lambda: cash,
            get_positions=lambda: {},
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=cash),
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
    subject, body = email.sent[0]
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
