from types import SimpleNamespace

import pytest

from src.research import rule_search
from src.research.backtest import Rule
from src.research.rule_search import (
    STATUS_EVALUATED,
    STATUS_FAILED,
    candidates,
    cooldown_left,
    distance,
    evaluate,
    in_range,
    streak_count,
)

CURRENT = Rule(0.0, True, "09:10", 2.0)


def test_there_are_160_distinct_candidates():
    rules = candidates()
    assert len(rules) == 160 and len(set(rules)) == 160
    assert all(in_range(r) for r in rules)


def test_in_range_rejects_off_grid_values():
    assert not in_range(Rule(0.15, True, "09:10", 2.0))
    assert not in_range(Rule(0.2, True, "09:11", 2.0))


def test_distance_counts_changed_knobs():
    assert distance(CURRENT, CURRENT) == 0
    assert distance(CURRENT, Rule(0.2, False, "09:10", 2.0)) == 2


class FakeBacktester:
    """규칙마다 고르기·시험 구간 일 수익률을 정해 둔 가짜."""

    def __init__(self, n_days, scores):
        self.days = [f"d{i:03d}" for i in range(n_days)]
        self.scores = scores              # Rule -> (고르기 구간 값, 시험 구간 값)
        self.seen_days = []

    def returns(self, rule, days):
        self.seen_days.append(tuple(days))
        select, test = self.scores.get(rule, (-1.0, -1.0))
        test_days = set(self.days[-rule_search.TEST_DAYS:])
        return [test if d in test_days else select for d in days]


def test_evaluate_needs_enough_days():
    assert evaluate(FakeBacktester(119, {}), CURRENT) is None


def test_evaluate_picks_on_the_select_window_only():
    best = Rule(0.2, False, "09:10", 2.0)
    tempting = Rule(0.3, False, "10:00", 1.0)       # 시험 구간에서만 좋다 — 골라지면 안 된다
    bt = FakeBacktester(140, {CURRENT: (0.0, 0.01), best: (0.3, 0.1), tempting: (0.1, 5.0)})

    ev = evaluate(bt, CURRENT)

    assert ev.best == best
    assert ev.select_best == pytest.approx(0.3)
    assert ev.test_best == pytest.approx(0.1) and ev.test_current == pytest.approx(0.01)
    assert ev.beats and ev.positive


def test_evaluate_breaks_ties_toward_the_current_rule():
    near = Rule(0.0, True, "09:10", 2.5)
    far = Rule(0.3, False, "10:00", 1.0)
    bt = FakeBacktester(140, {near: (0.2, 0.1), far: (0.2, 0.1)})
    assert evaluate(bt, CURRENT).best == near


def test_beats_needs_the_margin():
    best = Rule(0.2, False, "09:10", 2.0)
    bt = FakeBacktester(140, {CURRENT: (0.0, 0.10), best: (0.3, 0.14)})
    ev = evaluate(bt, CURRENT)
    assert not ev.beats and ev.positive


def row(best="A", status=STATUS_EVALUATED, beats=True, positive=True, applied=False):
    return SimpleNamespace(status=status, best_rule=best, beats=beats, positive=positive, applied=applied)


def test_streak_counts_trailing_same_best_rows():
    assert streak_count([row("B"), row("A"), row("A"), row("A")]) == 3
    assert streak_count([row("A")] * 4) == 4
    assert streak_count([row("A"), row("A", beats=False)]) == 0
    assert streak_count([row("A"), row("A", status=STATUS_FAILED), row("A")]) == 1


def test_cooldown_after_an_applied_row():
    assert cooldown_left([row(), row()]) == 0
    assert cooldown_left([row(applied=True), row(), row()]) == 2
    assert cooldown_left([row(applied=True)] + [row()] * 4) == 0
