"""2호 전략 규칙 후보 탐색과 자동 적용 조건 판정 (스펙 2026-10-29 3·4절).

순수 함수만 둔다 — DB·메일·`.env`는 `rule_tuning`이 맡는다.
1등은 고르기 구간(최근 63거래일을 뺀 앞 기간)에서만 고르고, 시험 구간(최근 63거래일)은 현행과의
비교에만 쓴다. 시험 구간이 고르기에 새면 우연히 좋은 후보를 고르게 된다.
"""
import itertools
from dataclasses import dataclass
from statistics import mean
from typing import List, Optional, Sequence

from src.research.backtest import Rule

SIGNAL_BANDS = (0.0, 0.1, 0.2, 0.3)
FLIP_OPTIONS = (True, False)
DECISION_TIMES = ("09:05", "09:10", "09:30", "10:00")
STOP_PERCENTS = (1.0, 1.5, 2.0, 2.5, 3.0)

TEST_DAYS = 63
MIN_SELECT_DAYS = 57
# 시험 구간에서 1등이 현행보다 이만큼(%p/일) 나아야 한다
MIN_EDGE_PERCENT = 0.05
# 같은 1등이 이만큼 연속돼야 적용하고, 적용한 뒤 이만큼 평가가 지나야 다시 적용한다
STREAK_EVALUATIONS = 4
COOLDOWN_EVALUATIONS = 4

STATUS_EVALUATED = "evaluated"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_FAILED = "failed"

_TIE_TOLERANCE = 1e-12


def candidates() -> List[Rule]:
    return [
        Rule(band, flip, hhmm, stop)
        for band, flip, hhmm, stop in itertools.product(
            SIGNAL_BANDS, FLIP_OPTIONS, DECISION_TIMES, STOP_PERCENTS
        )
    ]


def in_range(rule: Rule) -> bool:
    return (
        rule.band_percent in SIGNAL_BANDS
        and rule.flip_enabled in FLIP_OPTIONS
        and rule.decision_hhmm in DECISION_TIMES
        and rule.stop_percent in STOP_PERCENTS
    )


def distance(a: Rule, b: Rule) -> int:
    """다른 손잡이 수 — 동점일 때 현행에 가까운 후보를 고르는 데 쓴다."""
    return sum(
        (a.band_percent != b.band_percent, a.flip_enabled != b.flip_enabled,
         a.decision_hhmm != b.decision_hhmm, a.stop_percent != b.stop_percent)
    )


@dataclass(frozen=True)
class Evaluation:
    current: Rule
    best: Rule
    select_current: float
    select_best: float
    test_current: float
    test_best: float

    @property
    def beats(self) -> bool:
        return self.test_best - self.test_current >= MIN_EDGE_PERCENT - _TIE_TOLERANCE

    @property
    def positive(self) -> bool:
        return self.test_best > 0


def evaluate(backtester, current: Rule) -> Optional[Evaluation]:
    """고르기 구간 1등과 현행을 시험 구간에서 비교한다. 거래일이 모자라면 None.

    일 평균은 쉬는 날을 0%로 넣어 전체 거래일로 나눈다 (`Backtester.day_return`이 쉬면 0을 준다).
    """
    days = backtester.days
    if len(days) < TEST_DAYS + MIN_SELECT_DAYS:
        return None
    select, test = days[:-TEST_DAYS], days[-TEST_DAYS:]

    def average(rule: Rule, window: Sequence[str]) -> float:
        return mean(backtester.returns(rule, window))

    scored = [(average(rule, select), rule) for rule in candidates()]
    top = max(score for score, _ in scored)
    best = min(
        (rule for score, rule in scored if score >= top - _TIE_TOLERANCE),
        key=lambda rule: distance(rule, current),
    )
    return Evaluation(
        current=current,
        best=best,
        select_current=average(current, select),
        select_best=top,
        test_current=average(current, test),
        test_best=average(best, test),
    )


def streak_count(rows) -> int:
    """끝에서부터 '평가함 + 조건 1·2 충족 + 같은 1등'이 몇 번 이어졌는지."""
    count, target = 0, None
    for row in reversed(list(rows)):
        if not (row.status == STATUS_EVALUATED and row.beats and row.positive):
            break
        if target is None:
            target = row.best_rule
        elif row.best_rule != target:
            break
        count += 1
    return count


def cooldown_left(rows) -> int:
    """마지막 적용 뒤 다시 적용할 수 있을 때까지 남은 평가 횟수 (0이면 가능)."""
    rows = list(rows)
    applied = [i for i, row in enumerate(rows) if row.applied]
    if not applied:
        return 0
    since = len(rows) - 1 - applied[-1]
    return max(0, COOLDOWN_EVALUATIONS - since)
