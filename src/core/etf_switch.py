"""2호 전략 장중 전환 판정 — 산 ETF가 틀린 방향이면 반대 ETF로 갈아탄다 (스펙 2026-11-11 3절).

세 조건이 모두 참일 때만 판정한다: KODEX 200 가격이 산 시점 대비 보유 방향과 반대로 문턱 이상
움직였고(C), KODEX 200 체결강도도 반대이고(B), 보유 종목 순손익이 마이너스다. 그날 1번째 종목이면
전환(매도 후 반대 ETF 매수), 2번째 종목이면 매도만이다.

여기는 순수 로직이다 — 주문·DB·시세 수신은 엔진(`TradingEngine._check_etf_switch`)이 맡는다.
근거가 약한 규칙이다: C만의 백테스트는 어느 문턱에서도 현행보다 나빴고 B는 검증되지 않았다 (스펙 1·11절).
"""
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from enum import Enum
from typing import Iterable, Optional, Set, Tuple

from src.strategy.etf_regime import INVERSE_ETF, LONG_ETF

# 이 시각을 넘으면 판정하지 않는다 (2026-11-12 사용자 지시로 15:14 → 14:30) — 늦게 갈아타면
# 곧 15:15 강제청산이라 왕복 비용만 낸다. 1절 백테스트도 14:30 마감으로 돌렸다
SWITCH_END_TIME = dt_time(14, 30)
# 전환 매수(매도 체결 대기 포함)의 마감 — 15:15 강제청산과 겹치지 않기 위한 여유.
# 판정 마감과 따로 둔다: 14:29에 판 종목의 반대 ETF를 14:30에 포기하면 안 된다
SWITCH_BUY_END_TIME = dt_time(15, 14)
# KODEX 200 틱이 이보다 오래됐으면 지수 가격을 모르는 것으로 본다
INDEX_STALE_SECONDS = 60.0
# `ExitReason.ETF_SWITCH.value` — 다리가 전환으로 끝났는지 가리는 데 쓴다
SWITCH_REASON = "etf_switch"


class SwitchKind(Enum):
    SWITCH = "switch"          # 1번째 종목 — 매도 후 반대 ETF 매수
    SELL_ONLY = "sell_only"    # 2번째 종목 — 매도만, 현금으로 마감


@dataclass(frozen=True)
class Decision:
    """판정 결과와 그 시점의 세 조건 값(모두 %) — 알림과 `etf_switch_legs` 기록에 쓴다."""
    kind: SwitchKind
    against: float
    strength: float
    net: float


@dataclass(frozen=True)
class Leg:
    leg: int                   # 절대 번호 — 홀수 = 첫 종목(아침·버튼 매수), 짝수 = 전환 매수
    ticker: str
    index_entry: float         # 이 종목을 산 시점의 KODEX 200 가격
    started_at: datetime


@dataclass(frozen=True)
class PendingSwitch:
    """전환 매도는 나갔고 반대 ETF 매수는 아직인 상태 (`SwitchFlow`가 집어 간다)."""
    sold_ticker: str
    sold_summary: str          # 알림에 실을 판 종목 요약
    quantity: int              # 매도 주문 수량 — 체결 확인의 기준
    sell_order_id: str
    buy_ticker: str
    buy_name: str
    index_entry: float         # 2번째 다리의 기준 지수 가격 = 매도 주문 시점의 KODEX 200
    decision: Decision
    at: datetime


def opposite_etf(ticker: str) -> Tuple[str, str]:
    return INVERSE_ETF if ticker == LONG_ETF[0] else LONG_ETF


