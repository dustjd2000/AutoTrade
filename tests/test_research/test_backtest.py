import pytest

from src.research.backtest import Backtester, Rule, trade_return

COMMISSION = 0.00015


def minutes(start="0900", end="1530"):
    out = []
    h, m = int(start[:2]), int(start[2:])
    while f"{h:02d}{m:02d}" <= end:
        out.append(f"{h:02d}{m:02d}")
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def flat_day(price, overrides=None):
    """하루 391봉. `overrides`={HHMM: 가격}이면 그 시각부터 그 가격이 이어진다."""
    overrides = overrides or {}
    bars, current = [], price
    for t in minutes():
        current = overrides.get(t, current)
        bars.append((t, float(current)))
    return bars


def test_trade_return_holds_to_1515_with_costs():
    bars = flat_day(10000, {"0910": 10000, "1000": 10100})
    result = trade_return(bars, "0910", 2.0, COMMISSION, half_tick=2.5)
    expected = ((10100 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100
    assert result == pytest.approx(expected)


def test_trade_return_stops_at_the_first_bar_past_the_stop():
    bars = flat_day(10000, {"1000": 9900, "1001": 9750, "1002": 9000})
    result = trade_return(bars, "0910", 2.0, COMMISSION, half_tick=0.0)
    assert result == pytest.approx((9750 / 10000 - 1 - 2 * COMMISSION) * 100)   # 1001봉에서 -2.5%로 손절


def test_trade_return_without_an_entry_bar_is_none():
    assert trade_return([("0900", 1.0)], "0910", 2.0, COMMISSION) is None


def test_rule_json_round_trip():
    rule = Rule(0.2, False, "09:10", 2.0)
    assert Rule.from_json(rule.to_json()) == rule


def make_history(signal_up: bool, days=22):
    """신호 계산용 20일 + 거래일 2일. 지난날은 KODEX 200이 장중 +0.5%(또는 -0.5%) 움직였다."""
    long_bars, inverse_bars = {}, {}
    move = 10050 if signal_up else 9950
    for i in range(days):
        day = f"202610{i + 1:02d}"
        long_bars[day] = flat_day(10000, {"1200": move})
        inverse_bars[day] = flat_day(1000)
    return long_bars, inverse_bars


def test_backtester_days_start_after_the_lookback():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert bt.days == ["20261021", "20261022"]


def test_backtester_buys_kodex200_on_an_up_signal():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    result = bt.day_return("20261021", Rule(0.0, True, "09:10", 2.0))
    # KODEX 200이 12:00에 +0.5%로 올라 15:15까지 유지
    assert result == pytest.approx(((10050 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100)


def test_backtester_rests_inside_the_band_as_zero():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert bt.day_return("20261021", Rule(1.0, True, "09:10", 2.0)) == 0.0


def test_backtester_flips_on_the_morning_move():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261021"] = flat_day(10000, {"0905": 9970})       # 결정 전 -0.3%
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    flipped = bt.day_return("20261021", Rule(0.0, True, "09:10", 2.0))
    kept = bt.day_return("20261021", Rule(0.0, False, "09:10", 2.0))
    inverse_cost = ((1000 - 2.5) / (1000 + 2.5) - 1 - 2 * COMMISSION) * 100   # 인버스 가격 변화 없음
    assert flipped == pytest.approx(inverse_cost)
    assert kept != pytest.approx(inverse_cost)


def test_backtester_skips_thin_days():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261022"] = long_bars["20261022"][:100]
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert "20261022" not in bt.days
