from datetime import date
from types import SimpleNamespace

from src.llm.exit_advisor import CODE_CHANGE_HISTORY, EXIT_PROMPT_SECTION_ORDER
from src.llm.exit_tuner import (
    MAX_SECTIONS_PER_CHANGE,
    MIN_REVIEWS_FOR_TUNING,
    ExitPromptTuner,
    build_exit_tune_system_prompt,
    build_exit_tune_user_prompt,
    group_by_version,
    parse_tune_response,
    sanitize_sections,
    with_code_history,
)
from src.logger.trade_store import ExitReviewRow

LONG = "## 시간\n" + ("지침 " * 20).strip()


def row(version, outcome, net_pnl=1000.0, net_return=0.01, peak=0.02, day=date(2026, 9, 22)):
    return ExitReviewRow(
        day=day, ticker="005930", name="삼성전자", exit_prompt_version=version, net_pnl=net_pnl,
        net_return=net_return, peak_return=peak, outcome=outcome, exit_reason="ai_judgment",
        decision_count=3, review="평가문",
    )


def test_group_by_version_counts_outcomes_and_skips_unknown():
    stats = group_by_version([
        row("v2", "captured", 1000.0, 0.01, 0.02),
        row("v2", "missed", -500.0, -0.005, 0.015),
        row("v2", "unknown", None, None, None),
        row("20260929", "no_chance", -2000.0, -0.02, -0.001),
    ])
    by = {s.version: s for s in stats}
    v2 = by["v2"]
    assert (v2.count, v2.captured, v2.missed, v2.no_chance) == (2, 1, 1, 0)
    assert v2.net_pnl_total == 500.0
    assert abs(v2.avg_net_return - 0.0025) < 1e-12
    # 놓친 폭은 고점이 플러스였던 종목만: (0.02-0.01 + 0.015-(-0.005)) / 2
    assert abs(v2.avg_missed_gap - 0.015) < 1e-12
    assert by["20260929"].avg_missed_gap is None


def test_exit_sanitize_allows_one_editable_section_only():
    order = EXIT_PROMPT_SECTION_ORDER
    assert MAX_SECTIONS_PER_CHANGE == 1
    assert sanitize_sections({"time": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {"time": LONG}
    assert sanitize_sections({"time": LONG, "criteria": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {}
    assert sanitize_sections({"role": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {}
    assert sanitize_sections({"time": "짧음"}, order, MAX_SECTIONS_PER_CHANGE) == {}


def test_system_prompt_states_the_rules():
    prompt = build_exit_tune_system_prompt()
    assert "순손익" in prompt
    assert "1절" in prompt
    assert "loss_positions" in prompt and "time" in prompt and "criteria" in prompt
    assert "되돌리는" in prompt
    # 사람이 코드로 고친 변경(v5·v6)을 되돌리지 말라는 규칙 (2026-10-29)
    assert "v5" in prompt and "v6" in prompt
    assert "반납이 절반 미만" in prompt and "지수 방향이 유리" in prompt


def test_sample_gate_is_five_reviews():
    """2호는 하루 ETF 1종목이라 5건이면 약 5거래일이다 (사용자 결정 2026-10-29)."""
    assert MIN_REVIEWS_FOR_TUNING == 5


def test_code_change_history_comes_first_in_the_history_given_to_the_tuner():
    text = with_code_history("- 20261030: 자동 수정 이유")
    assert text.startswith(CODE_CHANGE_HISTORY)
    assert text.endswith("- 20261030: 자동 수정 이유")
    assert "v5" in CODE_CHANGE_HISTORY and "v6" in CODE_CHANGE_HISTORY
    assert with_code_history("") == CODE_CHANGE_HISTORY


def test_parse_tune_response():
    result = parse_tune_response(
        '{"change": true, "reason": " 놓침 5건 ", "sections": {"time": "## 시간\\n본문"}}'
    )
    assert result.change is True and result.reason == "놓침 5건"
    assert result.sections == {"time": "## 시간\n본문"}
    assert parse_tune_response('{"change": false, "reason": "r"}').sections == {}


def test_user_prompt_carries_stats_rows_sections_and_history():
    stats = group_by_version([row("v2", "missed", -500.0, -0.005, 0.015)])
    prompt = build_exit_tune_user_prompt(
        stats,
        [row("v2", "missed", -500.0, -0.005, 0.015)],
        {key: f"## {key}\n본문" for key in EXIT_PROMPT_SECTION_ORDER},
        "## 역할\n잠김",
        "- v1: 이전 이유",
    )
    assert "v2: 1건" in prompt and "놓침 1" in prompt
    assert "평가문" in prompt
    assert "### loss_positions" in prompt
    assert "## 역할\n잠김" in prompt
    assert "- v1: 이전 이유" in prompt


def test_tune_returns_none_when_api_raises():
    tuner = ExitPromptTuner.__new__(ExitPromptTuner)
    tuner.settings = SimpleNamespace(llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("down")

    tuner._client = Boom()
    assert tuner.tune([], [], {}, "") is None
