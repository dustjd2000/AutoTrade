from datetime import date, datetime

from src.core.events import BuyExecution, BuyOutcome, BuyRecord
from src.logger.trade_store import DailySummary, MonthlySummary, TradeRow
from src.notification import templates

DAY = date(2026, 7, 29)


def make_summary(**overrides):
    """2026-07-29 실제 매매 — 카카오 익절, LG디스플레이 손절."""
    trades = [
        TradeRow("035720", "카카오", 5, 36300.0, 37000.0, 3500.0, fees=409.0),
        TradeRow("034220", "LG디스플레이", 22, 9080.0, 8890.0, -4180.0, fees=430.0),
    ]
    defaults = dict(
        day=DAY,
        buy_count=2,
        sell_count=2,
        realized_pnl=-680.0,
        trades=trades,
        cost=381260.0,
        fees=839.0,
    )
    defaults.update(overrides)
    return DailySummary(**defaults)


def make_monthly(**overrides):
    """누적 실현손익 +12,340원, 수수료·세금 1,258원 → 순손익 +11,082원."""
    defaults = dict(realized_pnl=12340.0, fees=1258.0, base_asset=1_386_517.0)
    defaults.update(overrides)
    return MonthlySummary(**defaults)


def render(summary=None, monthly=None, cash=1220735.0, sync_failed=False):
    return templates.daily_report_email(
        summary or make_summary(),
        monthly or make_monthly(),
        cash,
        sync_failed=sync_failed,
    )


def test_subject_and_parts():
    subject, text, html = render()

    assert subject == "[AutoTrade] 2026-07-29 매매 결과 리포트"
    assert "<table" in html
    assert "<table" not in text


def test_text_table_lists_each_trade_with_return():
    _, text, _ = render()

    assert "(035720)카카오" in text
    assert "36,300" in text and "37,000" in text
    assert "+3,500원" in text and "+1.93%" in text
    assert "-4,180원" in text and "-2.09%" in text


def test_totals_include_fees_and_net_return():
    _, text, html = render()

    for body in (text, html):
        assert "381,260" in body          # 투입원가
        assert "-680원" in body           # 실현손익
        assert "-839원" in body           # 수수료·세금
        assert "-1,519원" in body         # 순손익
        assert "-0.18%" in body           # 실현 수익률
        assert "-0.40%" in body           # 순수익률


def test_profit_and_loss_are_coloured_differently():
    _, _, html = render()

    assert templates.COLOR_PROFIT in html  # 카카오 익절
    assert templates.COLOR_LOSS in html    # LG디스플레이 손절


def test_unclosed_position_shown_without_return():
    summary = make_summary(
        trades=[TradeRow("068270", "셀트리온", 1, 182000.0, None, None, fees=20.0)],
        cost=0.0,
        realized_pnl=0.0,
        sell_count=0,
        buy_count=1,
    )
    _, text, html = render(summary)

    assert "보유중" in text
    assert "보유중" in html


def test_monthly_section_present():
    _, text, html = render()

    for body in (text, html):
        assert "2026-07" in body
        assert "+12,340원" in body
        assert "+0.89%" in body


def test_monthly_section_nets_out_fees():
    """이번 달 실제 차익은 수수료·세금을 뺀 순손익이다."""
    _, text, html = render()

    for body in (text, html):
        assert "-1,258원" in body    # 누적 수수료·세금
        assert "+11,082원" in body   # 누적 순손익 = 12,340 - 1,258
        assert "+0.80%" in body      # 누적 순수익률


def test_monthly_net_loss_after_fees_is_coloured_as_loss():
    """실현손익이 +여도 수수료를 빼면 -가 될 수 있다 — 이때 순손익은 손실색이어야 한다."""
    monthly = make_monthly(realized_pnl=500.0, fees=1258.0)
    _, text, html = render(monthly=monthly)

    assert "-758원" in text
    assert f'color:{templates.COLOR_LOSS};">-758원' in html


def test_monthly_section_shows_cash_without_sign():
    """예수금은 손익이 아니라 잔고라 +/- 부호가 붙으면 안 된다."""
    _, text, html = render(cash=1220735.0)

    for body in (text, html):
        assert "현재 예수금: 1,220,735원" in body
        assert "+1,220,735원" not in body


def test_empty_day():
    summary = make_summary(trades=[], buy_count=0, sell_count=0, realized_pnl=0.0, cost=0.0, fees=0.0)
    _, text, html = render(summary)

    assert "오늘 체결된 매매가 없습니다." in text
    assert "오늘 체결된 매매가 없습니다." in html


