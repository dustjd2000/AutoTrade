from types import SimpleNamespace

import pytest

from src.data.collector import DailyStockData
from src.llm.recommender import (
    LLMRecommender,
    build_system_prompt,
    build_user_prompt,
    cap_quota,
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


def test_parse_accepts_empty_array():
    """확신 종목이 없다는 유효한 결과 — 예외가 아니라 빈 리스트로 온다."""
    assert parse_recommendations("[]") == []


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
    recommender.settings = SimpleNamespace(llm_model="claude-sonnet-5", target_stock_count=3)
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


def test_recommend_returns_none_on_empty_recommendations():
    """형식은 정상이지만 LLM이 확신 종목을 하나도 고르지 않은 경우 — 매수는 스킵한다."""
    text = '{"recommendations": []}'
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
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


def test_build_user_prompt_reflects_target_count():
    daily_data = [
        DailyStockData(
            ticker="005930",
            name="삼성전자",
            change_rate=1.5,
            volume=1_000_000,
            gap_rate=0.8,
            headlines=[],
        )
    ]
    prompt = build_user_prompt(daily_data, target_count=5)

    assert "대형주 4종목 + 중형주 1종목" in prompt
    assert "총 5개" in prompt


def test_build_system_prompt_reflects_target_count():
    prompt = build_system_prompt(target_count=5)

    assert "5종목을 선별" in prompt
    assert "대형주에서 4종목, 중형주에서 1종목" in prompt


@pytest.mark.parametrize(
    "target_count, expected",
    [(1, (1, 0)), (2, (1, 1)), (3, (2, 1)), (10, (9, 1))],
)
def test_cap_quota_reserves_one_slot_for_mid_cap(target_count, expected):
    """중형주는 항상 1종목. 단 한 종목만 뽑는 설정에서는 자금 전부가 얇은 종목에 들어가므로 제외."""
    assert cap_quota(target_count) == expected


def test_build_user_prompt_groups_by_cap_tier_and_shows_volume_surge():
    daily_data = [
        DailyStockData(
            ticker="005930",
            name="삼성전자",
            change_rate=1.5,
            volume=1_000_000,
            gap_rate=0.8,
            cap_tier="대형주",
            volume_surge=3.42,
        ),
        DailyStockData(
            ticker="006340",
            name="중형종목",
            change_rate=2.0,
            volume=500_000,
            gap_rate=1.9,
            cap_tier="중형주",
        ),
    ]
    prompt = build_user_prompt(daily_data, target_count=3)

    assert "## 대형주 (1종목)" in prompt
    assert "## 중형주 (1종목)" in prompt
    assert "평균대비 3.42배" in prompt
    # 급증률을 못 구한 종목은 그 기준을 빼고 보라고 알려야 한다
    assert "평균대비 판단불가" in prompt
