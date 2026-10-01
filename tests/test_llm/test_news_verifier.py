import json
from types import SimpleNamespace

from src.llm.news_verifier import (
    NewsVerdict,
    NewsVerifier,
    build_news_system_prompt,
    build_news_user_prompt,
    parse_news_verdict,
)


def test_parse_reads_all_three_fields():
    raw = '{"checked": true, "blocking": true, "reason": "실적 하향"}'
    v = parse_news_verdict("259960", raw)
    assert v == NewsVerdict(ticker="259960", checked=True, blocking=True, reason="실적 하향")


def test_parse_defaults_to_not_blocking_when_the_field_is_missing():
    """형식이 어긋나도 매수를 막는 쪽으로 기울지 않는다 — 기본은 통과다."""
    raw = '{"checked": true, "reason": "판단 불가"}'
    v = parse_news_verdict("259960", raw)
    assert v.blocking is False


def test_parse_treats_a_non_boolean_checked_as_unchecked():
    """max_uses에 걸려 확인을 못 했는데 '특이사항 없음'으로 답한 사례가 실측됐다."""
    raw = '{"checked": "maybe", "blocking": false, "reason": "검색 제한"}'
    v = parse_news_verdict("259960", raw)
    assert v.checked is False


def test_system_prompt_says_a_falling_price_is_not_bad_news():
    """전부 낙폭 종목이라, 이걸 적지 않으면 추천 전원이 탈락한다."""
    system = build_news_system_prompt()
    assert "주가가 떨어지고 있다는 것 자체는 악재가 아닙니다" in system


def test_system_prompt_forbids_answering_from_memory():
    """절대 규칙 2와 같은 함정 — 검색 없이 기억으로 답하면 안 된다."""
    system = build_news_system_prompt()
    assert "검색 결과에 근거하지 않은 내용" in system
    assert "checked" in system


def test_user_prompt_names_the_stock():
    prompt = build_news_user_prompt("259960", "크래프톤")
    assert "크래프톤" in prompt and "259960" in prompt


def _verifier(responder):
    """스트리밍 호출만 가짜로 바꾼 검증기."""
    v = NewsVerifier.__new__(NewsVerifier)
    v.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-sonnet-5")

    class _Stream:
        def __init__(self, text):
            self._text = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(())

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=self._text)],
            )

    def stream(**kwargs):
        return _Stream(responder(kwargs))

    v._client = SimpleNamespace(
        with_options=lambda **_: SimpleNamespace(messages=SimpleNamespace(stream=stream))
    )
    return v


def test_verify_returns_one_verdict_per_ticker():
    def responder(kwargs):
        text = kwargs["messages"][0]["content"]
        blocking = "크래프톤" in text
        return json.dumps({"checked": True, "blocking": blocking, "reason": "r"})

    verdicts = _verifier(responder).verify(
        [("259960", "크래프톤"), ("005930", "삼성전자")], timeout_seconds=60
    )

    assert verdicts["259960"].blocking is True
    assert verdicts["005930"].blocking is False


def test_verify_drops_a_ticker_whose_call_fails():
    """실패한 종목은 키 자체가 없다 — 호출측이 '판정 없음 = 통과'로 읽는다."""
    def responder(kwargs):
        if "크래프톤" in kwargs["messages"][0]["content"]:
            raise RuntimeError("API 실패")
        return json.dumps({"checked": True, "blocking": False, "reason": "r"})

    verdicts = _verifier(responder).verify(
        [("259960", "크래프톤"), ("005930", "삼성전자")], timeout_seconds=60
    )

    assert "259960" not in verdicts
    assert "005930" in verdicts


def test_verify_returns_empty_for_no_items():
    assert _verifier(lambda k: "{}").verify([], timeout_seconds=60) == {}
