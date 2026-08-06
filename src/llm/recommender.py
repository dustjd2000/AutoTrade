import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.data.collector import DailyStockData

logger = logging.getLogger(__name__)

# 프롬프트 템플릿 버전 — 추천 근거를 나중에 추적할 수 있도록 코드로 버전 관리한다 (PRD 5.5-B).
PROMPT_TEMPLATE_VERSION = "v5"

# 목표 매수가가 전일 종가에서 이 비율을 벗어나면 경계로 자른다 (PRD 5.5-B '주문 방식').
# LLM이 자릿수를 틀리는 것을 막는 가드레일이며, 정상 범위의 판단에는 개입하지 않는다.
PRICE_GUARDRAIL_RATIO = 0.05

# KRX 호가 단위 (2023-01-25 개편, 코스피 기준). (가격 하한, 단위)를 내림차순으로 둔다.
# 각 하한은 바로 위 단위의 배수라, 단위로 내림해도 다른 구간으로 넘어가지 않는다.
TICK_SIZES = (
    (500_000, 1000),
    (200_000, 500),
    (50_000, 100),
    (20_000, 50),
    (5_000, 10),
    (2_000, 5),
    (0, 1),
)

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
                    "target_price": {
                        "type": "integer",
                        "description": "오늘 매수할 목표 가격 (원 단위 정수)",
                    },
                    "reason": {"type": "string", "description": "급등이 예상되는 근거"},
                },
                "required": ["ticker", "name", "target_price", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": [RECOMMENDATION_KEY],
    "additionalProperties": False,
}

def tick_size(price: float) -> int:
    """해당 가격대의 호가 단위 (원)."""
    for threshold, tick in TICK_SIZES:
        if price >= threshold:
            return tick
    return 1


