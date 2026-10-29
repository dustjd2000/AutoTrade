import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import anthropic

from config.settings import Settings
from src.core.exit_review import CAPTURED, MISSED, NO_CHANCE, UNKNOWN
from src.llm.common import MAX_TOKENS, extract_json
from src.llm.exit_advisor import CODE_CHANGE_HISTORY, EXIT_PROMPT_SECTION_ORDER
from src.logger.trade_store import ExitReviewRow

logger = logging.getLogger(__name__)

EXIT_TUNE_PROMPT_TEMPLATE_VERSION = "v1"

# 한 번에 고칠 수 있는 절 수. 매도 판단은 결과에 시장 운이 크게 섞여, 두 절을 함께 바꾸면
# 어느 쪽이 효과였는지 추천 쪽보다도 가리기 어렵다 (스펙 2026-09-22 4.3).
MAX_SECTIONS_PER_CHANGE = 1
# 현재 버전으로 검증된 종목(판정 불가 제외)이 이만큼 쌓이기 전에는 튜너를 부르지 않는다.
# 2호는 하루 ETF 1종목이라 5건이면 약 5거래일이다 — 하루치 우연으로 고치지 않기 위한 코드 게이트다.
# (1호 시절에는 하루 약 2종목에 10건이었다. 2호용으로 복구하면서 사용자가 5건으로 낮췄다, 2026-10-29.)
MIN_REVIEWS_FOR_TUNING = 5
# 이보다 짧은 본문은 지침 구실을 못 한다 — 실수로 빈 절을 쓰는 것을 막는다.
MIN_SECTION_LENGTH = 50

TUNE_KEY_SECTIONS = "sections"

EXIT_TUNE_SCHEMA = {
    "type": "object",
    "properties": {
        "change": {"type": "boolean", "description": "프롬프트를 고칠지 여부"},
        "reason": {"type": "string", "description": "고치는/고치지 않는 이유와 근거 수치"},
        TUNE_KEY_SECTIONS: {
            "type": "object",
            "properties": {
                key: {"type": "string", "description": f"{key} 절의 전문 (## 헤더 포함)"}
                for key in EXIT_PROMPT_SECTION_ORDER
            },
            "additionalProperties": False,
        },
    },
    "required": ["change", "reason", TUNE_KEY_SECTIONS],
    "additionalProperties": False,
}


@dataclass
class TuneResult:
    change: bool
    reason: str
    sections: Dict[str, str] = field(default_factory=dict)


