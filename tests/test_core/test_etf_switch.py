"""2호 장중 전환 판정 (스펙 2026-11-11 3절)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.etf_switch import SwitchKind, SwitchState, opposite_etf

LONG, INVERSE = "069500", "114800"
BOUGHT = datetime(2026, 11, 12, 9, 5, 0)
NOW = datetime(2026, 11, 12, 10, 0, 0)


def state(ticker=LONG, leg=1):
    s = SwitchState(index_percent=0.5)
    s.begin_leg(leg, ticker, 10000.0, BOUGHT)
    return s


def decide(s, ticker=LONG, net=-0.3, index_now=9940.0, strength=80.0, now=NOW, index_at=None):
    return s.evaluate(ticker, net, index_now, index_at or now, strength, now)


def test_long_holder_switches_when_the_index_fell_and_sellers_lead():
    decision = decide(state())
    assert decision.kind is SwitchKind.SWITCH
    assert decision.against == pytest.approx(0.6)
    assert (decision.strength, decision.net) == (80.0, -0.3)


def test_inverse_holder_switches_when_the_index_rose_and_buyers_lead():
    decision = decide(state(INVERSE), ticker=INVERSE, index_now=10060.0, strength=130.0)
    assert decision.kind is SwitchKind.SWITCH
    assert decision.against == pytest.approx(0.6)


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_now": 9960.0},          # 지수 반대 0.4% — 문턱 미만
        {"strength": 120.0},            # 롱 보유인데 매수 체결이 우세
        {"strength": 100.0},            # 정확히 100은 반대가 아니다
        {"net": 0.0},                   # 순손익이 마이너스가 아니다
        {"strength": None},             # 체결강도 모름
        {"index_now": None},            # 지수 틱이 아직 없다
    ],
)
def test_any_missing_condition_means_no_switch(overrides):
    assert decide(state(), **overrides) is None


def test_index_moving_in_our_favour_is_not_against():
    assert decide(state(), index_now=10060.0) is None
    assert decide(state(INVERSE), ticker=INVERSE, index_now=9940.0, strength=130.0) is None


def test_second_leg_only_sells():
    assert decide(state(INVERSE, leg=2), ticker=INVERSE, index_now=10060.0, strength=130.0).kind \
        is SwitchKind.SELL_ONLY


def test_no_decision_after_1514():
    s = state()
    assert decide(s, now=NOW.replace(hour=15, minute=14, second=0)) is not None
    assert decide(s, now=NOW.replace(hour=15, minute=14, second=1)) is None


def test_stale_index_tick_means_no_decision():
    s = state()
    assert decide(s, index_at=NOW - timedelta(seconds=60)) is not None
    assert decide(s, index_at=NOW - timedelta(seconds=61)) is None


def test_no_decision_for_a_ticker_without_an_open_leg():
    assert decide(state(), ticker=INVERSE, index_now=10060.0, strength=130.0) is None


def test_ended_leg_is_not_evaluated_and_a_switch_is_remembered():
    s = state()
    s.end_leg(switched=True)
    assert decide(s) is None
    assert s.switched_today is True


def test_state_rolls_over_at_a_new_day():
    s = state()
    s.end_leg(switched=True)
    tomorrow = NOW + timedelta(days=1)
    assert s.has_leg(1, tomorrow.date()) is False
    s.begin_leg(1, LONG, 10000.0, tomorrow)
    assert s.switched_today is False


def row(leg, ticker, ended=False, reason=None):
    return SimpleNamespace(
        leg=leg, ticker=ticker, index_entry=10000.0, started_at=BOUGHT,
        ended_at=NOW if ended else None, end_reason=reason,
    )


def test_restore_reopens_the_first_leg():
    s = SwitchState(0.5)
    s.restore([row(1, LONG)], NOW.date())
    assert s.open_leg(LONG, NOW.date()).index_entry == 10000.0
    assert s.switched_today is False and s.has_leg(1, NOW.date())


def test_restore_reopens_the_second_leg_as_switched():
    s = SwitchState(0.5)
    s.restore([row(1, LONG, ended=True, reason="etf_switch"), row(2, INVERSE)], NOW.date())
    assert s.open_leg(INVERSE, NOW.date()).leg == 2
    assert s.switched_today is True


def test_restore_after_a_switch_sell_without_a_buy_stays_in_cash():
    """전환 매도 뒤·매수 전에 재시작됐다 — 그날은 다시 사지 않는다."""
    s = SwitchState(0.5)
    s.restore([row(1, LONG, ended=True, reason="etf_switch")], NOW.date())
    assert s.open_leg(LONG, NOW.date()) is None
    assert s.switched_today is True


def test_opposite_etf():
    assert opposite_etf(LONG)[0] == INVERSE
    assert opposite_etf(INVERSE)[0] == LONG