def test_sync_failure_and_rejections_are_flagged():
    _, text, html = render(make_summary(rejected_count=2), sync_failed=True)

    for body in (text, html):
        assert "체결 내역 조회에 실패" in body
        assert "주문 실패 2건" in body


# ── 09:00 매수 결과 ─────────────────────────────────────────
BUY_AT = datetime(2026, 7, 30, 9, 0, 12)


def make_execution(**overrides):
    """예수금 500만 → 종목당 833,333원. 하나는 체결, 하나는 접수, 하나는 1주 값이 배정액 초과."""
    records = [
        BuyRecord(
            ticker="005930",
            name="삼성전자",
            outcome=BuyOutcome.FILLED,
            quantity=14,
            reference_price=57000.0,
            filled_quantity=14,
            filled_price=57300.0,
            order_id="1",
        ),
        BuyRecord(
            ticker="000660",
            name="SK하이닉스",
            outcome=BuyOutcome.ORDERED,
            quantity=4,
            reference_price=198000.0,
            order_id="2",
        ),
        BuyRecord(
            ticker="207940",
            name="삼성바이오로직스",
            outcome=BuyOutcome.SKIPPED,
            reference_price=1030000.0,
            note="1주 1,030,000원이 배정액 833,333원을 초과",
        ),
    ]
    defaults = dict(
        at=BUY_AT,
        cash=5_000_000.0,
        amount_per_stock=833_333.0,
        records=records,
        take_profit_percent=2.0,
        stop_loss_percent=2.0,
    )
    defaults.update(overrides)
    return BuyExecution(**defaults)


def render_buys(execution=None):
    return templates.buy_result_email(execution or make_execution())


def test_buy_subject_counts_only_ordered_stocks():
    subject, text, html = render_buys()

    assert subject == "[AutoTrade] 2026-07-30 매수 실행 결과 2/3종목"
    assert "2026-07-30 09:00" in text
    assert "<table" in html
    assert "<table" not in text


def test_buy_table_shows_price_quantity_and_amount():
    _, text, html = render_buys()

    for body in (text, html):
        assert "(005930)삼성전자" in body
        assert "57,300" in body           # 체결가
        assert "802,200" in body          # 57,300 × 14주
        assert "(000660)SK하이닉스" in body
        assert "198,000" in body          # 접수분은 산정 기준가
        assert "792,000" in body


def test_buy_states_are_labelled():
    _, text, _ = render_buys()

    assert "체결" in text
    assert "접수" in text
    assert "건너뜀" in text


def test_buy_exit_lines_are_derived_from_shown_price():
    _, text, _ = render_buys()

    assert "58,446" in text and "56,154" in text    # 삼성전자 ±2%
    assert "201,960" in text and "194,040" in text  # SK하이닉스 ±2%


def test_buy_header_shows_cash_allocation_and_total():
    _, text, html = render_buys()

    for body in (text, html):
        assert "5,000,000원" in body            # 예수금
        assert "833,333원" in body              # 종목당 배정
        assert "예수금의 16.7%" in body
        assert "1,594,200원" in body            # 총 투입금액
        assert "+2.00% / -2.00%" in body


def test_buy_allocation_share_omitted_without_cash():
    """예수금 조회가 0으로 오더라도 0으로 나누지 않는다."""
    _, text, html = render_buys(make_execution(cash=0.0))

    for body in (text, html):
        assert "예수금의" not in body


def test_stocks_not_bought_are_listed_with_reason():
    _, text, html = render_buys()

    for body in (text, html):
        assert "매수하지 못한 종목" in body
        assert "(207940)삼성바이오로직스" in body
        assert "배정액 833,333원을 초과" in body
    assert templates.COLOR_WARN in html


def test_pending_order_note_only_when_something_is_pending():
    _, with_pending, _ = render_buys()
    assert "체결가는 아직 확정되지 않았습니다" in with_pending

    records = [r for r in make_execution().records if r.outcome != BuyOutcome.ORDERED]
    _, all_filled, _ = render_buys(make_execution(records=records))
    assert "체결가는 아직 확정되지 않았습니다" not in all_filled


def test_buy_fill_query_failure_is_flagged():
    _, text, html = render_buys(make_execution(fills_synced=False))

    for body in (text, html):
        assert "체결 내역 조회에 실패" in body


def test_buy_email_without_any_record():
    _, text, html = render_buys(make_execution(records=[]))

    for body in (text, html):
        assert "매수를 시도한 종목이 없습니다." in body
