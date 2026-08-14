from types import SimpleNamespace

import pytest

from src.data.collector import DailyStockData
from src.llm.recommender import (
    LLMRecommender,
    StockRecommendation,
    apply_price_guardrail,
    attach_recommend_price,
    build_system_prompt,
    build_user_prompt,
    drop_unknown_tickers,
    normalize_target_price,
    parse_recommendations,
    tick_size,
    warn_invalid_sell_targets,
)


def stock(ticker="005930", name="삼성전자", **kwargs):
    defaults = dict(
        prev_close=70000.0,
        prev_high=71000.0,
        prev_low=69000.0,
        prev_change_rate=1.5,
        prev_volume=1_000_000,
        volume_surge=0.0,
        prev_range_pct=2.86,
        today_price=0.0,
        today_change_rate=0.0,
        today_volume=0,
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
    assert normalize_target_price(70_050, reference_price=70_000) == 70_000


def test_normalize_clamps_price_above_guardrail():
    # 기준가 70,000원 → 상한 73,500원 → 호가 단위(100원) 내림
    assert normalize_target_price(90_000, reference_price=70_000) == 73_500


def test_normalize_clamps_price_below_guardrail():
    # 하한 66,500원
    assert normalize_target_price(10_000, reference_price=70_000) == 66_500


def test_normalize_leaves_price_inside_guardrail_alone():
    assert normalize_target_price(69_000, reference_price=70_000) == 69_000


def test_normalize_without_reference_price_only_fixes_tick():
    """기준가를 모르면 가드레일 없이 호가 단위만 맞춘다."""
    assert normalize_target_price(70_050, reference_price=0.0) == 70_000


def test_apply_price_guardrail_uses_matching_stock():
    recommendations = [StockRecommendation("005930", "삼성전자", 99_999, "수급")]

    apply_price_guardrail(recommendations, [stock(prev_close=70_000.0)])

    assert recommendations[0].target_price == 73_500


def test_attach_recommend_price_carries_today_price():
    """09:10 갭 하락 판정 기준가는 추천 시각의 현재가다 (PRD 5.5-B)."""
    recommendations = [StockRecommendation("005930", "삼성전자", 70_000, "수급")]

    attach_recommend_price(
        recommendations, [stock(prev_close=70_000.0, today_price=71_400.0)]
    )

    assert recommendations[0].recommend_price == 71_400.0


def test_attach_recommend_price_leaves_zero_when_today_price_unknown():
    """현재가를 못 받았으면 0으로 둔다 — 0은 '모름'이고 갭 판정이 꺼진다."""
    recommendations = [StockRecommendation("005930", "삼성전자", 70_000, "수급")]

    attach_recommend_price(recommendations, [stock(today_price=0.0)])

    assert recommendations[0].recommend_price == 0.0


def test_attach_recommend_price_leaves_zero_for_unknown_tickers():
    """찾지 못하면 0(모름) — 갭 하락 판정이 건너뛰어져 매수를 막지 않는다."""
    recommendations = [StockRecommendation("000660", "SK하이닉스", 250_000, "HBM")]

    attach_recommend_price(recommendations, [stock(ticker="005930")])

    assert recommendations[0].recommend_price == 0.0


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


def test_recommend_drops_stock_missing_from_candidates():
    """후보 밖 종목은 전일 종가를 몰라 ±5% 가드레일이 꺼진다 — 주문 전에 걸러낸다."""
    text = (
        '{"recommendations": ['
        '{"ticker": "068270", "name": "셀트리온", "target_price": 180000, "reason": "수급"},'
        '{"ticker": "999999", "name": "없는종목", "target_price": 5000, "reason": "환각"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    result = _fake_recommender(response).recommend([stock(ticker="068270", prev_close=180_000.0)])

    assert [r.ticker for r in result] == ["068270"]


def test_recommend_keeps_going_when_fewer_than_target_count_remain():
    """모자란 몫은 현금으로 남긴다 — 기존 정책(PRD 10절)과 같게 유지한다."""
    text = (
        '{"recommendations": ['
        '{"ticker": "068270", "name": "셀트리온", "target_price": 180000, "reason": "수급"},'
        '{"ticker": "999999", "name": "없는종목", "target_price": 5000, "reason": "환각"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    recommender = _fake_recommender(response)  # target_stock_count=3

    result = recommender.recommend([stock(ticker="068270", prev_close=180_000.0)])

    assert len(result) == 1


def test_recommend_returns_none_when_every_stock_is_off_list():
    """전부 후보 밖이면 매수할 근거가 남지 않으므로 그날은 스킵한다."""
    text = (
        '{"recommendations": [{"ticker": "999999", "name": "없는종목",'
        ' "target_price": 5000, "reason": "환각"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])

    assert _fake_recommender(response).recommend([stock(ticker="068270")]) is None


def test_drop_unknown_tickers_keeps_only_candidates():
    recommendations = [
        StockRecommendation("005930", "삼성전자", 70_000, "수급"),
        StockRecommendation("999999", "없는종목", 5_000, "환각"),
    ]

    kept = drop_unknown_tickers(recommendations, [stock(ticker="005930")])

    assert [r.ticker for r in kept] == ["005930"]


# ── 프롬프트 ────────────────────────────────────────────────
def test_build_user_prompt_includes_previous_day_data():
    prompt = build_user_prompt([stock(headlines=["신규 수주 공시"], volume_surge=3.42)])

    assert "005930" in prompt
    assert "삼성전자" in prompt
    assert "+1.50%" in prompt
    assert "70,000원" in prompt
    assert "평균대비 3.42배" in prompt
    assert "신규 수주 공시" in prompt


def test_build_user_prompt_includes_the_recent_price_band():
    """전일 하루만 보면 지금 가격이 최근 범위 어디인지 알 수 없다 (v6)."""
    prompt = build_user_prompt(
        [stock(recent_high=77000.0, recent_low=63000.0, moving_average=68000.0)]
    )

    assert "77,000" in prompt
    assert "63,000" in prompt
    assert "68,000" in prompt


def test_build_user_prompt_omits_the_recent_band_when_unavailable():
    """산출하지 못한 값을 0원으로 적으면 LLM이 그 숫자를 근거로 삼는다."""
    prompt = build_user_prompt([stock(recent_high=0.0, recent_low=0.0, moving_average=0.0)])

    assert "최근" not in prompt
    assert "이동평균" not in prompt


def test_build_system_prompt_explains_the_recent_band():
    prompt = build_system_prompt(target_count=3)

    assert "최근" in prompt
    assert "이동평균" in prompt


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


def test_build_system_prompt_states_both_day_basis():
    """전일 지표와 당일 지표를 함께 쓴다는 사실이 프롬프트에 드러나야 한다 (프롬프트 v9).

    2026-08-14 이전에는 '전일 데이터만' 쓴다고 알렸다 — 추천이 장 전이라 당일 지표가
    존재하지 않았기 때문이다 (PRD 10절 '개장 후 추천으로 이동').
    """
    prompt = build_system_prompt(target_count=3)

    assert "전일" in prompt
    assert "당일" in prompt
    assert "09:40" in prompt  # 미체결 취소 규칙을 알려야 목표가를 현실적으로 잡는다


# ── 목표 매도가 (참고용, v7) ─────────────────────────────────
def test_parse_reads_target_sell_price():
    raw = (
        '[{"ticker": "005930", "name": "삼성전자", "target_price": 70000,'
        ' "target_sell_price": 72000, "reason": "수급"}]'
    )
    assert parse_recommendations(raw)[0].target_sell_price == 72_000


def test_parse_defaults_sell_price_to_zero_when_missing():
    """참고용 값이므로 빠져 있어도 추천 자체를 버리지 않는다."""
    raw = '[{"ticker": "005930", "name": "삼성전자", "target_price": 70000, "reason": "수급"}]'
    assert parse_recommendations(raw)[0].target_sell_price == 0


def test_recommend_leaves_the_sell_target_untouched():
    """매수가는 ±5%로 잘려도 매도가는 손대지 않는다 — 보정하면 관찰 데이터가 오염된다."""
    text = (
        '{"recommendations": [{"ticker": "068270", "name": "셀트리온",'
        ' "target_price": 1800000, "target_sell_price": 195000, "reason": "수급"}]}'
    )
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    result = _fake_recommender(response).recommend([stock(ticker="068270", prev_close=180_000.0)])

    assert result[0].target_price == 189_000  # 가드레일이 잘랐다
    assert result[0].target_sell_price == 195_000  # 매도가는 LLM이 낸 값 그대로


def test_warn_invalid_sell_targets_logs_when_not_above_buy(caplog):
    recommendations = [StockRecommendation("005930", "삼성전자", 70_000, "수급", 69_000)]

    with caplog.at_level("WARNING"):
        warn_invalid_sell_targets(recommendations)

    assert "목표 매도가가 매수가보다 높지 않습니다" in caplog.text
    assert recommendations[0].target_sell_price == 69_000  # 경고만 하고 값은 그대로 둔다


def test_warn_invalid_sell_targets_ignores_missing_value(caplog):
    """0은 '산출 안 됨'이라 경고 대상이 아니다."""
    with caplog.at_level("WARNING"):
        warn_invalid_sell_targets([StockRecommendation("005930", "삼성전자", 70_000, "수급")])

    assert caplog.text == ""


def test_build_system_prompt_asks_for_a_sell_target():
    prompt = build_system_prompt(target_count=3)

    assert "목표 매도가" in prompt
    assert "target_sell_price" in prompt


# ── 당일 지표 (PRD 5.5-B '당일 지표 병행 수집', 프롬프트 v9) ─────────────────


def test_guardrail_uses_today_price_when_available():
    """가드레일 기준은 당일 현재가다 — 갭이 큰 날 정상적인 목표가가 잘리지 않게 한다."""
    recommendations = [StockRecommendation("005930", "삼성전자", 76_000, "테스트")]
    # 전일 종가 70,000 / 당일 현재가 77,000 → ±5% 밴드는 73,150 ~ 80,850
    apply_price_guardrail(
        recommendations, [stock(prev_close=70_000.0, today_price=77_000.0)]
    )

    assert recommendations[0].target_price == 76_000  # 전일 종가 기준이면 73,500으로 잘렸다


def test_guardrail_falls_back_to_prev_close_without_today_price():
    """당일 현재가를 모르면 전일 종가로 돌아간다."""
    recommendations = [StockRecommendation("005930", "삼성전자", 90_000, "테스트")]
    apply_price_guardrail(recommendations, [stock(prev_close=70_000.0, today_price=0.0)])

    assert recommendations[0].target_price == 73_500


def test_user_prompt_includes_today_metrics():
    """프롬프트에 당일 등락률·현재가·전일 변동폭이 실린다."""
    prompt = build_user_prompt(
        [
            stock(
                today_price=71_400.0,
                today_change_rate=2.0,
                today_volume=123_456,
                prev_range_pct=2.86,
            )
        ]
    )

    assert "당일 등락률 +2.00%" in prompt
    assert "현재가 71,400원" in prompt
    assert "당일 거래량 123,456" in prompt
    assert "전일 변동폭 2.86%" in prompt


def test_user_prompt_marks_missing_today_metrics():
    """당일 지표를 못 받은 종목은 '당일 지표 없음'으로 적는다 — 0을 근거로 삼지 않게 한다."""
    prompt = build_user_prompt([stock(today_price=0.0)])

    assert "당일 지표 없음" in prompt
    assert "당일 등락률" not in prompt


def test_user_prompt_drops_premarket_disclaimer():
    """'장 시작 전에는 당일 지표가 없다'는 전제는 더 이상 사실이 아니다."""
    prompt = build_user_prompt([stock(today_price=71_400.0)])

    assert "장 시작 전" not in prompt
