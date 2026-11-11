import pytest

from src.research.backtest import Backtester, Rule, switch_day_return, trade_return

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


def test_trade_return_takes_profit_at_the_first_bar_past_the_line():
    bars = flat_day(10000, {"1000": 10100, "1001": 10250, "1002": 10500})
    result = trade_return(bars, "0910", 2.0, COMMISSION, half_tick=0.0, take_profit_percent=2.0)
    assert result == pytest.approx((10250 / 10000 - 1 - 2 * COMMISSION) * 100)   # 1001봉에서 +2.5%로 익절


def test_trade_return_exits_on_giveback_from_an_activated_peak():
    bars = flat_day(10000, {"1000": 10080, "1001": 10050, "1002": 10020, "1003": 10100})
    result = trade_return(
        bars, "0910", 2.0, COMMISSION, half_tick=0.0,
        trail_activate_percent=0.5, trail_giveback_percent=0.25,
    )
    # 고점 +0.77% 뒤 1001봉 +0.47%(반납 0.30%p)에서 판다
    assert result == pytest.approx((10050 / 10000 - 1 - 2 * COMMISSION) * 100)


def test_trade_return_ignores_giveback_below_the_activation_peak():
    bars = flat_day(10000, {"1000": 10030, "1001": 10000, "1002": 10100})
    result = trade_return(
        bars, "0910", 2.0, COMMISSION, half_tick=0.0,
        trail_activate_percent=0.5, trail_giveback_percent=0.25,
    )
    assert result == pytest.approx((10100 / 10000 - 1 - 2 * COMMISSION) * 100)   # 15:15까지 보유


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


# ── 장중 전환 (C만 — 체결강도는 분봉으로 재현하지 못한다, 스펙 2026-11-11 8절) ──
def net(buy, sell):
    return (sell / buy - 1 - 2 * COMMISSION) * 100


def test_switch_sells_the_first_leg_and_rides_the_opposite_etf():
    long_bars = flat_day(10000, {"1000": 9940})                  # 지수·롱 -0.6%
    inverse_bars = flat_day(1000, {"1100": 1010})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    # 10:00 봉에 롱을 팔고 같은 봉의 인버스(1,000원)로 갈아타 15:15까지 보유
    assert result == pytest.approx(net(10000, 9940) + net(1000, 1010))


def test_second_leg_only_sells_when_the_index_turns_again():
    long_bars = flat_day(10000, {"1000": 9940, "1100": 10010})   # 2번째 다리 기준(9,940) 대비 +0.7%
    inverse_bars = flat_day(1000, {"1100": 995, "1200": 900})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    # 11:00 봉에 인버스를 팔고 끝낸다 — 12:00의 급락은 맞지 않고, 롱을 다시 사지도 않는다
    assert result == pytest.approx(net(10000, 9940) + net(1000, 995))


def test_no_switch_while_the_index_move_is_below_the_threshold():
    long_bars = flat_day(10000, {"1000": 9960})                  # -0.4%
    inverse_bars = flat_day(1000, {"1100": 1010})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(10000, 9960))


def test_no_switch_after_1514():
    long_bars = flat_day(10000, {"1515": 9940})
    inverse_bars = flat_day(1000, {"1520": 1100})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(10000, 9940))             # 15:15 청산일 뿐 전환이 아니다


def test_inverse_first_leg_switches_to_long_when_the_index_rises():
    long_bars = flat_day(10000, {"1000": 10060, "1100": 10160})
    inverse_bars = flat_day(1000, {"1000": 994})
    result = switch_day_return(
        inverse_bars, long_bars, long_bars, False, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(1000, 994) + net(10060, 10160))


def test_switch_percent_zero_matches_the_plain_trade():
    long_bars = flat_day(10000, {"1000": 9940})
    inverse_bars = flat_day(1000, {"1100": 1010})
    assert switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.0
    ) == trade_return(long_bars, "0910", 2.0, COMMISSION)


def test_backtester_adds_both_legs_when_switching_is_on():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261021"] = flat_day(10000, {"1000": 9940})
    inverse_bars["20261021"] = flat_day(1000, {"1100": 1010})
    rule = Rule(0.0, True, "09:10", 2.0)

    plain = Backtester(long_bars, inverse_bars, COMMISSION).day_return("20261021", rule)
    switched = Backtester(long_bars, inverse_bars, COMMISSION, switch_percent=0.5).day_return(
        "20261021", rule
    )

    first = ((9940 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100
    second = ((1010 - 2.5) / (1000 + 2.5) - 1 - 2 * COMMISSION) * 100
    assert plain == pytest.approx(first)
    assert switched == pytest.approx(first + second)
