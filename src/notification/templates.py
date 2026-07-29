import unicodedata
from datetime import date
from html import escape
from typing import List, Optional

from src.core.events import format_stock
from src.llm.recommender import StockRecommendation
from src.logger.trade_store import DailySummary, MonthlySummary, TradeRow

# 이익은 빨강, 손실은 파랑 (국내 증권 관례)
COLOR_PROFIT = "#d32f2f"
COLOR_LOSS = "#1565c0"
COLOR_FLAT = "#555555"


def recommendation_email(recommendations: List[StockRecommendation], today: date) -> tuple[str, str]:
    """08:45 LLM 추천 결과 이메일 (PRD 5.5-B 3단계) — 자동 유효성 검증 없이 그대로 전달."""
    subject = f"[AutoTrade] {today:%Y-%m-%d} 급등 예상 대형주 추천 {len(recommendations)}종목"

    lines = [f"{today:%Y-%m-%d} LLM 추천 결과입니다.", ""]
    for i, r in enumerate(recommendations, start=1):
        lines.extend([f"{i}. {format_stock(r.ticker, r.name)}", f"   추천 근거: {r.reason}", ""])

    if len(recommendations) < 3:
        lines.append(
            f"※ 추천 종목이 {len(recommendations)}개로 3개 미만입니다. "
            "종목당 매수금액은 예수금의 1/6로 고정되며, 나머지 몫은 현금으로 유지됩니다."
        )
        lines.append("")

    lines.append("※ 이 추천은 사전 유효성 검증(거래정지·상장폐지 등)을 거치지 않았습니다.")
    return subject, "\n".join(lines)


def daily_report_email(
    summary: DailySummary,
    monthly: MonthlySummary,
    cash: float,
    sync_failed: bool = False,
) -> tuple[str, str, str]:
    """15:30 일일/월간 성과 리포트 이메일 (PRD 5.11).

    (제목, 평문, HTML)을 돌려준다 — 표는 HTML로 보이고, 평문만 읽는 클라이언트에서도
    같은 내용이 등폭 정렬로 남는다.
    """
    subject = f"[AutoTrade] {summary.day:%Y-%m-%d} 매매 결과 리포트"
    notes = _report_notes(summary, sync_failed)
    return (
        subject,
        _report_text(summary, monthly, cash, notes),
        _report_html(summary, monthly, cash, notes),
    )


def _report_notes(summary: DailySummary, sync_failed: bool) -> List[str]:
    notes = ["※ 정규장 마감(15:30) 직전 집계이므로 마감 체결분이 반영되지 않았을 수 있습니다."]
    if sync_failed:
        notes.insert(
            0,
            "※ 체결 내역 조회에 실패해 접수 기준으로 집계했습니다. 수치가 불완전할 수 있습니다.",
        )
    if summary.rejected_count:
        notes.append(f"※ 주문 실패 {summary.rejected_count}건이 있었습니다. 로그를 확인하세요.")
    return notes


# ── 값 표기 ─────────────────────────────────────────────────
def _won(value: float) -> str:
    return f"{value:+,.0f}원"


def _balance(value: float) -> str:
    """예수금처럼 부호가 의미 없는 잔고 금액 — 손익용 _won()과 달리 +를 붙이지 않는다."""
    return f"{value:,.0f}원"


def _percent(value: float) -> str:
    return f"{value:+.2f}%"


def _color(value: Optional[float]) -> str:
    if value is None or value == 0:
        return COLOR_FLAT
    return COLOR_PROFIT if value > 0 else COLOR_LOSS


def _row_cells(trade: TradeRow) -> tuple[str, str, str, str, str, str]:
    """표 한 줄의 셀 값 — 평문과 HTML이 같은 값을 쓰도록 한 곳에서 만든다."""
    if trade.sell_price is None:
        return (trade.label, f"{trade.buy_price:,.0f}", "보유중", f"{trade.quantity}", "-", "-")
    return (
        trade.label,
        f"{trade.buy_price:,.0f}",
        f"{trade.sell_price:,.0f}",
        f"{trade.quantity}",
        _won(trade.pnl or 0.0),
        _percent(trade.return_pct or 0.0),
    )


HEADERS = ("종목", "매수가", "매도가", "수량", "손익", "수익률")


