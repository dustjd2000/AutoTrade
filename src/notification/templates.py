import unicodedata
from datetime import date
from html import escape
from typing import Dict, List, Optional

from src.core.events import (
    BuyExecution,
    BuyOutcome,
    BuyRecord,
    UnsellableView,
    format_stock,
)
from src.llm.recommender import StockRecommendation
from src.logger.trade_store import DailySummary, MonthlySummary, RecommendationRow, TradeRow
from src.notification import chart
from src.risk.manager import exit_trigger_price

# 이익은 빨강, 손실은 파랑 (국내 증권 관례)
COLOR_PROFIT = "#d32f2f"
COLOR_LOSS = "#1565c0"
COLOR_FLAT = "#555555"
# 매수하지 못한 종목(건너뜀·실패) — 손실색(파랑)을 쓰면 손익 표기와 헷갈린다
COLOR_WARN = "#e65100"


def recommendation_email(
    recommendations: List[StockRecommendation],
    today: date,
    investable_ratio: float,
    target_stock_count: int,
) -> tuple[str, str]:
    """09:05 LLM 추천 결과 이메일 (PRD 5.5-B 3단계) — 자동 유효성 검증 없이 그대로 전달."""
    subject = f"[AutoTrade] {today:%Y-%m-%d} 급등 예상 대형주 추천 {len(recommendations)}종목"

    lines = [f"{today:%Y-%m-%d} LLM 추천 결과입니다.", ""]
    for i, r in enumerate(recommendations, start=1):
        lines.append(f"{i}. {format_stock(r.ticker, r.name)}")
        lines.append(f"   목표 매수가: {r.target_price:,}원")
        sell_line = _sell_target_line(r)
        if sell_line:
            lines.append(sell_line)
        outlook_line = _outlook_line(r)
        if outlook_line:
            lines.append(outlook_line)
        lines.extend([f"   추천 근거: {r.reason}", ""])

    if len(recommendations) < target_stock_count:
        per_stock_ratio = investable_ratio / target_stock_count
        lines.append(
            f"※ 추천 종목이 {len(recommendations)}개로 {target_stock_count}개 미만입니다. "
            f"종목당 매수금액은 주문가능금액의 {per_stock_ratio * 100:.1f}%로 고정되며, "
            "나머지 몫은 현금으로 유지됩니다."
        )
        lines.append("")

    lines.append("※ 09:08에 위 목표 매수가로 지정가 주문을 넣고, 10:10까지 체결되지 않으면 취소합니다.")
    # 매도가를 한 줄도 싣지 못했으면 이 주석도 뺀다 — 메일에 없는 값을 설명하는 꼴이 된다
    if any(_sell_target_line(r) for r in recommendations):
        lines.append(
            "※ 목표 매도가는 LLM의 참고 수치이며 주문에 사용되지 않습니다 — 실제 매도는 "
            "순손익 기준 익절·손절과 15:15 강제청산이 담당합니다."
        )
    # 전망 줄을 한 줄도 싣지 못했으면 이 주석도 뺀다 — 메일에 없는 값을 설명하는 꼴이 된다
    if any(_outlook_line(r) for r in recommendations):
        lines.append("※ 오늘 전망은 LLM의 참고 수치이며 주문에 사용되지 않습니다.")
    lines.append("※ 이 추천은 사전 유효성 검증(거래정지·상장폐지 등)을 거치지 않았습니다.")
    return subject, "\n".join(lines)


def _sell_target_line(r: StockRecommendation) -> str:
    """추천 메일의 목표 매도가 한 줄. 산출되지 않았으면(0) 빈 문자열이라 줄이 통째로 빠진다.

    매수가 대비 상승률을 함께 적는다 — 절대 가격만으로는 이 목표가 익절선(순손익 기준)보다
    위인지 아래인지 한눈에 들어오지 않는다.
    """
    if r.target_sell_price <= 0 or r.target_price <= 0:
        return ""
    gain = (r.target_sell_price - r.target_price) / r.target_price * 100
    return f"   목표 매도가: {r.target_sell_price:,}원 (매수가 대비 {gain:+.2f}%, 참고용)"


def _outlook_line(r: StockRecommendation) -> str:
    """추천 메일의 오늘 전망 한 줄. 산출되지 않았으면("") 빈 문자열이라 줄이 통째로 빠진다."""
    if not r.outlook:
        return ""
    return f"   오늘 전망: {r.outlook}"


