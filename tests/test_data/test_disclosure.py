from datetime import date
from types import SimpleNamespace

import pytest

from src.data import disclosure
from src.data.disclosure import (
    DisclosureAPIError,
    DisclosureClient,
    blocking_disclosure,
    previous_business_day,
)


def fake_response(payload):
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


def page(items, total_page=1, status="000"):
    return {"status": status, "message": "정상", "total_page": total_page, "list": items}


def item(stock_code, report_nm):
    return {"stock_code": stock_code, "report_nm": report_nm, "corp_cls": "Y"}


def stub_pages(monkeypatch, pages):
    """요청 순서대로 pages를 돌려주고, 호출 시 넘어온 params를 모아 둔다."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return fake_response(pages[len(calls) - 1])

    monkeypatch.setattr(disclosure.requests, "get", fake_get)
    return calls


# ── 배제 판정 ─────────────────────────────────────────────


def test_blocking_disclosure_catches_dilution_and_fraud():
    assert blocking_disclosure(["주요사항보고서(유상증자결정)"]) == "주요사항보고서(유상증자결정)"
    assert blocking_disclosure(["횡령ㆍ배임혐의발생"]) == "횡령ㆍ배임혐의발생"
    assert blocking_disclosure(["불성실공시법인지정예고"]) == "불성실공시법인지정예고"


def test_blocking_disclosure_ignores_spacing_variations():
    """공시명 띄어쓰기가 들쭉날쭉하게 와도 같은 판정이 나와야 한다."""
    assert blocking_disclosure(["주요사항보고서 (전환사채권 발행결정)"]) is not None


def test_blocking_disclosure_lets_lawsuits_through():
    """소송등의제기는 배제하지 않는다 — 대형주에는 일상적으로 뜬다 (PRD 5.5-B)."""
    assert blocking_disclosure(["소송등의제기·신청"]) is None


def test_blocking_disclosure_returns_none_for_ordinary_filings():
    assert blocking_disclosure(["분기보고서", "단일판매ㆍ공급계약체결"]) is None


def test_blocking_disclosure_reports_the_first_match_among_many():
    titles = ["분기보고서", "주요사항보고서(유상증자결정)", "횡령ㆍ배임혐의발생"]
    assert blocking_disclosure(titles) == "주요사항보고서(유상증자결정)"


# ── 조회 기간 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 8, 12), date(2026, 8, 11)),  # 수요일 → 화요일
        (date(2026, 8, 10), date(2026, 8, 7)),   # 월요일 → 금요일
        (date(2026, 8, 9), date(2026, 8, 7)),    # 일요일 → 금요일
        (date(2026, 8, 8), date(2026, 8, 7)),    # 토요일 → 금요일
    ],
)
def test_previous_business_day_skips_the_weekend(today, expected):
    assert previous_business_day(today) == expected


def test_fetch_requests_the_previous_business_day_through_today(monkeypatch):
    calls = stub_pages(monkeypatch, [page([])])

    DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 10))

    assert calls[0]["bgn_de"] == "20260807"
    assert calls[0]["end_de"] == "20260810"
    assert calls[0]["corp_cls"] == "Y"


# ── 수집 ─────────────────────────────────────────────────


def test_fetch_groups_titles_by_ticker(monkeypatch):
    stub_pages(monkeypatch, [page([
        item("005930", "분기보고서"),
        item("005930", "단일판매ㆍ공급계약체결"),
        item("000660", "주요사항보고서(유상증자결정)"),
    ])])

    result = DisclosureClient("key").fetch(["005930", "000660"], today=date(2026, 8, 12))

    assert result == {
        "005930": ["분기보고서", "단일판매ㆍ공급계약체결"],
        "000660": ["주요사항보고서(유상증자결정)"],
    }


def test_fetch_drops_tickers_outside_the_universe(monkeypatch):
    """코스피 전체 공시를 받아오므로 유니버스 밖 종목이 대부분이다."""
    stub_pages(monkeypatch, [page([item("005930", "분기보고서"), item("999999", "분기보고서")])])

    result = DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 12))

    assert list(result) == ["005930"]


def test_fetch_follows_pagination_until_the_last_page(monkeypatch):
    calls = stub_pages(monkeypatch, [
        page([item("005930", "1페이지")], total_page=2),
        page([item("005930", "2페이지")], total_page=2),
    ])

    result = DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 12))

    assert [params["page_no"] for params in calls] == [1, 2]
    assert result == {"005930": ["1페이지", "2페이지"]}


def test_fetch_stops_at_the_page_cap(monkeypatch):
    """total_page가 이상하게 커도 무한히 돌지 않는다."""
    pages = [page([item("005930", f"{n}")], total_page=9999) for n in range(disclosure.MAX_PAGES)]
    calls = stub_pages(monkeypatch, pages)

    DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 12))

    assert len(calls) == disclosure.MAX_PAGES


def test_fetch_returns_empty_when_dart_has_no_data(monkeypatch):
    """공휴일 다음날처럼 조회 결과가 없는 상태(013)는 오류가 아니다."""
    stub_pages(monkeypatch, [page([], status="013")])

    assert DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 12)) == {}


def test_fetch_raises_on_error_status(monkeypatch):
    stub_pages(monkeypatch, [page([], status="020")])

    with pytest.raises(DisclosureAPIError):
        DisclosureClient("key").fetch(["005930"], today=date(2026, 8, 12))


def test_fetch_skips_the_call_without_an_api_key(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("키가 없으면 호출하지 않아야 한다")

    monkeypatch.setattr(disclosure.requests, "get", boom)

    assert DisclosureClient("").fetch(["005930"]) == {}
