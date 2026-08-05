from types import SimpleNamespace

from src.api.market_data import StockDetail
from src.data.collector import DataCollector, LargeMidCapUniverse, NewsClient


def make_universe(rows):
    client = SimpleNamespace(request=lambda *a, **kw: ({"list": rows}, {}))
    return LargeMidCapUniverse(client)


def row(code, name="종목", size="대형주", market="거래소", state="증거금40%", warning="0"):
    return {
        "code": code,
        "name": name,
        "upSizeName": size,
        "marketName": market,
        "state": state,
        "orderWarning": warning,
    }


def fake_market_data(detail_fn, average_volume=100.0):
    return SimpleNamespace(
        get_stock_detail=detail_fn,
        get_average_volume=lambda ticker, **kw: average_volume,
    )


def detail(ticker, change_rate=1.5, gap_rate=0.8, volume=500):
    return StockDetail(
        ticker=ticker, price=1000.0, volume=volume, change_rate=change_rate, gap_rate=gap_rate
    )


def collect(universe, market_data, **kwargs):
    """request_interval=0으로 테스트에서 대기하지 않는다."""
    return DataCollector(market_data, universe, NewsClient(), request_interval=0, **kwargs).collect()


def test_universe_keeps_large_and_mid_cap_kospi_stocks():
    universe = make_universe([
        row("005930", "삼성전자"),
        row("111111", "중형주종목", size="중형주"),
        row("777777", "소형주종목", size="소형주"),
        row("222222", "ETF종목", market="ETF"),
        row("333333", "분류없음", size=""),
    ])

    # 소형주는 유동성이 얇아 제외한다 (확정 2026-08-05)
    assert universe.get_tickers() == ["005930", "111111"]


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
    assert LargeMidCapUniverse(client).get_tickers() == []


def test_collector_gathers_details_for_each_ticker():
    universe = make_universe([row("005930", "삼성전자"), row("000660", "SK하이닉스")])

    result = collect(universe, fake_market_data(lambda t: detail(t)))

    assert {d.ticker for d in result} == {"005930", "000660"}
    picked = next(d for d in result if d.ticker == "005930")
    assert picked.name == "삼성전자"
    assert picked.change_rate == 1.5
    assert picked.gap_rate == 0.8
    assert picked.cap_tier == "대형주"


def test_collector_skips_failing_ticker_without_aborting():
    universe = make_universe([row("005930"), row("000660"), row("035420")])

    def flaky(ticker):
        if ticker == "000660":
            raise RuntimeError("일시적 조회 실패")
        return detail(ticker)

    result = collect(universe, fake_market_data(flaky))

    # 한 종목이 실패해도 나머지는 수집된다
    assert {d.ticker for d in result} == {"005930", "035420"}


def test_collector_returns_empty_when_universe_fails():
    def boom(*a, **kw):
        raise RuntimeError("유니버스 조회 실패")

    universe = LargeMidCapUniverse(SimpleNamespace(request=boom))

    assert collect(universe, fake_market_data(lambda t: detail(t))) == []


def test_collector_drops_stocks_without_direction_signal():
    """등락률·시가갭이 모두 0인 종목은 근거로 쓸 수 없어 후보에서 뺀다.

    장 전에는 동시호가 예상체결가가 잡히지 않은 종목이 이렇게 넘어오는데, 남겨두면
    개수를 채우라는 지시에 밀려 "등락률 +0.00%"를 근거로 든 추천이 나온다.
    """
    universe = make_universe([row("005930", "신호있음"), row("000660", "신호없음")])

    def by_ticker(ticker):
        if ticker == "000660":
            return detail(ticker, change_rate=0.0, gap_rate=0.0)
        return detail(ticker)

    result = collect(universe, fake_market_data(by_ticker))

    assert [d.ticker for d in result] == ["005930"]


def test_collector_keeps_stock_when_only_one_of_the_two_signals_is_zero():
    universe = make_universe([row("005930"), row("000660")])

    def by_ticker(ticker):
        if ticker == "000660":
            return detail(ticker, change_rate=-1.2, gap_rate=0.0)
        return detail(ticker, change_rate=0.0, gap_rate=2.0)

    result = collect(universe, fake_market_data(by_ticker))

    assert {d.ticker for d in result} == {"005930", "000660"}


def test_collector_shortlists_top_gap_stocks_per_cap_tier():
    universe = make_universe([
        row("100000", "대형1"),
        row("200000", "대형2"),
        row("300000", "대형3"),
        row("400000", "중형1", size="중형주"),
        row("500000", "중형2", size="중형주"),
    ])
    gaps = {"100000": 1.0, "200000": 5.0, "300000": 3.0, "400000": 0.5, "500000": 4.0}

    result = collect(
        universe,
        fake_market_data(lambda t: detail(t, gap_rate=gaps[t])),
        shortlist_by_cap={"대형주": 2, "중형주": 1},
    )

    # 규모별로 시가갭 상위만, 큰 순서대로 남는다
    assert [d.ticker for d in result] == ["200000", "300000", "500000"]


def test_collector_computes_volume_surge_against_average():
    universe = make_universe([row("005930")])

    result = collect(
        universe,
        fake_market_data(lambda t: detail(t, volume=900), average_volume=300.0),
    )

    assert result[0].volume_surge == 3.0


def test_collector_leaves_volume_surge_zero_when_average_unavailable():
    """평균 거래량을 못 구하면 급증률을 비워 둔다 — 프롬프트에 '판단불가'로 나간다."""
    universe = make_universe([row("005930")])

    def boom(ticker, **kw):
        raise RuntimeError("일봉 조회 실패")

    market_data = SimpleNamespace(
        get_stock_detail=lambda t: detail(t), get_average_volume=boom
    )

    assert collect(universe, market_data)[0].volume_surge == 0.0
