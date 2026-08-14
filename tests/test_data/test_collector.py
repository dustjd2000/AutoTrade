from types import SimpleNamespace

import pytest

from src.api.market_data import PreviousDayMetrics
from src.data.collector import DataCollector, LargeCapUniverse
from src.data.disclosure import DisclosureClient


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
        recent_high=close * 1.10,
        recent_low=close * 0.90,
        moving_average=close * 0.95,
    )


def fake_market_data_with_quotes(metrics_fn, quote_fn):
    """당일 현재가까지 주는 가짜 클라이언트.

    quote_fn(ticker)가 None을 돌려주면 조회 실패로 본다 (예외를 던지는 경우와 같은 경로).
    """

    def get_current_price(ticker):
        result = quote_fn(ticker)
        if result is None:
            raise RuntimeError("조회 실패")
        return result

    return SimpleNamespace(
        get_previous_day_metrics=metrics_fn,
        get_current_price=get_current_price,
    )


def quote(price, volume=1000):
    return SimpleNamespace(price=price, volume=volume)


def fake_disclosures(by_ticker):
    return SimpleNamespace(fetch=lambda tickers: by_ticker)


def collect(universe, market_data, disclosures=None, **kwargs):
    """request_interval=0으로 테스트에서 대기하지 않는다.

    공시 클라이언트를 넘기지 않으면 키 없는 DisclosureClient가 들어가 빈 결과를 돌려준다
    — 조회도 배제도 일어나지 않아 공시 도입 전과 같은 경로가 된다.
    """
    return DataCollector(
        market_data,
        universe,
        disclosures or DisclosureClient(""),
        request_interval=0,
        **kwargs,
    ).collect()


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


def test_collector_carries_the_recent_price_band_to_the_prompt_data():
    """LLM이 목표 매수가를 정하려면 최근 가격대가 필요하다 (프롬프트 v6)."""
    universe = make_universe([row("005930", "삼성전자")])

    result = collect(universe, fake_market_data(lambda t: metrics(t, close=10000.0)))

    assert result[0].recent_high == 11000.0
    assert result[0].recent_low == 9000.0
    assert result[0].moving_average == 9500.0


# ── 공시 (PRD 5.5-B '공시 수집과 악재 배제') ─────────────────


def test_collector_drops_stocks_with_a_blocking_disclosure():
    universe = make_universe([row("005930", "삼성전자"), row("000660", "SK하이닉스")])
    disclosures = fake_disclosures({"000660": ["주요사항보고서(유상증자결정)"]})

    result = collect(universe, fake_market_data(metrics), disclosures)

    assert [d.ticker for d in result] == ["005930"]


def test_collector_carries_disclosure_titles_into_the_prompt_data():
    universe = make_universe([row("005930", "삼성전자")])
    disclosures = fake_disclosures({"005930": ["단일판매ㆍ공급계약체결", "분기보고서"]})

    result = collect(universe, fake_market_data(metrics), disclosures)

    assert result[0].headlines == ["단일판매ㆍ공급계약체결", "분기보고서"]


def test_collector_keeps_only_the_latest_headlines():
    universe = make_universe([row("005930", "삼성전자")])
    disclosures = fake_disclosures({"005930": [f"공시{n}" for n in range(6)]})

    result = collect(universe, fake_market_data(metrics), disclosures)

    assert result[0].headlines == ["공시0", "공시1", "공시2"]


def test_collector_excludes_before_shortlisting_so_the_slot_is_refilled():
    """배제된 종목의 자리는 다음 후보가 채운다 — 쇼트리스트 뒤에 배제하면 정원이 준다."""
    universe = make_universe([row(code) for code in ("100000", "200000", "300000")])
    surges = {"100000": 5.0, "200000": 3.0, "300000": 1.0}
    disclosures = fake_disclosures({"200000": ["주요사항보고서(유상증자결정)"]})

    result = collect(
        universe,
        fake_market_data(lambda t: metrics(t, volume_surge=surges[t])),
        disclosures,
        shortlist_size=2,
    )

    assert [d.ticker for d in result] == ["100000", "300000"]


def test_collector_continues_without_disclosures_when_the_lookup_fails():
    """공시 하나 때문에 그날 추천 전체가 멈추지 않는다. 대신 알림을 남긴다."""
    universe = make_universe([row("005930", "삼성전자")])

    def boom(tickers):
        raise RuntimeError("DART 조회 실패")

    alerts = []
    result = DataCollector(
        fake_market_data(metrics),
        universe,
        SimpleNamespace(fetch=boom),
        request_interval=0,
        notify=alerts.append,
    ).collect()

    assert [d.ticker for d in result] == ["005930"]
    assert result[0].headlines == []
    assert len(alerts) == 1 and "DART" in alerts[0]


