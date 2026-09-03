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


class FakeBlock:
    """`response.content`의 텍스트 블록 하나를 흉내낸다."""

    def __init__(self, text, type_="text"):
        self.type = type_
        self.text = text


class FakeResponse:
    """`messages.create(...)`가 돌려주는 응답 객체를 흉내낸다."""

    def __init__(self, stop_reason="end_turn", content=None):
        self.stop_reason = stop_reason
        self.content = content if content is not None else []


class FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class FakeClient:
    """`self._client.with_options(...).messages.create(...)` 경로를 흉내내며 고정 응답을 돌려준다."""

    def __init__(self, response):
        self.messages = FakeMessages(response)

    def with_options(self, **kwargs):
        return self


def _reviewer_with_response(response):
    reviewer = LLMReviewer.__new__(LLMReviewer)
    reviewer.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")
    reviewer._client = FakeClient(response)
    return reviewer


def test_review_returns_none_when_stop_reason_is_max_tokens():
    """사고 토큰에 예산을 다 쓰고 잘린 응답은 예외 없이 None으로 끝나야 한다."""
    reviewer = _reviewer_with_response(
        FakeResponse(stop_reason="max_tokens", content=[FakeBlock("아무 텍스트")])
    )
    assert reviewer.review([item()]) is None


def test_review_returns_none_when_stop_reason_is_refusal():
    """모델이 응답을 거부한 경우도 예외 없이 None으로 끝나야 한다."""
    reviewer = _reviewer_with_response(FakeResponse(stop_reason="refusal", content=[]))
    assert reviewer.review([item()]) is None


def test_review_returns_none_when_response_text_is_empty():
    """텍스트 블록이 공백뿐이면(또는 없으면) 예외 없이 None으로 끝나야 한다."""
    reviewer = _reviewer_with_response(
        FakeResponse(stop_reason="end_turn", content=[FakeBlock("   ")])
    )
    assert reviewer.review([item()]) is None


def test_review_returns_none_when_response_text_is_malformed_json():
    """parse_reviews가 던지는 예외가 review() 밖으로 새어나가지 않고 None으로 끝나야 한다."""
    reviewer = _reviewer_with_response(
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock("이 자리에 답을 드릴 수 없습니다.")],
        )
    )
    assert reviewer.review([item()]) is None
