from types import SimpleNamespace

from src.api.market_data import StockDetail
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


def test_universe_keeps_only_large_cap_kospi_stocks():
    universe = make_universe([
        row("005930", "삼성전자"),
        row("111111", "중형주종목", size="중형주"),
        row("222222", "ETF종목", market="ETF"),
        row("333333", "분류없음", size=""),
    ])

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


def test_collector_gathers_details_for_each_ticker():
    universe = make_universe([row("005930", "삼성전자"), row("000660", "SK하이닉스")])
    market_data = SimpleNamespace(
        get_stock_detail=lambda t: StockDetail(
            ticker=t, price=1000.0, volume=500, change_rate=1.5, gap_rate=0.8
        )
    )

    # request_interval=0으로 테스트에서 대기하지 않는다
    result = DataCollector(market_data, universe, NewsClient(), request_interval=0).collect()

    assert [d.ticker for d in result] == ["005930", "000660"]
    assert result[0].name == "삼성전자"
    assert result[0].change_rate == 1.5
    assert result[0].gap_rate == 0.8


def test_collector_skips_failing_ticker_without_aborting():
    universe = make_universe([row("005930"), row("000660"), row("035420")])

    def flaky(ticker):
        if ticker == "000660":
            raise RuntimeError("일시적 조회 실패")
        return StockDetail(ticker=ticker, price=1.0, volume=1, change_rate=0.0, gap_rate=0.0)

    result = DataCollector(
        SimpleNamespace(get_stock_detail=flaky), universe, NewsClient(), request_interval=0
    ).collect()

    # 한 종목이 실패해도 나머지는 수집된다
    assert [d.ticker for d in result] == ["005930", "035420"]


def test_collector_returns_empty_when_universe_fails():
    def boom(*a, **kw):
        raise RuntimeError("유니버스 조회 실패")

    universe = LargeCapUniverse(SimpleNamespace(request=boom))
    result = DataCollector(SimpleNamespace(), universe, NewsClient(), request_interval=0).collect()

    assert result == []
