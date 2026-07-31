import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.data.collector import DailyStockData

logger = logging.getLogger(__name__)

# 프롬프트 템플릿 버전 — 추천 근거를 나중에 추적할 수 있도록 코드로 버전 관리한다 (PRD 5.5-B).
PROMPT_TEMPLATE_VERSION = "v2"

# 응답 토큰 한도. 사고(thinking) 토큰과 본문이 이 한도를 함께 쓰므로 넉넉히 잡는다.
# 부족하면 사고에 예산을 다 쓰고 본문이 비거나 잘려 파싱이 실패한다.
# (비스트리밍 요청 권장 상한 — 이보다 크게 잡으면 HTTP 타임아웃 위험이 있다)
MAX_TOKENS = 16000

# 응답 스키마 — 형식을 프롬프트로 부탁하지 않고 구조화 출력으로 API가 보장하게 한다.
# 최상위를 객체로 감싼 것은 스키마 제약(모든 객체에 additionalProperties: false 필요) 때문이다.
RECOMMENDATION_KEY = "recommendations"
RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        RECOMMENDATION_KEY: {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드"},
                    "name": {"type": "string", "description": "종목명"},
                    "reason": {"type": "string", "description": "급등이 예상되는 근거"},
                },
                "required": ["ticker", "name", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": [RECOMMENDATION_KEY],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 한국 주식시장(코스피/코스닥) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
사용자가 제공하는 당일 데이터만을 근거로, 오늘 장중 상대적으로 강한 상승 흐름을 보일 가능성이 높은
코스피 대형주(시가총액 상위 100종목) 최대 3종목을 선별합니다.

## 절대 규칙
1. 반드시 제공된 데이터에 있는 종목 중에서만 선택하십시오. 목록에 없는 종목을 추천하지 마십시오.
2. 당신의 학습 데이터에 있는 과거 정보나 기억(종목에 대한 일반적 평판 등)에 의존하지 마십시오. 오직 아래
   제공된 당일 데이터만 근거로 삼으십시오.
3. 서로 다른 종목만 선택하십시오 (중복 불가).
4. 근거가 확실한 종목이 3개 미만이면 억지로 채우지 말고, 확신이 서는 종목만 그 수만큼(최소 1개) 추천하십시오.

## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
- 시가 갭의 방향성과 크기
- 전일 종가 대비 등락률
- 거래량 — 절대적인 평균 대비 급증 여부는 판단할 수 없으니, 함께 제공된 다른 종목과의 상대적 규모로만
  참고하십시오
- 뉴스/공시 헤드라인의 구체성 (실적·수주·계약 등 구체적 재료인지, 단순 언급인지)

## 근거 작성 지침
reason은 반드시 제공된 데이터의 구체적 수치를 인용해 작성하십시오.
("등락률 +2.15%, 시가갭 +1.8%"처럼 구체적으로. "긍정적 모멘텀", "상승 여력" 같은 모호한 표현은 금지합니다.)"""


@dataclass
class StockRecommendation:
    ticker: str
    name: str
    reason: str


def build_user_prompt(daily_data: List[DailyStockData]) -> str:
    is_premarket = any(d.is_premarket for d in daily_data)

    if is_premarket:
        lines = [
            "장 시작 전(동시호가) 시점의 코스피 대형주 데이터입니다.",
            "아래 수치는 정규장 체결이 아니라 **동시호가 예상체결가·예상체결량** 기준이며,",
            "'등락률'과 '시가갭'은 전일 종가 대비 예상체결가의 괴리를 뜻합니다.",
            "",
        ]
    else:
        lines = ["오늘의 코스피 대형주 종목별 데이터:"]

    for d in daily_data:
        headlines = "; ".join(d.headlines) if d.headlines else "없음"
        lines.append(
            f"- {d.ticker} {d.name}: 등락률 {d.change_rate:+.2f}%, "
            f"거래량 {d.volume:,}, 시가갭 {d.gap_rate:+.2f}%, 뉴스/공시: {headlines}"
        )
    lines.append("\n위 데이터를 참고해 급등 예상 종목을 최대 3개까지, 근거가 확실한 만큼만 JSON 배열로 추천하세요.")
    return "\n".join(lines)


def _extract_json(raw_text: str) -> str:
    """응답에서 JSON 본문만 꺼낸다.

    구조화 출력을 쓰면 본문은 순수 JSON이지만, 모델이나 프롬프트를 바꿨을 때
    마크다운 코드펜스(```json)나 앞뒤 설명이 붙어도 깨지지 않도록 방어한다.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()

    # 앞뒤에 산문이 섞였다면 첫 '{'/'[' 부터 마지막 '}'/']' 까지만 취한다
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos != -1]
    end = max(text.rfind("}"), text.rfind("]"))
    if starts and end > min(starts):
        text = text[min(starts) : end + 1]
    return text


def parse_recommendations(raw_text: str) -> List[StockRecommendation]:
    """LLM 응답을 구조화된 추천 목록으로 파싱한다.

    스키마상 최상위는 {"recommendations": [...]} 객체이지만, 배열만 온 경우도 받아들인다.
    형식을 벗어나면 예외를 던지며, 호출측(LLMRecommender.recommend)에서 이를 잡아
    "해당일 매수 스킵"으로 처리한다 (PRD 5.5-B — 무리하게 대체 로직으로 매수하지 않음).
    """
    data = json.loads(_extract_json(raw_text))
    if isinstance(data, dict):
        data = data.get(RECOMMENDATION_KEY, data)
    if not isinstance(data, list) or not data:
        raise ValueError("LLM response must be a non-empty JSON array")

    # 같은 종목이 두 번 추천되면 한 종목에 두 배로 투입되어 분산이 깨진다.
    # JSON 스키마로는 배열 원소의 유일성을 표현할 수 없으므로 여기서 걸러낸다.
    recommendations = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("LLM response item is not an object")
        ticker = item["ticker"]
        if ticker in seen:
            logger.warning("중복 추천 종목을 건너뜁니다: %s (%s)", ticker, item["name"])
            continue
        seen.add(ticker)
        recommendations.append(
            StockRecommendation(ticker=ticker, name=item["name"], reason=item["reason"])
        )
    return recommendations


class LLMRecommender:
    """1호 전략의 LLM 추천 모듈 — Anthropic Claude API 사용 (PRD 10절 확정, 2026-07-27)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def recommend(
        self, daily_data: List[DailyStockData], timeout_seconds: float = 120.0
    ) -> Optional[List[StockRecommendation]]:
        """LLM 호출 및 응답 파싱. 실패/타임아웃/형식 오류 시 None을 반환하고 해당일 매수는 스킵된다."""
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                temperature=0.3,  # 매수 판단이므로 일관성 우선 — 창의성 불필요
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(daily_data)}],
                # 응답 형식을 API가 스키마로 강제한다 (설명이 섞이거나 코드펜스가 붙는 것을 방지)
                output_config={
                    "format": {"type": "json_schema", "schema": RECOMMENDATION_SCHEMA}
                },
            )
        except Exception:
            logger.exception("LLM 호출이 실패했거나 타임아웃되었습니다.")
            return None

        # 사고 토큰이 예산을 다 쓰면 본문이 비거나 잘린 채로 온다 — 파싱 전에 걸러낸다
        if response.stop_reason == "max_tokens":
            logger.error(
                "LLM 응답이 max_tokens(%d)에 걸려 잘렸습니다. 사용량: %s",
                MAX_TOKENS,
                response.usage,
            )
            return None
        if response.stop_reason == "refusal":
            logger.error("LLM이 응답을 거부했습니다: %s", getattr(response, "stop_details", None))
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error(
                "LLM 응답에 텍스트가 없습니다. stop_reason=%s, 블록=%s, 사용량=%s",
                response.stop_reason,
                [getattr(block, "type", None) for block in response.content],
                response.usage,
            )
            return None

        try:
            recommendations = parse_recommendations(raw_text)
        except Exception:
            # 원문을 남기지 않으면 형식이 어떻게 어긋났는지 추적할 수 없다
            logger.exception("LLM 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None

        logger.info(
            "LLM recommended %d stock(s) (prompt_version=%s): %s",
            len(recommendations),
            PROMPT_TEMPLATE_VERSION,
            [r.ticker for r in recommendations],
        )
        return recommendations
