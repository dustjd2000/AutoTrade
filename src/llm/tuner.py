import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import (
    DEFAULT_PROMPT_SECTIONS,
    MAX_TOKENS,
    PROMPT_SECTION_ORDER,
    _extract_json,
)
from src.logger.trade_store import RecommendationRow

logger = logging.getLogger(__name__)

TUNE_PROMPT_TEMPLATE_VERSION = "v1"

# 한 번에 고칠 수 있는 절 수. 다섯 절을 한꺼번에 바꾸면 무엇이 효과가 있었는지 영원히 모른다.
MAX_SECTIONS_PER_CHANGE = 2
# 이보다 짧은 본문은 지침 구실을 못 한다 — 실수로 빈 절을 쓰는 것을 막는다.
MIN_SECTION_LENGTH = 50

TUNE_KEY_SECTIONS = "sections"
TUNE_SCHEMA = {
    "type": "object",
    "properties": {
        "change": {"type": "boolean", "description": "프롬프트를 고칠지 여부"},
        "reason": {"type": "string", "description": "고치는/고치지 않는 이유와 근거 수치"},
        TUNE_KEY_SECTIONS: {
            "type": "object",
            "properties": {
                key: {"type": "string", "description": f"{key} 절의 전문 (## 헤더 포함)"}
                for key in PROMPT_SECTION_ORDER
            },
            "additionalProperties": False,
        },
    },
    "required": ["change", "reason", TUNE_KEY_SECTIONS],
    "additionalProperties": False,
}


@dataclass
class VersionStats:
    """한 프롬프트 버전의 성과 — 에이전트가 자기 수정 전후를 비교하는 근거다."""

    version: str
    count: int
    buy_hit: int
    sell_hit: int
    avg_change_rate: float


@dataclass
class TuneResult:
    change: bool
    reason: str
    sections: Dict[str, str] = field(default_factory=dict)


def group_by_version(rows: List[RecommendationRow]) -> List[VersionStats]:
    """검증된 추천을 `prompt_version`별로 묶는다. 버전 이름 순으로 돌려준다."""
    buckets: Dict[str, List[RecommendationRow]] = {}
    for row in rows:
        buckets.setdefault(row.prompt_version or "(없음)", []).append(row)

    stats = []
    for version, group in buckets.items():
        rates = [r.actual_change_rate for r in group if r.actual_change_rate is not None]
        stats.append(
            VersionStats(
                version=version,
                count=len(group),
                buy_hit=sum(1 for r in group if r.buy_target_hit),
                sell_hit=sum(1 for r in group if r.sell_target_hit),
                avg_change_rate=sum(rates) / len(rates) if rates else 0.0,
            )
        )
    return sorted(stats, key=lambda s: s.version)


def sanitize_sections(raw: Dict[str, str]) -> Dict[str, str]:
    """적용 전 안전장치 (PRD '프롬프트 자동 수정').

    개수 검사가 **가장 먼저**이고 걸러내기 전 원본 개수를 센다 — 걸러낸 뒤에 세면
    잘못된 절을 섞어 보내는 것으로 제한을 우회할 수 있다.

    스키마가 이미 키를 다섯 개로 제한하지만 코드에서 한 번 더 자른다. 스키마는 모델이
    지키는 소프트 제약이고, 잠긴 절이 실제로 반영되는 것은 고장이기 때문이다
    (`drop_other_setups`가 유형 제한을 코드로 다시 자르는 것과 같은 이유다).
    """
    if not isinstance(raw, dict):
        return {}
    if len(raw) > MAX_SECTIONS_PER_CHANGE:
        logger.warning(
            "한 번에 고칠 수 있는 절은 %d개입니다 — %d개가 와서 전체를 버립니다: %s",
            MAX_SECTIONS_PER_CHANGE, len(raw), list(raw),
        )
        return {}

    kept = {}
    for key, text in raw.items():
        if key not in PROMPT_SECTION_ORDER:
            logger.warning("고칠 수 없는 절이라 무시합니다: %s", key)
            continue
        if not isinstance(text, str):
            logger.warning("절 본문이 문자열이 아니라 무시합니다: %s (%s)", key, type(text).__name__)
            continue
        body = (text or "").strip()
        if len(body) < MIN_SECTION_LENGTH:
            logger.warning("본문이 너무 짧아 무시합니다: %s (%d자)", key, len(body))
            continue
        kept[key] = body
    return kept


