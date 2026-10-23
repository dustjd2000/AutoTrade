"""2호 전략의 AI 매도 판단 트리거 — 순손익률이 1%p 단계를 새로 넘을 때마다 (PRD 5.5-C '매도 판단').

1호는 15분 주기로 묻는다. 2호는 주기 없이, 종목 순손익률이 **+1%·+2%·+3%…**(이익 쪽 새 고점)
또는 **-1%·-2%·-3%…**(손실 쪽 새 저점)에 처음 닿을 때만 묻는다 (사용자 결정 2026-10-23).
같은 단계 안에 머무는 동안은 다시 묻지 않는다 — 30초 폴링마다 LLM을 부르지 않게 하는 장치다.
"""
import math
from datetime import date
from typing import Dict, Optional, Tuple

STEP_PERCENT = 1.0
# 단계가 자연히 상한이 되지만(손절 -5%), 급등락 날 비용을 묶어 두는 그물
MAX_TRIGGERS_PER_DAY = 20


def step_of(net_return: float) -> int:
    """순손익률(비율)이 속한 단계. +1.3% → 1, -2.1% → -2, ±1% 안쪽 → 0."""
    percent = net_return * 100
    if percent >= STEP_PERCENT:
        return int(math.floor(percent / STEP_PERCENT))
    if percent <= -STEP_PERCENT:
        return -int(math.floor(-percent / STEP_PERCENT))
    return 0


class StepTrigger:
    """종목별로 이미 물어본 가장 높은 이익 단계와 가장 낮은 손실 단계를 기억한다. 날이 바뀌면 비운다."""

    def __init__(self) -> None:
        self._day: Optional[date] = None
        self._fired: Dict[str, Tuple[int, int]] = {}   # ticker → (최고 이익 단계, 최저 손실 단계)
        self.count = 0

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._fired = {}
            self.count = 0

    def crossed(self, ticker: str, net_return: float, today: date) -> Optional[int]:
        """새 단계에 처음 닿았으면 그 단계를, 아니면 None. 닿은 단계는 기록한다."""
        self._roll(today)
        if self.count >= MAX_TRIGGERS_PER_DAY:
            return None
        step = step_of(net_return)
        if step == 0:
            return None
        high, low = self._fired.get(ticker, (0, 0))
        if step > high:
            self._fired[ticker] = (step, low)
        elif step < low:
            self._fired[ticker] = (high, step)
        else:
            return None
        self.count += 1
        return step
