import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Awaitable, Callable, List, Optional, Union

logger = logging.getLogger(__name__)

Job = Union[Callable[[], None], Callable[[], Awaitable[None]]]


@dataclass
class ScheduledJob:
    trigger_time: dt_time
    job: Job
    name: str
    _fired_date: str = field(default="", init=False)


class TimeScheduler:
    """지정된 시각(HH:MM)에 콜백을 실행하는 시간 기반 스케줄러 (PRD 5.5-A).

    실시간 시세 이벤트 기반 전략(WebSocketClient 콜백)과 별개의 asyncio 태스크로
    병행 실행되는 것을 전제로 한다.
    """

    def __init__(self, poll_interval_seconds: float = 5.0):
        self._jobs: List[ScheduledJob] = []
        self._poll_interval = poll_interval_seconds
        self._running = False

    def add_job(self, trigger_time: dt_time, job: Job, name: str) -> None:
        self._jobs.append(ScheduledJob(trigger_time=trigger_time, job=job, name=name))
        logger.info("Scheduled job registered: %s at %s", name, trigger_time)

    def skip_past_due_for_today(self, now: Optional[datetime] = None) -> None:
        """스케줄러 시작 시점에 이미 지난 트리거는 당일 소급 실행하지 않고 건너뛴다."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        for scheduled in self._jobs:
            if now.time() >= scheduled.trigger_time:
                scheduled._fired_date = today

    def due_jobs(self, now: Optional[datetime] = None) -> List[ScheduledJob]:
        """지금 실행되어야 하는(오늘 아직 실행되지 않은) 작업 목록을 반환하고 실행 처리한다."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        due = []
        for scheduled in self._jobs:
            if scheduled._fired_date != today and now.time() >= scheduled.trigger_time:
                scheduled._fired_date = today
                due.append(scheduled)
        return due

    async def run(self) -> None:
        self._running = True
        self.skip_past_due_for_today()
        logger.info("Scheduler started with %d job(s).", len(self._jobs))
        while self._running:
            for scheduled in self.due_jobs():
                logger.info("Triggering scheduled job: %s", scheduled.name)
                await self._run_job(scheduled)
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Scheduler stopped.")

    async def _run_job(self, scheduled: ScheduledJob) -> None:
        try:
            result = scheduled.job()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Scheduled job '%s' raised an exception.", scheduled.name)
