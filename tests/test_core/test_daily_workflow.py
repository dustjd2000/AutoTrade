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
)
from src.logger.trade_store import DailySummary, MonthlySummary, TradeRow
from src.strategy.base import StockRecommendation
from src.strategy.etf_regime import ETFRegimeStrategy


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


def make_workflow(recommendations=None, cash=12_000_000):
    """오늘 결정이 끝난 상태의 워크플로 — `recommendations`가 그날 살 종목이다.

    결정 단계(KODEX 200 일봉 조회)는 `test_etf_workflow.py`가 본다. 여기서는 결정을 마친
    것으로 표시해(`_etf_decided_on`) 매수·취소·메일·표 같은 공용 흐름만 본다. 투입 비율
    1/6은 예수금 1,200만 원 → 배정 200만 원 → 현재가 1,000원 × 2,000주가 되게 고른 값이다.
    """
    strategy = ETFRegimeStrategy(investable_ratio=1 / 6)
    if recommendations:
        strategy.set_recommendations(recommendations)
    email = FakeEmail()
    order_client = FakeOrderClient()
    notifications = []

    engine = SimpleNamespace(
        market_data=SimpleNamespace(
            get_current_price=lambda t: MarketData(ticker=t, price=1000.0, volume=100)
        ),
        order_client=order_client,
        risk_manager=SimpleNamespace(
            approve=lambda *a, **kw: True,
            record_order=lambda *a, **kw: None,
            stop_loss_ratio=0.02,
            take_profit_ratio=0.0,
            commission_rate=0.00015,
            tax_rate=0.0018,
            slippage_rate=0.001,
        ),
        note_open_position=lambda ticker: None,
        last_price=lambda ticker: 0.0,
        # 매수 예정 표가 접수 행을 잔고와 대조한다 (_settled_row) — 기본은 '아직 미체결'
        position_snapshot=lambda: [],
        notify=notifications.append,
        unsellable_snapshot=lambda: [],
    )

    workflow = DailyWorkflow(
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(
            get_cash=lambda: cash,
            get_positions=lambda: {},
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=cash, cash=cash),
        ),
        trade_store=SimpleNamespace(record_fill=lambda *a, **kw: None, has_buy_order=lambda *a: False),
        email=email,
    )
    workflow._etf_decided_on = date.today()
    return workflow, email, order_client, notifications, strategy


def set_price(workflow, price: float) -> None:
    """매수 직전 현재가를 정한다 — 시장가 수량이 이 값으로 정해진다."""
    workflow.engine.market_data = SimpleNamespace(
        get_current_price=lambda t: MarketData(ticker=t, price=price, volume=100)
    )


def fill_board(workflow, recommendations, day) -> None:
    """결정 직후의 '매수 예정' 표를 채운다 — 결정 단계(일봉 조회) 없이 표만 본다."""
    workflow._set_buy_board(day, workflow._board_from_recommendations(recommendations))


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


def test_buy_result_email_not_sent_without_buy_plans():
    """추천이 없어 매수 자체를 시도하지 않았으면 추천 스킵 알림으로 충분하다."""
    email, _, _ = run_buys([])

    assert email.sent == []


def test_buy_skipped_when_one_share_exceeds_allocation():
    """1주 현재가가 배정액을 넘으면 주문하지 않는다 (시장가라 수량은 현재가로 정한다).

    건너뜀은 별도 알림 메일을 보내지 않고 매수 예정 표에만 남긴다.
    """
    # 배정 = 2,000,000 × 1/6 ≈ 333,333원 < 1주 1,000,000원
    recs = [
        StockRecommendation(
            ticker="207940", name="삼성바이오로직스", target_price=1_000_000, reason="a"
        )
    ]
    workflow, email, order_client, notifications, strategy = make_workflow(
        recommendations=recs, cash=2_000_000
    )
    set_price(workflow, 1_000_000.0)

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


# 매도예상가(BuyPlanView.sell_price, 익절선을 역산한 값)는 2026-09-18에 걷어냈다 —
# 익절 자동 청산이 없는 지금 "이 종목 혼자였다면 익절선이 어디인지" 보여주는 값이
# 의미가 없어졌다. 그 값을 확인하던 테스트도 함께 지운다.


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

    fill_board(workflow, board_recs(), BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.current_price == 1_050.0


def test_buy_plan_current_price_is_blank_before_the_first_tick():
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())

    fill_board(workflow, board_recs(), BOARD_DAY)

    (plan,) = workflow.buy_plan_snapshot(today=BOARD_DAY)
    assert plan.current_price == 0.0


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
    fill_board(workflow, recs, BOARD_DAY)

    workflow.drop_buy_plans(["005930"], today=BOARD_DAY)

    assert workflow.buy_plan_snapshot(today=BOARD_DAY) == []
    workflow.execute_buys()
    assert order_client.orders == []


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
    set_price(workflow, 9_000_000.0)
    workflow.execute_buys()  # 1주 가격이 배정액을 넘어 '건너뜀'
    (plan,) = workflow.buy_plan_snapshot()
    assert plan.status == "건너뜀"

    assert workflow.drop_buy_plans(["005930"]) == []

    assert order_client.cancelled == []