def build_tune_system_prompt() -> str:
    return f"""당신은 주식 추천 프롬프트를 성과 데이터에 근거해 다듬는 편집자입니다.

## 역할
아래 프롬프트로 만들어진 추천의 실제 결과를 보고, 프롬프트의 **판단·서술 지침**을 고칠지
판단합니다. 고칠 필요가 없으면 고치지 않는 것이 정상입니다.

## 고칠 수 있는 절
`judgment_criteria`(판단 기준), `buy_target`(목표 매수가 작성 지침),
`sell_target`(목표 매도가 작성 지침), `outlook`(오늘 전망 작성 지침),
`reason`(근거 작성 지침) — 이 다섯뿐입니다.

`역할`·`절대 규칙`·`추천 유형` 세 절은 **고칠 수 없습니다.** 프로그램 코드가 그 규칙을
강제하고 있어, 문장만 바꾸면 코드와 어긋나 추천이 통째로 버려집니다. 참고용으로 보여줄
뿐이니, 나머지 다섯 절이 그 규칙과 모순되지 않게 하는 데만 쓰십시오.

## 규칙
1. **한 번에 최대 {MAX_SECTIONS_PER_CHANGE}절만** 고치십시오. 더 많이 고치면 무엇이 효과가
   있었는지 알 수 없어 전체가 폐기됩니다.
2. 고치는 절은 `##` 헤더 줄을 포함한 **전문**을 주십시오. 일부만 주면 나머지가 사라집니다.
3. `reason`에는 **근거로 삼은 수치를 인용**하십시오. ("v11 12건 중 목표 매도가 도달 2건"처럼)
4. **표본이 부족하면 고치지 마십시오.** 버전별 건수가 한 자릿수면 우연과 신호를 구분할 수
   없습니다. 그럴 때는 `change: false`로 두고 그 이유를 적으십시오.
5. 이전 변경 이력이 함께 주어집니다. 직전에 고친 것을 되돌리는 방향으로 다시 고치지
   마십시오 — 그러면 프롬프트가 왔다 갔다 하기만 합니다.
6. 데이터가 말하지 않는 것을 지어내지 마십시오. 주어진 집계와 개별 결과만 근거로 삼습니다."""


def build_tune_user_prompt(
    stats: List[VersionStats],
    rows: List[RecommendationRow],
    sections: Dict[str, str],
    locked_text: str,
    why_history: str,
) -> str:
    lines = ["## 버전별 성과 (검증이 끝난 추천만)"]
    if stats:
        for s in stats:
            lines.append(
                f"- {s.version}: {s.count}건 | 목표 매수가 도달 {s.buy_hit}건 | "
                f"목표 매도가 도달 {s.sell_hit}건 | 평균 등락률 {s.avg_change_rate:+.2f}%"
            )
    else:
        lines.append("- 아직 검증된 추천이 없습니다.")

    lines.append("\n## 개별 결과")
    for r in rows:
        lines.append(
            f"- {r.day} {r.ticker} {r.name} ({r.prompt_version}): "
            f"추천가 {r.recommend_price:,.0f} / 목표매수 {r.target_price:,} / "
            f"목표매도 {r.target_sell_price:,} → 고 {r.actual_high:,.0f} / 저 {r.actual_low:,.0f} / "
            f"종 {r.actual_close:,.0f} ({r.actual_change_rate:+.2f}%)"
        )
        lines.append(f"  전망: {r.outlook}")
        if r.review:
            lines.append(f"  평가: {r.review}")

    lines.append("\n## 고칠 수 없는 절 (참고용)")
    lines.append(locked_text)

    lines.append("\n## 현재 고칠 수 있는 절")
    for key in PROMPT_SECTION_ORDER:
        lines.append(f"\n### {key}\n{sections[key]}")

    lines.append("\n## 이전 변경 이력")
    lines.append(why_history or "(없음 — 아직 고친 적이 없습니다)")

    lines.append("\n위 결과를 근거로 프롬프트를 고칠지 판단하고, 고친다면 그 절의 전문을 주십시오.")
    return "\n".join(lines)


def parse_tune_response(raw_text: str) -> TuneResult:
    """응답을 `TuneResult`로. 형식이 어긋나면 예외를 던진다 — 호출측이 잡아 무변경으로 끝낸다."""
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("tune response must be a JSON object")
    sections = data.get(TUNE_KEY_SECTIONS) or {}
    return TuneResult(
        change=bool(data.get("change")),
        reason=str(data.get("reason", "")).strip(),
        sections=sections if isinstance(sections, dict) else {},
    )


class PromptTuner:
    """추천 프롬프트를 성과 데이터로 다듬는 모듈 — 추천·검증과 같은 모델, 별도 프롬프트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def tune(
        self,
        stats: List[VersionStats],
        rows: List[RecommendationRow],
        sections: Dict[str, str],
        locked_text: str,
        why_history: str = "",
        timeout_seconds: float = 120.0,
    ) -> Optional[TuneResult]:
        """수정 판단을 받아 돌려준다. 실패하면 None — 그날은 아무것도 고치지 않는다."""
        user_prompt = build_tune_user_prompt(stats, rows, sections, locked_text, why_history)
        logger.info(
            "프롬프트 수정 요청 (tune_prompt_version=%s, %d건):\n%s",
            TUNE_PROMPT_TEMPLATE_VERSION, len(rows), user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_tune_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": TUNE_SCHEMA}},
            )
        except Exception:
            logger.exception("프롬프트 수정 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("프롬프트 수정 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("프롬프트 수정 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_tune_response(raw_text)
        except Exception:
            logger.exception("프롬프트 수정 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
