from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List


@dataclass
class TracePoint:
    """한 시점의 손익 상태 — AI 매도 판단이 되돌림을 알아보는 근거다."""

    at: datetime
    portfolio_return: float          # 보유 합산 순손익률
    per_ticker: Dict[str, float]     # 종목별 순손익률
    prices: Dict[str, float]         # 종목별 현재가


class ExitTrace:
    """당일 순손익 궤적 (PRD 5.5-B 'AI 매도 판단').

    스냅샷 하나로는 +0.4%가 올라오는 중인지 +0.9%에서 밀린 것인지 구분할 수 없다.
    두 상황의 정답이 정반대라 경로가 필요하다.

    메모리에만 둔다 — 하루치 최대 24개 지점이라 파일로 남길 이유가 없고, 엔진을 다시
    켜면 비는 것이 정상이다. 그 사실은 `partial`로 드러낸다 — 없는 것을 있는 것처럼
    보여주면 AI가 궤적 전체를 봤다고 착각한다.
    """

    def __init__(self):
        self._points: List[TracePoint] = []

    def append(
        self,
        at: datetime,
        portfolio_return: float,
        per_ticker: Dict[str, float],
        prices: Dict[str, float],
    ) -> None:
        # 넘겨받은 dict를 그대로 들면 호출측이 나중에 고칠 때 과거 지점까지 바뀐다
        self._points.append(
            TracePoint(
                at=at,
                portfolio_return=portfolio_return,
                per_ticker=dict(per_ticker),
                prices=dict(prices),
            )
        )

    def points(self) -> List[TracePoint]:
        """사본을 돌려준다 — 호출측이 리스트를 건드려도 내부 상태가 흔들리지 않는다."""
        return list(self._points)

    @property
    def count(self) -> int:
        return len(self._points)

    @property
    def partial(self) -> bool:
        """궤적이 비어 있는가 — 엔진을 장중에 다시 켠 직후가 그 상태다."""
        return not self._points

    def clear(self) -> None:
        """일일 초기화(08:40)에서 비운다."""
        self._points.clear()