def _price_move_line(row: RecommendationRow) -> str:
    """검증 메일의 '추천 시각가 → 종가' 한 줄. 산출되지 않은 값(0)은 그 조각만 뺀다.

    recommend_price==0은 추천 시각 현재가 조회 실패(당일 지표 없음)를, actual_change_rate==0은
    전일 봉이 없어 등락률을 못 낸 것을 뜻한다(TodayMetrics docstring) — 둘 다 실제로 0원/0%인
    것과 구분되지 않아, 있는 그대로 적으면 사실이 아닌 값처럼 읽힌다.
    """
    prefix = f"추천 시각가: {row.recommend_price:,.0f}원 → " if row.recommend_price > 0 else ""
    rate = f" ({row.actual_change_rate:+.2f}%)" if row.actual_change_rate != 0 else ""
    return f"   {prefix}종가: {row.actual_close:,.0f}원{rate}"


def recommendation_review_email(
    rows: List[RecommendationRow], today: date
) -> tuple[str, str]:
    """15:35 추천 검증 이메일 (PRD 5.5-B '추천 검증').

    일일 리포트와 별개의 메일이다 — 리포트는 보유 종목이 전부 매도되면 15:30 이전에 조기
    발송될 수 있고, 그때는 당일 고가·저가·종가가 아직 확정되지 않는다.

    표가 없어 HTML을 함께 만들지 않는다. (제목, 평문)만 돌려준다.
    """
    subject = f"[AutoTrade] {today:%Y-%m-%d} 추천 검증 {len(rows)}종목"

    lines = [f"{today:%Y-%m-%d} 추천 종목의 실제 움직임입니다.", ""]
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. {row.label}")
        if row.actual_close is None:
            lines.extend(["   당일 봉 조회 실패 — 실제 움직임을 확인하지 못했습니다.", ""])
            continue
        lines.append(_price_move_line(row))
        lines.append(f"   당일 고가/저가: {row.actual_high:,.0f}원 / {row.actual_low:,.0f}원")
        lines.append(
            f"   목표 매수가: {row.target_price:,}원 — "
            + ("도달" if row.buy_target_hit else "미도달 (매수 무산)")
        )
        if row.target_sell_price > 0 and row.sell_target_hit is not None:
            lines.append(
                f"   목표 매도가: {row.target_sell_price:,}원 — "
                + ("도달" if row.sell_target_hit else "미도달")
            )
        if row.outlook:
            lines.append(f"   전망: {row.outlook}")
        if row.review:
            lines.append(f"   평가: {row.review}")
        lines.append("")

    lines.append("※ 목표 매수가·매도가와 전망은 참고 수치이며 주문에 사용되지 않습니다.")
    lines.append("※ 목표 매수가 '도달'은 당일 저가가 그 가격까지 내려왔다는 뜻이며, 실제 매수")
    lines.append("   여부는 09:08 갭 판정과 10:10 미체결 취소가 따로 정합니다.")
    return subject, "\n".join(lines)


def prompt_tuning_email(
    today: date,
    old_version: str,
    new_version: str,
    reason: str,
    stats: List["VersionStats"],
    before: Dict[str, str],
    after: Dict[str, str],
) -> tuple[str, str]:
    """추천 프롬프트를 자동 수정한 날 나가는 이메일 (PRD '프롬프트 자동 수정').

    고친 날만 발송한다 — 고치지 않은 날은 호출되지 않는다. 사람이 개입할 유일한 지점이므로
    되돌리는 방법을 본문에 함께 적는다.

    표가 없어 HTML을 함께 만들지 않는다. (제목, 평문)만 돌려준다.
    """
    subject = f"[AutoTrade] {today:%Y-%m-%d} 추천 프롬프트 수정 ({old_version} → {new_version})"

    lines = [
        f"{today:%Y-%m-%d} 추천 프롬프트를 자동으로 수정했습니다 ({old_version} → {new_version}).",
        "",
        "## 수정 이유",
        reason or "(없음)",
        "",
        "## 근거 — 버전별 성과",
    ]
    for s in stats:
        lines.append(
            f" - {s.version}: {s.count}건 | 목표 매수가 도달 {s.buy_hit}건 | "
            f"목표 매도가 도달 {s.sell_hit}건 | 평균 등락률 {s.avg_change_rate:+.2f}%"
        )

    for key in sorted(after):
        lines.extend(["", f"## 바뀐 절: {key}", "", "[이전]", before.get(key, "(없음)"), "", "[이후]", after[key]])

    lines.extend([
        "",
        f"※ 되돌리려면 data/prompt/history/{old_version}/ 의 파일들을 data/prompt/ 로 복사하십시오.",
        "※ 수정된 프롬프트는 다음 거래일 추천부터 적용됩니다 (엔진 재시작 불필요).",
    ])
    return subject, "\n".join(lines)


