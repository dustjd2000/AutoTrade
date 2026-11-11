"""2호 전략 규칙 백테스트 — 1분봉으로 규칙 하나의 거래일별 순수익률을 낸다 (스펙 2026-10-29 5.2).

실전과 어긋나지 않도록 방향은 실전 함수(`etf_regime.pick_etf`)로 정한다. 신호는 직전 20거래일
KODEX 200의 장중(첫 봉 → 마지막 봉) 수익률 평균이다 — 실전은 일봉 시가·종가를 쓰므로 첫 봉 종가만큼
차이가 난다. 청산은 실전처럼 손절·익절·고점 반납·15:15를 재현한다. AI 매도 판단은 재현하지 않는다.

장중 전환(스펙 2026-11-11)은 **지수 가격 조건(C)만** 재현한다 — 1분봉에는 매수/매도 체결 구분이 없어
체결강도 조건(B)을 넣을 수 없다. 그래서 백테스트는 실전보다 전환이 많다("B 없이 가장 자주 전환했을 때").
"""
import json
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

from src.research.minute_store import Bar, DayBars
from src.strategy.etf_regime import LONG_ETF, LOOKBACK_DAYS, pick_etf

# 반일장·수집 누락일을 거르는 기준 — 정규장 하루는 약 390봉이다
MIN_BARS_PER_DAY = 300
CLOSE_OUT_HHMM = "1515"
# 시장가 체결이 호가 한 칸을 건너간다고 보고 체결마다 반칸(ETF 호가단위 5원)을 잃는다
HALF_TICK_WON = 2.5
# 장중 전환 판정 마감 — 실전(`etf_switch.SWITCH_END_TIME`)과 같다
SWITCH_END_HHMM = "1514"


@dataclass(frozen=True)
class Rule:
    band_percent: float
    flip_enabled: bool
    decision_hhmm: str      # "HH:MM" — `.env`의 DECISION_TIME 꼴
    stop_percent: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "Rule":
        return Rule(**json.loads(text))


def usable_days(long_bars: DayBars, inverse_bars: DayBars) -> List[str]:
    """두 종목 모두 봉이 충분한 날 (오름차순)."""
    return sorted(
        day
        for day in long_bars
        if day in inverse_bars
        and len(long_bars[day]) >= MIN_BARS_PER_DAY
        and len(inverse_bars[day]) >= MIN_BARS_PER_DAY
    )


def intraday_return(bars: List[Bar]) -> float:
    return (bars[-1][1] / bars[0][1] - 1) * 100


def _leg(
    bars: List[Bar],
    entry_index: int,
    stop_percent: float,
    commission_ratio: float,
    half_tick: float,
    take_profit_percent: float,
    trail_activate_percent: float,
    trail_giveback_percent: float,
    index_bars: Optional[List[Bar]] = None,
    is_long: bool = True,
    switch_percent: float = 0.0,
) -> Tuple[Optional[float], Optional[str], bool]:
    """`entry_index` 봉에 사서 청산 조건에 처음 닿은 봉에 판다. (순손익률, 판 봉의 HHMM, 전환 여부).

    청산 조건(실전과 같은 순서): 순손익률 ≤ −손절, ≥ +익절, 고점(≥ 활성 문턱)에서 반납폭만큼 하락,
    15:15 이후 첫 봉. 그 어느 것도 아니면 전환 조건을 본다 — `switch_percent`가 0보다 크고, 지수
    (`index_bars` = KODEX 200)가 산 봉 대비 보유 방향과 반대로 그만큼 움직였고, 순손익이 마이너스이며
    15:14 이전이다. 익절·고점 반납·전환은 0이면 끈다. 산 뒤에 봉이 없으면 순손익률은 None.
    """
    entry_time, entry_price = bars[entry_index]
    entry = entry_price + half_tick
    index_by_time: Dict[str, float] = dict(index_bars) if index_bars and switch_percent > 0 else {}
    # 산 봉의 지수 가격 — 그 분에 지수 봉이 없으면 그 앞의 마지막 봉
    index_entry = next((p for t, p in reversed(index_bars or []) if t <= entry_time), None)
    index_now = index_entry
    net, sold_at = None, None
    peak = float("-inf")
    for t, price in bars[entry_index + 1:]:
        net = ((price - half_tick) / entry - 1 - 2 * commission_ratio) * 100
        sold_at = t
        peak = max(peak, net)
        if (
            net <= -stop_percent
            or (take_profit_percent > 0 and net >= take_profit_percent)
            or (trail_giveback_percent > 0 and peak >= trail_activate_percent
                and peak - net >= trail_giveback_percent)
            or t >= CLOSE_OUT_HHMM
        ):
            return net, t, False
        if index_by_time and index_entry:
            index_now = index_by_time.get(t, index_now)
            move = (index_now / index_entry - 1) * 100
            against = -move if is_long else move
            if against >= switch_percent and net < 0 and t <= SWITCH_END_HHMM:
                return net, t, True
    return net, sold_at, False


