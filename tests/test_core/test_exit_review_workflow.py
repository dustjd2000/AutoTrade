"""15:35 매도 판단 검증 (스펙 2026-09-22 3절)."""
import sqlite3
from datetime import date, datetime

from src.llm.exit_advisor import make_exit_prompt_store
from src.logger.trade_store import AIExitDecisionRow

from tests.test_core.test_daily_workflow import build_workflow

DAY = date(2026, 9, 22)


class FakeExitReviewer:
    def __init__(self):
        self.result = None
        self.calls = []

    def review(self, items, timeout_seconds=120.0):
        self.calls.append(items)
        return self.result


def build_exit_workflow(tmp_path):
    workflow = build_workflow(tmp_path)
    workflow.exit_reviewer = FakeExitReviewer()
    workflow.exit_prompt_store = make_exit_prompt_store(tmp_path / "exit_prompt")
    return workflow


def insert_trade(workflow, ticker, side, hhmm, price, avg=None, pnl=None, fee=0.0, reason=None, name="삼성생명"):
    with sqlite3.connect(workflow.trade_store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp, name, commission, tax, exit_reason)
               VALUES (?, ?, ?, 'filled', 2, 2, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (f"{ticker}{side}{hhmm}", ticker, side, price, avg, pnl, f"2026-09-22T{hhmm}:00", name, fee, reason),
        )


def samsung_life_day(workflow):
    insert_trade(workflow, "032830", "buy", "09:05", 298_000.0, fee=80.0)
    insert_trade(workflow, "032830", "sell", "15:15", 290_000.0, avg=298_000.0, pnl=-16_000.0, fee=1_240.0, reason="day_end")
    workflow.trade_store.save_position_peak(DAY, "032830", -0.0073, datetime(2026, 9, 22, 9, 20), 297_000.0)
    workflow.trade_store.save_ai_exit_decisions([
        AIExitDecisionRow(
            day=DAY, at=datetime(2026, 9, 22, 10, 20), ticker="032830", name="삼성생명", sell=False,
            ok=True, reason="손실은 손절선이 관리", net_return=-0.0274, peak_return=-0.0073,
            current_price=290_500.0, exit_prompt_version="v2",
        )
    ])


def test_review_saves_rows_and_sends_mail(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)
    workflow.exit_reviewer.result = {"032830": "10:20 하방선 이탈 뒤 보유했다."}

    workflow.review_exits(DAY)

    [row] = workflow.trade_store.exit_reviews_for(DAY)
    assert row.net_pnl == -17_320.0
    assert row.outcome == "no_chance"
    assert row.exit_reason == "day_end"
    assert row.exit_prompt_version == "v2"
    assert row.decision_count == 1
    assert row.review == "10:20 하방선 이탈 뒤 보유했다."
    subject, body, _ = workflow.email.sent[-1]
    assert "매도 판단 검증" in subject and "-17,320원" in subject
    assert "순이익 기회 없음" in body
    assert "10:20" in body and "손실은 손절선이 관리" in body
    assert "10:20 하방선 이탈 뒤 보유했다." in body


def test_review_passes_the_morning_outlook_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)
    workflow.trade_store.save_recommendations(DAY, [], "v11")  # 추천 없음 — 전망 빈 문자열
    workflow.review_exits(DAY)

    [items] = workflow.exit_reviewer.calls
    assert items[0].row.ticker == "032830"
    assert items[0].outlook == ""
    assert [d.reason for d in items[0].decisions] == ["손실은 손절선이 관리"]


def test_no_mail_on_a_day_without_sells(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.review_exits(DAY)
    assert workflow.email.sent == []
    assert workflow.exit_reviewer.calls == []


def test_reviewer_failure_still_sends_numbers(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)

    def boom(items, timeout_seconds=120.0):
        raise RuntimeError("down")

    workflow.exit_reviewer.review = boom
    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].review == ""
    assert "매도 판단 검증" in workflow.email.sent[-1][0]


