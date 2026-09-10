"""장중 공시가 AI 매도 판단으로 이어지는 경로 (PRD 5.5-B '장중 공시').

`DisclosureWatch` 자체의 비교 로직은 test_disclosure_watch.py가 본다. 여기서는 런타임과
붙은 뒤의 동작 — 언제 조회하는지, 악재 공시가 호출 주기를 앞당기는지, 공시가 프롬프트까지
실제로 도달하는지 — 를 본다.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.core.runtime import ai_exit_due, maybe_poll_disclosures, maybe_run_ai_exit_cycle

from tests.test_core.test_ai_exit import (
    BUY_TIME,
    DEFAULT_CALL_LIMIT,
    DEFAULT_INTERVAL_MINUTES,
    IN_WINDOW,
    SATURDAY,
    holding,
    make_runtime,
)

TICKER = "005930"
BLOCKING = "유상증자 결정"
ROUTINE = "분기보고서"

BEFORE_OPEN = datetime(2026, 9, 9, 8, 50)


class FakeWatch:
    """`DisclosureWatch`의 표면만 흉내 낸다 — 조회 인자와 소비 여부를 기록한다."""

    def __init__(self, urgent=False, triggered=None, headlines=None, new_headlines=None):
        self.urgent_pending = urgent
        self._triggered = list(triggered or [])
        self._headlines = dict(headlines or {})
        self._new_headlines = dict(new_headlines or {})
        self.polls = []
        self.takes = 0

    def poll(self, tickers, now):
        self.polls.append((list(tickers), now))
        return list(self._triggered)

    def take_urgent(self):
        self.takes += 1
        pending = self.urgent_pending
        self.urgent_pending = False
        return pending

    def headlines_for(self, ticker):
        return list(self._headlines.get(ticker, []))

    def new_headlines_for(self, ticker):
        return list(self._new_headlines.get(ticker, []))


def poll_runtime(watch, holdings=(TICKER,)):
    """`maybe_poll_disclosures`가 쓰는 표면만 갖춘 런타임."""
    notes = []
    engine = SimpleNamespace(open_tickers=list(holdings), notify=notes.append)
    return SimpleNamespace(engine=engine, disclosure_watch=watch), notes


# ── 조회 게이트 ──────────────────────────────────────────────
def test_poll_runs_during_market_hours_with_holdings():
    watch = FakeWatch()
    runtime, _ = poll_runtime(watch)

    asyncio.run(maybe_poll_disclosures(runtime, IN_WINDOW))

    assert watch.polls == [([TICKER], IN_WINDOW)]


def test_no_poll_without_holdings():
    """팔 것이 없으면 공시를 볼 이유가 없다."""
    watch = FakeWatch()
    runtime, _ = poll_runtime(watch, holdings=())

    asyncio.run(maybe_poll_disclosures(runtime, IN_WINDOW))

    assert watch.polls == []


def test_no_poll_outside_market_hours():
    watch = FakeWatch()
    runtime, _ = poll_runtime(watch)

    asyncio.run(maybe_poll_disclosures(runtime, BEFORE_OPEN))
    asyncio.run(maybe_poll_disclosures(runtime, SATURDAY))

    assert watch.polls == []


def test_poll_failure_does_not_propagate():
    """공시를 못 봤다고 매매가 멈추면 안 된다 — 아침 수집과 같은 규약이다."""

    class ExplodingWatch(FakeWatch):
        def poll(self, tickers, now):
            raise RuntimeError("DART 500")

    runtime, _ = poll_runtime(ExplodingWatch())

    assert asyncio.run(maybe_poll_disclosures(runtime, IN_WINDOW)) == []


def test_blocking_disclosure_is_alerted():
    watch = FakeWatch(triggered=[(TICKER, BLOCKING)])
    runtime, notes = poll_runtime(watch)

    triggered = asyncio.run(maybe_poll_disclosures(runtime, IN_WINDOW))

    assert triggered == [(TICKER, BLOCKING)]
    assert len(notes) == 1
    assert BLOCKING in notes[0]


# ── 즉시 판단 트리거 ─────────────────────────────────────────
def test_urgent_trigger_skips_the_interval():
    """악재 공시가 뜨면 호출 주기를 기다리지 않는다."""
    just_called = IN_WINDOW - timedelta(minutes=1)
    runtime, _, _ = make_runtime(
        holdings=[holding()], disclosure_watch=FakeWatch(urgent=True)
    )

    assert ai_exit_due(runtime, IN_WINDOW, just_called) is True


def test_without_urgent_trigger_the_interval_still_applies():
    just_called = IN_WINDOW - timedelta(minutes=1)
    runtime, _, _ = make_runtime(
        holdings=[holding()], disclosure_watch=FakeWatch(urgent=False)
    )

    assert ai_exit_due(runtime, IN_WINDOW, just_called) is False


def test_urgent_trigger_skips_the_daily_cap():
    """상한을 함께 넘기지 않으면 그날 마지막 정규 사이클이 대신 빠진다."""
    runtime, _, _ = make_runtime(
        holdings=[holding()],
        ai_exit_calls=DEFAULT_CALL_LIMIT,
        disclosure_watch=FakeWatch(urgent=True),
    )

    assert ai_exit_due(runtime, IN_WINDOW, None) is True


def test_urgent_trigger_does_not_override_the_other_gates():
    """거래일·설정·보유 종목은 공시와 무관하게 그대로 본다."""
    disabled, _, _ = make_runtime(
        holdings=[holding()], ai_exit_enabled=False, disclosure_watch=FakeWatch(urgent=True)
    )
    empty, _, _ = make_runtime(holdings=[], disclosure_watch=FakeWatch(urgent=True))
    weekend, _, _ = make_runtime(
        holdings=[holding()], disclosure_watch=FakeWatch(urgent=True)
    )

    assert ai_exit_due(disabled, IN_WINDOW, None) is False
    assert ai_exit_due(empty, IN_WINDOW, None) is False
    assert ai_exit_due(weekend, SATURDAY, None) is False


def test_cycle_consumes_the_urgent_trigger():
    """비우지 않으면 다음 주기까지 계속 주기를 건너뛴다."""
    watch = FakeWatch(urgent=True)
    runtime, _, _ = make_runtime(holdings=[holding()], disclosure_watch=watch)

    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))

    assert watch.takes == 1
    assert watch.urgent_pending is False


# ── 프롬프트 도달 ────────────────────────────────────────────
def test_disclosures_reach_the_advisor():
    watch = FakeWatch(
        headlines={TICKER: [BLOCKING, ROUTINE]},
        new_headlines={TICKER: [BLOCKING]},
    )
    runtime, _, advisor = make_runtime(
        holdings=[holding(ticker=TICKER)], disclosure_watch=watch
    )

    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))

    view = advisor.calls[0]["holdings"][0]
    assert view.headlines == [BLOCKING, ROUTINE]
    assert view.new_headlines == [BLOCKING]


def test_cycle_works_without_a_disclosure_watch():
    """감시가 붙지 않은 런타임(테스트·구형 조립)에서도 판단은 그대로 돈다."""
    runtime, _, advisor = make_runtime(holdings=[holding()], disclosure_watch=None)

    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))

    view = advisor.calls[0]["holdings"][0]
    assert view.headlines == []
    assert view.new_headlines == []


# ── 이익 반납 트리거 (PRD 5.5-B '이익 반납 감시') ──────────────
def test_giveback_skips_the_interval_and_the_cap():
    """공시와 같은 통로를 쓴다 — 주기와 하루 상한을 함께 건너뛴다."""
    just_called = IN_WINDOW - timedelta(minutes=1)
    runtime, engine, _ = make_runtime(
        holdings=[holding()], ai_exit_calls=DEFAULT_CALL_LIMIT, drawdown_ratio=0.03
    )
    engine.exit_drawdown.update({"005930": 0.0446})
    engine.exit_drawdown.update({"005930": 0.0141})

    assert engine.exit_drawdown.urgent_pending is True
    assert ai_exit_due(runtime, IN_WINDOW, just_called) is True


def test_cycle_consumes_the_giveback_trigger():
    runtime, engine, _ = make_runtime(holdings=[holding()], drawdown_ratio=0.03)
    engine.exit_drawdown.update({"005930": 0.0446})
    engine.exit_drawdown.update({"005930": 0.0141})

    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))

    assert engine.exit_drawdown.urgent_pending is False


def test_peak_reaches_the_advisor():
    """실시간 콜백이 잡은 고점이 프롬프트까지 도달해야 한다 — 궤적에는 없는 봉우리다."""
    runtime, engine, advisor = make_runtime(holdings=[holding()], drawdown_ratio=0.03)
    engine.exit_drawdown.update({"005930": 0.0446}, portfolio=0.0236)

    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))

    view = advisor.calls[0]["holdings"][0]
    assert view.peak_return == 0.0446
