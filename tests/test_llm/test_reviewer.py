from types import SimpleNamespace

from src.llm.reviewer import (
    LLMReviewer,
    ReviewInput,
    build_review_system_prompt,
    build_review_user_prompt,
    parse_reviews,
)


def item(ticker="005930", **kwargs):
    defaults = dict(
        name="삼성전자",
        outlook="오전 중 이동평균 82,300원 회복 시도",
        target_price=70_000,
        target_sell_price=71_400,
        recommend_price=70_500.0,
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
    )
    defaults.update(kwargs)
    return ReviewInput(ticker=ticker, **defaults)


def test_parse_reviews_maps_by_ticker():
    raw = '{"reviews": [{"ticker": "005930", "review": "오전 회복 시도는 맞았습니다."}]}'
    assert parse_reviews(raw) == {"005930": "오전 회복 시도는 맞았습니다."}


def test_parse_reviews_accepts_bare_array():
    raw = '[{"ticker": "005930", "review": "맞았습니다."}]'
    assert parse_reviews(raw) == {"005930": "맞았습니다."}


def test_parse_reviews_skips_items_without_ticker():
    """어느 종목의 평가인지 모르는 항목 하나가 나머지 평가를 잃게 하면 안 된다."""
    raw = '{"reviews": [{"review": "종목이 없다"}, {"ticker": "005930", "review": "ok"}]}'
    assert parse_reviews(raw) == {"005930": "ok"}


def test_user_prompt_includes_actual_numbers_and_outlook():
    prompt = build_review_user_prompt([item()])
    assert "005930" in prompt
    assert "72,000" in prompt                        # 실제 고가
    assert "이동평균 82,300원 회복 시도" in prompt      # 전망 원문


def test_system_prompt_forbids_advice():
    assert "조언" in build_review_system_prompt()


def test_review_returns_none_when_api_raises():
    reviewer = LLMReviewer.__new__(LLMReviewer)
    reviewer.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    reviewer._client = Boom()
    assert reviewer.review([item()]) is None


def test_review_returns_none_for_empty_items():
    reviewer = LLMReviewer.__new__(LLMReviewer)
    reviewer.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")
    reviewer._client = None
    assert reviewer.review([]) is None
