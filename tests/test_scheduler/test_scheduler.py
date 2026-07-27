from datetime import datetime, time as dt_time

from src.scheduler.scheduler import TimeScheduler


def test_due_jobs_returns_empty_before_trigger_time():
    scheduler = TimeScheduler()
    scheduler.add_job(dt_time(9, 0), lambda: None, "buy")

    now = datetime(2026, 7, 27, 8, 59)
    assert scheduler.due_jobs(now) == []


def test_due_jobs_fires_once_at_or_after_trigger_time():
    scheduler = TimeScheduler()
    scheduler.add_job(dt_time(9, 0), lambda: None, "buy")

    first_check = datetime(2026, 7, 27, 9, 0)
    due = scheduler.due_jobs(first_check)
    assert [j.name for j in due] == ["buy"]

    second_check = datetime(2026, 7, 27, 9, 5)
    assert scheduler.due_jobs(second_check) == []


def test_due_jobs_refires_on_a_new_day():
    scheduler = TimeScheduler()
    scheduler.add_job(dt_time(9, 0), lambda: None, "buy")

    scheduler.due_jobs(datetime(2026, 7, 27, 9, 0))
    due_next_day = scheduler.due_jobs(datetime(2026, 7, 28, 9, 0))
    assert [j.name for j in due_next_day] == ["buy"]


def test_skip_past_due_for_today_does_not_fire_immediately():
    scheduler = TimeScheduler()
    scheduler.add_job(dt_time(8, 45), lambda: None, "recommend")
    scheduler.add_job(dt_time(15, 20), lambda: None, "force_close")

    startup_time = datetime(2026, 7, 27, 10, 0)  # 08:45은 이미 지났고 15:20은 아직
    scheduler.skip_past_due_for_today(startup_time)

    assert scheduler.due_jobs(startup_time) == []
    # 15:20이 되면 예정대로 실행되어야 한다
    assert [j.name for j in scheduler.due_jobs(datetime(2026, 7, 27, 15, 20))] == ["force_close"]


def test_job_execution_via_run_job(monkeypatch):
    import asyncio

    scheduler = TimeScheduler()
    calls = []
    scheduler.add_job(dt_time(9, 0), lambda: calls.append("ran"), "buy")

    scheduled = scheduler.due_jobs(datetime(2026, 7, 27, 9, 0))[0]
    asyncio.run(scheduler._run_job(scheduled))

    assert calls == ["ran"]


def test_job_exception_is_caught_and_logged():
    import asyncio

    scheduler = TimeScheduler()

    def failing_job():
        raise RuntimeError("boom")

    scheduler.add_job(dt_time(9, 0), failing_job, "buy")
    scheduled = scheduler.due_jobs(datetime(2026, 7, 27, 9, 0))[0]

    # 예외가 전파되지 않고 내부에서 로깅된 후 넘어가야 한다
    asyncio.run(scheduler._run_job(scheduled))
