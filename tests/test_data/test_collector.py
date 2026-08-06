from types import SimpleNamespace

from src.api.market_data import PreviousDayMetrics
from src.data.collector import DataCollector, LargeCapUniverse, NewsClient


def make_universe(rows):
    client = SimpleNamespace(request=lambda *a, **kw: ({"list": rows}, {}))
    return LargeCapUniverse(client)


def row(code, name="종목", size="대형주", market="거래소", state="증거금40%", warning="0"):
    return {
        "code": code,
        "name": name,
        "upSizeName": size,
        "marketName": market,
        "state": state,
        "orderWarning": warning,
    }


def fake_market_data(metrics_fn):
    return SimpleNamespace(get_previous_day_metrics=metrics_fn)


def metrics(ticker, change_rate=1.5, volume_surge=1.0, close=10000.0, volume=500):
    return PreviousDayMetrics(
        ticker=ticker,
        close=close,
        high=close * 1.02,
        low=close * 0.98,
        change_rate=change_rate,
        volume=volume,
        volume_surge=volume_surge,
    )


def collect(universe, market_data, **kwargs):
    """request_interval=0으로 테스트에서 대기하지 않는다."""
    return DataCollector(market_data, universe, NewsClient(), request_interval=0, **kwargs).collect()


def test_universe_keeps_only_large_cap_kospi_stocks():
    universe = make_universe([
        row("005930", "삼성전자"),
        row("111111", "중형주종목", size="중형주"),
        row("777777", "소형주종목", size="소형주"),
        row("222222", "ETF종목", market="ETF"),
        row("333333", "분류없음", size=""),
    ])

    # 중형주는 호가가 얇아 지정가 매수 조건이 나빠 제외한다 (확정 2026-08-06)
    assert universe.get_tickers() == ["005930"]


def test_universe_excludes_halted_and_flagged_stocks():
    universe = make_universe([
        row("005930", "정상"),
        row("444444", "거래정지", state="증거금100%|거래정지"),
        row("555555", "관리종목", state="관리종목"),
        row("666666", "투자주의", warning="3"),
    ])

    assert universe.get_tickers() == ["005930"]


def test_universe_returns_empty_when_response_shape_unexpected():
    client = SimpleNamespace(request=lambda *a, **kw: ({"return_code": 0}, {}))
    assert LargeCapUniverse(client).get_tickers() == []


def test_collector_gathers_previous_day_metrics_for_each_ticker():
    universe = make_universe([row("005930", "삼성전자"), row("000660", "SK하이닉스")])

    result = collect(universe, fake_market_data(lambda t: metrics(t, close=70000.0)))

    assert {d.ticker for d in result} == {"005930", "000660"}
    picked = next(d for d in result if d.ticker == "005930")
    assert picked.name == "삼성전자"
    assert picked.prev_change_rate == 1.5
    assert picked.prev_close == 70000.0
    assert picked.prev_high == 71400.0


def test_collector_skips_failing_ticker_without_aborting():
    universe = make_universe([row("005930"), row("000660"), row("035420")])

    def flaky(ticker):
        if ticker == "000660":
            raise RuntimeError("일시적 조회 실패")
        return metrics(ticker)

    result = collect(universe, fake_market_data(flaky))

    # 한 종목이 실패해도 나머지는 수집된다
    assert {d.ticker for d in result} == {"005930", "035420"}


def test_collector_skips_ticker_without_usable_candles():
    """일봉이 없어 전일 지표를 못 만든 종목(None)은 조용히 빠진다."""
    universe = make_universe([row("005930"), row("000660")])

    def by_ticker(ticker):
        return None if ticker == "000660" else metrics(ticker)

    assert [d.ticker for d in collect(universe, fake_market_data(by_ticker))] == ["005930"]


def test_collector_returns_empty_when_universe_fails():
    def boom(*a, **kw):
        raise RuntimeError("유니버스 조회 실패")

    universe = LargeCapUniverse(SimpleNamespace(request=boom))

    assert collect(universe, fake_market_data(lambda t: metrics(t))) == []


def test_collector_shortlists_risen_stocks_by_volume_surge():
    universe = make_universe([row(code) for code in ("100000", "200000", "300000")])
    surges = {"100000": 1.0, "200000": 5.0, "300000": 3.0}

    result = collect(
        universe,
        fake_market_data(lambda t: metrics(t, volume_surge=surges[t])),
        shortlist_size=2,
    )

    # 급증 배수가 큰 순서대로 남는다
    assert [d.ticker for d in result] == ["200000", "300000"]


def test_collector_puts_risen_stocks_ahead_of_fallen_ones():
    """하락 종목은 급증 배수가 더 커도 상승 종목 뒤로 밀린다."""
    universe = make_universe([row("100000"), row("200000")])
    falls = {"100000": -2.0, "200000": 0.5}
    surges = {"100000": 9.0, "200000": 1.2}

    result = collect(
        universe,
        fake_market_data(lambda t: metrics(t, change_rate=falls[t], volume_surge=surges[t])),
    )

    assert [d.ticker for d in result] == ["200000", "100000"]


def test_collector_fills_remaining_slots_with_fallen_stocks():
    """전 종목이 하락한 날에도 후보가 비면 안 된다 — 추천 자체가 스킵된다."""
    universe = make_universe([row("100000"), row("200000")])
    surges = {"100000": 1.0, "200000": 4.0}

    result = collect(
        universe,
        fake_market_data(lambda t: metrics(t, change_rate=-1.0, volume_surge=surges[t])),
    )

    assert [d.ticker for d in result] == ["200000", "100000"]
