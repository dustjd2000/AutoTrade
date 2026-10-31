"""2호 전략(지수 ETF 국면 추종)의 아침 결정 분기 (PRD 5.5-C).

1호와 갈리는 곳은 추천 단계와 15:35 검증뿐이다 — 매수·청산은 같은 경로라 여기서 다시 보지 않는다.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.core import daily_workflow
from src.core.daily_workflow import DailyWorkflow
from src.core import actions
from src.core.events import MarketData, OrderResult, OrderStatus, OrderType
from src.strategy.etf_regime import INVERSE_ETF, LONG_ETF, ETFRegimeStrategy

TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_workflow, "DEFAULT_REPORT_MARK_PATH", tmp_path / "final_report_sent")
    monkeypatch.setattr(daily_workflow, "DEFAULT_BUY_RECORDS_PATH", tmp_path / "buy_records.json")


    # 매수 기록 파일은 date.today()로 날짜를 찍는다 — 실제 날짜가 TODAY와 다르면 '오늘 것이 아니다'로 무시된다
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(daily_workflow, "date", FixedDate)


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None, images=None):
        self.sent.append((subject, message))


def candles(change_percent, days=20, traded=True):
    """오늘 봉 + 지난 `days`거래일 봉 (장중 수익률이 매일 `change_percent`%)."""
    rows = [{"dt": TODAY.strftime("%Y%m%d"), "open_pric": "10000", "close_pric": "10000",
             "trde_qty": "500" if traded else "0"}]
    for i in range(1, days + 1):
        day = TODAY - timedelta(days=i)
        rows.append({
            "dt": day.strftime("%Y%m%d"),
            "open_pric": "10000",
            "close_pric": f"{10000 * (1 + change_percent / 100):.0f}",
            "trde_qty": "1000",
        })
    return rows


class FakeMarketData:
    def __init__(self, rows, price=30000.0):
        self.rows = rows
        self.price = price
        self.ohlcv_calls = []
        self.price_calls = []

    def get_ohlcv(self, ticker, period="D", count=100, base_date=None):
        self.ohlcv_calls.append((ticker, base_date))
        return list(self.rows)

    def get_current_price(self, ticker):
        self.price_calls.append(ticker)
        return MarketData(ticker=ticker, price=self.price, volume=100)


class FakeOrderClient:
    def __init__(self, status=OrderStatus.PENDING, error=None):
        self.orders = []
        self.status = status
        self.error = error

    def send_order(self, request):
        self.orders.append(request)
        return OrderResult(order_id=str(len(self.orders)), ticker=request.ticker, side=request.side,
                           status=self.status, quantity=request.quantity, error_message=self.error)


def make_workflow(rows, returns=(), kill_percent=5.0, start=None, cash=1_000_000, price=30000.0,
                  order_status=OrderStatus.PENDING, order_error=None, band=0.0, flip=True,
                  get_positions=lambda: {}, risk_manager=None):
    email = FakeEmail()
    notifications, closed = [], []
    market_data = FakeMarketData(rows, price=price)
    order_client = FakeOrderClient(order_status, order_error)
    opened = []
    strategy = ETFRegimeStrategy(investable_ratio=0.5)
    store_calls = []
    recorded = []

    def closed_trade_returns(start_day, up_to, tickers):
        store_calls.append((start_day, up_to, tuple(tickers)))
        return list(returns)

    engine = SimpleNamespace(
        market_data=market_data,
        notify=notifications.append,
        note_market_closed=closed.append,
        order_client=order_client,
        risk_manager=risk_manager or SimpleNamespace(
            approve=lambda *a, **kw: True, record_order=lambda *a, **kw: None),
        note_open_position=opened.append,
    )
    workflow = DailyWorkflow(
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(get_cash=lambda: cash, get_positions=get_positions),
        trade_store=SimpleNamespace(
            closed_trade_returns=closed_trade_returns,
            record_fill=recorded.append,
            # 실제 TradeStore.has_buy_order처럼 거부되지 않은 매수 기록만 본다
            has_buy_order=lambda day, tickers: any(
                r.ticker in tickers and r.status != OrderStatus.REJECTED for r in recorded
            ),
        ),
        email=email,
        etf_kill_loss_percent=kill_percent,
        etf_start_date=start,
        signal_band_percent=band,
        flip_enabled=flip,
    )
    return SimpleNamespace(
        workflow=workflow, email=email, notifications=notifications, closed=closed,
        market_data=market_data, strategy=strategy, store_calls=store_calls,
        order_client=order_client, opened=opened,
    )


def test_rising_regime_buys_kodex200_at_the_current_price():
    t = make_workflow(candles(0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    recs = t.strategy._recommendations
    assert [(r.ticker, r.target_price, r.recommend_price) for r in recs] == [(LONG_ETF[0], 30000, 30000.0)]
    assert t.market_data.price_calls == [LONG_ETF[0]]
    assert len(t.email.sent) == 1 and "KODEX 200 시장가 매수" in t.email.sent[0][0]


def test_falling_regime_buys_the_inverse_etf():
    t = make_workflow(candles(-0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [INVERSE_ETF[0]]


def test_inverse_signal_flips_to_long_when_the_index_is_up_today():
    """10/28 — 인버스가 -3% 맞은 날 같은 신호로 인버스를 다시 샀다. 오늘 지수가 올랐으면 뒤집는다."""
    rows = candles(-0.5)
    rows[0]["close_pric"] = "10050"  # 오늘 시가 대비 +0.5%

    t = make_workflow(rows)
    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]
    assert "뒤집었다" in t.strategy._recommendations[0].reason


def test_signal_reads_two_queries_so_twenty_past_days_are_available():
    """한 번 조회가 20거래일치라 당일 봉이 섞이면 19개뿐이다 — 기준일을 하루 당긴 조회를 함께 한다."""
    t = make_workflow(candles(0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.market_data.ohlcv_calls == [
        (LONG_ETF[0], TODAY), (LONG_ETF[0], TODAY - timedelta(days=1)),
    ]


def test_weak_signal_still_buys():
    t = make_workflow(candles(0.1))

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]


def test_no_signal_rests_and_says_so():
    t = make_workflow(candles(0.5, days=10))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert t.market_data.price_calls == []
    assert "쉽니다" in t.email.sent[0][0]


def test_short_history_rests_instead_of_guessing():
    t = make_workflow(candles(0.5, days=10))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []


def test_no_trade_today_marks_market_closed():
    t = make_workflow(candles(0.5, traded=False))

    t.workflow.recommend_and_notify(today=TODAY)

    assert len(t.closed) == 1
    assert t.strategy._recommendations == []
    assert t.email.sent == []


def test_kill_switch_stops_buying_when_cumulative_loss_reaches_the_limit():
    t = make_workflow(candles(0.5), returns=[-2.0, -1.5, -1.5], start=date(2026, 10, 1))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert t.market_data.ohlcv_calls == []
    assert "자동 중단" in t.email.sent[0][0]
    assert "ETF_START_DATE" in t.email.sent[0][1]
    assert t.store_calls == [(date(2026, 10, 1), TODAY, ("069500", "114800"))]


def test_kill_switch_lets_trading_continue_above_the_limit():
    t = make_workflow(candles(0.5), returns=[-2.0, -1.5, 1.0])

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]


def test_kill_switch_zero_means_off():
    t = make_workflow(candles(0.5), returns=[-50.0], kill_percent=0)

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.store_calls == []
    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]


# ── 매수: 결정 즉시 시장가 (2026-10-23) ─────────────────────────
def test_buy_sends_a_market_order_right_after_the_decision():
    t = make_workflow(candles(-0.5), cash=1_000_000, price=1250.0)
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    [order] = t.order_client.orders
    assert order.ticker == INVERSE_ETF[0]
    assert order.order_type == OrderType.MARKET and order.price is None
    assert order.quantity == 400          # 1,000,000 × 50% ÷ 1,250
    assert t.opened == [INVERSE_ETF[0]]


def test_buy_is_not_repeated_once_ordered_today():
    """매수 시각 안전망이 같은 날 두 번 사지 않는다 — 매수 기록 파일이 표시다."""
    t = make_workflow(candles(0.5))
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)
    t.workflow._execute_etf_buy(TODAY)

    assert len(t.order_client.orders) == 1


def test_buy_is_not_repeated_after_the_result_mail_cleared_the_record_file():
    """2026-10-30 — 09:10 매수가 곧 체결돼 결과 메일이 먼저 나가며 기록 파일을 지웠고, 09:13
    안전망이 같은 ETF를 한 번 더 샀다. DB 매수 기록이 남아 있으면 사지 않는다."""
    t = make_workflow(candles(0.5))
    t.workflow.recommend_and_notify(today=TODAY)
    t.workflow._execute_etf_buy(TODAY)

    t.workflow._clear_buy_records()       # cancel_unfilled_buys가 결과 메일을 보내며 하는 일
    t.workflow._execute_etf_buy(TODAY)

    assert len(t.order_client.orders) == 1


def test_buy_is_skipped_when_an_etf_is_already_held():
    """매수 주문이 모호하게 실패해 REJECTED로 기록돼도 실제로는 체결됐을 수 있다 — 보유를 본다."""
    held = {LONG_ETF[0]: SimpleNamespace(quantity=10)}
    t = make_workflow(candles(0.5), get_positions=lambda: held)
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.order_client.orders == []


def test_buy_is_skipped_when_the_balance_cannot_be_read():
    def broken():
        raise RuntimeError("잔고 조회 실패")

    t = make_workflow(candles(0.5), get_positions=broken)
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.order_client.orders == []
    assert any("[경고]" in n for n in t.notifications)


def test_decision_mail_lists_the_live_exit_rules():
    rm = SimpleNamespace(
        approve=lambda *a, **kw: True, record_order=lambda *a, **kw: None,
        stop_loss_ratio=0.02, take_profit_ratio=0.02,
        trail_activate_ratio=0.005, trail_giveback_ratio=0.0025,
    )
    t = make_workflow(candles(0.5), risk_manager=rm)

    t.workflow.recommend_and_notify(today=TODAY)

    body = t.email.sent[0][1]
    assert "손절 -2%" in body and "익절 +2%" in body
    assert "고점 반납 0.25%p(고점 +0.5% 이상)" in body
    assert "AI 매도 판단" in body and "AI 매도 판단 없음" not in body


def test_buy_decides_again_when_the_decision_was_lost_by_a_restart():
    """2026-10-23 09:07 재시작으로 결정이 사라져 매수가 빠졌다 — 안전망이 다시 정해서 산다."""
    t = make_workflow(candles(0.5))

    t.workflow._execute_etf_buy(TODAY)    # recommend_and_notify를 거치지 않은 새 엔진

    assert [o.ticker for o in t.order_client.orders] == [LONG_ETF[0]]


def test_rest_day_is_not_decided_twice():
    t = make_workflow(candles(0.5, days=10))
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.order_client.orders == []
    assert len(t.email.sent) == 1         # '쉽니다' 메일은 한 번만


def test_quantity_leaves_room_for_the_market_order_margin():
    """시장가 매수 증거금은 상한가(+30%) 기준이라, 투입 비율이 높으면 그 한도가 수량을 정한다."""
    t = make_workflow(candles(0.5), cash=1_300_000, price=10000.0)
    t.workflow.strategy.investable_ratio = 1.0
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.order_client.orders[0].quantity == 100   # 1,300,000 ÷ (10,000 × 1.3)


def test_rejected_order_can_be_retried_by_the_safety_net():
    t = make_workflow(candles(0.5), order_status=OrderStatus.REJECTED, order_error="잔고 부족")
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)
    t.workflow._execute_etf_buy(TODAY)

    assert len(t.order_client.orders) == 2
    assert any("매수 거부" in n for n in t.notifications)


def test_recommend_action_chains_the_buy_in_etf_mode():
    """2호는 결정 직후 같은 실행에서 산다 — 매수 단계는 루프 스레드(touches_orders)에서 돈다."""
    t = make_workflow(candles(0.5))
    runtime = SimpleNamespace(workflow=t.workflow)

    steps = actions.manual_steps(runtime, "recommend")

    assert [s.touches_orders for s in steps] == [False, True]




def test_weak_signal_rests_inside_the_band_and_says_why():
    t = make_workflow(candles(0.1), band=0.2)

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert "쉽니다" in t.email.sent[0][0]
    assert "문턱" in t.email.sent[0][1]


def test_flip_can_be_disabled():
    rows = candles(-0.5)
    rows[0]["close_pric"] = "10050"          # 오늘 시가 대비 +0.5% — 뒤집기 조건
    t = make_workflow(rows, flip=False)

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [INVERSE_ETF[0]]
