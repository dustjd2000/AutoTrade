from datetime import date
from typing import List

from src.llm.recommender import StockRecommendation
from src.logger.trade_store import DailySummary


def recommendation_email(recommendations: List[StockRecommendation], today: date) -> tuple[str, str]:
    """08:45 LLM 추천 결과 이메일 (PRD 5.5-B 3단계) — 자동 유효성 검증 없이 그대로 전달."""
    subject = f"[AutoTrade] {today:%Y-%m-%d} 급등 예상 대형주 추천 {len(recommendations)}종목"

    lines = [f"{today:%Y-%m-%d} LLM 추천 결과입니다.", ""]
    for i, r in enumerate(recommendations, start=1):
        lines.extend([f"{i}. {r.name} ({r.ticker})", f"   추천 근거: {r.reason}", ""])

    if len(recommendations) < 3:
        lines.append(
            f"※ 추천 종목이 {len(recommendations)}개로 3개 미만입니다. "
            "종목당 매수금액은 예수금의 1/6로 고정되며, 나머지 몫은 현금으로 유지됩니다."
        )
        lines.append("")

    lines.append("※ 이 추천은 사전 유효성 검증(거래정지·상장폐지 등)을 거치지 않았습니다.")
    return subject, "\n".join(lines)


def daily_report_email(
    summary: DailySummary, monthly_pnl: float, monthly_return_pct: float
) -> tuple[str, str]:
    """15:30 일일/월간 성과 리포트 이메일 (PRD 5.11)."""
    subject = f"[AutoTrade] {summary.day:%Y-%m-%d} 매매 결과 리포트"

    lines = [
        f"{summary.day:%Y-%m-%d} 매매 결과",
        "",
        f"- 매수 체결: {summary.buy_count}건",
        f"- 매도 체결: {summary.sell_count}건",
        f"- 당일 실현손익: {summary.realized_pnl:+,.0f}원",
        "",
        f"이번 달 누적 ({summary.day:%Y-%m} 기준)",
        f"- 누적 실현손익: {monthly_pnl:+,.0f}원",
        f"- 누적 수익률: {monthly_return_pct:+.2f}%",
        "",
        "※ 정규장 마감(15:30) 직전 집계이므로 마감 체결분이 반영되지 않았을 수 있습니다.",
    ]
    return subject, "\n".join(lines)
