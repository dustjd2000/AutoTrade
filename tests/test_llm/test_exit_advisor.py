from datetime import datetime
from types import SimpleNamespace

from src.core.exit_trace import TracePoint
import pytest

from src.llm.exit_advisor import (
    ExitAdvisor,
    HoldingView,
    PositionExit,
    build_exit_system_prompt,
    build_exit_user_prompt,
    parse_exit_decision,
)


def holding(
    ticker="005930",
    net_return=0.004,
    headlines=None,
    new_headlines=None,
    target_sell_price=0.0,
    peak_return=None,
    current_price=36_100.0,
):
    return HoldingView(
        ticker=ticker, name="삼성전자", quantity=23, avg_price=35_850.0,
        current_price=current_price, net_return=net_return,
        outlook="오전 중 전일 고가 36,150원 돌파를 시도할 것으로 봅니다.",
        reason="이동평균 대비 -6.0%까지 밀린 상태",
        headlines=list(headlines or []),
        new_headlines=list(new_headlines or []),
        target_sell_price=target_sell_price,
        peak_return=peak_return,
    )


def trace_points():
    return [
        TracePoint(datetime(2026, 9, 9, 9, 20), 0.009, {"005930": 0.009}, {"005930": 36_300.0}),
        TracePoint(datetime(2026, 9, 9, 9, 35), 0.004, {"005930": 0.004}, {"005930": 36_100.0}),
    ]


def test_parse_reads_sell_and_reason():
    raw = '{"decisions": [{"ticker": "005930", "sell": true, "reason": "고점 대비 되돌림"}]}'
    result = parse_exit_decision(raw)
    assert len(result.decisions) == 1
    assert result.decisions[0].ticker == "005930"
    assert result.decisions[0].sell is True
    assert result.decisions[0].reason == "고점 대비 되돌림"


def test_parse_defaults_to_hold_when_sell_is_missing():
    """형식이 어긋나도 매도 쪽으로 기울지 않는다 — 기본은 보유다."""
    raw = '{"decisions": [{"ticker": "005930", "reason": "판단 불가"}]}'
    result = parse_exit_decision(raw)
    assert len(result.decisions) == 1
    assert result.decisions[0].sell is False


def test_user_prompt_carries_the_trace_and_the_lines():
    prompt = build_exit_user_prompt(
        [holding()], trace_points(),
        minutes_to_close=330, partial=False,
    )
    assert "09:20" in prompt and "09:35" in prompt      # 궤적
    assert "+0.90%" in prompt and "+0.40%" in prompt    # 경로가 숫자로 보인다
    assert "330" in prompt                               # 남은 시간
    assert "36,150원 돌파" in prompt                     # 아침 전망


def test_user_prompt_states_when_the_trace_is_partial():
    prompt = build_exit_user_prompt(
        [holding()], [],
        minutes_to_close=330, partial=True,
    )
    assert "궤적 일부 없음" in prompt


def test_system_prompt_makes_holding_the_default():
    prompt = build_exit_system_prompt()
    assert "기본은 보유" in prompt


def test_decide_returns_none_when_api_raises():
    advisor = ExitAdvisor.__new__(ExitAdvisor)
    advisor.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    advisor._client = Boom()
    assert advisor.decide([holding()], [], 330, False) is None


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
    assert advisor.decide([holding()], [], 330, False) is None


def test_decide_returns_none_when_stop_reason_is_refusal():
    """모델이 응답을 거부한 경우도 예외 없이 None으로 끝나야 한다."""
    advisor = _advisor_with_response(FakeResponse(stop_reason="refusal", content=[]))
    assert advisor.decide([holding()], [], 330, False) is None


def test_decide_returns_none_when_response_text_is_empty():
    """텍스트 블록이 공백뿐이면(또는 없으면) 예외 없이 None으로 끝나야 한다."""
    advisor = _advisor_with_response(
        FakeResponse(stop_reason="end_turn", content=[FakeBlock("   ")])
    )
    assert advisor.decide([holding()], [], 330, False) is None


def test_decide_returns_none_when_response_text_is_malformed_json():
    """parse_exit_decision이 던지는 예외가 decide() 밖으로 새어나가지 않고 None으로 끝나야 한다."""
    advisor = _advisor_with_response(
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock("이 자리에 답을 드릴 수 없습니다.")],
        )
    )
    assert advisor.decide([holding()], [], 330, False) is None


# ── 장중 공시 (PRD 5.5-B '장중 공시') ──────────────────────────
def test_prompt_marks_intraday_disclosures_as_new():
    """아침에 없던 공시는 `[신규]`로 구분해야 한다 — AI가 새 정보인지 알아야 한다."""
    view = holding(
        headlines=["유상증자 결정", "분기보고서"],
        new_headlines=["유상증자 결정"],
    )
    prompt = build_exit_user_prompt([view], trace_points(), 330, False)

    assert "오늘 공시: [신규] 유상증자 결정 / 분기보고서" in prompt


def test_prompt_says_none_when_there_is_no_disclosure():
    """조회 실패와 '공시 없음'을 구분하지 않는다 — 실패를 적으면 악재 신호로 읽힐 수 있다."""
    prompt = build_exit_user_prompt([holding()], trace_points(), 330, False)

    assert "오늘 공시: 없음" in prompt


def test_system_prompt_warns_against_selling_on_disclosure_alone():
    """대형주에는 정기보고서가 일상적으로 뜬다 — 공시 존재만으로 팔면 안 된다."""
    system = build_exit_system_prompt()

    assert "[신규]" in system
    assert "공시가 떴다는 사실만으로 팔지 마십시오" in system


