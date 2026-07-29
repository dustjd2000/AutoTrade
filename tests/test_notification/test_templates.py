from datetime import date

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
