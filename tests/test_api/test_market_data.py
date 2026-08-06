from datetime import date
from types import SimpleNamespace

from src.api.market_data import MarketDataClient


def make_client(response):
    client = MarketDataClient.__new__(MarketDataClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=lambda *a, **kw: (response, {}))
    return client


def test_get_current_price_returns_absolute_price():
    client = make_client({"cur_prc": "-179000", "trde_qty": "1000"})

    data = client.get_current_price("000660")

    assert data.price == 179000.0
    assert data.volume == 1000


def capturing_client(response):
    """요청 파라미터를 확인해야 하는 테스트용 — 마지막 호출 인자를 함께 돌려준다."""
    calls = []

    def request(path, api_id, params, *a, **kw):
        calls.append({"path": path, "api_id": api_id, "params": params})
        return response, {}

    client = MarketDataClient.__new__(MarketDataClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=request)
    return client, calls


def test_get_ohlcv_sends_required_query_date():
    """qry_dt는 ka10086의 필수값 — 비워 보내면 API가 return_code=2로 거부한다."""
    client, calls = capturing_client({"daly_stkpc": [{"trde_qty": "100"}]})

    client.get_ohlcv("005930", base_date=date(2026, 8, 5))

    assert calls[0]["params"]["qry_dt"] == "20260805"
    assert calls[0]["params"]["stk_cd"] == "005930"


def test_get_ohlcv_defaults_query_date_to_today():
    client, calls = capturing_client({"daly_stkpc": []})

    client.get_ohlcv("005930")

    assert calls[0]["params"]["qry_dt"] == date.today().strftime("%Y%m%d")


# ── 전일 지표 (ka10086 일봉 1회) ─────────────────────────────
def candle(day, close="70000", volume="1000", flu_rt="+1.50", high=None, low=None):
    return {
        "date": day,
        "close_pric": close,
        "high_pric": high or close,
        "low_pric": low or close,
        "flu_rt": flu_rt,
        "trde_qty": volume,
    }


def test_previous_day_metrics_skips_today_candle():
    """응답 맨 앞에는 당일 봉이 섞여 온다 — 이걸 쓰면 장 전과 장중 결과가 갈린다."""
    client = make_client(
        {
            "daly_stkpc": [
                candle("20260806", close="-71000", volume="0", flu_rt="0.00"),
                candle("20260805", close="-70000", volume="3000", flu_rt="+2.50"),
                candle("20260804", close="68000", volume="1000"),
                candle("20260803", close="67000", volume="1000"),
            ]
        }
    )

    result = client.get_previous_day_metrics("005930", today=date(2026, 8, 6))

    assert result.close == 70000.0            # 부호(-)는 등락 방향 표기라 절댓값을 쓴다
    assert result.change_rate == 2.5
    assert result.volume == 3000
    assert result.volume_surge == 3.0         # 전일 3,000 ÷ 그 이전 평균 1,000


def test_previous_day_metrics_excludes_previous_day_from_average():
    """전일 자신을 평균에 넣으면 재려던 급증분이 희석된다."""
    client = make_client(
        {
            "daly_stkpc": [
                candle("20260805", volume="400"),
                candle("20260804", volume="100"),
                candle("20260803", volume="100"),
            ]
        }
    )

    result = client.get_previous_day_metrics("005930", today=date(2026, 8, 6))

    assert result.volume_surge == 4.0


def test_previous_day_metrics_ignores_zero_volume_days_in_average():
    client = make_client(
        {
            "daly_stkpc": [
                candle("20260805", volume="600"),
                candle("20260804", volume="0"),
                candle("20260803", volume="200"),
            ]
        }
    )

    assert client.get_previous_day_metrics("005930", today=date(2026, 8, 6)).volume_surge == 3.0


def test_previous_day_metrics_leaves_surge_zero_without_earlier_candles():
    """급증률을 못 구하면 0으로 남겨 프롬프트에 '판단불가'로 나간다."""
    client = make_client({"daly_stkpc": [candle("20260805", volume="500")]})

    assert client.get_previous_day_metrics("005930", today=date(2026, 8, 6)).volume_surge == 0.0


def test_previous_day_metrics_returns_none_without_usable_candles():
    client = make_client({"daly_stkpc": [candle("20260806")]})

    assert client.get_previous_day_metrics("005930", today=date(2026, 8, 6)) is None


def test_previous_day_metrics_keeps_high_and_low():
    client = make_client(
        {"daly_stkpc": [candle("20260805", close="70000", high="+72000", low="-69000")]}
    )

    result = client.get_previous_day_metrics("005930", today=date(2026, 8, 6))

    assert (result.high, result.low) == (72000.0, 69000.0)
