"""추천된 종목에 구체적 악재가 있는지 웹검색으로 확인한다 (PRD 5.5-B '뉴스 검증').

추천 프롬프트가 보는 입력은 가격·거래량·DART 공시 제목뿐이라, "회사가 구조적으로
나쁘다"를 알 방법이 없었다 — 크래프톤을 9/29에 사서 잃고 10/01에 더 떨어졌다는 이유로
다시 산 것이 계기다 (PRD 10절).

**추천된 종목만** 본다. 후보 25종목을 전부 보면 시간도 비용도 감당이 안 된다.
종목당 1회·병렬 호출에 `max_uses=3`·`effort=low`를 걸어 2종목 기준 30초다 — 한 번에
물으면 238초였다 (스펙 2절 실측).
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Dict, List, Optional, Tuple

import anthropic

from config.settings import Settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 2000
# 검색 횟수 상한. 늘리면 느려지고(실측 5회에서 117초) 줄이면 확인을 못 한다.
MAX_SEARCHES_PER_TICKER = 3

NEWS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checked", "blocking", "reason"],
    "properties": {
        "checked": {"type": "boolean"},
        "blocking": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

_SYSTEM_PROMPT = """당신은 한국 주식 단기 매매 직전에 종목의 악재를 확인하는 조사자입니다.

## 역할
주어진 종목 하나에 대해 **웹을 검색해** 최근 2주 내에 주가에 불리한 구체적 사건이 있었는지
확인하고, 오늘 단기 매수를 피할 만한지 판단합니다.

## 악재로 보는 것
실적 하향·어닝쇼크, 신작/수주/수출 부진, 유상증자·전환사채 같은 지분 희석, 규제·제재,
소송·횡령·배임, 애널리스트 목표가 하향, 주요 고객 이탈.

## 악재로 보지 않는 것
**주가가 떨어지고 있다는 것 자체는 악재가 아닙니다.** 이 매매 전략은 최근 낙폭이 큰 종목을
의도적으로 고릅니다. "주가 하락", "약세", "조정", "외국인 매도", "차익실현 매물" 같은 수급·
가격 서술은 `blocking`의 근거가 되지 못합니다. 떨어진 **이유**가 위 '악재로 보는 것'에
해당할 때만 `blocking`입니다.
수개월 전 기사도 근거가 되지 못합니다 — 최근 2주가 기준입니다.

## 출력
- `checked` — 검색 결과를 실제로 확인했으면 true. 검색이 막혔거나 결과가 없어 판단 근거를
  얻지 못했으면 **false**입니다. 확인하지 못한 것을 "악재 없음"으로 적지 마십시오.
- `blocking` — 위 기준의 구체적 악재가 있으면 true. 애매하면 false입니다.
- `reason` — 근거를 한 문장으로. **검색 결과에 근거하지 않은 내용을 쓰지 마십시오.**
  당신의 학습 데이터에 있는 종목 평판이나 과거 기억은 근거가 아닙니다. 근거 기사가 없으면
  `checked`를 false로 두고 그 사실을 적으십시오."""


@dataclass(frozen=True)
class NewsVerdict:
    """한 종목에 대한 악재 판정."""

    ticker: str
    checked: bool   # 웹검색이 실제로 확인했는가
    blocking: bool  # 오늘 매수를 피할 만한 구체적 악재가 있는가
    reason: str


def build_news_system_prompt() -> str:
    return _SYSTEM_PROMPT


def build_news_user_prompt(ticker: str, name: str) -> str:
    return (
        f"{name}({ticker})에 대해 최근 2주 내 주가에 불리한 구체적 악재가 있는지 "
        "웹에서 확인하고 판단하세요."
    )


def parse_news_verdict(ticker: str, raw_text: str) -> NewsVerdict:
    """판정 응답을 파싱한다.

    `blocking`이 불리언 `true`가 아니면 통과로 떨어진다 — 형식 오류가 매수를 막는 쪽으로
    기울면 안 된다. `checked`도 같은 규약이라, 문자열 "maybe" 같은 값은 '확인 못 함'이다.
    """
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("뉴스 검증 응답이 JSON 객체가 아닙니다")
    return NewsVerdict(
        ticker=ticker,
        checked=data.get("checked") is True,
        blocking=data.get("blocking") is True,
        reason=str(data.get("reason", "")).strip(),
    )


def _extract_json(raw_text: str) -> str:
    """구조화 출력이라 보통 그대로지만, 코드펜스가 붙어 오는 경우를 한 번 벗긴다."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


class NewsVerifier:
    """추천 종목의 악재를 웹검색으로 확인한다.

    **실패한 종목은 반환 dict에 키 자체를 넣지 않는다** — 호출측이 "판정 없음 = 통과"로
    읽는다. 검증은 추가 안전장치지 관문이 아니라, 막히면 종전대로 매수해야 한다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def verify(
        self, items: List[Tuple[str, str]], timeout_seconds: float
    ) -> Dict[str, NewsVerdict]:
        """(ticker, name) 목록을 병렬로 검증한다. 판정이 난 종목만 담아 돌려준다."""
        if not items:
            return {}
        logger.info(
            "뉴스 검증 요청 (%d종목, 예산 %.0f초): %s",
            len(items), timeout_seconds, ", ".join(f"{t} {n}" for t, n in items),
        )
        # 종목마다 따로 물어야 빠르다 — 한 번에 물으면 서로의 검색이 직렬화된다 (238초 vs 30초)
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            results = pool.map(lambda item: self._verify_one(*item, timeout_seconds), items)
        verdicts = {v.ticker: v for v in results if v is not None}
        for v in verdicts.values():
            logger.info(
                "뉴스 검증: %s checked=%s blocking=%s — %s",
                v.ticker, v.checked, v.blocking, v.reason,
            )
        return verdicts

    def _verify_one(
        self, ticker: str, name: str, timeout_seconds: float
    ) -> Optional[NewsVerdict]:
        deadline = monotonic() + timeout_seconds
        try:
            # 스트리밍 + max_retries=0은 추천·매도 판단과 같은 이유다 — 재시도가 예산을
            # 배로 늘리고, 비스트리밍은 생성이 길어진 날 read timeout에 그대로 걸린다.
            with self._client.with_options(
                timeout=timeout_seconds, max_retries=0
            ).messages.stream(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_news_system_prompt(),
                messages=[{"role": "user", "content": build_news_user_prompt(ticker, name)}],
                tools=[
                    {
                        "type": "web_search_20260209",
                        "name": "web_search",
                        "max_uses": MAX_SEARCHES_PER_TICKER,
                    }
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": NEWS_SCHEMA},
                    # 악재가 있나 없나는 깊은 추론이 필요한 질문이 아니다. low로 30초, 비용도 준다
                    "effort": "low",
                },
            ) as stream:
                for _ in stream:
                    if monotonic() > deadline:
                        raise TimeoutError(
                            f"뉴스 검증 예산 {timeout_seconds:.0f}초를 넘겨 중단합니다: {ticker}"
                        )
                response = stream.get_final_message()
        except Exception:
            logger.exception("뉴스 검증 호출이 실패했습니다 (%s %s) — 통과로 둡니다.", ticker, name)
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("뉴스 검증 응답이 정상 종료되지 않았습니다 (%s): %s", ticker, response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("뉴스 검증 응답에 텍스트가 없습니다 (%s).", ticker)
            return None
        try:
            return parse_news_verdict(ticker, raw_text)
        except Exception:
            logger.exception("뉴스 검증 응답 파싱 실패 (%s). 원문(앞 300자): %s", ticker, raw_text[:300])
            return None