def buy_result_email(execution: BuyExecution) -> tuple[str, str, str]:
    """09:08 매수 실행 직후 결과 이메일 (PRD 5.5-B 5·6단계).

    (제목, 평문, HTML)을 돌려준다 — 일일 리포트와 같은 형식이다.
    주문 접수 직후라 체결가가 아직 없을 수 있으므로 상태 열로 구분해 표기한다.
    """
    ordered = execution.ordered
    subject = (
        f"[AutoTrade] {execution.at:%Y-%m-%d} 매수 실행 결과 "
        f"{len(ordered)}/{len(execution.records)}종목"
    )
    notes = _buy_notes(execution)
    return subject, _buy_text(execution, notes), _buy_html(execution, notes)


def daily_report_email(
    summary: DailySummary,
    monthly: MonthlySummary,
    yearly: MonthlySummary,
    cash: float,
    sync_failed: bool = False,
    closed_out: bool = False,
    unsellable: Optional[List[UnsellableView]] = None,
    chart_cid: Optional[str] = None,
    yearly_chart_cid: Optional[str] = None,
) -> tuple[str, str, str]:
    """15:35 일일/월간/연간 성과 리포트 이메일 (PRD 5.11).

    (제목, 평문, HTML)을 돌려준다 — 표는 HTML로 보이고, 평문만 읽는 클라이언트에서도
    같은 내용이 등폭 정렬로 남는다.

    closed_out=True는 보유 종목을 전부 매도해 15:35보다 앞서 보내는 최종 리포트다.
    unsellable은 오늘 매도하지 못한 종목 — 메일만 보는 상황에서도 잔여 포지션을 알 수 있어야 한다.
    `yearly`는 연초부터의 같은 집계다 (2026-09-01) — 월 블록 아래에 같은 꼴로 붙는다.
    두 chart_cid는 각각 월(날짜별)·연(달별) 누적 꺾은선 PNG를 HTML에 끼워 넣는다
    (평문에는 없다).
    """
    subject = f"[AutoTrade] {summary.day:%Y-%m-%d} 매매 결과 리포트"
    notes = _report_notes(summary, sync_failed, closed_out)
    unsellable = unsellable or []
    return (
        subject,
        _report_text(summary, monthly, yearly, cash, notes, unsellable),
        _report_html(
            summary, monthly, yearly, cash, notes, unsellable, chart_cid, yearly_chart_cid
        ),
    )


def _report_notes(
    summary: DailySummary, sync_failed: bool, closed_out: bool = False
) -> List[str]:
    if closed_out:
        notes = [
            "※ 보유 종목을 전부 매도한 직후 집계입니다 "
            "(추가 매수가 없으면 15:35 정기 리포트는 생략됩니다)."
        ]
    else:
        notes = ["※ 체결이 확인된 주문만 집계에 들어갑니다 — 접수 후 체결 대기 중인 주문은 빠집니다."]
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
    # 팔렸지만 원가를 모르는 경우(전일 이월분을 수동 매도) 손익은 0원이 아니라 '모름'이다
    return (
        trade.label,
        f"{trade.buy_price:,.0f}" if trade.buy_price else "-",
        f"{trade.sell_price:,.0f}",
        f"{trade.quantity}",
        _won(trade.pnl) if trade.pnl is not None else "-",
        _percent(trade.return_pct) if trade.return_pct is not None else "-",
    )


HEADERS = ("종목", "매수가", "매도가", "수량", "손익", "수익률")

UNSELLABLE_HEADING = "매도하지 못한 종목"


def _unsellable_reason(item: UnsellableView) -> str:
    """표기할 사유 — 제외된 건은 자동 청산되지 않는다는 사실을 함께 적는다."""
    if item.excluded:
        return f"{item.reason} · 보유 목록에서 제외되어 자동 청산되지 않습니다"
    return item.reason


