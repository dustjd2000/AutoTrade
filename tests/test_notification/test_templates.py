from datetime import date, datetime

from src.core.events import BuyExecution, BuyOutcome, BuyRecord, TuningOutcome, UnsellableView
from src.llm.exit_tuner import ExitVersionStats
from src.logger.trade_store import (
    AIExitDecisionRow,
    DailySummary,
    ExitReviewRow,
    MonthlySummary,
    TradeRow,
)
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


def make_yearly(**overrides):
    """올해 누적 실현손익 +98,700원, 수수료·세금 7,400원 → 순손익 +91,300원."""
    defaults = dict(realized_pnl=98700.0, fees=7400.0, base_asset=1_300_000.0)
    defaults.update(overrides)
    return MonthlySummary(**defaults)


def render(
    summary=None,
    monthly=None,
    yearly=None,
    cash=1220735.0,
    sync_failed=False,
    closed_out=False,
    unsellable=None,
    chart_cid=None,
    yearly_chart_cid=None,
):
    return templates.daily_report_email(
        summary or make_summary(),
        monthly or make_monthly(),
        yearly or make_yearly(),
        cash,
        sync_failed=sync_failed,
        closed_out=closed_out,
        unsellable=unsellable,
        chart_cid=chart_cid,
        yearly_chart_cid=yearly_chart_cid,
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


def test_yearly_section_present():
    """월 블록 아래에 같은 꼴의 올해 누적이 붙는다 (2026-09-01)."""
    _, text, html = render()

    for body in (text, html):
        assert "올해 누적 (2026년 기준)" in body
        assert "+98,700원" in body          # 올해 누적 실현손익
        assert "+91,300원" in body          # 수수료·세금을 뺀 순손익
        assert body.index("이번 달 누적") < body.index("올해 누적")


def test_yearly_section_omits_cash():
    """주문가능금액은 기간과 무관한 값이라 월 블록에만 둔다."""
    _, text, html = render()

    for body in (text, html):
        assert body.count("현재 주문가능금액") == 1


def test_yearly_chart_is_embedded_only_when_given():
    _, text, html = render(chart_cid="monthly-cumulative", yearly_chart_cid="yearly-cumulative")

    assert 'src="cid:yearly-cumulative"' in html
    assert html.index("cid:monthly-cumulative") < html.index("cid:yearly-cumulative")
    assert "cid:" not in text, "평문 파트에는 그래프가 없다"

    _, _, only_monthly = render(chart_cid="monthly-cumulative")
    assert "cid:yearly-cumulative" not in only_monthly


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
    """비용이 0인 이 표에서는 손절가 = 단가 -2%가 그대로 나온다."""
    _, text, _ = render_buys()

    assert "56,154" in text   # 삼성전자 -2%
    assert "194,040" in text  # SK하이닉스 -2%


def test_buy_exit_lines_reflect_commission_tax_slippage():
    """수수료·세금·슬리피지가 있으면 손절가는 naive -2%보다 더 벌어진다."""
    from src.risk.manager import exit_trigger_price

    execution = make_execution(commission_percent=0.015, tax_percent=0.18, slippage_percent=0.1)
    _, text, _ = render_buys(execution)

    expected_sl = exit_trigger_price(57300.0, -0.02, 0.00015, 0.0018, 0.001)
    assert f"{expected_sl:,.0f}" in text
    assert "56,154" not in text  # naive -2% 값이 아니어야 한다


def test_buy_header_shows_cash_allocation_and_total():
    _, text, html = render_buys()

    for body in (text, html):
        assert "5,000,000원" in body            # 주문가능금액
        assert "833,333원" in body              # 종목당 배정
        assert "주문가능금액의 16.7%" in body
        assert "1,594,200원" in body            # 총 투입금액
        assert "-2.00%" in body


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


def test_monthly_chart_is_embedded_only_when_given():
    """그래프 PNG는 CID 인라인 첨부로 붙는다 — Gmail이 본문 <svg>는 지운다."""
    _, text, html = render(chart_cid="monthly-cumulative")

    assert 'src="cid:monthly-cumulative"' in html
    assert "cid:" not in text, "평문 파트에는 그래프가 없다"

    _, _, without = render()
    assert "<img" not in without




# ── 매도 판단 검증 · 매도 프롬프트 수정 메일 (2026-10-29 복구, PRD 5.14·5.15) ──
def _exit_row(review=""):
    return ExitReviewRow(
        day=date(2026, 10, 29), ticker="069500", name="KODEX 200", exit_prompt_version="v6",
        net_pnl=-2_180.0, net_return=-0.0061, peak_return=0.0156, outcome="missed",
        exit_reason="day_end", decision_count=2, review=review,
    )


def _exit_decision():
    return AIExitDecisionRow(
        day=date(2026, 10, 29), at=datetime(2026, 10, 29, 9, 54), ticker="069500", name="KODEX 200",
        sell=False, ok=True, reason="이익이 남아 있다", net_return=0.0021, peak_return=0.0156,
        current_price=36_100.0, exit_prompt_version="v6",
    )


def test_exit_review_email_lists_numbers_decisions_and_review():
    subject, body = templates.exit_review_email(
        [_exit_row("09:54 이익이 녹는 중에도 보유했다.")], [_exit_decision()], date(2026, 10, 29)
    )
    assert "매도 판단 검증 1종목" in subject and "-2,180원" in subject
    assert "순이익 기회를 놓침 1" in body
    assert "15:15 강제청산" in body
    assert "09:54" in body and "이익이 남아 있다" in body
    assert "평가: 09:54 이익이 녹는 중에도 보유했다." in body


def test_exit_review_email_reports_the_prompt_change():
    tuning = TuningOutcome(changed=True, reason="놓침 5건", old_version="v6", new_version="20261030")
    _, body = templates.exit_review_email([_exit_row()], [], date(2026, 10, 30), tuning=tuning)
    assert "■ 매도 프롬프트 자동 수정" in body
    assert "수정함 (v6 → 20261030)" in body and "놓침 5건" in body


def test_exit_review_email_reports_why_the_prompt_was_not_changed():
    tuning = TuningOutcome(changed=False, reason="이번 달 순수익 +6.10% ≥ 기준 5.0%")
    _, body = templates.exit_review_email([_exit_row()], [], date(2026, 10, 30), tuning=tuning)
    assert "수정 안 함 — 이번 달 순수익 +6.10% ≥ 기준 5.0%" in body


def test_exit_review_email_has_no_tuning_block_without_an_outcome():
    _, body = templates.exit_review_email([_exit_row()], [], date(2026, 10, 30))
    assert "프롬프트 자동 수정" not in body


def test_exit_prompt_tuning_email_shows_versions_reason_and_diff():
    subject, body = templates.exit_prompt_tuning_email(
        date(2026, 10, 30), "v6", "20261030", "놓침 5건 중 4건이라 이익 확정을 앞당겼습니다.",
        [ExitVersionStats("v6", 5, 1, 4, 0, -2_500.0, -0.005, 0.012)],
        before={"time": "## 시간\n예전 내용"},
        after={"time": "## 시간\n새 내용"},
    )
    assert "매도 프롬프트 수정 (v6 → 20261030)" in subject
    assert "놓침 5건 중 4건" in body
    assert "v6: 5건" in body
    assert "예전 내용" in body and "새 내용" in body
    assert "data/exit_prompt/history/v6/" in body


from src.research.backtest import Rule as _Rule
from src.logger.trade_store import RuleEvaluationRow as _RuleEvaluationRow


def test_rule_evaluation_email_reports_conditions():
    row = _RuleEvaluationRow(
        day=date(2026, 11, 6), status="evaluated",
        current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(),
        best_rule=_Rule(0.2, False, "09:10", 2.0).to_json(),
        select_current=-0.3, select_best=0.2, test_current=-0.1, test_best=0.09,
        beats=True, positive=True,
    )
    subject, body = templates.rule_evaluation_email(row, streak=2, cooldown=0)

    assert "주간 평가" in subject
    assert "문턱 0.2% / 뒤집기 끔 / 결정 09:10 / 손절 2%" in body
    assert "2/4" in body
    assert "+0.090%" in body


def test_rule_evaluation_email_for_an_applied_change():
    row = _RuleEvaluationRow(day=date(2026, 11, 27), status="evaluated",
                             current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(),
                             best_rule=_Rule(0.2, False, "09:10", 2.0).to_json(),
                             select_current=0.0, select_best=0.2, test_current=0.0, test_best=0.1,
                             beats=True, positive=True, applied=True, note="자동 적용")
    subject, body = templates.rule_evaluation_email(
        row, streak=4, cooldown=0, applied_values={"ETF_FLIP_ENABLED": ("1", "0")}
    )

    assert "자동 변경" in subject
    assert "ETF_FLIP_ENABLED: 1 → 0" in body


def test_rule_evaluation_email_when_not_evaluated():
    row = _RuleEvaluationRow(day=date(2026, 10, 30), status="insufficient_data",
                             current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(), note="거래일 100일 < 120일")
    subject, body = templates.rule_evaluation_email(row, streak=0, cooldown=0)
    assert "평가하지 않았습니다" in body and "거래일 100일" in body