def test_unknown_rows_are_not_sent_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    insert_trade(workflow, "005930", "sell", "11:00", 70_000.0, avg=None, pnl=None, reason="manual", name="삼성전자")

    workflow.review_exits(DAY)

    assert workflow.exit_reviewer.calls == []
    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "unknown"


def test_review_works_without_a_reviewer(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.exit_reviewer = None
    samsung_life_day(workflow)

    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "no_chance"


# ── 매도 프롬프트 자동 수정 (스펙 4절) · 월 순수익 게이트 (4-A) ──
from types import SimpleNamespace

from src.llm.exit_advisor import EXIT_PROMPT_TEMPLATE_VERSION
from src.logger.trade_store import ExitReviewRow, MonthlySummary

LONG_TIME = "## 시간\n" + ("새 시간 지침 " * 10).strip()


class FakeExitTuner:
    def __init__(self):
        self.result = None
        self.calls = []

    def tune(self, stats, rows, sections, locked_text, why_history="", timeout_seconds=120.0):
        self.calls.append(rows)
        return self.result


def seed_reviews(workflow, count, version=EXIT_PROMPT_TEMPLATE_VERSION, outcome="missed"):
    for i in range(count):
        workflow.trade_store.save_exit_review(
            ExitReviewRow(
                day=date(2026, 9, 1 + i), ticker="005930", name="삼성전자", exit_prompt_version=version,
                net_pnl=-500.0, net_return=-0.005, peak_return=0.015, outcome=outcome,
                exit_reason="day_end", decision_count=5,
            )
        )


def tuning_workflow(tmp_path, reviews=10):
    workflow = build_exit_workflow(tmp_path)
    workflow.exit_tuner = FakeExitTuner()
    seed_reviews(workflow, reviews)
    return workflow


def monthly(workflow, net_pnl):
    workflow.trade_store.monthly_summary = lambda year, month, up_to: MonthlySummary(
        realized_pnl=net_pnl, fees=0.0
    )


def test_exit_tuning_applies_one_section_and_mails(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(change=True, reason="놓침 10건", sections={"time": LONG_TIME})

    workflow.tune_exit_prompt(date(2026, 9, 29))

    assert workflow.exit_prompt_store.load_sections()["time"] == LONG_TIME
    assert workflow.exit_prompt_store.load_version() == "20260929"
    assert "매도 프롬프트 수정" in workflow.email.sent[-1][0]


def test_exit_tuning_waits_for_ten_reviews(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=9)
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_unknown_reviews_do_not_count_toward_the_gate(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=9)
    workflow.trade_store.save_exit_review(
        ExitReviewRow(
            day=date(2026, 9, 20), ticker="000660", name="", exit_prompt_version=EXIT_PROMPT_TEMPLATE_VERSION,
            net_pnl=None, net_return=None, peak_return=None, outcome="unknown", exit_reason="", decision_count=0,
        )
    )
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_rejects_two_sections(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(
        change=True, reason="r", sections={"time": LONG_TIME, "criteria": "## 판단 기준\n" + "x" * 60}
    )
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_prompt_store.load_version() == EXIT_PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []


def test_exit_tuning_is_skipped_when_the_month_is_good(tmp_path):
    """총자산 12,000,000원에 이번 달 순손익 600,000원 → 기준자산 11,400,000원 대비 +5.26%."""
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 600_000.0)
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_runs_below_the_monthly_bar(tmp_path):
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 500_000.0)  # +4.35%
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="표본 부족", sections={})
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert len(workflow.exit_tuner.calls) == 1


def test_exit_tuning_is_skipped_when_the_balance_is_unknown(tmp_path):
    workflow = tuning_workflow(tmp_path)

    def boom():
        raise RuntimeError("token")

    workflow.account.get_balance_snapshot = boom
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_skips_without_a_tuner(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner = None
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.email.sent == []