def normalize_target_price(target_price: float, prev_close: float) -> int:
    """LLM이 제시한 목표 매수가를 주문 가능한 값으로 보정한다 (PRD 5.5-B '주문 방식').

    1. 전일 종가 대비 ±5%를 벗어나면 그 경계로 자른다 — 자릿수를 틀린 값만 막는 가드레일이다.
    2. 호가 단위로 내림한다 — 단위에 맞지 않는 가격은 주문이 거부된다. 내림(더 낮은 가격)으로
       맞추는 것은 매수에 불리하지 않은 방향이라 택했다.

    전일 종가를 모르면(0 이하) 가드레일 없이 호가 단위만 맞춘다.
    """
    price = target_price
    if prev_close > 0:
        price = min(
            max(price, prev_close * (1 - PRICE_GUARDRAIL_RATIO)),
            prev_close * (1 + PRICE_GUARDRAIL_RATIO),
        )
    tick = tick_size(price)
    return max(int(price // tick) * tick, tick)


def build_system_prompt(target_count: int) -> str:
    return f"""당신은 한국 주식시장(코스피) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
사용자가 제공하는 **전일(직전 거래일) 마감 데이터만을** 근거로, 오늘 장중 상대적으로 강한 상승
흐름을 보일 가능성이 높은 코스피 대형주 {target_count}종목을 선별하고, 각 종목을 오늘 매수할
**목표 매수가**를 제시합니다. 사용자가 제공하는 목록은 이미 전일 거래량 급증 배수 상위로 추려진
후보군입니다.

## 절대 규칙
1. 반드시 제공된 데이터에 있는 종목 중에서만 선택하십시오. 목록에 없는 종목을 추천하지 마십시오.
2. 당신의 학습 데이터에 있는 과거 정보나 기억(종목에 대한 일반적 평판 등)에 의존하지 마십시오. 오직
   사용자 메시지로 제공되는 전일 데이터만 근거로 삼으십시오.
3. 서로 다른 종목만 선택하십시오 (중복 불가).
4. 절대적인 확신이 없어도, 제공된 종목 중 상대적으로 가장 강한 신호를 보이는 종목 순으로 반드시
   {target_count}종목을 채우십시오. 후보 자체가 부족한 경우에만 더 적게 선정할 수 있습니다.
5. target_price는 **원 단위 정수**로, 해당 종목의 전일 종가 대비 ±5% 이내에서 제시하십시오.

## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
- 전일 거래량 급증 배수 — 그 이전 거래일들의 평균 거래량 대비 전일 거래량의 배수입니다. 평소보다
  뚜렷하게 많은 거래가 실린 종목(2배 이상)은 재료가 발생해 관심이 몰렸다는 뜻이므로 가장 무겁게
  보십시오. '판단불가'로 표시된 종목은 이 기준을 적용하지 말고 나머지 기준으로만 평가하십시오
- 전일 등락률의 방향과 크기 — 거래량이 함께 늘며 오른 종목이 다음 거래일까지 흐름을 잇는 경우가
  많습니다. 거래량만 터지고 크게 하락한 종목은 악재일 가능성을 함께 고려하십시오
- 전일 종가가 고가·저가 사이 어디에 위치하는지 — 고가 근처에서 마감했다면 매수세가 장 마감까지
  유지됐다는 뜻입니다
- 뉴스/공시 헤드라인의 구체성 (실적·수주·계약 등 구체적 재료인지, 단순 언급인지)

## 목표 매수가 작성 지침
오늘 09:00에 이 가격으로 지정가 매수 주문을 내고, **09:30까지 체결되지 않으면 그날 그 종목은
매수하지 않습니다.** 너무 낮게 잡으면 매수 자체가 무산되고, 너무 높게 잡으면 비싸게 사게 됩니다.
전일 종가와 고가·저가 범위를 근거로 오늘 실제 체결될 만한 가격을 제시하십시오.

## 근거 작성 지침
reason은 반드시 제공된 데이터의 구체적 수치를 인용해 작성하십시오.
("전일 등락률 +2.15%, 전일 거래량 320,450주(평균 대비 3.4배)"처럼 구체적으로.
"긍정적 모멘텀", "상승 여력" 같은 모호한 표현은 금지합니다.)"""


@dataclass
class StockRecommendation:
    ticker: str
    name: str
    target_price: int
    reason: str


def build_user_prompt(daily_data: List[DailyStockData], target_count: int = 3) -> str:
    lines = [
        "코스피 대형주의 **전일(직전 거래일) 마감 기준** 데이터입니다.",
        "장 시작 전에는 당일 지표(등락률·시가갭·거래량)가 아직 존재하지 않으므로 전일 데이터만 제공합니다.",
        "'평균대비'는 그 이전 거래일 평균 거래량 대비 전일 거래량의 배수입니다.",
        f"\n## 후보 ({len(daily_data)}종목)",
    ]
    for d in daily_data:
        headlines = "; ".join(d.headlines) if d.headlines else "없음"
        surge = f"{d.volume_surge:.2f}배" if d.volume_surge else "판단불가"
        lines.append(
            f"- {d.ticker} {d.name}: 전일 종가 {d.prev_close:,.0f}원"
            f"(고가 {d.prev_high:,.0f} / 저가 {d.prev_low:,.0f}), "
            f"전일 등락률 {d.prev_change_rate:+.2f}%, "
            f"전일 거래량 {d.prev_volume:,}(평균대비 {surge}), "
            f"뉴스/공시: {headlines}"
        )

    lines.append(
        f"\n위 데이터를 참고해 오늘 급등이 예상되는 종목 {target_count}개와 "
        "각 종목의 목표 매수가를 추천하세요."
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
            StockRecommendation(
                ticker=ticker,
                name=item["name"],
                # 스키마가 정수를 요구하지만 문자열로 오더라도 받아들인다
                target_price=int(float(item["target_price"])),
                reason=item["reason"],
            )
        )
    return recommendations


def apply_price_guardrail(
    recommendations: List[StockRecommendation], daily_data: List[DailyStockData]
) -> None:
    """목표 매수가를 주문 가능한 값으로 보정한다 (제자리 수정, PRD 5.5-B '주문 방식').

    보정으로 값이 바뀌면 로그에 남긴다 — LLM이 낸 값과 실제 주문가가 다르면 나중에 추천을
    되짚을 때 혼란스럽다.
    """
    prev_close = {data.ticker: data.prev_close for data in daily_data}
    for rec in recommendations:
        original = rec.target_price
        rec.target_price = normalize_target_price(original, prev_close.get(rec.ticker, 0.0))
        if rec.target_price != original:
            logger.info(
                "목표 매수가 보정: %s %s — %s원 → %s원 (전일 종가 %s원)",
                rec.ticker,
                rec.name,
                f"{original:,}",
                f"{rec.target_price:,}",
                f"{prev_close.get(rec.ticker, 0.0):,.0f}",
            )


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

        apply_price_guardrail(recommendations, daily_data)
        logger.info(
            "LLM recommended %d stock(s) (prompt_version=%s): %s",
            len(recommendations),
            PROMPT_TEMPLATE_VERSION,
            [f"{r.ticker}@{r.target_price:,}" for r in recommendations],
        )
        return recommendations
