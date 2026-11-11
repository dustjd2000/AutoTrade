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
