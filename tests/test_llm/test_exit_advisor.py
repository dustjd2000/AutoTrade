from datetime import datetime
from types import SimpleNamespace

from src.core.exit_trace import TracePoint
from src.llm.exit_advisor import (
    ExitAdvisor,
    HoldingView,
    build_exit_system_prompt,
    build_exit_user_prompt,
    parse_exit_decision,
)


def holding(ticker="005930", net_return=0.004, headlines=None, new_headlines=None):
    return HoldingView(
        ticker=ticker, name="삼성전자", quantity=23, avg_price=35_850.0,
        current_price=36_100.0, net_return=net_return,
        outlook="오전 중 전일 고가 36,150원 돌파를 시도할 것으로 봅니다.",
        reason="이동평균 대비 -6.0%까지 밀린 상태",
        headlines=list(headlines or []),
        new_headlines=list(new_headlines or []),
    )


def trace_points():
    return [
        TracePoint(datetime(2026, 9, 9, 9, 20), 0.009, {"005930": 0.009}, {"005930": 36_300.0}),
        TracePoint(datetime(2026, 9, 9, 9, 35), 0.004, {"005930": 0.004}, {"005930": 36_100.0}),
    ]


def test_parse_reads_sell_and_reason():
    result = parse_exit_decision('{"sell": true, "reason": "고점 대비 되돌림"}')
    assert result.sell is True
    assert result.reason == "고점 대비 되돌림"


def test_parse_defaults_to_hold_when_sell_is_missing():
    """형식이 어긋나도 매도 쪽으로 기울지 않는다 — 기본은 보유다."""
    result = parse_exit_decision('{"reason": "판단 불가"}')
    assert result.sell is False


def test_user_prompt_carries_the_trace_and_the_lines():
    prompt = build_exit_user_prompt(
        [holding()], trace_points(), portfolio_return=0.004,
        stop_loss_ratio=0.02, take_profit_ratio=0.005,
        minutes_to_close=330, partial=False,
    )
    assert "09:20" in prompt and "09:35" in prompt      # 궤적
    assert "+0.90%" in prompt and "+0.40%" in prompt    # 경로가 숫자로 보인다
    assert "-2.00%" in prompt                            # 손절선
    assert "+0.50%" in prompt                            # 익절 기준선
    assert "330" in prompt                               # 남은 시간
    assert "36,150원 돌파" in prompt                     # 아침 전망


def test_user_prompt_states_when_the_trace_is_partial():
    prompt = build_exit_user_prompt(
        [holding()], [], portfolio_return=0.004,
        stop_loss_ratio=0.02, take_profit_ratio=0.005,
        minutes_to_close=330, partial=True,
    )
    assert "궤적 일부 없음" in prompt


def test_system_prompt_makes_holding_the_default():
    prompt = build_exit_system_prompt()
    assert "기본은 보유" in prompt
    assert "손절" in prompt


def test_decide_returns_none_when_api_raises():
    advisor = ExitAdvisor.__new__(ExitAdvisor)
    advisor.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    advisor._client = Boom()
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None


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


def _advisor_with_response(response):
    advisor = ExitAdvisor.__new__(ExitAdvisor)
    advisor.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")
    advisor._client = FakeClient(response)
    return advisor


def test_decide_returns_none_when_stop_reason_is_max_tokens():
    """사고 토큰에 예산을 다 쓰고 잘린 응답은 예외 없이 None으로 끝나야 한다."""
    advisor = _advisor_with_response(
        FakeResponse(stop_reason="max_tokens", content=[FakeBlock("아무 텍스트")])
    )
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None


def test_decide_returns_none_when_stop_reason_is_refusal():
    """모델이 응답을 거부한 경우도 예외 없이 None으로 끝나야 한다."""
    advisor = _advisor_with_response(FakeResponse(stop_reason="refusal", content=[]))
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None


def test_decide_returns_none_when_response_text_is_empty():
    """텍스트 블록이 공백뿐이면(또는 없으면) 예외 없이 None으로 끝나야 한다."""
    advisor = _advisor_with_response(
        FakeResponse(stop_reason="end_turn", content=[FakeBlock("   ")])
    )
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None


def test_decide_returns_none_when_response_text_is_malformed_json():
    """parse_exit_decision이 던지는 예외가 decide() 밖으로 새어나가지 않고 None으로 끝나야 한다."""
    advisor = _advisor_with_response(
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock("이 자리에 답을 드릴 수 없습니다.")],
        )
    )
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None


# ── 장중 공시 (PRD 5.5-B '장중 공시') ──────────────────────────
def test_prompt_marks_intraday_disclosures_as_new():
    """아침에 없던 공시는 `[신규]`로 구분해야 한다 — AI가 새 정보인지 알아야 한다."""
    view = holding(
        headlines=["유상증자 결정", "분기보고서"],
        new_headlines=["유상증자 결정"],
    )
    prompt = build_exit_user_prompt([view], trace_points(), 0.004, 0.02, 0.005, 330, False)

    assert "오늘 공시: [신규] 유상증자 결정 / 분기보고서" in prompt


def test_prompt_says_none_when_there_is_no_disclosure():
    """조회 실패와 '공시 없음'을 구분하지 않는다 — 실패를 적으면 악재 신호로 읽힐 수 있다."""
    prompt = build_exit_user_prompt([holding()], trace_points(), 0.004, 0.02, 0.005, 330, False)

    assert "오늘 공시: 없음" in prompt


def test_system_prompt_warns_against_selling_on_disclosure_alone():
    """대형주에는 정기보고서가 일상적으로 뜬다 — 공시 존재만으로 팔면 안 된다."""
    system = build_exit_system_prompt()

    assert "[신규]" in system
    assert "공시가 떴다는 사실만으로 팔지 마십시오" in system
