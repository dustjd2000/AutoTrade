"""체결강도 계산기 — 최근 몇 분의 매수 체결량 ÷ 매도 체결량 × 100 (스펙 2026-11-11 4.1).

2호 전략 장중 전환이 "다른 사람들의 매수 추세"를 보는 두 입력 중 하나다. KODEX 200(069500)의
실시간 체결 틱에서 부호 있는 체결량(양수 = 매수 체결, 음수 = 매도 체결)을 받아 쌓는다.
창이 다 차기 전에는 값을 내지 않는다("모름") — 몇 틱만으로 방향을 판정하지 않기 위해서다.
"""
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Optional, Tuple

# 틱이 이보다 오래 끊기면(재접속 등) 그 사이의 체결을 모르므로 창을 처음부터 다시 채운다
GAP_SECONDS = 60.0
# 매도 체결이 하나도 없을 때의 상한 — 0으로 나누지 않는다
MAX_STRENGTH = 999.0


class TradeStrength:
    def __init__(self, window_minutes: float = 5.0):
        self.window_minutes = window_minutes
        self._window = timedelta(minutes=window_minutes)
        self._ticks: Deque[Tuple[datetime, int]] = deque()
        # 창 안 합계 — 틱마다 다시 더하지 않도록 넣고 뺄 때 함께 고친다
        self._buy = 0
        self._sell = 0
        # 끊김 없이 틱을 받기 시작한 시각. 여기서 창 길이만큼 지나야 값을 낸다
        self._covered_from: Optional[datetime] = None
        self._last_at: Optional[datetime] = None

    def add(self, at: datetime, signed_volume: int) -> None:
        if self._last_at is None or (at - self._last_at).total_seconds() > GAP_SECONDS:
            self._ticks.clear()
            self._buy = self._sell = 0
            self._covered_from = at
        self._last_at = at
        if signed_volume > 0:
            self._buy += signed_volume
        elif signed_volume < 0:
            self._sell -= signed_volume
        else:
            return
        self._ticks.append((at, signed_volume))
        self._trim(at)

    def _trim(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._ticks and self._ticks[0][0] < cutoff:
            _, volume = self._ticks.popleft()
            if volume > 0:
                self._buy -= volume
            else:
                self._sell += volume

    def value(self, now: datetime) -> Optional[float]:
        """`now` 기준 창 안의 체결강도. 창이 덜 찼거나 체결이 없으면 None("모름")."""
        if self._covered_from is None or now - self._covered_from < self._window:
            return None
        self._trim(now)
        if self._buy == 0 and self._sell == 0:
            return None
        if self._sell == 0:
            return MAX_STRENGTH
        return min(self._buy / self._sell * 100, MAX_STRENGTH)


def strength_between(
    past_volume: Optional[int],
    past_strength_cum: Optional[float],
    now_volume: Optional[int],
    now_strength_cum: Optional[float],
) -> Optional[float]:
    """두 시점의 누적값(당일 누적 거래량 V, 누적 체결강도 C)으로 그 사이 구간의 체결강도를 구한다.

    C = 누적 매수량 ÷ 누적 매도량 × 100 이므로 r = C/100 일 때 누적 매수 B = V·r/(1+r),
    누적 매도 S = V/(1+r) 다. 구간 체결강도 = (B2-B1) ÷ (S2-S1) × 100.
    엔진이 재시작돼 틱 창이 비어 있을 때 분 스냅샷으로 5분 체결강도를 이어 보려는 용도다.
    C는 소수 둘째 자리로 반올림돼 오므로 ΔB가 살짝 음수가 될 수 있다 — 0으로 자르고 나눈다.
    입력이 없거나 C가 0 이하이거나 거래량이 줄었으면 None("모름").
    """
    if past_volume is None or past_strength_cum is None:
        return None
    if now_volume is None or now_strength_cum is None:
        return None
    if past_strength_cum <= 0 or now_strength_cum <= 0 or now_volume < past_volume:
        return None
    r1 = past_strength_cum / 100
    r2 = now_strength_cum / 100
    buy = max(now_volume * r2 / (1 + r2) - past_volume * r1 / (1 + r1), 0.0)
    sell = now_volume / (1 + r2) - past_volume / (1 + r1)
    if sell <= 0:
        return MAX_STRENGTH if buy > 0 else None
    return min(buy / sell * 100, MAX_STRENGTH)
