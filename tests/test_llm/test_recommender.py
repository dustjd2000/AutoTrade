from types import SimpleNamespace

import pytest

from src.data.collector import DailyStockData
from src.llm.recommender import (
    LLMRecommender,
    build_user_prompt,
    parse_recommendations,
)


def test_parse_valid_json_array():
    raw = (
        '[{"ticker": "005930", "name": "삼성전자", "reason": "외국인 순매수 전환"},'
        ' {"ticker": "000660", "name": "SK하이닉스", "reason": "HBM 수요 증가"}]'
    )
    result = parse_recommendations(raw)

    assert len(result) == 2
    assert result[0].ticker == "005930"
    assert result[1].name == "SK하이닉스"


def test_parse_rejects_non_json():
    with pytest.raises(Exception):
        parse_recommendations("죄송하지만 추천을 드릴 수 없습니다.")


def test_parse_rejects_empty_array():
    with pytest.raises(ValueError):
        parse_recommendations("[]")


def test_parse_rejects_missing_field():
    with pytest.raises(KeyError):
        parse_recommendations('[{"ticker": "005930", "name": "삼성전자"}]')


def test_parse_schema_object_form():
    """구조화 출력의 최상위 형태 — {"recommendations": [...]}"""
    raw = '{"recommendations": [{"ticker": "005930", "name": "삼성전자", "reason": "수급 개선"}]}'
    result = parse_recommendations(raw)

    assert len(result) == 1
    assert result[0].ticker == "005930"


def test_parse_strips_markdown_code_fence():
    raw = '```json\n[{"ticker": "005930", "name": "삼성전자", "reason": "수급 개선"}]\n```'
    assert parse_recommendations(raw)[0].ticker == "005930"


def test_parse_ignores_surrounding_prose():
    raw = '네, 분석 결과입니다:\n[{"ticker": "005930", "name": "삼성전자", "reason": "수급"}]\n참고하세요.'
    assert parse_recommendations(raw)[0].ticker == "005930"


def test_parse_drops_duplicate_tickers():
    """같은 종목이 두 번 오면 한 종목에 두 배로 투입되므로 걸러내야 한다."""
    raw = (
        '[{"ticker": "000660", "name": "SK하이닉스", "reason": "모멘텀"},'
        ' {"ticker": "005930", "name": "삼성전자", "reason": "거래량"},'
        ' {"ticker": "000660", "name": "SK하이닉스", "reason": "업종 강세"}]'
    )
    result = parse_recommendations(raw)

    assert [r.ticker for r in result] == ["000660", "005930"]


def test_parse_rejects_empty_string():
    """본문이 비어 오는 경우 — max_tokens 소진 시 실제로 발생했다."""
    with pytest.raises(Exception):
        parse_recommendations("")


# ── recommend()의 방어 로직 ──────────────────────────────────
def _fake_recommender(response) -> LLMRecommender:
    """API 호출만 가짜로 바꾼 recommender."""
    recommender = LLMRecommender.__new__(LLMRecommender)
    recommender.settings = SimpleNamespace(llm_model="claude-sonnet-5")
    recommender._client = SimpleNamespace(
        with_options=lambda **kw: SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kwargs: response)
        )
    )
    return recommender


def _response(stop_reason, blocks):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=blocks,
        usage=SimpleNamespace(input_tokens=100, output_tokens=8192),
        stop_details=None,
    )


def test_recommend_returns_none_when_truncated():
    """사고 토큰이 예산을 다 써 응답이 잘리면 매수를 스킵해야 한다."""
    response = _response("max_tokens", [SimpleNamespace(type="text", text='[{"ticker": "0059')])
    assert _fake_recommender(response).recommend([]) is None


def test_recommend_returns_none_on_empty_text():
    response = _response("end_turn", [SimpleNamespace(type="thinking", thinking="...")])
    assert _fake_recommender(response).recommend([]) is None


def test_recommend_returns_none_on_refusal():
    response = _response("refusal", [])
    assert _fake_recommender(response).recommend([]) is None


def test_recommend_parses_successful_response():
    text = '{"recommendations": [{"ticker": "068270", "name": "셀트리온", "reason": "수급"}]}'
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    result = _fake_recommender(response).recommend([])

    assert result is not None
    assert result[0].ticker == "068270"


def test_build_user_prompt_includes_all_stock_data():
    daily_data = [
        DailyStockData(
            ticker="005930",
            name="삼성전자",
            change_rate=1.5,
            volume=1_000_000,
            gap_rate=0.8,
            headlines=["신규 수주 공시"],
        )
    ]
    prompt = build_user_prompt(daily_data)

    assert "005930" in prompt
    assert "삼성전자" in prompt
    assert "+1.50%" in prompt
    assert "신규 수주 공시" in prompt