def trade_return(
    bars: List[Bar],
    entry_hhmm: str,
    stop_percent: float,
    commission_ratio: float,
    half_tick: float = HALF_TICK_WON,
    take_profit_percent: float = 0.0,
    trail_activate_percent: float = 0.0,
    trail_giveback_percent: float = 0.0,
) -> Optional[float]:
    """`entry_hhmm`(HHMM) 이후 첫 봉에 사서, 청산 조건에 처음 닿은 봉에 판다 (`_leg` 참고).

    순손익률(%) = (매도가 − 반호가) / (매수가 + 반호가) − 1 − 수수료 양방향. 진입할 봉이 없으면 None.
    """
    entry_index = next((i for i, (t, _) in enumerate(bars) if t >= entry_hhmm), None)
    if entry_index is None:
        return None
    return _leg(
        bars, entry_index, stop_percent, commission_ratio, half_tick,
        take_profit_percent, trail_activate_percent, trail_giveback_percent,
    )[0]


def switch_day_return(
    first_bars: List[Bar],
    second_bars: List[Bar],
    index_bars: List[Bar],
    first_is_long: bool,
    entry_hhmm: str,
    stop_percent: float,
    commission_ratio: float,
    switch_percent: float,
    half_tick: float = HALF_TICK_WON,
    take_profit_percent: float = 0.0,
    trail_activate_percent: float = 0.0,
    trail_giveback_percent: float = 0.0,
) -> Optional[float]:
    """장중 전환을 넣은 하루 순수익률(%) — 두 다리의 합 (스펙 2026-11-11 8절).

    1번째 다리(`first_bars`)가 전환 조건에 닿으면 그 봉에 팔고 같은 봉의 반대 ETF(`second_bars`)를
    사서 2번째 다리를 시작한다. 2번째 다리는 같은 조건이면 팔고 끝낸다(다시 사지 않는다).
    `switch_percent`가 0이면 `trade_return`과 같다. 진입할 봉이 없으면 None.
    """
    entry_index = next((i for i, (t, _) in enumerate(first_bars) if t >= entry_hhmm), None)
    if entry_index is None:
        return None
    exits = (stop_percent, commission_ratio, half_tick,
             take_profit_percent, trail_activate_percent, trail_giveback_percent)
    first, sold_at, switched = _leg(
        first_bars, entry_index, *exits,
        index_bars=index_bars, is_long=first_is_long, switch_percent=switch_percent,
    )
    if not switched:
        return first
    second_index = next((i for i, (t, _) in enumerate(second_bars) if t >= sold_at), None)
    if second_index is None:
        return first
    second, _, _ = _leg(
        second_bars, second_index, *exits,
        index_bars=index_bars, is_long=not first_is_long, switch_percent=switch_percent,
    )
    return first + (second or 0.0)


class Backtester:
    """분봉 한 벌로 여러 규칙을 평가한다. 같은 (날짜, 종목, 진입 시각, 손절) 매매는 한 번만 계산한다."""

    def __init__(
        self,
        long_bars: DayBars,
        inverse_bars: DayBars,
        commission_ratio: float,
        take_profit_percent: float = 0.0,
        trail_activate_percent: float = 0.0,
        trail_giveback_percent: float = 0.0,
        switch_percent: float = 0.0,
    ):
        # 장중 전환의 지수 문턱(%) — 0이면 전환을 넣지 않는다 (체결강도 조건은 재현하지 못한다)
        self._switch = switch_percent
        self._take_profit = take_profit_percent
        self._trail_activate = trail_activate_percent
        self._trail_giveback = trail_giveback_percent
        self._long = long_bars
        self._inverse = inverse_bars
        self._commission = commission_ratio
        all_days = usable_days(long_bars, inverse_bars)
        # 신호에 직전 20거래일이 필요하므로 그 뒤부터가 평가할 수 있는 날이다
        self.days: List[str] = all_days[LOOKBACK_DAYS:]
        self._signals: Dict[str, float] = {
            day: mean(intraday_return(long_bars[x]) for x in all_days[i - LOOKBACK_DAYS:i])
            for i, day in enumerate(all_days)
            if i >= LOOKBACK_DAYS
        }
        self._trades: Dict[Tuple[str, str, str, float], Optional[float]] = {}

    def _trade(self, day: str, ticker: str, hhmm: str, stop_percent: float) -> Optional[float]:
        key = (day, ticker, hhmm, stop_percent)
        if key not in self._trades:
            is_long = ticker == LONG_ETF[0]
            bars = self._long[day] if is_long else self._inverse[day]
            exits = dict(
                take_profit_percent=self._take_profit,
                trail_activate_percent=self._trail_activate,
                trail_giveback_percent=self._trail_giveback,
            )
            if self._switch > 0:
                other = self._inverse[day] if is_long else self._long[day]
                self._trades[key] = switch_day_return(
                    bars, other, self._long[day], is_long, hhmm, stop_percent,
                    self._commission, self._switch, **exits,
                )
            else:
                self._trades[key] = trade_return(bars, hhmm, stop_percent, self._commission, **exits)
        return self._trades[key]

    def day_return(self, day: str, rule: Rule) -> float:
        """그날 규칙대로 매매한 순수익률(%). 쉬거나 진입할 봉이 없으면 0."""
        hhmm = rule.decision_hhmm.replace(":", "")
        bars = self._long[day]
        entry_price = next((price for t, price in bars if t >= hhmm), None)
        if entry_price is None:
            return 0.0
        morning = (entry_price / bars[0][1] - 1) * 100
        pick = pick_etf(self._signals[day], morning, rule.band_percent, rule.flip_enabled)
        if pick is None:
            return 0.0
        result = self._trade(day, pick[0], hhmm, rule.stop_percent)
        return 0.0 if result is None else result

    def returns(self, rule: Rule, days: Sequence[str]) -> List[float]:
        return [self.day_return(day, rule) for day in days]
