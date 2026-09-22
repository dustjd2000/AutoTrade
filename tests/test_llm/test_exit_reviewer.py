from datetime import date, datetime
from types import SimpleNamespace

from src.llm.exit_reviewer import (
    ExitReviewer,
    ExitReviewInput,
    build_exit_review_system_prompt,
    build_exit_review_user_prompt,
)
from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow

DAY = date(2026, 9, 22)


def item():
    row = ExitReviewRow(
        day=DAY, ticker="032830", name="삼성생명", exit_prompt_version="v2", net_pnl=-17_320.0,
        net_return=-0.0291, peak_return=-0.0073, outcome="no_chance", exit_reason="day_end",
        decision_count=1,
    )
    d = AIExitDecisionRow(
        day=DAY, at=datetime(2026, 9, 22, 10, 20), ticker="032830", name="삼성생명", sell=False,
        ok=True, reason="손실은 손절선이 관리", net_return=-0.0274, peak_return=-0.0073,
        current_price=290_500.0, exit_prompt_version="v2",
    )
    return ExitReviewInput(row=row, decisions=[d], outlook="못 하면 293,500원까지 되밀림", target_sell_price=304_000)


def test_system_prompt_fixes_the_net_profit_yardstick():
    prompt = build_exit_review_system_prompt()
    assert "순손익" in prompt
    assert "덜 잃" in prompt
    assert "그 시점" in prompt


def test_user_prompt_carries_numbers_and_every_decision():
    prompt = build_exit_review_user_prompt([item()])
    assert "032830 삼성생명" in prompt
    assert "-17,320원" in prompt
    assert "-2.91%" in prompt
    assert "고점 -0.73%" in prompt
    assert "10:20" in prompt and "보유" in prompt and "손실은 손절선이 관리" in prompt
    assert "293,500원" in prompt
    assert "304,000원" in prompt


def test_review_returns_none_when_api_raises():
    reviewer = ExitReviewer.__new__(ExitReviewer)
    reviewer.settings = SimpleNamespace(llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    reviewer._client = Boom()
    assert reviewer.review([item()]) is None


def test_review_of_nothing_is_none():
    reviewer = ExitReviewer.__new__(ExitReviewer)
    assert reviewer.review([]) is None