# ── 평문 ────────────────────────────────────────────────────
def _display_width(text: str) -> int:
    """한글은 등폭 글꼴에서 두 칸을 차지하므로 글자 수 대신 표시 폭으로 정렬한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, right: bool = True) -> str:
    gap = max(0, width - _display_width(text))
    return " " * gap + text if right else text + " " * gap


def _report_text(
    summary: DailySummary,
    monthly: MonthlySummary,
    yearly: MonthlySummary,
    cash: float,
    notes: List[str],
    unsellable: List[UnsellableView],
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
            f"- 현재 주문가능금액: {_balance(cash)}",
            "",
            # 주문가능금액은 기간과 무관한 값이라 월 블록에만 둔다
            f"올해 누적 ({summary.day:%Y}년 기준)",
            f"- 누적 실현손익: {_won(yearly.realized_pnl)} ({_percent(yearly.return_pct)})",
            f"- 누적 수수료·세금: {_won(-yearly.fees)}",
            f"- 누적 순손익: {_won(yearly.net_pnl)} ({_percent(yearly.net_return_pct)})",
        ]
    )

    if unsellable:
        lines.extend(["", f"{UNSELLABLE_HEADING} ({len(unsellable)}건)"])
        lines.extend(f"- {item.label} — {_unsellable_reason(item)}" for item in unsellable)

    lines.extend(["", *notes])
    return "\n".join(lines)


# ── HTML ────────────────────────────────────────────────────
CHART_WIDTH_PX = chart.WIDTH_PX

_TH = "padding:6px 10px; border-bottom:2px solid #cccccc; font-weight:600; text-align:right;"
_TD = "padding:6px 10px; border-bottom:1px solid #eeeeee; text-align:right;"


def _cumulative_items(period: MonthlySummary, cash: Optional[float] = None) -> List[str]:
    """'이번 달 누적'·'올해 누적' 블록의 항목 — 두 블록이 같은 꼴이라야 나란히 읽힌다."""
    items = [
        '<ul style="margin:0; padding-left:18px; color:#333333;">',
        f'<li>누적 실현손익: <span style="color:{_color(period.realized_pnl)};">'
        f'{_won(period.realized_pnl)} ({_percent(period.return_pct)})</span></li>',
        f'<li>누적 수수료·세금: <span style="color:{COLOR_LOSS};">{_won(-period.fees)}</span></li>',
        f'<li style="font-size:16px;"><strong>누적 순손익: <span style="color:{_color(period.net_pnl)};">'
        f'{_won(period.net_pnl)} ({_percent(period.net_return_pct)})</span></strong></li>',
    ]
    if cash is not None:
        items.append(f"<li>현재 주문가능금액: {_balance(cash)}</li>")
    items.append("</ul>")
    return items


def _chart_img(cid: str, alt: str) -> str:
    """CID 인라인 첨부 그래프 — width 속성은 Outlook용이다 (style만 주면 원본 크기로 벌어진다)."""
    return (
        f'<img src="cid:{escape(cid)}" width="{CHART_WIDTH_PX}" alt="{escape(alt)}" '
        f'style="display:block; width:100%; max-width:{CHART_WIDTH_PX}px; height:auto; margin:10px 0 0;">'
    )


def _report_html(
    summary: DailySummary,
    monthly: MonthlySummary,
    yearly: MonthlySummary,
    cash: float,
    notes: List[str],
    unsellable: List[UnsellableView],
    chart_cid: Optional[str] = None,
    yearly_chart_cid: Optional[str] = None,
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
            f'<td style="{_TD}color:{COLOR_LOSS};">{_won(-summary.fees)}</td>'
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
            *_cumulative_items(monthly, cash),
        ]
    )
    if chart_cid:
        parts.append(_chart_img(chart_cid, "이번 달 누적 순손익 추이"))

    # 주문가능금액은 기간과 무관한 값이라 월 블록에만 둔다
    parts.extend(
        [
            f'<h3 style="font-size:15px; margin:20px 0 8px;">올해 누적 ({summary.day:%Y}년 기준)</h3>',
            *_cumulative_items(yearly),
        ]
    )
    if yearly_chart_cid:
        parts.append(_chart_img(yearly_chart_cid, "올해 달별 누적 순손익 추이"))

    if unsellable:
        parts.append(
            f'<h3 style="font-size:15px; margin:20px 0 8px;">{UNSELLABLE_HEADING}</h3>'
        )
        parts.append('<ul style="margin:0; padding-left:18px; color:#333333;">')
        parts.extend(
            f"<li>{escape(item.label)} — "
            f'<span style="color:{COLOR_WARN};">{escape(_unsellable_reason(item))}</span></li>'
            for item in unsellable
        )
        parts.append("</ul>")

    parts.extend(
        [
            '<p style="font-size:12px; color:#777777; margin-top:18px;">'
            + "<br>".join(escape(n) for n in notes)
            + "</p>",
            "</div>",
        ]
    )
    return "".join(parts)


# ── 09:08 매수 알림 ─────────────────────────────────────────
BUY_HEADERS = ("종목", "상태", "수량", "단가", "투입금액", "익절가", "손절가")

BUY_OUTCOME_LABELS = {
    BuyOutcome.FILLED: "체결",
    BuyOutcome.PARTIALLY_FILLED: "부분체결",
    BuyOutcome.ORDERED: "접수",
    BuyOutcome.SKIPPED: "건너뜀",
    BuyOutcome.FAILED: "실패",
    BuyOutcome.CANCELLED: "미체결 취소",
}


def _buy_notes(execution: BuyExecution) -> List[str]:
    notes = []
    if not execution.fills_synced:
        notes.append("※ 체결 내역 조회에 실패해 접수 기준으로 표기했습니다.")
    if any(r.outcome == BuyOutcome.ORDERED for r in execution.records):
        notes.append(
            "※ '접수'는 주문이 받아들여진 상태로, 체결가는 아직 확정되지 않았습니다 — "
            "단가는 주문에 쓴 목표 매수가입니다."
        )
    if any(r.outcome == BuyOutcome.CANCELLED for r in execution.records):
        notes.append(
            "※ '미체결 취소'는 10:10까지 목표 매수가에 닿지 않아 주문을 거둔 종목입니다 — "
            "그날 그 종목은 매수하지 않습니다."
        )
    notes.append(
        "※ 익절가·손절가는 위 익절/손절 라인에 닿는 가격입니다 "
        "(표의 단가 기준, 수수료·세금·슬리피지 반영)."
    )
    # 단순익절만 종목별 판정이라 익절가가 참고값이 아니라 그 종목의 실제 매도 지점이다
    if execution.simple_take_profit:
        notes.append(
            "※ 단순익절은 종목마다 따로 판정해 그 종목만 매도하고 나머지는 계속 보유합니다. "
            "손절만 보유 종목 전체를 합산해 판정하며, 닿으면 전량 매도합니다 — 이익 난 종목이 "
            "먼저 빠지면 남은 종목의 손실을 상쇄할 것이 없어져 손절이 더 쉽게 걸립니다."
        )
    else:
        notes.append(
            "※ 실제 판정은 계좌 평단가로 보유 종목 전체를 합산해 하며, 조건에 닿으면 전량 매도합니다 "
            "— 종목별 익절/손절은 없습니다. 위 가격은 '이 종목 혼자였다면' 기준의 참고값입니다."
        )
    notes.extend(
        [
            "※ 익절/손절 감시는 이 프로그램이 실행 중일 때만 동작합니다 (키움 REST 스탑오더 미지원).",
            "※ 체결가·수수료·손익은 15:35 리포트에서 확정됩니다.",
        ]
    )
    return notes


def _take_profit_line(execution: BuyExecution) -> str:
    """익절선 표기 — 단순익절이면 '+0.00%'로 적어 꺼진 것처럼 보이지 않게 이름으로 적는다."""
    if execution.simple_take_profit:
        return "단순익절 (종목별 0% 초과)"
    return f"+{execution.take_profit_percent:.2f}%"


def _buy_facts(execution: BuyExecution) -> List[tuple[str, str]]:
    """머리말의 라벨/값 쌍 — 평문과 HTML이 같은 값을 쓰도록 한 곳에서 만든다.

    종목당 배정 비율은 전략 규칙(예수금의 1/6)을 그대로 적지 않고 실제 값에서 계산한다 —
    규칙이 바뀌어도 메일이 거짓말하지 않는다.
    """
    share = (
        f" (주문가능금액의 {execution.amount_per_stock / execution.cash * 100:.1f}%)"
        if execution.cash > 0
        else ""
    )
    return [
        ("주문가능금액", _balance(execution.cash)),
        ("종목당 배정", f"{_balance(execution.amount_per_stock)}{share}"),
        ("총 투입금액", _balance(execution.invested)),
        (
            "익절 / 손절 라인 (합산 순손익)",
            f"{_take_profit_line(execution)} / -{execution.stop_loss_percent:.2f}%",
        ),
    ]


def _buy_row_cells(record: BuyRecord, execution: BuyExecution) -> tuple[str, ...]:
    """표 한 줄의 셀 값 — 매수하지 못한 종목은 금액 칸을 비운다 (사유는 표 아래에 적는다)."""
    state = BUY_OUTCOME_LABELS[record.outcome]
    if not record.outcome.is_ordered or record.price <= 0 or record.shares <= 0:
        return (record.label, state, "-", "-", "-", "-", "-")
    commission_rate = execution.commission_percent / 100
    tax_rate = execution.tax_percent / 100
    slippage_rate = execution.slippage_percent / 100
    take_profit_price = exit_trigger_price(
        record.price, execution.take_profit_percent / 100, commission_rate, tax_rate, slippage_rate
    )
    stop_loss_price = exit_trigger_price(
        record.price, -execution.stop_loss_percent / 100, commission_rate, tax_rate, slippage_rate
    )
    return (
        record.label,
        state,
        f"{record.shares:,}",
        f"{record.price:,.0f}",
        f"{record.amount:,.0f}",
        f"{take_profit_price:,.0f}",
        f"{stop_loss_price:,.0f}",
    )


def _buy_text(execution: BuyExecution, notes: List[str]) -> str:
    lines = [f"{execution.at:%Y-%m-%d %H:%M} 매수 실행 결과", ""]

    facts = _buy_facts(execution)
    label_width = max(_display_width(label) for label, _ in facts)
    lines.extend(f"- {_pad(label, label_width, right=False)}  {value}" for label, value in facts)
    lines.append("")

    if not execution.records:
        lines.append("매수를 시도한 종목이 없습니다.")
    else:
        cells = [BUY_HEADERS] + [_buy_row_cells(r, execution) for r in execution.records]
        widths = [max(_display_width(row[i]) for row in cells) for i in range(len(BUY_HEADERS))]
        divider = "-" * (sum(widths) + 2 * (len(widths) - 1))

        for i, row in enumerate(cells):
            lines.append(
                "  ".join(_pad(v, w, right=(c > 0)) for c, (v, w) in enumerate(zip(row, widths)))
            )
            if i == 0:
                lines.append(divider)
        lines.append(divider)

    if execution.not_bought:
        lines.extend(["", "매수하지 못한 종목"])
        lines.extend(
            f"- {r.label} — {r.note or BUY_OUTCOME_LABELS[r.outcome]}"
            for r in execution.not_bought
        )

    lines.extend(["", *notes])
    return "\n".join(lines)


def _buy_html(execution: BuyExecution, notes: List[str]) -> str:
    parts = [
        '<div style="font-family:-apple-system,\'Malgun Gothic\',sans-serif; font-size:14px; color:#222222;">',
        f'<h2 style="font-size:17px; margin:0 0 14px;">{execution.at:%Y-%m-%d %H:%M} 매수 실행 결과</h2>',
        '<ul style="margin:0 0 16px; padding-left:18px; color:#333333;">',
    ]
    parts.extend(f"<li>{label}: {value}</li>" for label, value in _buy_facts(execution))
    parts.append("</ul>")

    if not execution.records:
        parts.append('<p style="color:#555555;">매수를 시도한 종목이 없습니다.</p>')
    else:
        parts.append('<table style="border-collapse:collapse; font-size:14px;">')
        header = "".join(
            f'<th style="{_TH}{"text-align:left;" if i == 0 else ""}">{h}</th>'
            for i, h in enumerate(BUY_HEADERS)
        )
        parts.append(f"<tr>{header}</tr>")

        for record in execution.records:
            row = _buy_row_cells(record, execution)
            tone = "" if record.outcome.is_ordered else f"color:{COLOR_WARN};"
            cells = "".join(
                f'<td style="{_TD}{"text-align:left;" if i == 0 else ""}{tone}">{escape(v)}</td>'
                for i, v in enumerate(row)
            )
            parts.append(f"<tr>{cells}</tr>")
        parts.append("</table>")

    if execution.not_bought:
        parts.append('<h3 style="font-size:15px; margin:20px 0 8px;">매수하지 못한 종목</h3>')
        parts.append(f'<ul style="margin:0; padding-left:18px; color:{COLOR_WARN};">')
        parts.extend(
            f"<li>{escape(r.label)} — {escape(r.note or BUY_OUTCOME_LABELS[r.outcome])}</li>"
            for r in execution.not_bought
        )
        parts.append("</ul>")

    parts.extend(
        [
            '<p style="font-size:12px; color:#777777; margin-top:18px;">'
            + "<br>".join(escape(n) for n in notes)
            + "</p>",
            "</div>",
        ]
    )
    return "".join(parts)
