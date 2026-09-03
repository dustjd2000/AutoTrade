import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.data.collector import DailyStockData

logger = logging.getLogger(__name__)

# 프롬프트 템플릿 버전 — 추천 근거를 나중에 추적할 수 있도록 코드로 버전 관리한다 (PRD 5.5-B).
PROMPT_TEMPLATE_VERSION = "v11"

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

# 추천 유형 — LLM이 각 종목을 어떤 셋업으로 보고 골랐는지 스스로 밝히게 한다 (PRD 5.5-B '추천 유형 제한').
# 프롬프트로 유형을 제한하는 것만으로는 소프트 제약이라, 이 값으로 코드가 한 번 더 잘라낸다.
# 유형을 하나만 열거하면 전부 그 값으로 적어 내므로, 고르지 않을 유형까지 함께 둔다.
SETUP_REBOUND = "rebound"
SETUP_TYPES = (SETUP_REBOUND, "breakout", "momentum")

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
                    "target_sell_price": {
                        "type": "integer",
                        "description": "오늘 장중 도달할 것으로 보는 매도 목표 가격 (원 단위 정수)",
                    },
                    "reason": {"type": "string", "description": "급등이 예상되는 근거"},
                    "setup": {
                        "type": "string",
                        "enum": list(SETUP_TYPES),
                        "description": "이 종목을 고른 셋업 유형",
                    },
                    "outlook": {
                        "type": "string",
                        "description": "오늘 남은 장중 주가 움직임 전망",
                    },
                },
                "required": [
                    "ticker",
                    "name",
                    "target_price",
                    "target_sell_price",
                    "reason",
                    "setup",
                    "outlook",
                ],
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