# ── 평문 ────────────────────────────────────────────────────
def _display_width(text: str) -> int:
    """한글은 등폭 글꼴에서 두 칸을 차지하므로 글자 수 대신 표시 폭으로 정렬한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, right: bool = True) -> str:
    gap = max(0, width - _display_width(text))
    return " " * gap + text if right else text + " " * gap


def _report_text(
    summary: DailySummary, monthly: MonthlySummary, cash: float, notes: List[str]
) -> str:
    lines = [f"{summary.day:%Y-%m-%d} 매매 결과", ""]

    if not summary.trades:
        lines.append("오늘 체결된 매매가 없습니다.")
    else:
        cells = [HEADERS] + [_row_cells(t) for t in summary.trades]
        widths = [max(_display_width(row[i]) for row in cells) for i in range(len(HEADERS))]
        divider = "-" * (sum(widths) + 2 * (len(widths) - 1))

        lines.append("  ".join(_pad(h, w, right=(i > 0)) for i, (h, w) in enumerate(zip(HEADERS, widths))))
        lines.append(divider)
        for row in cells[1:]:
            lines.append("  ".join(_pad(v, w, right=(i > 0)) for i, (v, w) in enumerate(zip(row, widths))))
        lines.append(divider)
        lines.append(f"합계 (투입 {summary.cost:,.0f}원)")

        totals = [
            ("실현손익", _won(summary.realized_pnl), _percent(summary.return_pct)),
            ("수수료·세금", _won(-summary.fees), ""),
            ("순손익", _won(summary.net_pnl), _percent(summary.net_return_pct)),
        ]
        label_width = max(_display_width(label) for label, _, _ in totals)
        amount_width = max(_display_width(amount) for _, amount, _ in totals)
        for label, amount, pct in totals:
            row = f"  {_pad(label, label_width, right=False)}  {_pad(amount, amount_width)}"
            lines.append(f"{row}  {pct}" if pct else row)

    lines.extend(
        [
            "",
            f"이번 달 누적 ({summary.day:%Y-%m} 기준)",
            f"- 누적 실현손익: {_won(monthly.realized_pnl)} ({_percent(monthly.return_pct)})",
            f"- 누적 수수료·세금: {_won(-monthly.fees)}",
            f"- 누적 순손익: {_won(monthly.net_pnl)} ({_percent(monthly.net_return_pct)})",
            f"- 현재 예수금: {_balance(cash)}",
            "",
            *notes,
        ]
    )
    return "\n".join(lines)


# ── HTML ────────────────────────────────────────────────────
_TH = "padding:6px 10px; border-bottom:2px solid #cccccc; font-weight:600; text-align:right;"
_TD = "padding:6px 10px; border-bottom:1px solid #eeeeee; text-align:right;"


def _report_html(
    summary: DailySummary, monthly: MonthlySummary, cash: float, notes: List[str]
) -> str:
    parts = [
        '<div style="font-family:-apple-system,\'Malgun Gothic\',sans-serif; font-size:14px; color:#222222;">',
        f"<h2 style=\"font-size:17px; margin:0 0 14px;\">{summary.day:%Y-%m-%d} 매매 결과</h2>",
    ]

    if not summary.trades:
        parts.append('<p style="color:#555555;">오늘 체결된 매매가 없습니다.</p>')
    else:
        parts.append('<table style="border-collapse:collapse; font-size:14px;">')
        header = "".join(
            f'<th style="{_TH}{"text-align:left;" if i == 0 else ""}">{h}</th>'
            for i, h in enumerate(HEADERS)
        )
        parts.append(f"<tr>{header}</tr>")

        for trade in summary.trades:
            label, buy, sell, qty, pnl, pct = _row_cells(trade)
            tone = _color(trade.pnl)
            parts.append(
                f'<tr><td style="{_TD}text-align:left;">{escape(label)}</td>'
                f'<td style="{_TD}">{buy}</td>'
                f'<td style="{_TD}">{sell}</td>'
                f'<td style="{_TD}">{qty}</td>'
                f'<td style="{_TD}color:{tone};">{pnl}</td>'
                f'<td style="{_TD}color:{tone};">{pct}</td></tr>'
            )

        total_tone = _color(summary.realized_pnl)
        net_tone = _color(summary.net_pnl)
        parts.append(
            f'<tr><td colspan="4" style="{_TD}text-align:left; border-top:2px solid #cccccc;">'
            f"<strong>합계</strong> <span style=\"color:#777777;\">(투입 {summary.cost:,.0f}원)</span></td>"
            f'<td style="{_TD}border-top:2px solid #cccccc; color:{total_tone};"><strong>{_won(summary.realized_pnl)}</strong></td>'
            f'<td style="{_TD}border-top:2px solid #cccccc; color:{total_tone};"><strong>{_percent(summary.return_pct)}</strong></td></tr>'
        )
        parts.append(
            f'<tr><td colspan="4" style="{_TD}text-align:left; color:#777777;">수수료·세금</td>'
            f'<td style="{_TD}color:{COLOR_FLAT};">{_won(-summary.fees)}</td>'
            f'<td style="{_TD}"></td></tr>'
        )
        parts.append(
            f'<tr><td colspan="4" style="{_TD}text-align:left;"><strong>순손익</strong></td>'
            f'<td style="{_TD}color:{net_tone};"><strong>{_won(summary.net_pnl)}</strong></td>'
            f'<td style="{_TD}color:{net_tone};"><strong>{_percent(summary.net_return_pct)}</strong></td></tr>'
        )
        parts.append("</table>")

    parts.extend(
        [
            f'<h3 style="font-size:15px; margin:20px 0 8px;">이번 달 누적 ({summary.day:%Y-%m} 기준)</h3>',
            '<ul style="margin:0; padding-left:18px; color:#333333;">',
            f'<li>누적 실현손익: <span style="color:{_color(monthly.realized_pnl)};">'
            f'{_won(monthly.realized_pnl)} ({_percent(monthly.return_pct)})</span></li>',
            f'<li>누적 수수료·세금: <span style="color:{COLOR_FLAT};">{_won(-monthly.fees)}</span></li>',
            f'<li><strong>누적 순손익: <span style="color:{_color(monthly.net_pnl)};">'
            f'{_won(monthly.net_pnl)} ({_percent(monthly.net_return_pct)})</span></strong></li>',
            f"<li>현재 예수금: {_balance(cash)}</li>",
            "</ul>",
            '<p style="font-size:12px; color:#777777; margin-top:18px;">'
            + "<br>".join(escape(n) for n in notes)
            + "</p>",
            "</div>",
        ]
    )
    return "".join(parts)
