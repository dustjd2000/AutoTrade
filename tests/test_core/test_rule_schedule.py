from datetime import datetime, time
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


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, trigger_time, job, name=None):
        self.jobs[name] = (trigger_time, job)


def test_register_jobs_wires_rule_jobs_with_the_friday_guard(monkeypatch):
    submitted = []
    rt = SimpleNamespace(
        engine=SimpleNamespace(market_closed_today=False),
        runner=SimpleNamespace(submit=submitted.append),
    )
    settings = SimpleNamespace(recommend_time=time(9, 10), buy_time=time(9, 13))
    scheduler = FakeScheduler()

    runtime.register_jobs(scheduler, rt, settings)

    assert scheduler.jobs["collect_minute_bars"][0] == time(15, 45)
    assert scheduler.jobs["evaluate_rules"][0] == time(16, 0)

    class FixedNow(datetime):
        current = None

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(runtime, "datetime", FixedNow)
    evaluate = scheduler.jobs["evaluate_rules"][1]

    FixedNow.current = FixedNow(2026, 10, 29, 16, 0)  # 목요일
    evaluate()
    assert submitted == []

    FixedNow.current = FixedNow(2026, 10, 30, 16, 0)  # 금요일
    evaluate()
    assert submitted == ["evaluate_rules"]


def test_run_calls_on_ready_after_engine_start(monkeypatch):
    import asyncio

    order = []

    async def noop(_runtime):
        pass

    for name in ("watch_quote_stall", "watch_closeout_report", "watch_buy_result", "watch_cash_refresh", "watch_ai_exit"):
        monkeypatch.setattr(runtime, name, noop)
    monkeypatch.setattr(runtime, "adopt_carried_over_positions", lambda rt: order.append("adopt"))
    monkeypatch.setattr(runtime, "request_stop", lambda rt: None)

    async def idle():
        pass

    async def disconnect():
        pass

    rt = SimpleNamespace(
        engine=SimpleNamespace(start=lambda: order.append("start")),
        runner=SimpleNamespace(run=idle),
        ws_client=SimpleNamespace(connect=idle, disconnect=disconnect),
        scheduler=SimpleNamespace(run=idle),
        on_ready=lambda: order.append("ready"),
    )
    asyncio.run(runtime.run(rt))
    assert order == ["start", "adopt", "ready"]

    rt.on_ready = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    asyncio.run(runtime.run(rt))  # 콜백 예외가 엔진을 깨지 않는다