def test_prev_range_pct_from_previous_day_high_low():
    """전일 변동폭 = (고가 − 저가) ÷ 종가 × 100. 프롬프트용 값이라 거르지는 않는다."""
    universe = make_universe([row("005930")])
    md = fake_market_data(lambda t: metrics(t, close=10000.0))
    result = collect(universe, md)

    # metrics()의 고가 10200 / 저가 9800 → (10200 - 9800) / 10000 * 100 = 4.0
    assert result[0].prev_range_pct == pytest.approx(4.0)


def test_today_fields_default_to_zero():
    """당일 지표는 수집 전에는 0이다 — 0은 '산출 안 됨'이라는 기존 규약을 따른다."""
    universe = make_universe([row("005930")])
    md = fake_market_data(lambda t: metrics(t))
    result = collect(universe, md)

    assert result[0].today_price == 0.0
    assert result[0].today_change_rate == 0.0
    assert result[0].today_volume == 0


# ── 당일 지표 (PRD 5.5-B '당일 지표 병행 수집') ─────────────────


def test_today_metrics_calculated_from_current_price():
    """당일 등락률은 키움 필드가 아니라 현재가와 전일 종가로 직접 계산한다."""
    universe = make_universe([row("005930")])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        lambda t: quote(10200.0, volume=5000),
    )
    result = collect(universe, md, gap_down_tolerance_ratio=0.01)

    assert result[0].today_price == 10200.0
    assert result[0].today_volume == 5000
    assert result[0].today_change_rate == pytest.approx(2.0)


def test_gap_down_stock_dropped_from_candidates():
    """당일 -1% 미만으로 출발한 종목은 후보에서 뺀다."""
    universe = make_universe([row(f"{i:06d}") for i in range(10)])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        # 000000만 -2%, 나머지는 +1%
        lambda t: quote(9800.0 if t == "000000" else 10100.0),
    )
    result = collect(universe, md, gap_down_tolerance_ratio=0.01)

    assert "000000" not in [d.ticker for d in result]
    assert len(result) == 9


def test_small_gap_down_within_tolerance_is_kept():
    """-1% '이상'이면 통과한다 — 경계값에서 잘라내지 않는다."""
    universe = make_universe([row("005930")])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        lambda t: quote(9900.0),  # 정확히 -1.0%
    )
    result = collect(universe, md, gap_down_tolerance_ratio=0.01)

    assert [d.ticker for d in result] == ["005930"]


def test_quote_failure_does_not_drop_candidate():
    """현재가 조회 실패로 멀쩡한 종목을 잃지 않는다 — 필터를 통과시킨다."""
    universe = make_universe([row("005930")])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        lambda t: None,
    )
    result = collect(universe, md, gap_down_tolerance_ratio=0.01)

    assert [d.ticker for d in result] == ["005930"]
    assert result[0].today_price == 0.0


def test_filter_lifted_when_too_few_survive():
    """지수가 통째로 갭 하락한 날 — 필터를 걷고 진행하며 운영 알림을 보낸다."""
    universe = make_universe([row(f"{i:06d}") for i in range(10)])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        lambda t: quote(9500.0),  # 전 종목 -5%
    )
    alerts = []
    result = collect(universe, md, gap_down_tolerance_ratio=0.01, notify=alerts.append)

    assert len(result) == 10  # 하한(5) 미만이라 필터를 걷었다
    assert any("필터" in message for message in alerts)


def test_filter_disabled_when_tolerance_zero():
    """허용치 0은 '끔'이다 — 갭 하락 판정과 같은 규약."""
    universe = make_universe([row("005930")])
    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0),
        lambda t: quote(5000.0),  # -50%
    )
    result = collect(universe, md, gap_down_tolerance_ratio=0.0)

    assert [d.ticker for d in result] == ["005930"]


def test_prescreen_limits_quote_calls():
    """현재가 조회는 급증 배수 상위 PRESCREEN_SIZE 종목에만 돈다."""
    universe = make_universe([row(f"{i:06d}") for i in range(60)])
    asked = []

    def quote_fn(ticker):
        asked.append(ticker)
        return quote(10100.0)

    md = fake_market_data_with_quotes(
        lambda t: metrics(t, close=10000.0, volume_surge=float(t)),
        quote_fn,
    )
    collect(universe, md, gap_down_tolerance_ratio=0.01, prescreen_size=40)

    assert len(asked) == 40
