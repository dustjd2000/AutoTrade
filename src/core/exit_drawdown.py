import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 하루에 허용할 이익 반납 트리거 수. 한 번 발동한 종목은 **새 고점을 다시 찍기 전까지**
# 다시 발동하지 않으니 트리거 하나당 당일 신고점이 하나씩 필요하지만, 계단식으로 오르는
# 날에는 그만큼 자주 발동한다. 공시 트리거(3회)보다 넉넉한 것은 임계치가 1%p로 낮아
# 발동 자체가 잦기 때문이다 — 상한을 3에 두면 오전에 다 쓰고 오후에는 정규 주기만 남는다.
MAX_URGENT_TRIGGERS_PER_DAY = 5


@dataclass
class Retracement:
    """당일 고점 순손익률과 지금 값의 차이 — '얼마나 반납했나'를 담는다."""

    peak: float      # 당일 고점 순손익률 (비율)
    current: float   # 현재 순손익률 (비율)

    @property
    def given_back(self) -> float:
        """고점 대비 반납폭 (비율, 0.03 = 3.00%p)."""
        return max(0.0, self.peak - self.current)

    @property
    def given_back_share(self) -> float:
        """고점 이익의 몇 할을 반납했는지 (0~1). 고점이 이익이 아니면 0이다."""
        if self.peak <= 0:
            return 0.0
        return min(1.0, self.given_back / self.peak)


class DrawdownTracker:
    """종목별 순손익 고점과 그 대비 반납폭을 실시간으로 따라간다 (PRD 5.5-B '이익 반납 감시').

    AI 매도 판단은 설정 주기(기본 15분)마다만 도는데, 고점은 그 사이를 스쳐 지나간다 —
    2026-09-10 HD현대중공업이 그랬다. 당일 고가 482,000원(순손익 +5.1%)은 어떤 판단
    시점에도 잡히지 않았고, AI가 본 최댓값은 13:49의 +4.46%였다. 그리고 한 시간에 걸쳐
    +1.41%까지 반납하는 동안 매 주기가 "여전히 플러스"라며 보유했다.

    그래서 **실시간 시세 콜백에서** 고점을 따라가고, 고점 대비 설정폭 이상 반납하면 호출
    주기를 기다리지 않고 AI 판단을 앞당긴다. 손절과 같은 경로에서 돌지만 하는 일이 다르다
    — 손절은 그 자리에서 팔고, 이쪽은 **AI를 부를 시점만 앞당긴다.** 팔지 말지는 AI가 정한다.

    판정 단위가 **종목별**인 이유: 합산 순손익률은 종목별 순손익률의 매입금액 가중평균이라
    합산 반납폭은 언제나 최대 종목의 반납폭 이하다. 합산으로 걸면 한 종목이 크게 밀려도
    다른 종목이 희석해 발동하지 않는다 — 2026-09-10이 정확히 그랬다(합산 반납 1.51%p,
    종목별 최대 3.05%p). 발동만 종목별이고, 팔 때는 그대로 보유 목록 전체다.
    """

    def __init__(self, threshold_ratio: float):
        # 고점 대비 이만큼(비율) 반납하면 발동한다. 0 이하면 감시를 끈 것으로 본다.
        self.threshold_ratio = threshold_ratio
        self._peaks: Dict[str, float] = {}
        # 발동한 뒤 내려간 종목 — 새 고점을 찍어야 다시 무장한다
        self._armed: Dict[str, bool] = {}
        self._portfolio_peak: Optional[float] = None
        self._urgent_pending = False
        self._urgent_triggers = 0

    # ── 갱신 ────────────────────────────────────────────────
    def update(
        self, per_ticker: Dict[str, float], portfolio: Optional[float] = None
    ) -> List[str]:
        """고점을 갱신하고, 이번 틱에 임계치를 새로 넘긴 종목코드를 돌려준다.

        **고점이 이익일 때만 발동한다** — 종일 마이너스인 종목이 더 밀리는 것은 '반납'이
        아니라 그냥 손실이고, 그 구간은 손절이 맡는다.
        """
        if portfolio is not None:
            if self._portfolio_peak is None or portfolio > self._portfolio_peak:
                self._portfolio_peak = portfolio

        crossed: List[str] = []
        for ticker, current in per_ticker.items():
            peak = self._peaks.get(ticker)
            if peak is None or current > peak:
                self._peaks[ticker] = current
                self._armed[ticker] = True
                continue
            if self.threshold_ratio <= 0 or not self._armed.get(ticker, False):
                continue
            if peak <= 0:
                continue
            if peak - current >= self.threshold_ratio:
                self._armed[ticker] = False
                crossed.append(ticker)

        if not crossed:
            return []

        if self._urgent_triggers >= MAX_URGENT_TRIGGERS_PER_DAY:
            logger.warning(
                "이익 반납이 임계치를 넘었지만 즉시 판단 트리거가 하루 상한(%d회)에 걸려 "
                "다음 정규 주기로 미룹니다: %s",
                MAX_URGENT_TRIGGERS_PER_DAY,
                crossed,
            )
            return []

        self._urgent_triggers += 1
        self._urgent_pending = True
        return crossed

    # ── 프롬프트 재료 ────────────────────────────────────────
    def retracement(self, ticker: str, current: float) -> Optional[Retracement]:
        """그 종목의 고점 대비 반납. 아직 고점을 모르면 None이다."""
        peak = self._peaks.get(ticker)
        if peak is None:
            return None
        return Retracement(peak=peak, current=current)

    def portfolio_retracement(self, current: float) -> Optional[Retracement]:
        if self._portfolio_peak is None:
            return None
        return Retracement(peak=self._portfolio_peak, current=current)

    # ── 즉시 판단 트리거 ─────────────────────────────────────
    @property
    def urgent_pending(self) -> bool:
        return self._urgent_pending

    def take_urgent(self) -> bool:
        """트리거를 소비한다 — AI 판단 사이클이 실제로 돌 때 한 번 부른다."""
        pending = self._urgent_pending
        self._urgent_pending = False
        return pending

    def clear(self) -> None:
        """일일 초기화(08:40)에서 비운다 — 어제 고점을 오늘 기준으로 쓰면 안 된다."""
        self._peaks.clear()
        self._armed.clear()
        self._portfolio_peak = None
        self._urgent_pending = False
        self._urgent_triggers = 0
