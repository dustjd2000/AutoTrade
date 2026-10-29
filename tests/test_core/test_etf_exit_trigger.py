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
    assert fired == [None, 1, None, 0, None, 2, 0, -1, None, None, -2]


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


def test_fires_while_descending_from_a_profit_peak_incident_2026_10_29():
    """KODEX 200 +1.56% 고점에서 +0.21%까지 녹는 동안 하락 단계마다 묻는다."""
    trigger = StepTrigger()
    path = [0.0119, 0.0156, 0.0126, 0.0094, 0.0033, 0.0021, 0.006, -0.006]
    fired = [trigger.crossed("069500", r, DAY) for r in path]
    assert fired == [2, 3, 2, 1, 0, None, None, -1]


def test_descending_oscillation_does_not_refire():
    trigger = StepTrigger()
    path = [0.0156, 0.0094, 0.0101, 0.0094]
    assert [trigger.crossed("069500", r, DAY) for r in path] == [3, 1, None, None]


def test_new_high_resets_the_descent_floor():
    trigger = StepTrigger()
    path = [0.0156, 0.0094, 0.0206, 0.0094]
    assert [trigger.crossed("069500", r, DAY) for r in path] == [3, 1, 4, 1]


def test_step_zero_never_fires_without_a_prior_profit_step():
    trigger = StepTrigger()
    assert [trigger.crossed("069500", r, DAY) for r in (0.002, -0.002)] == [None, None]