class SwitchState:
    """오늘의 다리 상태. 날짜가 바뀌면 스스로 비운다.

    다리 번호는 회차로 이어 붙인다 (2026-11-13 사용자 지시): 1회차는 1·2, 버튼으로 다시 사면
    3·4, 그다음은 5·6… 홀수가 첫 종목, 짝수가 전환 종목이다. `switched_today`는 이름과 달리
    "현재 회차의 전환 1회를 썼는지"를 뜻한다.
    """

    def __init__(self, index_percent: float = 0.5):
        self.index_percent = index_percent
        self.day: Optional[date] = None
        self.switched_today = False
        self._open: Optional[Leg] = None
        self._seen: Set[int] = set()
        self._base = 0  # 현재 회차의 다리는 _base+1(첫 종목)·_base+2(전환 종목)

    @property
    def round_base(self) -> int:
        return self._base

    def _roll(self, today: date) -> None:
        if self.day != today:
            self.day = today
            self.switched_today = False
            self._open = None
            self._seen = set()
            self._base = 0

    def start_round(self, today: date) -> None:
        """버튼 매수 — 현재 회차가 이미 시작됐으면 새 회차를 연다 (아직이면 그대로 쓴다)."""
        self._roll(today)
        if self._base + 1 in self._seen:
            self._base = ((max(self._seen) + 1) // 2) * 2
            self.switched_today = False
            self._open = None

    def absolute(self, position: int) -> int:
        """현재 회차 안의 위치(1 = 첫 종목, 2 = 전환 종목)를 절대 다리 번호로 바꾼다."""
        return self._base + position

    def has_leg(self, leg: int, today: date) -> bool:
        """오늘 이 번호(절대)의 다리를 이미 시작했는지 (끝났어도 True)."""
        return self.day == today and leg in self._seen

    def begin_leg(self, leg: int, ticker: str, index_entry: float, at: datetime) -> None:
        """`leg`는 절대 다리 번호다."""
        self._roll(at.date())
        self._open = Leg(leg, ticker, index_entry, at)
        self._seen.add(leg)
        if leg % 2 == 0:
            self.switched_today = True

    def open_leg(self, ticker: str, today: date) -> Optional[Leg]:
        if self.day != today or self._open is None or self._open.ticker != ticker:
            return None
        return self._open

    def end_leg(self, switched: bool = False) -> None:
        """열린 다리를 닫는다. `switched`면 오늘의 전환 1회를 쓴 것으로 남긴다 —
        반대 ETF 매수가 실패해도 다시 시도하지 않는다 (현금 마감)."""
        self._open = None
        if switched:
            self.switched_today = True

    def restore(self, rows: Iterable, today: date) -> None:
        """엔진 재시작 뒤 `etf_switch_legs`의 오늘 행으로 상태를 되살린다 (스펙 5.4).

        전환으로 끝난 1번째 다리만 있고 2번째 다리가 없으면(매도 뒤·매수 전에 재시작) 전환을
        쓴 것으로 본다 — 다시 사면 이중 매수 위험이 있다. 현재 회차보다 큰 홀수 다리가 나오면
        그 다리부터 새 회차다.
        """
        self._roll(today)
        for row in sorted(rows, key=lambda r: r.leg):
            if row.leg % 2 == 1 and row.leg > self._base + 1:
                self._base = row.leg - 1
                self.switched_today = False
                self._open = None
            self._seen.add(row.leg)
            if row.leg % 2 == 0 or row.end_reason == SWITCH_REASON:
                self.switched_today = True
            if row.ended_at is None:
                self._open = Leg(row.leg, row.ticker, row.index_entry, row.started_at)

    def evaluate(
        self,
        ticker: str,
        net: float,
        index_now: Optional[float],
        index_at: Optional[datetime],
        strength: Optional[float],
        now: datetime,
    ) -> Optional[Decision]:
        """세 조건이 모두 참이면 판정을, 아니면 None. `net`은 보유 종목 순손익률(%)."""
        if now.time() > SWITCH_END_TIME:
            return None
        leg = self.open_leg(ticker, now.date())
        if leg is None or leg.index_entry <= 0:
            return None
        if not index_now or index_at is None:
            return None
        if (now - index_at).total_seconds() > INDEX_STALE_SECONDS:
            return None
        if strength is None:
            return None

        move = (index_now / leg.index_entry - 1) * 100
        long = ticker == LONG_ETF[0]
        against = -move if long else move
        strength_against = strength < 100 if long else strength > 100
        if against < self.index_percent or not strength_against or net >= 0:
            return None
        kind = SwitchKind.SWITCH if leg.leg % 2 == 1 and not self.switched_today else SwitchKind.SELL_ONLY
        return Decision(kind, against, strength, net)
