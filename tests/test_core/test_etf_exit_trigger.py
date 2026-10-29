from datetime import date

from src.core import etf_exit_trigger
from src.core.etf_exit_trigger import StepTrigger, step_of

DAY = date(2026, 10, 23)


def test_step_of_rounds_toward_zero_in_half_percent_steps():
    assert step_of(0.0049) == 0
    assert step_of(0.005) == 1
    assert step_of(0.0149) == 2
    assert step_of(-0.0049) == 0
    assert step_of(-0.005) == -1
    assert step_of(-0.0226) == -4


def test_fires_once_per_new_step_on_each_side():
    trigger = StepTrigger()
    path = [0.003, 0.006, 0.0075, 0.004, 0.0065, 0.0105, -0.002, -0.0055, -0.006, 0.0055, -0.0125]
    fired = [trigger.crossed("069500", r, DAY) for r in path]
    assert fired == [None, 1, None, None, None, 2, None, -1, None, None, -2]


def test_tickers_are_tracked_separately():
    trigger = StepTrigger()
    assert trigger.crossed("069500", 0.006, DAY) == 1
    assert trigger.crossed("114800", 0.006, DAY) == 1


def test_a_new_day_starts_over():
    trigger = StepTrigger()
    trigger.crossed("069500", 0.006, DAY)
    assert trigger.crossed("069500", 0.006, date(2026, 10, 26)) == 1


def test_daily_cap_bounds_the_llm_calls(monkeypatch):
    monkeypatch.setattr(etf_exit_trigger, "MAX_TRIGGERS_PER_DAY", 2)
    trigger = StepTrigger()
    assert trigger.crossed("A", 0.0055, DAY) == 1
    assert trigger.crossed("A", 0.0105, DAY) == 2
    assert trigger.crossed("A", 0.0155, DAY) is None


def test_custom_step_width_fires_at_its_own_steps():
    assert step_of(0.011, 1.0) == 1
    trigger = StepTrigger(1.0)
    assert [trigger.crossed("069500", r, DAY) for r in (0.006, 0.011, 0.021)] == [None, 1, 2]
