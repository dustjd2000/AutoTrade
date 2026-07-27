from types import SimpleNamespace

from src.api.market_data import MarketDataClient


def make_client(response):
    client = MarketDataClient.__new__(MarketDataClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=lambda *a, **kw: (response, {}))
    return client


def test_get_stock_detail_uses_kiwoom_supplied_change_rate():
    client = make_client(
        {"cur_prc": "+254000", "base_pric": "249500", "open_pric": "+257000",
         "flu_rt": "+1.80", "trde_qty": "23296044"}
    )

    detail = client.get_stock_detail("005930")

    assert detail.price == 254000.0        # 부호는 등락 방향일 뿐이므로 절댓값
    assert detail.change_rate == 1.80
    assert round(detail.gap_rate, 2) == 3.01   # (257000-249500)/249500*100
    assert detail.volume == 23296044


def test_get_stock_detail_handles_declining_stock_without_negative_price():
    client = make_client(
        {"cur_prc": "-179000", "base_pric": "180000", "open_pric": "-179500", "flu_rt": "-0.56"}
    )

    detail = client.get_stock_detail("000660")

    assert detail.price == 179000.0    # 하락 종목도 가격은 양수여야 한다
    assert detail.change_rate == -0.56
    assert detail.gap_rate < 0


def test_get_stock_detail_computes_change_rate_when_missing():
    client = make_client({"cur_prc": "11000", "base_pric": "10000", "open_pric": "10000"})

    detail = client.get_stock_detail("005930")

    assert detail.change_rate == 10.0   # flu_rt가 없으면 전일 종가로 직접 계산
    assert detail.gap_rate == 0.0


def test_get_stock_detail_survives_missing_base_price():
    client = make_client({"cur_prc": "11000"})

    detail = client.get_stock_detail("005930")

    assert detail.price == 11000.0
    assert detail.gap_rate == 0.0   # 0으로 나누지 않는다


def test_get_current_price_returns_absolute_price():
    client = make_client({"cur_prc": "-179000", "trde_qty": "1000"})

    data = client.get_current_price("000660")

    assert data.price == 179000.0
    assert data.volume == 1000
