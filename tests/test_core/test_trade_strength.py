"""KODEX 200 5분 체결강도 창 (스펙 2026-11-11 4.1)."""
from datetime import datetime, timedelta

import pytest

from src.core.trade_strength import MAX_STRENGTH, TradeStrength

T0 = datetime(2026, 11, 12, 9, 0, 0)


def feed(strength, start, minutes, buy, sell):
    """`start`부터 `minutes`분 동안 30초마다 매수 `buy`주·매도 `sell`주 체결을 넣는다."""
    at = start
    while at <= start + timedelta(minutes=minutes):
        if buy:
            strength.add(at, buy)
        if sell:
            strength.add(at, -sell)
        at += timedelta(seconds=30)


def test_unknown_until_the_window_is_covered():
    strength = TradeStrength(5)
    feed(strength, T0, 4, buy=10, sell=10)
    assert strength.value(T0 + timedelta(minutes=4)) is None


def test_ratio_of_buy_to_sell_volume_in_percent():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=30, sell=10)
    assert strength.value(T0 + timedelta(minutes=5)) == pytest.approx(300.0)


def test_ticks_outside_the_window_are_dropped():
    strength = TradeStrength(5)
    strength.add(T0, -10_000)                       # 창 밖으로 밀려날 큰 매도
    feed(strength, T0 + timedelta(seconds=30), 6, buy=10, sell=10)
    assert strength.value(T0 + timedelta(minutes=6, seconds=30)) == pytest.approx(100.0)


def test_no_sell_volume_caps_at_the_maximum():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=10, sell=0)
    assert strength.value(T0 + timedelta(minutes=5)) == MAX_STRENGTH


def test_no_buy_volume_is_zero():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=0, sell=10)
    assert strength.value(T0 + timedelta(minutes=5)) == 0.0


def test_a_gap_in_ticks_restarts_the_window():
    """재접속 뒤에는 다시 5분이 필요하다 — 끊긴 사이의 체결을 모른다."""
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=30, sell=10)
    resumed = T0 + timedelta(minutes=7)             # 2분 공백
    strength.add(resumed, 10)
    assert strength.value(resumed + timedelta(seconds=1)) is None
