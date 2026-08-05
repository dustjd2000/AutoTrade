import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.data.collector import LARGE_CAP_LABEL, MID_CAP_LABEL, DailyStockData

logger = logging.getLogger(__name__)

# 프롬프트 템플릿 버전 — 추천 근거를 나중에 추적할 수 있도록 코드로 버전 관리한다 (PRD 5.5-B).
PROMPT_TEMPLATE_VERSION = "v4"

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

def cap_quota(target_count: int) -> tuple:
    """(대형주 수, 중형주 수) — 중형주 1종목을 섞고 나머지는 대형주로 채운다 (확정 2026-08-05).

    한 종목만 뽑는 설정에서는 중형주로 채우지 않는다. 그 한 자리까지 중형주로 가면
    당일 자금 전부가 상대적으로 얇은 종목 하나에 들어간다.
    """
    if target_count <= 1:
        return target_count, 0
    return target_count - 1, 1


def build_system_prompt(target_count: int) -> str:
    large_count, mid_count = cap_quota(target_count)
    quota_rule = (
        f"대형주에서 {large_count}종목, 중형주에서 {mid_count}종목을 선정하십시오. "
        "각 종목의 규모 분류는 사용자 데이터에 표시되어 있습니다."
        if mid_count
        else f"대형주에서 {large_count}종목을 선정하십시오."
    )
    return f"""당신은 한국 주식시장(코스피) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
사용자가 제공하는 당일 데이터만을 근거로, 오늘 장중 상대적으로 강한 상승 흐름을 보일 가능성이 높은
코스피 종목 {target_count}종목을 선별합니다. 사용자가 제공하는 목록은 이미 시가갭 상위로 추려진
후보군이며, 규모(대형주/중형주)별로 나뉘어 있습니다.

## 절대 규칙
1. 반드시 제공된 데이터에 있는 종목 중에서만 선택하십시오. 목록에 없는 종목을 추천하지 마십시오.
2. 당신의 학습 데이터에 있는 과거 정보나 기억(종목에 대한 일반적 평판 등)에 의존하지 마십시오. 오직
   사용자 메시지로 제공되는 당일 데이터만 근거로 삼으십시오.
3. 서로 다른 종목만 선택하십시오 (중복 불가).
4. **규모별 배분: {quota_rule}**
5. 절대적인 확신이 없어도, 제공된 종목 중 상대적으로 가장 강한 신호를 보이는 종목 순으로 반드시
   위 배분대로 선정하십시오. 해당 규모의 후보 자체가 부족한 경우에만 더 적게 선정할 수 있습니다.

## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
- 거래량 급증 배수 — 20일 평균 거래량 대비 오늘 거래량의 배수입니다. 평소보다 뚜렷하게 많은 거래가
  실린 종목(2배 이상)은 그만큼 관심이 몰렸다는 뜻이므로 가장 무겁게 보십시오.
  '판단불가'로 표시된 종목은 이 기준을 적용하지 말고 나머지 기준으로만 평가하십시오
- 시가 갭·등락률의 방향성과 크기 — 동시호가(장 시작 전) 데이터에서는 두 값이 전일 종가 대비 예상체결가
  괴리로 같은 계산식이므로 사실상 하나의 신호로 취급하십시오
- 뉴스/공시 헤드라인의 구체성 (실적·수주·계약 등 구체적 재료인지, 단순 언급인지)

## 근거 작성 지침
reason은 반드시 제공된 데이터의 구체적 수치를 인용해 작성하십시오.
("등락률 +2.15%, 거래량 320,450주(평균 대비 3.4배)"처럼 구체적으로.
"긍정적 모멘텀", "상승 여력" 같은 모호한 표현은 금지합니다.)"""


@dataclass
class StockRecommendation:
    ticker: str
    name: str
    reason: str


def build_user_prompt(daily_data: List[DailyStockData], target_count: int = 3) -> str:
    is_premarket = any(d.is_premarket for d in daily_data)

    if is_premarket:
        lines = [
            "장 시작 전(동시호가) 시점의 코스피 종목 데이터입니다.",
            "아래 수치는 정규장 체결이 아니라 **동시호가 예상체결가·예상체결량** 기준이며,",
            "'등락률'과 '시가갭'은 전일 종가 대비 예상체결가의 괴리를 뜻합니다.",
            "'평균대비'는 20일 평균 거래량 대비 오늘 거래량의 배수입니다.",
        ]
    else:
        lines = [
            "오늘의 코스피 종목별 데이터입니다.",
            "'평균대비'는 20일 평균 거래량 대비 오늘 거래량의 배수입니다.",
        ]

    # 규모별로 묶어서 보여준다 — 배분 규칙이 규모 기준이라 섞어 놓으면 세기 어렵다
    for tier in (LARGE_CAP_LABEL, MID_CAP_LABEL):
        in_tier = [d for d in daily_data if d.cap_tier == tier]
        if not in_tier:
            continue
        lines.append(f"\n## {tier} ({len(in_tier)}종목)")
        for d in in_tier:
            headlines = "; ".join(d.headlines) if d.headlines else "없음"
            surge = f"{d.volume_surge:.2f}배" if d.volume_surge else "판단불가"
            lines.append(
                f"- {d.ticker} {d.name}: 등락률 {d.change_rate:+.2f}%, "
                f"거래량 {d.volume:,}(평균대비 {surge}), 시가갭 {d.gap_rate:+.2f}%, "
                f"뉴스/공시: {headlines}"
            )

    large_count, mid_count = cap_quota(target_count)
    quota = (
        f"대형주 {large_count}종목 + 중형주 {mid_count}종목"
        if mid_count
        else f"대형주 {large_count}종목"
    )
    lines.append(
        f"\n위 데이터를 참고해 급등 예상 종목을 {quota}, 총 {target_count}개를 JSON 배열로 추천하세요."
    )
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
    시스템 프롬프트가 상대 비교로 항상 target_count개를 채우도록 지시하므로 빈 배열은
    전 종목 데이터가 무의미한 극단적 예외 상황에서만 나와야 정상이다 — 그래도 형식상
    유효한 응답이므로 예외를 던지지 않고 빈 리스트를 반환하며, 호출측(LLMRecommender.recommend)이
    이를 "해당일 매수 스킵"으로 처리한다. 리스트가 아닌 형식만 예외를 던진다.
    """
    data = json.loads(_extract_json(raw_text))
    if isinstance(data, dict):
        data = data.get(RECOMMENDATION_KEY, data)
    if not isinstance(data, list):
        raise ValueError("LLM response must be a JSON array")

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
        target_count = self.settings.target_stock_count
        user_prompt = build_user_prompt(daily_data, target_count)
        # 어떤 입력으로 그 추천이 나왔는지 남긴다 — 추천이 타당했는지 되짚을 유일한 근거다
        logger.info(
            "LLM 요청 (prompt_version=%s, 후보 %d종목):\n%s",
            PROMPT_TEMPLATE_VERSION,
            len(daily_data),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_system_prompt(target_count),
                messages=[{"role": "user", "content": user_prompt}],
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

        if not recommendations:
            # 형식은 정상이지만 확신 가는 종목이 없다는 결과 — 파싱 실패와는 구분해 남긴다
            logger.warning(
                "LLM recommended 0 stock(s) (prompt_version=%s) — no confident picks today.",
                PROMPT_TEMPLATE_VERSION,
            )
            return None

        logger.info(
            "LLM recommended %d stock(s) (prompt_version=%s): %s",
            len(recommendations),
            PROMPT_TEMPLATE_VERSION,
            [r.ticker for r in recommendations],
        )
        return recommendations
