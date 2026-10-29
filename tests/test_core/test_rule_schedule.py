from datetime import datetime
from types import SimpleNamespace

from src.core import actions, runtime


def test_on_weekday_runs_only_on_that_weekday():
    calls = []
    friday = lambda: datetime(2026, 10, 30, 16, 0)
    thursday = lambda: datetime(2026, 10, 29, 16, 0)

    runtime._on_weekday(4, lambda: calls.append("x"), now_fn=thursday)()
    runtime._on_weekday(4, lambda: calls.append("x"), now_fn=friday)()

    assert calls == ["x"]


def test_rule_actions_run_off_the_loop_thread():
    research = SimpleNamespace(collect_minute_bars=lambda: None, evaluate_rules=lambda: None)
    rt = SimpleNamespace(research=research)

    for action in ("collect_minute_bars", "evaluate_rules"):
        steps = actions.manual_steps(rt, action)
        assert len(steps) == 1 and steps[0].touches_orders is False
        assert action in actions.SCHEDULED_ACTIONS
        assert action not in actions.MANUAL_ACTIONS
        assert action not in runtime.HOLIDAY_ALWAYS_ACTIONS
