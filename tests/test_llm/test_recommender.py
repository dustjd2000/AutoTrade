from types import SimpleNamespace

import pytest

from src.data.collector import DailyStockData
from src.llm.recommender import (
    LLMRecommender,
    StockRecommendation,
    apply_price_guardrail,
    build_system_prompt,
    build_user_prompt,
    normalize_target_price,
    parse_recommendations,
    tick_size,
)


def stock(ticker="005930", name="삼성전자", **kwargs):
    defaults = dict(
        prev_close=70000.0,
        prev_high=71000.0,
        prev_low=69000.0,
        prev_change_rate=1.5,
        prev_volume=1_000_000,
        volume_surge=0.0,
        headlines=[],
    )
    defaults.update(kwargs)
    return DailyStockData(ticker=ticker, name=name, **defaults)


def test_parse_valid_json_array():
    raw = (
        '[{"ticker": "005930", "name": "삼성전자", "target_price": 70000, "reason": "외국인 순매수"},'
        ' {"ticker": "000660", "name": "SK하이닉스", "target_price": 250000, "reason": "HBM 수요"}]'
    )
    result = parse_recommendations(raw)

    assert len(result) == 2
    assert result[0].ticker == "005930"
    assert result[0].target_price == 70000
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


def test_parse_accepts_target_price_as_string():
    """스키마는 정수를 요구하지만 문자열로 와도 깨지지 않아야 한다."""
    raw = '[{"ticker": "005930", "name": "삼성전자", "target_price": "70500.0", "reason": "수급"}]'
    assert parse_recommendations(raw)[0].target_price == 70500


def test_parse_schema_object_form():
    """구조화 출력의 최상위 형태 — {"recommendations": [...]}"""
    raw = (
        '{"recommendations": [{"ticker": "005930", "name": "삼성전자",'
        ' "target_price": 70000, "reason": "수급 개선"}]}'
    )
    result = parse_recommendations(raw)

    assert len(result) == 1
    assert result[0].ticker == "005930"


def test_parse_strips_markdown_code_fence():
    raw = (
        '```json\n[{"ticker": "005930", "name": "삼성전자",'
        ' "target_price": 70000, "reason": "수급 개선"}]\n```'
    )
    assert parse_recommendations(raw)[0].ticker == "005930"


def test_parse_ignores_surrounding_prose():
    raw = (
        '네, 분석 결과입니다:\n[{"ticker": "005930", "name": "삼성전자",'
        ' "target_price": 70000, "reason": "수급"}]\n참고하세요.'
    )
    assert parse_recommendations(raw)[0].ticker == "005930"


def test_parse_drops_duplicate_tickers():
    """같은 종목이 두 번 오면 한 종목에 두 배로 투입되므로 걸러내야 한다."""
    raw = (
        '[{"ticker": "000660", "name": "SK하이닉스", "target_price": 250000, "reason": "모멘텀"},'
        ' {"ticker": "005930", "name": "삼성전자", "target_price": 70000, "reason": "거래량"},'
        ' {"ticker": "000660", "name": "SK하이닉스", "target_price": 251000, "reason": "업종"}]'
    )
    result = parse_recommendations(raw)

    assert [r.ticker for r in result] == ["000660", "005930"]


def test_parse_rejects_empty_string():
    """본문이 비어 오는 경우 — max_tokens 소진 시 실제로 발생했다."""
    with pytest.raises(Exception):
        parse_recommendations("")


# ── 목표 매수가 보정 ────────────────────────────────────────
@pytest.mark.parametrize(
    "price, expected",
    [(1500, 1), (3000, 5), (12000, 10), (30000, 50), (100000, 100), (300000, 500), (700000, 1000)],
)
def test_tick_size_by_price_band(price, expected):
    assert tick_size(price) == expected


def test_normalize_rounds_down_to_tick():
    """호가 단위에 맞지 않는 가격은 주문이 거부된다 — 매수에 불리하지 않은 내림으로 맞춘다."""
    assert normalize_target_price(70_050, prev_close=70_000) == 70_000


def test_normalize_clamps_price_above_guardrail():
    # 전일 종가 70,000원 → 상한 73,500원 → 호가 단위(100원) 내림
    assert normalize_target_price(90_000, prev_close=70_000) == 73_500


def test_normalize_clamps_price_below_guardrail():
    # 하한 66,500원
    assert normalize_target_price(10_000, prev_close=70_000) == 66_500


def test_normalize_leaves_price_inside_guardrail_alone():
    assert normalize_target_price(69_000, prev_close=70_000) == 69_000


def test_normalize_without_prev_close_only_fixes_tick():
    """전일 종가를 모르면 가드레일 없이 호가 단위만 맞춘다."""
    assert normalize_target_price(70_050, prev_close=0.0) == 70_000


def test_apply_price_guardrail_uses_matching_stock():
    recommendations = [StockRecommendation("005930", "삼성전자", 99_999, "수급")]

    apply_price_guardrail(recommendations, [stock(prev_close=70_000.0)])

    assert recommendations[0].target_price == 73_500


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
    text = (
        '{"recommendations": [{"ticker": "068270", "name": "셀트리온",'
        ' "target_price": 180000, "reason": "수급"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    result = _fake_recommender(response).recommend([stock(ticker="068270", prev_close=180_000.0)])

    assert result is not None
    assert result[0].ticker == "068270"
    assert result[0].target_price == 180_000


def test_recommend_applies_guardrail_to_target_price():
    """LLM이 자릿수를 틀려도 전일 종가 ±5% 밖으로는 주문하지 않는다."""
    text = (
        '{"recommendations": [{"ticker": "068270", "name": "셀트리온",'
        ' "target_price": 1800000, "reason": "수급"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    result = _fake_recommender(response).recommend([stock(ticker="068270", prev_close=180_000.0)])

    assert result[0].target_price == 189_000


# ── 프롬프트 ────────────────────────────────────────────────
def test_build_user_prompt_includes_previous_day_data():
    prompt = build_user_prompt([stock(headlines=["신규 수주 공시"], volume_surge=3.42)])

    assert "005930" in prompt
    assert "삼성전자" in prompt
    assert "+1.50%" in prompt
    assert "70,000원" in prompt
    assert "평균대비 3.42배" in prompt
    assert "신규 수주 공시" in prompt


def test_build_user_prompt_marks_missing_volume_surge():
    """급증률을 못 구한 종목은 그 기준을 빼고 보라고 알려야 한다."""
    assert "평균대비 판단불가" in build_user_prompt([stock(volume_surge=0.0)])


def test_build_user_prompt_reflects_target_count():
    prompt = build_user_prompt([stock()], target_count=5)

    assert "종목 5개" in prompt
    assert "목표 매수가" in prompt


def test_build_system_prompt_reflects_target_count():
    prompt = build_system_prompt(target_count=5)

    assert "대형주 5종목" in prompt
    assert "5종목을 채우십시오" in prompt


def test_build_system_prompt_states_previous_day_basis():
    """당일 지표를 쓰지 않는다는 사실이 프롬프트에 드러나야 한다 (PRD 10절)."""
    prompt = build_system_prompt(target_count=3)

    assert "전일" in prompt
    assert "09:30" in prompt  # 미체결 취소 규칙을 알려야 목표가를 현실적으로 잡는다