def parse_tune_response(raw_text: str) -> TuneResult:
    """응답을 `TuneResult`로. 형식이 어긋나면 예외를 던진다 — 호출측이 잡아 무변경으로 끝낸다."""
    data = json.loads(extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("tune response must be a JSON object")
    sections = data.get(TUNE_KEY_SECTIONS) or {}
    return TuneResult(
        change=bool(data.get("change")),
        reason=str(data.get("reason", "")).strip(),
        sections=sections if isinstance(sections, dict) else {},
    )


def sanitize_sections(
    raw: Dict[str, str],
    allowed: Sequence[str] = EXIT_PROMPT_SECTION_ORDER,
    max_sections: int = MAX_SECTIONS_PER_CHANGE,
) -> Dict[str, str]:
    """적용 전 안전장치 (PRD 5.15) — 허용 키·최대 절 수·최소 본문 길이만 통과시킨다.

    개수 검사가 **가장 먼저**이고 걸러내기 전 원본 개수를 센다 — 걸러낸 뒤에 세면
    잘못된 절을 섞어 보내는 것으로 제한을 우회할 수 있다.

    스키마가 이미 키를 세 개로 제한하지만 코드에서 한 번 더 자른다. 스키마는 모델이
    지키는 소프트 제약이고, 잠긴 절이 실제로 반영되는 것은 고장이기 때문이다.
    """
    if not isinstance(raw, dict):
        return {}
    if len(raw) > max_sections:
        logger.warning(
            "한 번에 고칠 수 있는 절은 %d개입니다 — %d개가 와서 전체를 버립니다: %s",
            max_sections, len(raw), list(raw),
        )
        return {}

    kept = {}
    for key, text in raw.items():
        if key not in allowed:
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


@dataclass
class ExitVersionStats:
    """매도 프롬프트 한 버전의 성과 — 판정 불가(unknown)는 뺀다."""

    version: str
    count: int
    captured: int
    missed: int
    no_chance: int
    net_pnl_total: float
    avg_net_return: float
    avg_missed_gap: Optional[float]  # 고점이 플러스였던 종목의 평균 (고점 − 최종), 없으면 None


def group_by_version(rows: List[ExitReviewRow]) -> List[ExitVersionStats]:
    buckets: Dict[str, List[ExitReviewRow]] = {}
    for row in rows:
        if row.outcome == UNKNOWN:
            continue
        buckets.setdefault(row.exit_prompt_version or "(없음)", []).append(row)

    stats = []
    for version, group in buckets.items():
        returns = [r.net_return for r in group if r.net_return is not None]
        gaps = [
            r.peak_return - r.net_return
            for r in group
            if r.peak_return is not None and r.peak_return > 0 and r.net_return is not None
        ]
        stats.append(
            ExitVersionStats(
                version=version,
                count=len(group),
                captured=sum(1 for r in group if r.outcome == CAPTURED),
                missed=sum(1 for r in group if r.outcome == MISSED),
                no_chance=sum(1 for r in group if r.outcome == NO_CHANCE),
                net_pnl_total=sum(r.net_pnl or 0.0 for r in group),
                avg_net_return=sum(returns) / len(returns) if returns else 0.0,
                avg_missed_gap=sum(gaps) / len(gaps) if gaps else None,
            )
        )
    return sorted(stats, key=lambda s: s.version)


def build_exit_tune_system_prompt() -> str:
    return f"""당신은 장중 매도 판단 프롬프트를 성과 데이터에 근거해 다듬는 편집자입니다. 대상 전략은 지수 ETF
(KODEX 200·인버스) 국면 추종 당일 매매입니다.

## 역할
아래 프롬프트로 내린 매도 판단의 실제 결과를 보고, 프롬프트의 편집 가능한 절을 고칠지
판단합니다. 고칠 필요가 없으면 고치지 않는 것이 정상입니다.

## 잣대
성과는 **순손익(수수료·세금을 뺀 값)**으로만 봅니다. "순이익 확정"이 늘고 "순이익 기회를
놓침"과 순손실이 줄어야 개선입니다. 덜 잃은 것은 개선이 아닙니다.

## 고칠 수 있는 절
`loss_positions`(손실 중인 종목), `time`(시간), `criteria`(판단 기준) — 이 셋뿐입니다.
`역할`·`기본은 보유입니다`·`reason 작성 지침`은 **고칠 수 없습니다.** 참고용으로 보여줄
뿐이니, 고치는 절이 그 절들과 모순되지 않게 하는 데만 쓰십시오.

## 규칙
1. **한 번에 {MAX_SECTIONS_PER_CHANGE}절만** 고치십시오. 더 많이 고치면 전체가 폐기됩니다.
2. 고치는 절은 `##` 헤더 줄을 포함한 **전문**을 주십시오.
3. `reason`에는 **근거로 삼은 수치를 인용**하십시오. ("v2 12건 중 놓침 5건, 평균 놓친 폭 1.8%p"처럼)
4. 표본이 부족하거나 신호가 뚜렷하지 않으면 고치지 마십시오 (`change: false`와 그 이유).
5. 이전 변경 이력이 함께 주어집니다. 직전에 고친 것을 되돌리는 방향으로 다시 고치지
   마십시오 — 그러면 프롬프트가 왔다 갔다 하기만 합니다. 이력에는 자동 수정뿐 아니라 **사람이
   코드로 고친 변경(v5·v6)**도 들어 있습니다. 특히 '지수 때문에 자연스런 손실'·'통상적 변동'·
   '반납이 절반 미만'·'지수 방향이 유리'를 보유 근거로 금지한 것, 50% 반납 문턱을 없앤 것,
   하락 단계에서 이익 확정을 먼저 검토하게 한 것은 되돌리거나 약화하지 마십시오.
6. 데이터가 말하지 않는 것을 지어내지 마십시오."""


def _pct(ratio: Optional[float]) -> str:
    return "모름" if ratio is None else f"{ratio * 100:+.2f}%"


def build_exit_tune_user_prompt(
    stats: List[ExitVersionStats],
    rows: List[ExitReviewRow],
    sections: Dict[str, str],
    locked_text: str,
    why_history: str,
) -> str:
    lines = ["## 버전별 성과 (판정 불가 제외)"]
    for s in stats:
        gap = "-" if s.avg_missed_gap is None else f"{s.avg_missed_gap * 100:.2f}%p"
        lines.append(
            f"- {s.version}: {s.count}건 | 확정 {s.captured} / 놓침 {s.missed} / 기회 없음 {s.no_chance} | "
            f"순손익 합계 {s.net_pnl_total:+,.0f}원 | 평균 순손익률 {s.avg_net_return * 100:+.2f}% | "
            f"평균 놓친 폭 {gap}"
        )
    if not stats:
        lines.append("- 아직 검증된 매도가 없습니다.")

    lines.append("\n## 개별 결과")
    for r in rows:
        pnl = "모름" if r.net_pnl is None else f"{r.net_pnl:+,.0f}원"
        lines.append(
            f"- {r.day} {r.ticker} {r.name} ({r.exit_prompt_version or '-'}): {r.outcome} | "
            f"순손익 {pnl} ({_pct(r.net_return)}) | 고점 {_pct(r.peak_return)} | "
            f"청산 {r.exit_reason or '-'} | 판단 {r.decision_count}회"
        )
        if r.review:
            lines.append(f"  평가: {r.review}")

    lines.append("\n## 고칠 수 없는 절 (참고용)")
    lines.append(locked_text)

    lines.append("\n## 현재 고칠 수 있는 절")
    for key in EXIT_PROMPT_SECTION_ORDER:
        lines.append(f"\n### {key}\n{sections.get(key, '')}")

    lines.append("\n## 이전 변경 이력")
    lines.append(why_history or "(없음 — 아직 고친 적이 없습니다)")

    lines.append("\n위 결과를 근거로 프롬프트를 고칠지 판단하고, 고친다면 그 절의 전문을 주십시오.")
    return "\n".join(lines)


def with_code_history(why_history: str) -> str:
    """튜너에게 줄 변경 이력 — 사람이 코드로 고친 이력(`CODE_CHANGE_HISTORY`)을 맨 앞에 붙인다."""
    if not why_history:
        return CODE_CHANGE_HISTORY
    return f"{CODE_CHANGE_HISTORY}\n{why_history}"


class ExitPromptTuner:
    """매도 프롬프트를 검증 결과로 다듬는 모듈 — AI 매도 판단과 같은 모델, 별도 프롬프트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def tune(
        self,
        stats: List[ExitVersionStats],
        rows: List[ExitReviewRow],
        sections: Dict[str, str],
        locked_text: str,
        why_history: str = "",
        timeout_seconds: float = 120.0,
    ) -> Optional[TuneResult]:
        """수정 판단. 실패하면 None — 그날은 아무것도 고치지 않는다."""
        user_prompt = build_exit_tune_user_prompt(stats, rows, sections, locked_text, why_history)
        logger.info(
            "매도 프롬프트 수정 요청 (exit_tune_prompt_version=%s, %d건):\n%s",
            EXIT_TUNE_PROMPT_TEMPLATE_VERSION, len(rows), user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_exit_tune_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": EXIT_TUNE_SCHEMA}},
            )
        except Exception:
            logger.exception("매도 프롬프트 수정 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("매도 프롬프트 수정 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("매도 프롬프트 수정 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_tune_response(raw_text)
        except Exception:
            logger.exception("매도 프롬프트 수정 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
