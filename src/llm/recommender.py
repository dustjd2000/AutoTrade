import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.data.collector import DailyStockData

logger = logging.getLogger(__name__)

# 프롬프트 템플릿 버전 — 추천 근거를 나중에 추적할 수 있도록 코드로 버전 관리한다 (PRD 5.5-B).
PROMPT_TEMPLATE_VERSION = "v1"

SYSTEM_PROMPT = (
    "당신은 한국 주식시장(코스피/코스닥) 분석가입니다. "
    "주어진 코스피 대형주(시가총액 상위 100종목)의 당일 데이터를 바탕으로, "
    "오늘 급등이 예상되는 종목 3개를 선정하세요. "
    "반드시 아래 JSON 배열 형식으로만 답하고 다른 설명은 붙이지 마세요: "
    '[{"ticker": "종목코드", "name": "종목명", "reason": "추천 사유"}, ...]'
)


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
    lines.append("\n위 데이터를 참고해 급등 예상 종목 3개를 JSON 배열로 추천하세요.")
    return "\n".join(lines)


def parse_recommendations(raw_text: str) -> List[StockRecommendation]:
    """LLM 응답을 구조화된 추천 목록으로 파싱한다.

    형식을 벗어나면 예외를 던지며, 호출측(LLMRecommender.recommend)에서 이를 잡아
    "해당일 매수 스킵"으로 처리한다 (PRD 5.5-B — 무리하게 대체 로직으로 매수하지 않음).
    """
    data = json.loads(raw_text)
    if not isinstance(data, list) or not data:
        raise ValueError("LLM response must be a non-empty JSON array")

    recommendations = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("LLM response item is not an object")
        recommendations.append(
            StockRecommendation(ticker=item["ticker"], name=item["name"], reason=item["reason"])
        )
    return recommendations


class LLMRecommender:
    """1호 전략의 LLM 추천 모듈 — Anthropic Claude API 사용 (PRD 10절 확정, 2026-07-27)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def recommend(
        self, daily_data: List[DailyStockData], timeout_seconds: float = 60.0
    ) -> Optional[List[StockRecommendation]]:
        """LLM 호출 및 응답 파싱. 실패/타임아웃/형식 오류 시 None을 반환하고 해당일 매수는 스킵된다."""
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(daily_data)}],
            )
            raw_text = "".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            )
            recommendations = parse_recommendations(raw_text)
            logger.info(
                "LLM recommended %d stock(s) (prompt_version=%s): %s",
                len(recommendations),
                PROMPT_TEMPLATE_VERSION,
                [r.ticker for r in recommendations],
            )
            return recommendations
        except Exception:
            logger.exception("LLM recommendation failed or timed out.")
            return None
