from datetime import date

from src.logger.trade_store import DailyPoint
from src.notification import chart

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def points(*cumulatives):
    """8월 1일부터 하루씩, 주어진 누적 순손익을 갖는 점들."""
    running = 0.0
    made = []
    for i, cumulative in enumerate(cumulatives):
        made.append(DailyPoint(day=date(2026, 8, i + 1), net_pnl=cumulative - running, cumulative=cumulative))
        running = cumulative
    return made


def test_renders_png_bytes():
    png = chart.render_monthly_cumulative(points(1000.0, -500.0, 3000.0))

    assert png is not None
    assert png[:8] == PNG_MAGIC


def test_renders_a_single_point():
    """매달 첫 거래일 — 꺾은선은 아니지만 0선 대비 위치는 보여준다 (2026-09-01)."""
    png = chart.render_monthly_cumulative(points(1000.0))

    assert png is not None
    assert png[:8] == PNG_MAGIC


def test_no_chart_without_any_trade():
    """이번 달 매매가 아직 없으면 그릴 것이 없다 — 그래프 없이 숫자만 보낸다."""
    assert chart.render_monthly_cumulative([]) is None


def test_yearly_chart_labels_months():
    """연 그래프는 점 하나가 한 달이라 눈금도 날짜가 아니라 달이다 (2026-09-01)."""
    monthly_points = [
        DailyPoint(day=date(2026, 7, 1), net_pnl=1000.0, cumulative=1000.0),
        DailyPoint(day=date(2026, 8, 1), net_pnl=-300.0, cumulative=700.0),
    ]

    png = chart.render_yearly_cumulative(monthly_points)
    assert png is not None
    assert png[:8] == PNG_MAGIC

    fig = chart._figure(monthly_points, chart.MONTH_LABEL)
    try:
        labels = [t.get_text() for t in fig.axes[0].get_xticklabels()]
    finally:
        chart._close(fig)
    assert labels == ["07월", "08월"]


def test_missing_matplotlib_degrades_to_no_chart(monkeypatch):
    """차트 실패가 리포트 메일 자체를 막으면 안 된다."""
    monkeypatch.setattr(chart, "_figure", lambda *a, **k: (_ for _ in ()).throw(ImportError("no matplotlib")))

    assert chart.render_monthly_cumulative(points(1000.0, 2000.0)) is None
