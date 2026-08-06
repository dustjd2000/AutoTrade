from datetime import date, datetime

from src.core.events import BuyExecution, BuyOutcome, BuyRecord, UnsellableView
from src.llm.recommender import StockRecommendation
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


def render(
    summary=None,
    monthly=None,
    cash=1220735.0,
    sync_failed=False,
    closed_out=False,
    unsellable=None,
):
    return templates.daily_report_email(
        summary or make_summary(),
        monthly or make_monthly(),
        cash,
        sync_failed=sync_failed,
        closed_out=closed_out,
        unsellable=unsellable,
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
        assert "현재 주문가능금액: 1,220,735원" in body
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


def test_closeout_report_replaces_the_market_close_note():
    """전량 매도 직후 보낸 리포트에 '마감 직전 집계'라고 적으면 틀린 설명이 된다."""
    _, text, html = render(closed_out=True)

    for body in (text, html):
        assert "전부 매도한 직후 집계" in body
        assert "정규장 마감(15:30) 직전 집계" not in body


# ── 매도하지 못한 종목 ──────────────────────────────────────
UNSELLABLE_AT = datetime(2026, 7, 29, 15, 20, 3)


def make_unsellable():
    """제외된 건(거래정지 추정)과 재시도 여지가 있는 거부 건을 함께 담는다."""
    return [
        UnsellableView(
            ticker="118970",
            label="(118970)토비스",
            reason="종목 정보가 조회되지 않습니다",
            at=UNSELLABLE_AT,
            excluded=True,
        ),
        UnsellableView(
            ticker="034220",
            label="(034220)LG디스플레이",
            reason="매도 주문 거부: CB 발동중입니다. 취소주문만 가능합니다.",
            at=UNSELLABLE_AT,
        ),
    ]


def test_unsellable_stocks_are_listed_with_reasons():
    _, text, html = render(unsellable=make_unsellable())

    for body in (text, html):
        assert "매도하지 못한 종목" in body
        assert "(118970)토비스" in body
        assert "종목 정보가 조회되지 않습니다" in body
        assert "(034220)LG디스플레이" in body
        assert "CB 발동중" in body


def test_excluded_stock_warns_that_it_is_not_closed_automatically():
    """제외된 건은 계좌에 남는데 자동 청산되지 않으므로 그 사실까지 적어야 한다."""
    _, text, html = render(unsellable=make_unsellable())

    for body in (text, html):
        assert "보유 목록에서 제외되어 자동 청산되지 않습니다" in body
    # 재시도 여지가 있는 거부 건에는 붙지 않는다
    assert text.count("자동 청산되지 않습니다") == 1


def test_no_unsellable_section_when_everything_was_sold():
    _, text, html = render()

    assert "매도하지 못한 종목" not in text
    assert "매도하지 못한 종목" not in html


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
        take_profit_percent=0.5,
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
    """비용이 0인 이 표에서는 익절가 = 단가 +0.5%, 손절가 = 단가 -2%가 그대로 나온다."""
    _, text, _ = render_buys()

    assert "57,586" in text and "56,154" in text    # 삼성전자 +0.5% / -2%
    assert "198,990" in text and "194,040" in text  # SK하이닉스 +0.5% / -2%


def test_buy_exit_lines_reflect_commission_tax_slippage():
    """수수료·세금·슬리피지가 있으면 익절가는 naive +0.5%보다 더 벌어진다."""
    from src.risk.manager import exit_trigger_price

    execution = make_execution(commission_percent=0.015, tax_percent=0.18, slippage_percent=0.1)
    _, text, _ = render_buys(execution)

    expected_tp = exit_trigger_price(57300.0, 0.005, 0.00015, 0.0018, 0.001)
    expected_sl = exit_trigger_price(57300.0, -0.02, 0.00015, 0.0018, 0.001)
    assert f"{expected_tp:,.0f}" in text
    assert f"{expected_sl:,.0f}" in text
    assert "57,586" not in text  # naive +0.5% 값이 아니어야 한다


def test_buy_header_shows_cash_allocation_and_total():
    _, text, html = render_buys()

    for body in (text, html):
        assert "5,000,000원" in body            # 주문가능금액
        assert "833,333원" in body              # 종목당 배정
        assert "주문가능금액의 16.7%" in body
        assert "1,594,200원" in body            # 총 투입금액
        assert "+0.50% / -2.00%" in body


def test_buy_allocation_share_omitted_without_cash():
    """예수금 조회가 0으로 오더라도 0으로 나누지 않는다."""
    _, text, html = render_buys(make_execution(cash=0.0))

    for body in (text, html):
        assert "주문가능금액의" not in body


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


# ── recommendation_email ─────────────────────────────────────
def test_recommendation_email_notes_shortfall_percentage():
    recs = [StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="근거")]
    _, body = templates.recommendation_email(
        recs, DAY, investable_ratio=0.5, target_stock_count=4
    )

    assert "1개로 4개 미만" in body
    assert "12.5%" in body


def test_recommendation_email_omits_shortfall_note_when_full():
    recs = [
        StockRecommendation(ticker="005930", name="삼성전자", target_price=1000, reason="근거"),
        StockRecommendation(ticker="000660", name="SK하이닉스", target_price=1000, reason="근거"),
    ]
    _, body = templates.recommendation_email(
        recs, DAY, investable_ratio=0.5, target_stock_count=2
    )

    assert "미만입니다" not in body