# ── 목표 매도가 / 이익 반납 (PRD 5.5-B '이익 반납 감시') ────────
def test_prompt_says_when_the_sell_target_is_already_passed():
    """가격만 적어 두면 모델이 현재가와 대조하지 않는다 — 비교는 코드가 한다."""
    view = holding(target_sell_price=35_000.0, current_price=36_100.0)
    prompt = build_exit_user_prompt([view], trace_points(), 330, False)

    assert "아침 목표 매도가: 35,000원 — 현재가가 이미 +3.14% 넘어섰습니다" in prompt


def test_prompt_says_when_the_sell_target_is_not_reached():
    view = holding(target_sell_price=38_000.0, current_price=36_100.0)
    prompt = build_exit_user_prompt([view], trace_points(), 330, False)

    assert "아직 -5.00% 아래입니다" in prompt


def test_prompt_reports_the_giveback_as_a_number():
    """2026-09-10 현대중공업 궤적 — 고점 +4.46%에서 +1.41%면 3.05%p, 고점 이익의 68%다."""
    view = holding(net_return=0.0141, peak_return=0.0446)
    prompt = build_exit_user_prompt([view], trace_points(), 330, False)

    assert "되돌림: 당일 고점 +4.46% → 현재 +1.41% (3.05%p 반납, 고점 이익의 68%를 반납)" in prompt


def test_prompt_says_so_when_now_is_the_peak():
    view = holding(net_return=0.0446, peak_return=0.0446)
    prompt = build_exit_user_prompt([view], trace_points(), 330, False)

    assert "지금이 당일 고점입니다" in prompt


def test_system_prompt_makes_a_big_giveback_a_sell_reason():
    system = build_exit_system_prompt()

    assert "고점 이익의 절반 이상을 반납했다면 그것" in system
    assert "여전히 플러스" in system


def test_prompt_has_no_stop_loss_line():
    """손절선을 주면 AI가 '아직 여유가 있다'를 보유 근거로 쓴다 — 아예 주지 않는다.

    2026-09-09~18 로그에서 보유 유지 사유가 거의 전부 "손절선(-5.00%)까지 여유"였다.
    시스템 프롬프트가 신경 쓰지 말라고 했는데도 그랬다 (2026-09-18).
    """
    prompt = build_exit_user_prompt(
        holdings=[],
        trace=[],
        minutes_to_close=300,
        partial=False,
    )

    assert "손절선" not in prompt
    assert "남은 거리" not in prompt
    assert "익절" not in prompt


def test_system_prompt_has_no_baseline_section():
    prompt = build_exit_system_prompt()

    assert "손절선과 익절 기준선" not in prompt


# ── 종목별 판정 (2026-09-19) ──────────────────────────────
def test_parse_picks_only_the_sold_tickers():
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "고점 +2.1%에서 +0.9%로 절반 넘게 반납"},
        {"ticker": "000660", "sell": false, "reason": "아침 시나리오가 아직 유효"}
    ]}"""

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930", "000660"]) == ["005930"]


def test_parse_treats_missing_ticker_as_hold():
    """응답에 없는 보유 종목은 보유 — 누락이 매도 쪽으로 기울면 안 된다."""
    raw = '{"decisions": [{"ticker": "005930", "sell": true, "reason": "반납"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930", "000660"]) == ["005930"]


def test_parse_drops_unheld_ticker():
    """보유하지 않은 종목을 팔라고 해도 버린다 (환각 방어)."""
    raw = '{"decisions": [{"ticker": "999999", "sell": true, "reason": "환각"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930"]) == []


def test_parse_non_boolean_sell_is_hold():
    """sell이 불리언 true가 아니면 보유 — 기존 원칙 그대로."""
    raw = '{"decisions": [{"ticker": "005930", "sell": "yes", "reason": "애매"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930"]) == []


def test_parse_rejects_non_object():
    with pytest.raises(Exception):
        parse_exit_decision("[]")


def test_user_prompt_has_no_portfolio_aggregate():
    """합산을 주면 AI가 종목별 판단에 그 숫자를 쓴다 — 손절선을 뺀 것과 같은 이유다.

    2026-09-18에 손절선을 뺀 근거가 그대로 적용된다: 시스템 프롬프트가 신경 쓰지
    말라고 못박아도, 숫자가 주어지면 그것이 근거가 된다.
    """
    prompt = build_exit_user_prompt(
        holdings=[],
        trace=[],
        minutes_to_close=300,
        partial=False,
    )

    assert "합산" not in prompt


def test_system_prompt_asks_per_position_judgment():
    prompt = build_exit_system_prompt()

    assert "전량" not in prompt
    assert "종목마다" in prompt or "종목별" in prompt


def test_note_carries_per_ticker_reasons():
    """매도한 종목의 사유만 묶는다 — 청산 로그와 알림 메일이 이걸 싣는다."""
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "반납 절반 초과"},
        {"ticker": "000660", "sell": false, "reason": "유효"}
    ]}"""

    note = parse_exit_decision(raw).note(["005930", "000660"])

    assert "005930" in note
    assert "반납 절반 초과" in note
    assert "000660" not in note


def test_sell_tickers_dedupes_a_repeated_ticker():
    """스키마가 같은 종목코드의 중복 응답을 막지 않는다 — 한 번만 내야 한다."""
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "고점 반납"},
        {"ticker": "005930", "sell": true, "reason": "중복 판정"}
    ]}"""

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930"]) == ["005930"]


def test_note_dedupes_a_repeated_ticker():
    """note에도 같은 종목의 사유가 두 번 실리면 안 된다."""
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "고점 반납"},
        {"ticker": "005930", "sell": true, "reason": "중복 판정"}
    ]}"""

    note = parse_exit_decision(raw).note(["005930"])

    assert note == "005930: 고점 반납"
    assert note.count("005930") == 1
