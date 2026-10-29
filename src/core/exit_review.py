"""15:35 매도 판단 검증의 순수 계산 (스펙 2026-09-22 3.2).

잣대는 순수익이다 — 순손익(수수료·세금 차감)이 플러스로 확정됐는가. 덜 잃은 것, 종가보다
나은 값에 판 것은 성공이 아니다. 분류는 여기서 코드가 정한다 — LLM이 자기 판단을 채점하면서
분류까지 정하면 잣대가 흔들린다.

보유 중 고점은 `position_peaks`(슬리피지를 뺀 순손익률)이고 최종 순손익률은 체결 기준이라
슬리피지만큼 기준이 다르다. 분류 경계(0)에는 영향이 거의 없어 그대로 쓴다.
"""
from datetime import date
from typing import Dict, FrozenSet, List, Optional

from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow, TradeRow

CAPTURED = "captured"
MISSED = "missed"
NO_CHANCE = "no_chance"
UNKNOWN = "unknown"

OUTCOME_LABELS: Dict[str, str] = {
    CAPTURED: "순이익 확정",
    MISSED: "순이익 기회를 놓침",
    NO_CHANCE: "순이익 기회 없음",
    UNKNOWN: "판정 불가",
}


def classify_outcome(net_return: Optional[float], peak_return: Optional[float]) -> str:
    """순손익이 플러스면 고점과 무관하게 확정이다. 아니면 고점으로 기회가 있었는지 가른다."""
    if net_return is None:
        return UNKNOWN
    if net_return > 0:
        return CAPTURED
    if peak_return is None:
        return UNKNOWN
    if peak_return > 0:
        return MISSED
    return NO_CHANCE


def build_exit_review_rows(
    day: date,
    trades: List[TradeRow],
    peaks: Dict[str, float],
    decisions: List[AIExitDecisionRow],
    exit_reasons: Dict[str, str],
    unknown_pnl_tickers: FrozenSet[str] = frozenset(),
) -> List[ExitReviewRow]:
    """그날 매도한 종목마다 검증 한 줄. 팔지 않은 종목(sell_price None)은 뺀다.

    순손익 = 실현손익 − 그 종목의 당일 수수료·세금(매수분 포함, `TradeRow.fees`).
    버전은 그 종목의 **마지막** 판단에 쓰인 매도 프롬프트 버전이다.

    `unknown_pnl_tickers`에 든 종목은 순손익을 **일부만** 모르는 경우다 (F2, 2026-09-22
    최종 리뷰). `TradeRow.pnl`(`trade_store._pair_by_ticker`)은 그 종목의 매도 중 **알려진**
    `realized_pnl`만 합산하므로, 한 건이라도 불명이면 나머지 합계가 그대로 나와 "안다"로
    잘못 보인다 (스펙 3.2 "일부라도 모르면 순손익 모름"). 이 집합에 든 종목은 net_pnl/
    net_return을 강제로 None(=UNKNOWN)으로 떨어뜨린다.
    """
    by_ticker: Dict[str, List[AIExitDecisionRow]] = {}
    for d in decisions:
        by_ticker.setdefault(d.ticker, []).append(d)

    rows = []
    for trade in trades:
        if trade.sell_price is None:
            continue
        if trade.ticker in unknown_pnl_tickers:
            net_pnl = None
            net_return = None
        else:
            net_pnl = trade.pnl - trade.fees if trade.pnl is not None else None
            net_return = net_pnl / trade.cost if net_pnl is not None and trade.cost > 0 else None
        mine = by_ticker.get(trade.ticker, [])
        peak = peaks.get(trade.ticker)
        rows.append(
            ExitReviewRow(
                day=day,
                ticker=trade.ticker,
                name=trade.name or "",
                exit_prompt_version=mine[-1].exit_prompt_version if mine else "",
                net_pnl=net_pnl,
                net_return=net_return,
                peak_return=peak,
                outcome=classify_outcome(net_return, peak),
                exit_reason=exit_reasons.get(trade.ticker, ""),
                decision_count=len(mine),
            )
        )
    return rows
