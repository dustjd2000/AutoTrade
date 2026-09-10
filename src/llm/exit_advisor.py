import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.core.exit_trace import TracePoint
from src.llm.recommender import MAX_TOKENS, _extract_json

logger = logging.getLogger(__name__)

# 청산 판단 프롬프트 버전 — 추천 프롬프트(PROMPT_TEMPLATE_VERSION)와 따로 움직인다.
EXIT_PROMPT_TEMPLATE_VERSION = "v1"

# 응답 스키마 — sell/reason 둘뿐이라 recommender/reviewer와 달리 배열로 감싸지 않는다.
EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "sell": {"type": "boolean", "description": "지금 전량 매도할지 여부"},
        "reason": {"type": "string", "description": "판단 근거 — 궤적의 구체적 수치를 인용"},
    },
    "required": ["sell", "reason"],
    "additionalProperties": False,
}


@dataclass
class HoldingView:
    """청산 판단 시점의 보유 종목 스냅샷 — 평단·현재가와 아침에 본 시나리오를 함께 담는다."""

    ticker: str
    name: str
    quantity: int
    avg_price: float
    current_price: float
    net_return: float
    outlook: str
    reason: str
    # 오늘 DART 공시 제목 (최신순). `new_headlines`는 그중 장중에 새로 뜬 것으로,
    # 아침 추천 때는 없던 정보라 프롬프트에서 따로 표시한다 (PRD 5.5-B '장중 공시').
    headlines: List[str] = field(default_factory=list)
    new_headlines: List[str] = field(default_factory=list)


@dataclass
class ExitDecision:
    sell: bool
    reason: str


def _pct(ratio: float) -> str:
    """0~1 스케일 비율을 부호 있는 퍼센트 문자열로 바꾼다 (0.004 → "+0.40%")."""
    return f"{ratio * 100:+.2f}%"


def _headline_text(holding: HoldingView) -> str:
    """보유 종목 한 줄에 붙일 공시 문자열 — 장중에 새로 뜬 것에 `[신규]`를 붙인다.

    공시가 없는 것과 조회하지 못한 것을 구분하지 않는다 — 어느 쪽이든 AI가 쓸 정보가
    없다는 점은 같고, "조회 실패"를 적으면 AI가 그것을 악재 신호로 읽을 여지가 생긴다.
    """
    if not holding.headlines:
        return "없음"
    new = set(holding.new_headlines)
    return " / ".join(
        f"[신규] {title}" if title in new else title for title in holding.headlines
    )


def build_exit_system_prompt() -> str:
    return """당신은 한국 주식시장(코스피) 단기 매매의 장중 청산 여부를 판단하는 트레이더입니다.

## 역할
지금 보유 중인 종목 전체를 이 시점에 **전량** 정리할지 판단합니다. 종목별로 골라
파는 매도는 없습니다 — 판단은 항상 보유 목록 전체에 대해 한 번입니다.

## 기본은 보유입니다
확실한 근거가 없으면 팔지 않습니다. 이 판단은 15분 남짓한 주기로 반복해서 묻는
구조라, 매번 무언가 이유를 찾아 매도 쪽으로 기울기 쉽습니다. 그렇게 되면 이 판단
자체가 무의미해집니다. "이 정도면 팔아도 되지 않을까" 수준의 애매한 근거로는
`sell`을 `false`로 남기십시오.

## 손절선과 익절 기준선
- **손절선은 이 판단과 무관하게 코드가 실시간으로 자동 처리합니다.** 순손익률이
  손절선에 닿으면 이 판단이 실행되기 전에 이미 매도가 끝난 상태입니다. 손절선
  아래로 내려갈까 봐 미리 파는 것은 이 판단의 역할이 아니니 걱정하지 마십시오.
- **익절 기준선은 자동 청산 트리거가 아니라 사용자가 정한 목표치**입니다. 순손익률이
  그 위에 있다고 반드시 팔아야 하는 것도 아니고, 그 아래에 있다고 팔면 안 되는
  것도 아닙니다. 참고 지표일 뿐입니다.

## 시간
15:15가 되면 보유 종목 전체가 강제로 청산됩니다. 남은 시간이 짧을수록 지금 팔지
않아도 되는 이유(반등을 기다릴 시간)가 줄어든다는 뜻이므로, 남은 시간을 판단에
반영하십시오.

## 판단 기준
1. **되돌림** — 고점 대비 얼마나 밀렸는지, 그리고 그 되돌림이 얼마나 빠른 속도로
   진행되고 있는지를 궤적에서 읽으십시오.
2. **아침 전망의 유효성** — 이 종목을 고를 때 본 시나리오(`outlook`/`reason`)가
   지금도 살아 있는지, 아니면 이미 깨졌는지를 판단하십시오.
3. **장중 공시** — `[신규]`가 붙은 공시는 아침에 종목을 고를 때는 없던 정보입니다.
   그 내용이 아침 시나리오를 무너뜨리는지(유상증자·전환사채 발행 같은 지분 희석,
   횡령·배임, 실적 악화 등) 아니면 무관하거나 오히려 뒷받침하는지를 판단하십시오.
   **공시가 떴다는 사실만으로 팔지 마십시오** — 대형주에는 정기보고서처럼 주가와
   무관한 공시가 일상적으로 뜹니다. 다만 매도로 판단할 만한 악재 공시가 있다면 그것은
   궤적보다 우선하는 근거입니다. 되돌림이 아직 오지 않았어도 팔 수 있습니다.

## reason 작성 지침
`reason`에는 제공된 궤적의 **구체적 수치를 인용**하십시오. ("합산 순손익률이
09:20 +0.90%에서 09:35 +0.40%로 밀렸다"처럼.) "모멘텀이 약화되었다",
"분위기가 좋지 않다" 같은 모호한 표현은 금지합니다."""