def normalize_target_price(target_price: float, reference_price: float) -> int:
    """LLM이 제시한 목표 매수가를 주문 가능한 값으로 보정한다 (PRD 5.5-B '주문 방식').

    1. 기준가 대비 ±5%를 벗어나면 그 경계로 자른다 — 자릿수를 틀린 값만 막는 가드레일이다.
    2. 호가 단위로 내림한다 — 단위에 맞지 않는 가격은 주문이 거부된다. 내림(더 낮은 가격)으로
       맞추는 것은 매수에 불리하지 않은 방향이라 택했다.

    기준가는 **당일 현재가**다 (변경 2026-08-14). 프롬프트가 당일 가격을 보고 목표가를 내라고
    요구하므로 가드레일도 같은 기준이어야 한다 — 전일 종가로 두면 갭이 큰 날 정상적인 목표가가
    잘려나간다. 기준가를 모르면(0 이하) 가드레일 없이 호가 단위만 맞춘다.
    """
    price = target_price
    if reference_price > 0:
        price = min(
            max(price, reference_price * (1 - PRICE_GUARDRAIL_RATIO)),
            reference_price * (1 + PRICE_GUARDRAIL_RATIO),
        )
    tick = tick_size(price)
    return max(int(price // tick) * tick, tick)


def build_system_prompt(target_count: int) -> str:
    return f"""당신은 한국 주식시장(코스피) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
사용자가 제공하는 **전일 마감 데이터와 당일 장중 데이터를** 근거로, **최근 낙폭을 되돌리는 구간에
있는** 코스피 대형주 {target_count}종목을 선별하고, 각 종목의 **목표 매수가**와 **목표 매도가**를
제시합니다. 사용자가 제공하는 목록은 이미 전일 거래량 급증 배수 상위로 추려진 후보군입니다.

## 절대 규칙
1. 반드시 제공된 데이터에 있는 종목 중에서만 선택하십시오. 목록에 없는 종목을 추천하지 마십시오.
2. 당신의 학습 데이터에 있는 과거 정보나 기억(종목에 대한 일반적 평판 등)에 의존하지 마십시오. 오직
   사용자 메시지로 제공되는 데이터만 근거로 삼으십시오.
3. 서로 다른 종목만 선택하십시오 (중복 불가).
4. **아래 '추천 유형'의 `rebound`에 해당하는 종목만 선택하십시오.** 다른 신호가 아무리 강해도
   `rebound`가 아닌 종목은 추천하지 마십시오.
5. 절대적인 확신이 없어도, `rebound` 종목 중 상대적으로 가장 강한 신호를 보이는 종목 순으로
   {target_count}종목을 채우십시오. `rebound` 종목 자체가 그보다 적으면 더 적게 선정하고, 하나도
   없으면 빈 배열을 반환하십시오 — 유형을 바꿔 적어 정원을 채우지 마십시오.
6. target_price는 **원 단위 정수**로, 해당 종목의 **당일 현재가** 대비 ±5% 이내에서 제시하십시오.
   당일 지표가 없는 종목만 전일 종가를 기준으로 삼으십시오.
7. target_sell_price는 **원 단위 정수**로, 반드시 target_price보다 높아야 합니다.

## 추천 유형 (setup)
각 종목에 아래 셋 중 하나를 `setup`으로 붙이십시오. 실제로 보이는 대로 붙이고, 추천하고 싶은
종목에 맞춰 유형을 바꿔 적지 마십시오.
- `rebound` — **낙폭을 되돌리는 구간**입니다. 최근 가격대에서 크게 밀려 있고(전일 종가나 현재가가
  이동평균 아래이거나 최근 저가에 가깝습니다), 오늘 그 낙폭을 되돌리며 올라오고 있는 종목입니다.
- `breakout` — 최근 고가를 넘어섰거나 그 부근까지 올라온 돌파 구간의 종목입니다.
- `momentum` — 위 둘 어디에도 해당하지 않고, 전일 강세와 거래량만으로 흐름이 이어지는 종목입니다.

**이번 요청에서 추천할 종목은 `rebound`뿐입니다.** 나머지 두 유형은 유형 판정을 정직하게 하기
위한 선택지이며, 그렇게 판단한 종목은 목록에서 빼십시오.

## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
- **최근 가격대에서의 위치 — 낙폭이 얼마나 큰 상태인지**가 이번 선별의 전제입니다. '최근 고가/저가'는
  당일을 제외한 최근 20거래일 이내의 최고가·최저가이고, '이동평균'은 같은 구간의 종가 평균입니다.
  이동평균 아래로 내려와 있거나 최근 저가에 가까울수록 낙폭이 큰 상태이며, 이미 최근 고가를
  넘어선 종목은 이 전략의 대상이 아닙니다
- **당일 등락률과 현재가 — 그 낙폭을 오늘 실제로 되돌리고 있는지**를 봅니다. 낙폭이 컸더라도 오늘
  힘이 없는 종목보다, 오늘 상승으로 돌아선 종목이 앞섭니다. '당일 지표 없음'으로 표시된 종목은
  이 기준을 적용하지 말고 나머지 기준으로만 평가하십시오
- 전일 거래량 급증 배수 — 그 이전 거래일들의 평균 거래량 대비 전일 거래량의 배수입니다. 평소보다
  뚜렷하게 많은 거래가 실린 종목(2배 이상)은 재료가 발생해 관심이 몰렸다는 뜻이므로 되돌림의
  힘을 재는 근거로 무겁게 보십시오. '판단불가'로 표시된 종목은 이 기준을 적용하지 말고 나머지 기준으로만 평가하십시오
- 전일 등락률의 방향과 크기 — 거래량이 함께 늘며 오른 종목이 다음 거래일까지 흐름을 잇는 경우가
  많습니다. 거래량만 터지고 크게 하락한 종목은 악재일 가능성을 함께 고려하십시오
- 전일 종가가 고가·저가 사이 어디에 위치하는지 — 고가 근처에서 마감했다면 매수세가 장 마감까지
  유지됐다는 뜻입니다
- 공시(DART 전자공시) 제목의 구체성 — 공급계약·실적처럼 주가를 움직일 재료인지, 정기보고서나
  사무적 신고처럼 주가와 무관한 공시인지 구분하십시오. 유상증자·사채 발행·횡령·배임 같은 명백한
  악재 공시가 뜬 종목은 이미 후보에서 제외돼 목록에 없으므로 따로 걸러낼 필요가 없습니다
- 전일 변동폭 — 하루 안에 얼마나 흔들렸는지를 나타냅니다. 큰 변동폭은 그만큼 움직임이 큰 종목이라는
  뜻이며, 상방과 하방 어느 쪽으로도 벌어질 수 있습니다

## 목표 매수가 작성 지침
오늘 09:08에 이 가격으로 지정가 매수 주문을 내고, **10:10까지 체결되지 않으면 그날 그 종목은
매수하지 않습니다.** 너무 낮게 잡으면 매수 자체가 무산되고, 너무 높게 잡으면 비싸게 사게 됩니다.
**당일 현재가를 기준으로** 전일 종가·고가·저가와 최근 가격대(최근 고가/저가, 이동평균)를 함께 보고,
오늘 남은 장중에 실제 체결될 만한 가격을 제시하십시오. 현재가에서 크게 떨어진 값을 적으면 체결되지
않고, 현재가보다 훨씬 높은 값은 비싸게 사는 것입니다. 현재가가 최근 고가에 바짝 붙어 있다면 그
가격을 그대로 좇기보다 눌림을 기다리는 편이 유리하고, 이동평균 아래로 내려온 종목이라면 이동평균을
회복 목표로 참고하십시오.

## 목표 매도가 작성 지침
target_sell_price는 목표 매수가에 매수했다고 가정하고, **오늘 장중에 실제로 닿을 것으로 보는
매도 목표가**입니다. 전일 고가와 최근 고가가 저항으로 작용하는지, 이동평균에서 얼마나 떨어져
있는지를 근거로 삼으십시오. 하루 안에 닿지 못할 가격을 적지 말고, 오늘의 현실적인 상단을
제시하십시오.

## 오늘 전망 작성 지침
outlook은 **오늘 남은 장중에 이 종목이 어떻게 움직일 것으로 보는지**를 두 문장으로 적는
칸입니다. reason이 "왜 골랐는가"(과거)라면 outlook은 "앞으로 어떻게 될 것인가"(미래)입니다.
reason에 쓴 내용을 말만 바꿔 다시 쓰지 마십시오.

- **첫 문장 — 오전 흐름.** 당일 현재가·당일 등락률·당일 거래량을 근거로, 오전 중 어디까지
  시도할 것으로 보는지 적으십시오. '당일 지표 없음'인 종목은 이 문장을 생략하십시오.
- **둘째 문장 — 조건부 분기.** 상방과 하방을 **가격과 함께** 조건부로 적으십시오.
  ("~를 회복하면 ~까지 열려 있고, 회복하지 못하면 ~ 부근까지 되밀릴 수 있습니다")

금지 사항:
- 오후의 움직임을 단정하지 마십시오. 제공된 것은 일봉과 현재 시각의 현재가뿐이며, 장중
  시간대별 흐름을 알 수 있는 데이터는 없습니다.
- 학습 데이터의 기억(종목 평판, 과거 주가)에 의존하지 마십시오 — 절대 규칙 2와 같습니다.

예: "현재 +1.80%로 갭 상승 출발해 당일 거래량이 실려 있어 오전 중 이동평균 82,300원
회복을 시도할 것으로 봅니다. 회복하면 전일 고가 84,000원까지 열려 있고, 회복하지 못하면
최근 저가 77,000원 부근까지 되밀릴 수 있습니다."

## 근거 작성 지침
reason은 반드시 제공된 데이터의 구체적 수치를 인용해 작성하십시오.
("전일 등락률 +2.15%, 전일 거래량 320,450주(평균 대비 3.4배)"처럼 구체적으로.
"긍정적 모멘텀", "상승 여력" 같은 모호한 표현은 금지합니다.)
낙폭이 어디까지 밀렸고 오늘 얼마나 되돌렸는지를 반드시 함께 적으십시오
("이동평균 82,300원 대비 -6.1%인 77,300원까지 밀렸다가 오늘 +1.80%"처럼)."""


@dataclass
class StockRecommendation:
    ticker: str
    name: str
    target_price: int
    reason: str
    # 목표 매도가 — 추천 메일에 참고로 싣기만 하고 주문에는 쓰지 않는다 (PRD 5.5-B '목표 매도가').
    # 0은 '산출 안 됨'이며(거래량 급증 배수와 같은 규약), 그 경우 메일에서 줄이 통째로 빠진다.
    target_sell_price: int = 0
    # 추천 시각(09:05)의 현재가 — 09:08 갭 하락 판정의 기준값이다 (PRD 5.5-B '갭 하락한
    # 종목도 건너뛴다', 기준값 변경 2026-08-14). 전일 종가 대비 판정은 09:05 후보 선정이
    # 이미 맡았고, 여기서는 '추천한 뒤 무너진 종목'을 잡는다. 0은 '모름'이며 판정을 건너뛴다.
    #
    # 전일 종가는 여기까지 실어 나르지 않는다 (2026-08-14) — 목표가 가드레일은 추천 산출
    # 시점에 `DailyStockData`를 직접 보고 끝나므로, 09:08까지 넘길 이유가 없다.
    recommend_price: float = 0.0
    # LLM이 스스로 밝힌 셋업 유형 (PRD 5.5-B '추천 유형 제한'). `SETUP_REBOUND`가 아니면
    # `drop_other_setups`가 주문 경로에 들어가기 전에 잘라낸다. ""는 '밝히지 않음'이며
    # 마찬가지로 잘린다 — 유형을 확인하지 못한 추천을 통과시키면 필터가 조용히 꺼진다.
    setup: str = ""
    # LLM이 본 오늘의 움직임 전망 (PRD 5.5-B '오늘 전망'). 추천 메일 표시와 15:35 검증에만
    # 쓰고 주문에는 쓰지 않는다 — `target_sell_price`와 같은 방침이다. ""는 '산출 안 됨'이며,
    # 그 경우 메일에서 줄이 통째로 빠지고 검증 평가에서도 제외된다.
    outlook: str = ""


def build_user_prompt(daily_data: List[DailyStockData], target_count: int = 3) -> str:
    lines = [
        "코스피 대형주 데이터입니다. **전일(직전 거래일) 마감 지표**와 **오늘 장중 현재 지표**를",
        "함께 제공합니다.",
        "'평균대비'는 그 이전 거래일 평균 거래량 대비 전일 거래량의 배수입니다.",
        "'전일 변동폭'은 전일 고가와 저가의 차이를 종가로 나눈 값입니다.",
        f"\n## 후보 ({len(daily_data)}종목)",
    ]
    for d in daily_data:
        headlines = "; ".join(d.headlines) if d.headlines else "없음"
        surge = f"{d.volume_surge:.2f}배" if d.volume_surge else "판단불가"
        # 산출하지 못한 값은 줄에서 통째로 뺀다 — "최근 고가 0원"을 적으면 그 0을 근거로 삼는
        # 추천이 나온다 (거래량 급증 배수를 '판단불가'로 적는 것과 같은 이유다)
        band = ""
        if d.recent_high > 0 and d.recent_low > 0 and d.moving_average > 0:
            band = (
                f"최근 고가 {d.recent_high:,.0f} / 최근 저가 {d.recent_low:,.0f} / "
                f"이동평균 {d.moving_average:,.0f}, "
            )
        # 당일 지표도 같은 규약이다 — 못 받았으면 0을 적지 않고 '없음'이라고 밝힌다
        if d.today_price > 0:
            today = (
                f"**현재가 {d.today_price:,.0f}원, 당일 등락률 {d.today_change_rate:+.2f}%, "
                f"당일 거래량 {d.today_volume:,}**, "
            )
        else:
            today = "**당일 지표 없음(조회 실패)**, "
        lines.append(
            f"- {d.ticker} {d.name}: {today}"
            f"전일 종가 {d.prev_close:,.0f}원"
            f"(고가 {d.prev_high:,.0f} / 저가 {d.prev_low:,.0f}), "
            f"전일 등락률 {d.prev_change_rate:+.2f}%, "
            f"전일 변동폭 {d.prev_range_pct:.2f}%, "
            f"전일 거래량 {d.prev_volume:,}(평균대비 {surge}), "
            f"{band}"
            f"뉴스/공시: {headlines}"
        )

    lines.append(
        f"\n위 데이터를 참고해 오늘 급등이 예상되는 종목 {target_count}개와 "
        "각 종목의 목표 매수가·목표 매도가를 추천하세요."
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
                # 참고용 값이라 빠져 있어도 추천 자체를 버리지 않는다 (0 = 산출 안 됨)
                target_sell_price=int(float(item.get("target_sell_price", 0))),
                reason=item["reason"],
                # 스키마가 요구하지만 빠져 있어도 파싱은 통과시킨다 — 유형 필터가 걸러낸다
                setup=item.get("setup", ""),
                # 스키마가 요구하지만 빠져도 추천을 버리지 않는다 — 주문에 쓰이지 않는 값이다
                outlook=item.get("outlook", ""),
            )
        )
    return recommendations


def drop_other_setups(
    recommendations: List[StockRecommendation],
) -> List[StockRecommendation]:
    """낙폭 되돌림(`SETUP_REBOUND`) 외의 추천을 걸러낸다 (PRD 5.5-B '추천 유형 제한').

    프롬프트만으로는 소프트 제약이라 — 조건에 맞는 종목이 없는 날 LLM이 정원을 채우려고
    다른 유형을 섞어 낼 수 있다 — 코드가 한 번 더 자른다. 정원을 채우지 못한 몫은
    `build_buy_plans`가 현금으로 남기므로(1호 전략), 여기서 억지로 채우지 않는다.
    """
    kept = []
    for rec in recommendations:
        if rec.setup == SETUP_REBOUND:
            kept.append(rec)
        else:
            logger.info(
                "낙폭 되돌림이 아닌 추천을 제외합니다: %s %s (setup=%s) — %s",
                rec.ticker,
                rec.name,
                rec.setup or "없음",
                rec.reason,
            )
    return kept


def drop_unknown_tickers(
    recommendations: List[StockRecommendation], daily_data: List[DailyStockData]
) -> List[StockRecommendation]:
    """후보 목록에 없는 종목을 걸러낸다 — 프롬프트 '절대 규칙 1'을 코드로도 강제한다.

    후보 밖 종목은 전일 종가를 알 수 없어 `apply_price_guardrail`의 ±5% 경계가 통째로
    꺼진다. 자릿수를 틀린 값이 그대로 주문가가 되는, 가드레일이 가장 필요한 자리에서
    가드레일이 사라지는 셈이라 주문 경로에 들어가기 전에 잘라낸다.
    """
    known = {data.ticker for data in daily_data}
    kept = []
    for rec in recommendations:
        if rec.ticker in known:
            kept.append(rec)
        else:
            logger.warning(
                "후보에 없는 종목을 추천해 제외합니다: %s %s (목표가 %s원)",
                rec.ticker,
                rec.name,
                f"{rec.target_price:,}",
            )
    return kept


def apply_price_guardrail(
    recommendations: List[StockRecommendation], daily_data: List[DailyStockData]
) -> None:
    """목표 매수가를 주문 가능한 값으로 보정한다 (제자리 수정, PRD 5.5-B '주문 방식').

    보정으로 값이 바뀌면 로그에 남긴다 — LLM이 낸 값과 실제 주문가가 다르면 나중에 추천을
    되짚을 때 혼란스럽다.
    """
    # 당일 현재가가 기준이고, 못 받은 종목만 전일 종가로 돌아간다 (PRD 5.5-B '주문 방식')
    reference = {
        data.ticker: (data.today_price if data.today_price > 0 else data.prev_close)
        for data in daily_data
    }
    for rec in recommendations:
        original = rec.target_price
        rec.target_price = normalize_target_price(original, reference.get(rec.ticker, 0.0))
        if rec.target_price != original:
            logger.info(
                "목표 매수가 보정: %s %s — %s원 → %s원 (기준가 %s원)",
                rec.ticker,
                rec.name,
                f"{original:,}",
                f"{rec.target_price:,}",
                f"{reference.get(rec.ticker, 0.0):,.0f}",
            )


def attach_recommend_price(
    recommendations: List[StockRecommendation], daily_data: List[DailyStockData]
) -> None:
    """추천 시각의 현재가를 추천에 실어 09:08 갭 판정까지 넘긴다 (제자리 수정, PRD 5.5-B).

    09:08에 기준가를 다시 조회하지 않기 위해서다 (API 호출을 늘리지 않는다). 후보 밖
    종목은 `drop_unknown_tickers`가 이미 걸러낸 뒤라 정상적으로는 전부 채워지지만, 못
    찾으면 0(모름)으로 두어 판정을 건너뛰게 한다.
    """
    today_price = {data.ticker: data.today_price for data in daily_data}
    for rec in recommendations:
        rec.recommend_price = today_price.get(rec.ticker, 0.0)


def warn_invalid_sell_targets(recommendations: List[StockRecommendation]) -> None:
    """목표 매도가가 목표 매수가보다 낮으면 로그에 남긴다 — 값은 그대로 둔다.

    목표 매도가는 주문에 쓰이지 않는 참고 수치라(PRD 5.5-B '목표 매도가') 보정하지 않는다.
    보정하면 LLM이 실제로 무슨 값을 냈는지 나중에 되짚을 수 없고, 이 값을 청산에 쓸지
    판단하려고 모으는 관찰 데이터가 오염된다.
    """
    for rec in recommendations:
        if 0 < rec.target_sell_price <= rec.target_price:
            logger.warning(
                "목표 매도가가 매수가보다 높지 않습니다: %s %s — 매수 %s원 / 매도 %s원",
                rec.ticker,
                rec.name,
                f"{rec.target_price:,}",
                f"{rec.target_sell_price:,}",
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

        recommendations = drop_unknown_tickers(recommendations, daily_data)
        if not recommendations:
            logger.error("추천 종목이 모두 후보 밖입니다 — 오늘 매수를 스킵합니다.")
            return None

        recommendations = drop_other_setups(recommendations)
        if not recommendations:
            logger.warning(
                "낙폭 되돌림 구간의 종목이 없습니다 — 오늘 매수를 스킵합니다 "
                "(prompt_version=%s).",
                PROMPT_TEMPLATE_VERSION,
            )
            return None

        apply_price_guardrail(recommendations, daily_data)
        attach_recommend_price(recommendations, daily_data)
        warn_invalid_sell_targets(recommendations)
        # 매수가와 매도가를 함께 남긴다 — 나중에 실제 고가와 대조해 목표 매도가가
        # 쓸 만했는지 되짚을 유일한 근거다 (DB에는 남지 않는다)
        logger.info(
            "LLM recommended %d stock(s) (prompt_version=%s): %s",
            len(recommendations),
            PROMPT_TEMPLATE_VERSION,
            [f"{r.ticker}@{r.target_price:,}→{r.target_sell_price:,}" for r in recommendations],
        )
        return recommendations
