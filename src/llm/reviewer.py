import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import MAX_TOKENS, _extract_json

logger = logging.getLogger(__name__)

# 평가 프롬프트 버전 — 추천 프롬프트(PROMPT_TEMPLATE_VERSION)와 따로 움직인다.
REVIEW_PROMPT_TEMPLATE_VERSION = "v1"

REVIEW_KEY = "reviews"
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        REVIEW_KEY: {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드"},
                    "review": {
                        "type": "string",
                        "description": "전망과 실제 움직임을 대조한 한두 문장 평가",
                    },
                },
                "required": ["ticker", "review"],
                "additionalProperties": False,
            },
        }
    },
    "required": [REVIEW_KEY],
    "additionalProperties": False,
}


@dataclass
class ReviewInput:
    """평가 한 건의 입력 — 아침의 전망과 그날의 실제 움직임 한 쌍."""

    ticker: str
    name: str
    outlook: str
    target_price: int
    target_sell_price: int
    recommend_price: float
    actual_high: float
    actual_low: float
    actual_close: float
    actual_change_rate: float


def build_review_system_prompt() -> str:
    return """당신은 한국 주식시장(코스피) 단기 매매의 사후 검증을 담당하는 분석가입니다.

## 역할
오늘 아침에 제시된 종목별 **전망**과, 장 마감 후 확정된 **실제 움직임**을 대조해
전망이 어디까지 맞았고 어디서 어긋났는지를 종목마다 한두 문장으로 적습니다.

## 규칙
1. 오직 사용자 메시지로 제공되는 수치만 근거로 삼으십시오. 학습 데이터의 기억(종목 평판,
   과거 주가, 뉴스)에 의존하지 마십시오.
2. 반드시 **제공된 수치를 인용**하십시오. ("이동평균 82,300원 회복을 예상했으나 고가가
   72,000원에 그쳤습니다"처럼) "대체로 맞았다", "아쉬웠다" 같은 모호한 표현은 금지합니다.
3. 잘 맞았으면 맞았다고, 빗나갔으면 빗나갔다고 그대로 적으십시오. 어느 쪽으로도 포장하지
   마십시오.
4. **다음 추천에 대한 조언이나 전략 제안을 하지 마십시오.** 오늘 무슨 일이 있었는지만
   적습니다.
5. 제공된 모든 종목에 대해 하나씩 적으십시오. 종목코드를 그대로 돌려주십시오."""


def build_review_user_prompt(items: List[ReviewInput]) -> str:
    lines = [
        "오늘 아침 추천한 종목의 전망과, 장 마감 후 확정된 실제 움직임입니다.",
        "'목표 매수가'는 아침에 지정가 매수를 시도한 가격이고, '목표 매도가'는 오늘 중",
        "닿을 것으로 봤던 가격입니다.",
        f"\n## 종목 ({len(items)}종목)",
    ]
    for it in items:
        lines.append(
            f"- {it.ticker} {it.name}: "
            f"추천 시각 현재가 {it.recommend_price:,.0f}원, "
            f"목표 매수가 {it.target_price:,}원, "
            f"목표 매도가 {it.target_sell_price:,}원 → "
            f"실제 고가 {it.actual_high:,.0f}원 / 저가 {it.actual_low:,.0f}원 / "
            f"종가 {it.actual_close:,.0f}원 ({it.actual_change_rate:+.2f}%)"
        )
        lines.append(f"  아침 전망: {it.outlook}")

    lines.append("\n각 종목의 전망이 실제와 어떻게 달랐는지 한두 문장으로 평가하세요.")
    return "\n".join(lines)


def parse_reviews(raw_text: str) -> Dict[str, str]:
    """평가 응답을 {종목코드: 평가문}으로 파싱한다.

    스키마상 최상위는 {"reviews": [...]}이지만 배열만 와도 받아들인다.
    종목코드가 없는 항목은 어느 종목의 평가인지 알 수 없으므로 버린다 — 예외를 던지면
    나머지 종목의 평가까지 잃는다.
    """
    data = json.loads(_extract_json(raw_text))
    if isinstance(data, dict):
        data = data.get(REVIEW_KEY, data)
    if not isinstance(data, list):
        raise ValueError("LLM review response must be a JSON array")

    reviews: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip()
        review = str(item.get("review", "")).strip()
        if ticker and review:
            reviews[ticker] = review
    return reviews


class LLMReviewer:
    """추천 사후 평가 모듈 — 아침 추천과 같은 모델을 쓰되 프롬프트와 스키마는 따로 둔다.

    `recommender.py`에 섞지 않은 이유는, 한 파일이 추천과 사후 평가를 함께 지면
    서로 다른 주기로 움직이는 두 프롬프트가 한 버전 상수를 나눠 쓰게 되기 때문이다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def review(
        self, items: List[ReviewInput], timeout_seconds: float = 120.0
    ) -> Optional[Dict[str, str]]:
        """평가를 받아 {종목코드: 평가문}으로 돌려준다. 실패하면 None — 검증 메일은 그대로 나간다."""
        if not items:
            return None
        user_prompt = build_review_user_prompt(items)
        logger.info(
            "LLM 검증 요청 (review_prompt_version=%s, %d종목):\n%s",
            REVIEW_PROMPT_TEMPLATE_VERSION,
            len(items),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_review_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            )
        except Exception:
            logger.exception("LLM 검증 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("LLM 검증 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("LLM 검증 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_reviews(raw_text)
        except Exception:
            logger.exception("LLM 검증 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