def build_exit_user_prompt(
    holdings: List[HoldingView],
    trace: List[TracePoint],
    portfolio_return: float,
    stop_loss_ratio: float,
    take_profit_ratio: float,
    minutes_to_close: int,
    partial: bool,
) -> str:
    lines = [
        "보유 종목을 지금 전량 정리할지 판단하기 위한 현재 상황입니다.",
        "\n## 시간",
        f"- 15:15 강제청산까지 {minutes_to_close}분 남았습니다.",
        "\n## 기준선",
        f"- 손절선: {_pct(-stop_loss_ratio)} "
        f"(남은 거리 {(portfolio_return + stop_loss_ratio) * 100:.2f}%p — "
        "닿으면 코드가 자동으로 처리하므로 이 판단이 신경 쓸 필요는 없습니다)",
        f"- 익절 기준선: {_pct(take_profit_ratio)} (자동 청산 기준이 아니라 참고용 목표입니다)",
        f"- 현재 합산 순손익률: {_pct(portfolio_return)}",
    ]

    lines.append("\n## 순손익률 궤적 (시각 → 합산 / 종목별)")
    if partial:
        lines.append("궤적 일부 없음 — 엔진을 장중에 다시 켰습니다.")
    for point in trace:
        per_ticker = ", ".join(f"{t} {_pct(v)}" for t, v in point.per_ticker.items())
        lines.append(f"- {point.at:%H:%M} 합산 {_pct(point.portfolio_return)} ({per_ticker})")

    lines.append(f"\n## 보유 종목 ({len(holdings)}종목)")
    for h in holdings:
        lines.append(
            f"- {h.ticker} {h.name}: 평단 {h.avg_price:,.0f}원, 현재가 {h.current_price:,.0f}원, "
            f"수량 {h.quantity}주, 순손익률 {_pct(h.net_return)}"
        )
        lines.append(f"  아침 근거: {h.reason}")
        lines.append(f"  아침 전망: {h.outlook}")
        lines.append(f"  오늘 공시: {_headline_text(h)}")

    lines.append("\n지금 전량 매도할지 판단하세요.")
    return "\n".join(lines)


def parse_exit_decision(raw_text: str) -> ExitDecision:
    """청산 판단 응답을 파싱한다.

    `sell`이 없거나 형식이 어긋나면(불리언 `true`가 아니면) sell=False로 떨어진다 —
    기본은 보유이므로, 형식 오류가 매도 쪽으로 기울면 안 된다. JSON 자체가 깨졌거나
    최상위가 객체가 아니면 이 함수가 예외를 던지고, 호출측(ExitAdvisor.decide)이
    이를 None으로 받는다.
    """
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("LLM exit response must be a JSON object")
    return ExitDecision(
        sell=data.get("sell") is True,
        reason=str(data.get("reason", "")).strip(),
    )


class ExitAdvisor:
    """보유 종목을 지금 전량 정리할지 판단하는 LLM 모듈 (PRD 5.5-B 'AI 매도 판단').

    합산 순손익 기준 익절 자동 청산을 걷어낸 자리를 대신한다. 손절선은 이 모듈과
    무관하게 실시간 시세 콜백이 그대로 지킨다 — 여기서는 그 위쪽, "지금 팔아야 할
    만큼 되돌림이 왔는가"만 주기적으로 LLM에 묻는다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def decide(
        self,
        holdings: List[HoldingView],
        trace: List[TracePoint],
        portfolio_return: float,
        stop_loss_ratio: float,
        take_profit_ratio: float,
        minutes_to_close: int,
        partial: bool,
        timeout_seconds: float = 120.0,
    ) -> Optional[ExitDecision]:
        """청산 여부를 판단해 돌려준다. 실패하면 None — 호출측은 그 주기에 아무것도 팔지 않는다."""
        user_prompt = build_exit_user_prompt(
            holdings,
            trace,
            portfolio_return,
            stop_loss_ratio,
            take_profit_ratio,
            minutes_to_close,
            partial,
        )
        logger.info(
            "AI 매도 판단 요청 (exit_prompt_version=%s, 보유 %d종목):\n%s",
            EXIT_PROMPT_TEMPLATE_VERSION,
            len(holdings),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_exit_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                # "지금 팔까"는 깊은 추론이 필요한 질문이 아니고, 비용의 60~70%가 사고
                # 토큰이다 (스펙 10절) — effort를 낮춰 매 주기 호출 비용을 줄인다.
                output_config={
                    "format": {"type": "json_schema", "schema": EXIT_SCHEMA},
                    "effort": "low",
                },
            )
        except Exception:
            logger.exception("AI 매도 판단 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("AI 매도 판단 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error(
                "AI 매도 판단 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason
            )
            return None

        try:
            return parse_exit_decision(raw_text)
        except Exception:
            logger.exception("AI 매도 판단 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
