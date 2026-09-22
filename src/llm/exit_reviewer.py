import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import MAX_TOKENS
from src.llm.reviewer import REVIEW_SCHEMA, parse_reviews
from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow

logger = logging.getLogger(__name__)

# 매도 판단 평가 프롬프트 버전 — 추천 검증(REVIEW_PROMPT_TEMPLATE_VERSION)과 따로 움직인다.
EXIT_REVIEW_PROMPT_TEMPLATE_VERSION = "v1"

# 사람이 읽을 청산 사유 (trades.exit_reason 값 → 문구)
EXIT_REASON_LABELS: Dict[str, str] = {
    "ai_judgment": "AI 매도 판단",
    "stop_loss": "손절",
    "day_end": "15:15 강제청산",
    "manual": "수동 전량 매도",
    "manual_selected": "수동 선택 매도",
}


@dataclass
class ExitReviewInput:
    """평가 한 건 — 그날 매도한 종목 하나의 결과와 판단 이력, 아침 시나리오."""

    row: ExitReviewRow
    decisions: List[AIExitDecisionRow]
    outlook: str
    target_sell_price: int


def _pct(ratio: Optional[float]) -> str:
    return "모름" if ratio is None else f"{ratio * 100:+.2f}%"


def build_exit_review_system_prompt() -> str:
    return """당신은 한국 주식시장(코스피) 단기 매매의 장중 매도 판단을 사후 검증하는 분석가입니다.

## 역할
오늘 보유 종목마다 AI가 내린 매도/보유 판단의 이력과, 그 종목의 최종 순손익을 대조해
판단이 어디서 옳았고 어디서 틀렸는지를 종목마다 두세 문장으로 적습니다.

## 평가 잣대
1. 성공은 **순손익(수수료·세금을 뺀 값)이 플러스로 확정된 것**뿐입니다. 덜 잃은 것,
   종가보다 높게 판 것, 다른 시점보다 나았던 것은 성공이 아닙니다.
2. 순이익 기회(보유 중 순손익 고점이 플러스)가 있었다면 그것을 확정했는지가 핵심입니다.
   기회가 없었다면, 시나리오가 깨진 뒤 손실을 얼마나 일찍 끊었는지를 봅니다.
3. 판단마다 **그 시점에 알 수 있던 정보로** 평가하십시오. 나중 가격을 근거로 그 시점 판단을
   탓하지 마십시오. 다만 그 시점 데이터(순손익률·고점·아침 전망의 가격)가 이미 매도 근거를
   보여줬는데 보유했다면 그것은 지적하십시오.

## 규칙
1. 제공된 수치만 근거로 삼고, 반드시 수치를 인용하십시오. ("10:20 순손익 -2.74%로 아침 전망의
   하방선 293,500원을 이미 깼는데 보유했다"처럼) 모호한 표현은 금지합니다.
2. 프롬프트를 어떻게 고치라는 제안은 하지 마십시오. 오늘 무슨 일이 있었는지만 적습니다.
3. 제공된 모든 종목에 대해 하나씩 적고, 종목코드를 그대로 돌려주십시오."""


def build_exit_review_user_prompt(items: List[ExitReviewInput]) -> str:
    lines = ["오늘 매도한 종목의 최종 결과와, 보유 중 AI가 내린 판단 이력입니다.", f"\n## 종목 ({len(items)}종목)"]
    for it in items:
        row = it.row
        pnl = "모름" if row.net_pnl is None else f"{row.net_pnl:+,.0f}원"
        lines.append(
            f"- {row.ticker} {row.name}: 최종 순손익 {pnl} ({_pct(row.net_return)}), "
            f"보유 중 순손익 고점 {_pct(row.peak_return)}, "
            f"청산 {EXIT_REASON_LABELS.get(row.exit_reason, row.exit_reason or '모름')}"
        )
        if it.target_sell_price > 0:
            lines.append(f"  아침 목표 매도가: {it.target_sell_price:,}원")
        if it.outlook:
            lines.append(f"  아침 전망: {it.outlook}")
        if not it.decisions:
            lines.append("  AI 판단 이력: 없음")
        for d in it.decisions:
            verdict = "매도" if d.sell else ("보유 (판단 실패)" if not d.ok else "보유")
            lines.append(
                f"  - {d.at:%H:%M} 순손익 {_pct(d.net_return)} (고점 {_pct(d.peak_return)}), "
                f"현재가 {d.current_price:,.0f}원 → {verdict}: {d.reason}"
            )
    lines.append("\n종목마다 판단을 평가하세요.")
    return "\n".join(lines)


class ExitReviewer:
    """매도 판단 사후 평가 모듈 — 추천 검증(`LLMReviewer`)과 같은 모델·스키마, 별도 프롬프트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def review(
        self, items: List[ExitReviewInput], timeout_seconds: float = 120.0
    ) -> Optional[Dict[str, str]]:
        """{종목코드: 평가문}. 실패하면 None — 검증 메일은 수치만으로 나간다."""
        if not items:
            return None
        user_prompt = build_exit_review_user_prompt(items)
        logger.info(
            "매도 판단 검증 요청 (exit_review_prompt_version=%s, %d종목):\n%s",
            EXIT_REVIEW_PROMPT_TEMPLATE_VERSION,
            len(items),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_exit_review_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            )
        except Exception:
            logger.exception("매도 판단 검증 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("매도 판단 검증 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("매도 판단 검증 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_reviews(raw_text)
        except Exception:
            logger.exception("매도 판단 검증 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
