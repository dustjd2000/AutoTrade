from datetime import date

from src.core import etf_exit_trigger
from src.core.etf_exit_trigger import StepTrigger, step_of

DAY = date(2026, 10, 23)


def test_step_of_rounds_toward_zero_in_one_percent_steps():
    assert step_of(0.0099) == 0
    assert step_of(0.01) == 1
    assert step_of(0.0299) == 2
    assert step_of(-0.0099) == 0
    assert step_of(-0.01) == -1
    assert step_of(-0.0451) == -4


def test_fires_once_per_new_step_on_each_side():
    trigger = StepTrigger()
    path = [0.005, 0.012, 0.015, 0.008, 0.013, 0.021, -0.004, -0.011, -0.012, 0.011, -0.025]
    fired = [trigger.crossed("069500", r, DAY) for r in path]
    assert fired == [None, 1, None, None, None, 2, None, -1, None, None, -2]


def test_tickers_are_tracked_separately():
    trigger = StepTrigger()
    assert trigger.crossed("069500", 0.012, DAY) == 1
    assert trigger.crossed("114800", 0.012, DAY) == 1


def test_a_new_day_starts_over():
    trigger = StepTrigger()
    trigger.crossed("069500", 0.012, DAY)
    assert trigger.crossed("069500", 0.012, date(2026, 10, 26)) == 1


def test_daily_cap_bounds_the_llm_calls(monkeypatch):
    monkeypatch.setattr(etf_exit_trigger, "MAX_TRIGGERS_PER_DAY", 2)
    trigger = StepTrigger()
    assert trigger.crossed("A", 0.011, DAY) == 1
    assert trigger.crossed("A", 0.021, DAY) == 2
    assert trigger.crossed("A", 0.031, DAY) is None
