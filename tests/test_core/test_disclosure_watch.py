"""장중 공시 감시 (PRD 5.5-B '장중 공시').

DART가 공시 시각을 주지 않아 "장중에 새로 뜬 것"을 목록 비교로 가려낸다 — 그 비교가
이 파일의 주제다. 기준선을 언제 잡는지, 같은 공시가 두 번 트리거하지 않는지, 악재가
아닌 공시는 트리거하지 않는지를 본다.
"""

from datetime import datetime

from src.core.disclosure_watch import (
    MAX_HEADLINES_PER_TICKER,
    MAX_URGENT_TRIGGERS_PER_DAY,
    DisclosureWatch,
)

TICKER = "005930"
OTHER = "000660"

MORNING = datetime(2026, 9, 10, 9, 30)
MIDDAY = datetime(2026, 9, 10, 11, 0)
AFTERNOON = datetime(2026, 9, 10, 14, 0)
NEXT_DAY = datetime(2026, 9, 11, 9, 30)

ROUTINE = "분기보고서"
BLOCKING = "유상증자 결정"


class FakeClient:
    """조회할 때마다 준비된 응답을 순서대로 돌려준다 — 호출 인자도 기록한다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def fetch(self, tickers, today=None, since=None):
        self.calls.append((list(tickers), today, since))
        return self._responses.pop(0) if self._responses else {}


def test_first_poll_only_sets_the_baseline():
    """엔진을 켠 시점에 이미 있던 공시는 '장중 신규'가 아니다."""
    watch = DisclosureWatch(FakeClient([{TICKER: [BLOCKING]}]))

    assert watch.poll([TICKER], MORNING) == []
    assert watch.urgent_pending is False
    assert watch.new_headlines_for(TICKER) == []
    # 신규는 아니지만 프롬프트에는 그대로 실린다
    assert watch.headlines_for(TICKER) == [BLOCKING]


def test_disclosure_appearing_after_the_baseline_is_new():
    watch = DisclosureWatch(FakeClient([{TICKER: [ROUTINE]}, {TICKER: [BLOCKING, ROUTINE]}]))
    watch.poll([TICKER], MORNING)

    triggered = watch.poll([TICKER], MIDDAY)

    assert triggered == [(TICKER, BLOCKING)]
    assert watch.urgent_pending is True
    assert watch.new_headlines_for(TICKER) == [BLOCKING]


def test_routine_disclosure_does_not_trigger():
    """대형주에는 정기보고서가 일상적으로 뜬다 — 공시가 떴다는 사실만으로 부르지 않는다."""
    watch = DisclosureWatch(FakeClient([{TICKER: []}, {TICKER: [ROUTINE]}]))
    watch.poll([TICKER], MORNING)

    triggered = watch.poll([TICKER], MIDDAY)

    assert triggered == []
    assert watch.urgent_pending is False
    # 트리거는 안 해도 신규 표시는 남아 다음 정규 주기의 프롬프트에 실린다
    assert watch.new_headlines_for(TICKER) == [ROUTINE]


def test_same_disclosure_triggers_only_once():
    watch = DisclosureWatch(
        FakeClient([{TICKER: []}, {TICKER: [BLOCKING]}, {TICKER: [BLOCKING]}])
    )
    watch.poll([TICKER], MORNING)
    watch.poll([TICKER], MIDDAY)
    watch.take_urgent()

    assert watch.poll([TICKER], AFTERNOON) == []
    assert watch.urgent_pending is False
    # 트리거는 한 번뿐이지만 '신규' 표시는 그날 내내 유지된다
    assert watch.new_headlines_for(TICKER) == [BLOCKING]


def test_take_urgent_consumes_the_flag():
    watch = DisclosureWatch(FakeClient([{TICKER: []}, {TICKER: [BLOCKING]}]))
    watch.poll([TICKER], MORNING)
    watch.poll([TICKER], MIDDAY)

    assert watch.take_urgent() is True
    assert watch.take_urgent() is False
    assert watch.urgent_pending is False


def test_urgent_triggers_are_capped_per_day():
    """공시가 쏟아지는 날에도 상한 밖에서 LLM 호출이 늘어나지 않는다."""
    titles = [f"유상증자 결정 {i}" for i in range(MAX_URGENT_TRIGGERS_PER_DAY + 2)]
    responses = [{TICKER: []}] + [{TICKER: titles[: i + 1]} for i in range(len(titles))]
    watch = DisclosureWatch(FakeClient(responses))
    watch.poll([TICKER], MORNING)

    granted = 0
    for _ in titles:
        watch.poll([TICKER], MIDDAY)
        if watch.take_urgent():
            granted += 1

    assert granted == MAX_URGENT_TRIGGERS_PER_DAY


def test_new_disclosures_come_first_when_truncated():
    """제목을 자를 때 장중 신규가 먼저 남아야 한다 — 아침에 본 것은 이미 반영됐다."""
    old = [f"기존공시{i}" for i in range(MAX_HEADLINES_PER_TICKER)]
    watch = DisclosureWatch(FakeClient([{TICKER: old}, {TICKER: [BLOCKING] + old}]))
    watch.poll([TICKER], MORNING)
    watch.poll([TICKER], MIDDAY)

    headlines = watch.headlines_for(TICKER)

    assert len(headlines) == MAX_HEADLINES_PER_TICKER
    assert headlines[0] == BLOCKING


def test_state_resets_on_a_new_day():
    """어제 공시를 오늘 기준선으로 쓰면 오늘 아침 공시가 전부 '신규'가 된다."""
    watch = DisclosureWatch(FakeClient([{TICKER: [ROUTINE]}, {TICKER: [ROUTINE]}]))
    watch.poll([TICKER], MORNING)

    assert watch.poll([TICKER], NEXT_DAY) == []  # 새 날의 첫 조회 = 기준선
    assert watch.new_headlines_for(TICKER) == []


def test_poll_asks_for_today_only():
    """장중 감시는 당일만 본다 — 전일까지 받으면 어제 공시가 '신규'로 섞인다."""
    client = FakeClient([{TICKER: []}])
    DisclosureWatch(client).poll([TICKER], MIDDAY)

    tickers, today, since = client.calls[0]
    assert tickers == [TICKER]
    assert today == MIDDAY.date()
    assert since == MIDDAY.date()


def test_holdings_without_disclosures_are_empty():
    watch = DisclosureWatch(FakeClient([{TICKER: [ROUTINE]}]))
    watch.poll([TICKER, OTHER], MORNING)

    assert watch.headlines_for(OTHER) == []
    assert watch.new_headlines_for(OTHER) == []
