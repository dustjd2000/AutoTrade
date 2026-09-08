from datetime import date
from types import SimpleNamespace

from src.llm.recommender import DEFAULT_PROMPT_SECTIONS
from src.llm.tuner import (
    MAX_SECTIONS_PER_CHANGE,
    PromptTuner,
    group_by_version,
    parse_tune_response,
    sanitize_sections,
)
from src.logger.trade_store import RecommendationRow


def row(version="v11", buy=True, sell=False, rate=1.0, ticker="005930"):
    return RecommendationRow(
        day=date(2026, 9, 4), ticker=ticker, name="종목", prompt_version=version,
        recommend_price=100.0, target_price=100, target_sell_price=110,
        setup="rebound", reason="근거", outlook="전망",
        actual_high=110.0, actual_low=90.0, actual_close=105.0, actual_change_rate=rate,
        buy_target_hit=buy, sell_target_hit=sell, review="평가",
    )


LONG = "## 오늘 전망 작성 지침\n" + ("가" * 80)


def test_group_by_version_counts_hits_and_average():
    stats = group_by_version([
        row("v11", buy=True, sell=False, rate=2.0),
        row("v11", buy=False, sell=False, rate=0.0),
        row("v12", buy=True, sell=True, rate=4.0),
    ])
    by = {s.version: s for s in stats}
    assert by["v11"].count == 2 and by["v11"].buy_hit == 1 and by["v11"].sell_hit == 0
    assert by["v11"].avg_change_rate == 1.0
    assert by["v12"].count == 1 and by["v12"].sell_hit == 1


def test_sanitize_keeps_a_valid_section():
    assert sanitize_sections({"outlook": LONG}) == {"outlook": LONG}


def test_sanitize_drops_unknown_and_locked_keys():
    assert sanitize_sections({"역할": LONG}) == {}
    assert sanitize_sections({"absolute_rules": LONG}) == {}


def test_sanitize_drops_short_or_blank_body():
    assert sanitize_sections({"outlook": "짧다"}) == {}
    assert sanitize_sections({"outlook": "   "}) == {}


def test_sanitize_drops_non_string_body():
    """모델이 문자열이 아닌 값을 줘도(스키마는 소프트 제약일 뿐) 예외 없이 걸러야 한다."""
    assert sanitize_sections({"outlook": 123}) == {}
    assert sanitize_sections({"outlook": ["가", "나"]}) == {}
    assert sanitize_sections({"outlook": {"key": "값"}}) == {}


def test_sanitize_discards_everything_when_over_the_limit():
    """걸러내기 전 원본 개수로 센다 — 잘못된 절을 섞어 제한을 우회할 수 없어야 한다."""
    raw = {"outlook": LONG, "reason": LONG, "역할": "x"}
    assert len(raw) > MAX_SECTIONS_PER_CHANGE
    assert sanitize_sections(raw) == {}


def test_sanitize_allows_exactly_the_limit():
    raw = {"outlook": LONG, "reason": LONG}
    assert set(sanitize_sections(raw)) == {"outlook", "reason"}


def test_parse_reads_change_and_sections():
    result = parse_tune_response(
        '{"change": true, "reason": "이유", "sections": {"outlook": "%s"}}' % LONG.replace("\n", "\\n")
    )
    assert result.change is True
    assert result.reason == "이유"
    assert "outlook" in result.sections


def test_parse_no_change():
    result = parse_tune_response('{"change": false, "reason": "표본이 얇다", "sections": {}}')
    assert result.change is False
    assert result.sections == {}


def test_tune_returns_none_when_api_raises():
    tuner = PromptTuner.__new__(PromptTuner)
    tuner.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    tuner._client = Boom()
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None


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


def _tuner_with_response(response):
    tuner = PromptTuner.__new__(PromptTuner)
    tuner.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")
    tuner._client = FakeClient(response)
    return tuner


def test_tune_returns_none_when_stop_reason_is_max_tokens():
    """사고 토큰에 예산을 다 쓰고 잘린 응답은 예외 없이 None으로 끝나야 한다."""
    tuner = _tuner_with_response(
        FakeResponse(stop_reason="max_tokens", content=[FakeBlock("아무 텍스트")])
    )
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None


def test_tune_returns_none_when_stop_reason_is_refusal():
    """모델이 응답을 거부한 경우도 예외 없이 None으로 끝나야 한다."""
    tuner = _tuner_with_response(FakeResponse(stop_reason="refusal", content=[]))
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None


def test_tune_returns_none_when_response_text_is_empty():
    """텍스트 블록이 공백뿐이면(또는 없으면) 예외 없이 None으로 끝나야 한다."""
    tuner = _tuner_with_response(
        FakeResponse(stop_reason="end_turn", content=[FakeBlock("   ")])
    )
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None


def test_tune_returns_none_when_response_text_is_malformed_json():
    """parse_tune_response가 던지는 예외가 tune() 밖으로 새어나가지 않고 None으로 끝나야 한다."""
    tuner = _tuner_with_response(
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock("이 자리에 답을 드릴 수 없습니다.")],
        )
    )
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None
