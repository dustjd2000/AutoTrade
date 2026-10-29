"""15:40 매도 판단 검증과 매도 프롬프트 자동 수정 (2호용 복구, 2026-10-29, PRD 5.14·5.15)."""
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

from src.llm.exit_advisor import EXIT_PROMPT_TEMPLATE_VERSION, make_exit_prompt_store
from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow, MonthlySummary, TradeStore
from src.strategy.base import StockRecommendation

from tests.test_core.test_daily_workflow import make_workflow

DAY = date(2026, 10, 29)
LONG_TIME = "## 시간\n" + ("새 시간 지침 " * 10).strip()


class FakeExitReviewer:
    def __init__(self):
        self.result = None
        self.calls = []

    def review(self, items, timeout_seconds=120.0):
        self.calls.append(items)
        return self.result


class FakeExitTuner:
    def __init__(self):
        self.result = None
        self.calls = []

    def tune(self, stats, rows, sections, locked_text, why_history="", timeout_seconds=120.0):
        self.calls.append(SimpleNamespace(rows=rows, why_history=why_history))
        return self.result


def build_exit_workflow(tmp_path):
    workflow, email, order_client, _, strategy = make_workflow(recommendations=[])
    workflow.trade_store = TradeStore(tmp_path / "trades.db")
    workflow.exit_reviewer = FakeExitReviewer()
    workflow.exit_tuner = FakeExitTuner()
    workflow.exit_prompt_store = make_exit_prompt_store(tmp_path / "exit_prompt")
    return workflow


def insert_trade(workflow, ticker, side, hhmm, price, avg=None, pnl=None, fee=0.0, reason=None, name="KODEX 200"):
    with sqlite3.connect(workflow.trade_store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp, name, commission, tax, exit_reason)
               VALUES (?, ?, ?, 'filled', 2, 2, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (f"{ticker}{side}{hhmm}", ticker, side, price, avg, pnl, f"{DAY.isoformat()}T{hhmm}:00", name, fee, reason),
        )


def kodex_day(workflow):
    """KODEX 200 한 종목을 사서 15:15에 손실로 청산한 날."""
    insert_trade(workflow, "069500", "buy", "09:05", 36_000.0, fee=80.0)
    insert_trade(workflow, "069500", "sell", "15:15", 35_000.0, avg=36_000.0, pnl=-2_000.0, fee=100.0, reason="day_end")
    workflow.trade_store.save_position_peak(DAY, "069500", -0.0073, datetime(2026, 10, 29, 9, 20), 35_900.0)
    workflow.trade_store.save_ai_exit_decisions([
        AIExitDecisionRow(
            day=DAY, at=datetime(2026, 10, 29, 10, 20), ticker="069500", name="KODEX 200", sell=False,
            ok=True, reason="지수 방향 확인 필요", net_return=-0.0274, peak_return=-0.0073,
            current_price=35_100.0, exit_prompt_version=EXIT_PROMPT_TEMPLATE_VERSION,
        )
    ])


def review_row(day, ticker="069500", version=EXIT_PROMPT_TEMPLATE_VERSION, outcome="missed"):
    return ExitReviewRow(
        day=day, ticker=ticker, name="", exit_prompt_version=version, net_pnl=-500.0, net_return=-0.005,
        peak_return=0.015, outcome=outcome, exit_reason="day_end", decision_count=5,
    )


def seed_reviews(workflow, count, ticker="069500", version=EXIT_PROMPT_TEMPLATE_VERSION, outcome="missed"):
    for i in range(count):
        workflow.trade_store.save_exit_review(review_row(date(2026, 10, 1 + i), ticker, version, outcome))


def monthly(workflow, net_pnl):
    workflow.trade_store.monthly_summary = lambda year, month, up_to: MonthlySummary(
        realized_pnl=net_pnl, fees=0.0
    )


def _review_mail_body(workflow):
    [mail] = [m for m in workflow.email.sent if "매도 판단 검증" in m[0]]
    return mail[1]


# ── 매도 판단 검증 ──
def test_review_saves_rows_and_sends_mail(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)
    workflow.exit_reviewer.result = {"069500": "10:20 지수 방향을 확인하지 않고 보유했다."}

    workflow.review_exits(DAY)

    [row] = workflow.trade_store.exit_reviews_for(DAY)
    assert row.ticker == "069500"
    assert row.net_pnl == -2_180.0
    assert row.outcome == "no_chance"
    assert row.exit_reason == "day_end"
    assert row.exit_prompt_version == EXIT_PROMPT_TEMPLATE_VERSION
    assert row.decision_count == 1
    assert row.review == "10:20 지수 방향을 확인하지 않고 보유했다."
    subject, body, _ = workflow.email.sent[-1]
    assert "매도 판단 검증" in subject and "-2,180원" in subject
    assert "순이익 기회 없음" in body
    assert "10:20" in body and "지수 방향 확인 필요" in body


def test_review_runs_for_the_etf_strategy(tmp_path):
    """1호 시절에는 2호 모드에서 통째로 건너뛰었다 — 2호도 AI 매도 판단이 돌므로 이제 돈다."""
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)
    workflow.review_exits(DAY)
    assert len(workflow.trade_store.exit_reviews_for(DAY)) == 1


def test_review_ignores_non_etf_tickers(tmp_path):
    """사용자가 직접 판 주식 같은 ETF 밖 종목은 2호 매도 판단의 표본이 아니다."""
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)
    insert_trade(workflow, "005930", "sell", "11:00", 70_000.0, avg=71_000.0, pnl=-2_000.0, reason="manual", name="삼성전자")

    workflow.review_exits(DAY)

    assert [r.ticker for r in workflow.trade_store.exit_reviews_for(DAY)] == ["069500"]


def test_review_passes_the_morning_decision_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)
    workflow.strategy.set_recommendations([
        StockRecommendation(
            ticker="069500", name="KODEX 200", target_price=36_000, reason="직전 20거래일 장중 평균 +0.300%",
            outlook="시나리오는 '최근 20거래일의 장중 흐름이 오늘도 이어진다'",
        )
    ])

    workflow.review_exits(DAY)

    [items] = workflow.exit_reviewer.calls
    assert items[0].row.ticker == "069500"
    assert "장중 흐름이 오늘도 이어진다" in items[0].outlook
    assert items[0].target_sell_price == 0
    assert [d.reason for d in items[0].decisions] == ["지수 방향 확인 필요"]


def test_review_passes_empty_outlook_after_an_engine_restart(tmp_path):
    """엔진이 재시작돼 오늘 결정(메모리)이 비었으면 전망 칸은 빈 문자열이다."""
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)

    workflow.review_exits(DAY)

    [items] = workflow.exit_reviewer.calls
    assert items[0].outlook == "" and items[0].target_sell_price == 0


def test_no_mail_on_a_day_without_sells(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.review_exits(DAY)
    assert workflow.email.sent == []
    assert workflow.exit_reviewer.calls == []


def test_reviewer_failure_still_sends_numbers(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    kodex_day(workflow)

    def boom(items, timeout_seconds=120.0):
        raise RuntimeError("down")

    workflow.exit_reviewer.review = boom
    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].review == ""
    assert "매도 판단 검증" in workflow.email.sent[-1][0]


def test_partial_unknown_pnl_saves_outcome_unknown(tmp_path):
    """같은 종목의 매도 두 건 중 한 건만 realized_pnl을 몰라도 그 종목 전체가 '순손익 모름'이다."""
    workflow = build_exit_workflow(tmp_path)
    insert_trade(workflow, "069500", "buy", "09:05", 36_000.0, fee=80.0)
    insert_trade(workflow, "069500", "sell", "13:00", 35_500.0, avg=36_000.0, pnl=-1_000.0, fee=60.0, reason="ai_judgment")
    insert_trade(workflow, "069500", "sell", "15:15", 35_000.0, avg=36_000.0, pnl=None, fee=64.0, reason="day_end")

    workflow.review_exits(DAY)

    [row] = workflow.trade_store.exit_reviews_for(DAY)
    assert row.outcome == "unknown"
    assert row.net_pnl is None


def test_unknown_rows_are_not_sent_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    insert_trade(workflow, "069500", "sell", "11:00", 36_000.0, avg=None, pnl=None, reason="manual")

    workflow.review_exits(DAY)

    assert workflow.exit_reviewer.calls == []
    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "unknown"


def test_review_works_without_a_reviewer(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.exit_reviewer = None
    kodex_day(workflow)

    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "no_chance"


# ── 매도 프롬프트 자동 수정 (PRD 5.15) · 월 순수익 게이트 (4-A) ──
def tuning_workflow(tmp_path, reviews=5):
    workflow = build_exit_workflow(tmp_path)
    seed_reviews(workflow, reviews)
    return workflow


def test_exit_tuning_applies_one_section_and_mails(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(change=True, reason="놓침 5건", sections={"time": LONG_TIME})

    outcome = workflow.tune_exit_prompt(date(2026, 10, 30))

    assert outcome.changed and outcome.old_version == EXIT_PROMPT_TEMPLATE_VERSION
    assert workflow.exit_prompt_store.load_sections()["time"] == LONG_TIME
    assert workflow.exit_prompt_store.load_version() == "20261030"
    assert "매도 프롬프트 수정" in workflow.email.sent[-1][0]


def test_exit_tuning_gate_is_five_reviews(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=4)
    outcome = workflow.tune_exit_prompt(date(2026, 10, 30))
    assert workflow.exit_tuner.calls == []
    assert not outcome.changed and "4건 < 기준 5건" in outcome.reason


def test_old_stock_rows_do_not_count_toward_the_gate(tmp_path):
    """exit_reviews에 남은 옛 1호 주식 행(같은 버전 이름이라도)은 표본이 아니다."""
    workflow = tuning_workflow(tmp_path, reviews=3)
    seed_reviews(workflow, 10, ticker="032830")

    workflow.tune_exit_prompt(date(2026, 10, 30))

    assert workflow.exit_tuner.calls == []


def test_old_stock_rows_are_not_shown_to_the_tuner(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=5)
    workflow.trade_store.save_exit_review(review_row(date(2026, 10, 20), ticker="032830"))
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="r", sections={})

    workflow.tune_exit_prompt(date(2026, 10, 30))

    [call] = workflow.exit_tuner.calls
    assert {r.ticker for r in call.rows} == {"069500"}


def test_unknown_reviews_do_not_count_toward_the_gate(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=4)
    workflow.trade_store.save_exit_review(review_row(date(2026, 10, 20), outcome="unknown"))
    workflow.tune_exit_prompt(date(2026, 10, 30))
    assert workflow.exit_tuner.calls == []


def test_tuner_gets_the_code_change_history_before_its_own_history(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_prompt_store.save({"time": LONG_TIME}, "첫 자동 수정", date(2026, 10, 29))
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="r", sections={})
    seed_reviews(workflow, 5, version="20261029")  # 새 버전 표본

    workflow.tune_exit_prompt(date(2026, 10, 30))

    [call] = workflow.exit_tuner.calls
    assert call.why_history.index("v6 (2026-10-29") < call.why_history.index("첫 자동 수정")
    assert "v5 (2026-10-28" in call.why_history


def test_exit_tuning_rejects_two_sections(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(
        change=True, reason="r", sections={"time": LONG_TIME, "criteria": "## 판단 기준\n" + "x" * 60}
    )
    outcome = workflow.tune_exit_prompt(date(2026, 10, 30))
    assert not outcome.changed
    assert workflow.exit_prompt_store.load_version() == EXIT_PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []


def test_exit_tuning_is_skipped_when_the_month_is_good(tmp_path):
    """총자산 12,000,000원에 이번 달 순손익 600,000원 → 기준자산 11,400,000원 대비 +5.26%."""
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 600_000.0)
    workflow.tune_exit_prompt(date(2026, 10, 30))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_runs_below_the_monthly_bar(tmp_path):
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 500_000.0)  # +4.35%
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="표본 부족", sections={})
    workflow.tune_exit_prompt(date(2026, 10, 30))
    assert len(workflow.exit_tuner.calls) == 1


def test_exit_tuning_is_skipped_when_the_balance_is_unknown(tmp_path):
    workflow = tuning_workflow(tmp_path)

    def boom():
        raise RuntimeError("token")

    workflow.account.get_balance_snapshot = boom
    workflow.tune_exit_prompt(date(2026, 10, 30))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_skips_without_a_tuner(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner = None
    assert workflow.tune_exit_prompt(date(2026, 10, 30)) is None
    assert workflow.email.sent == []


def test_exit_tuning_ignores_stale_prompt_files_written_for_an_older_code_version(tmp_path):
    """사람이 코드 프롬프트를 고친 뒤(v6)에는 옛 자동 수정 파일 위에서 고치지 않는다."""
    workflow = tuning_workflow(tmp_path)
    folder = tmp_path / "exit_prompt"
    folder.mkdir()
    (folder / "time.md").write_text("## 시간\n옛 자동 수정본", encoding="utf-8")
    (folder / "version").write_text("20261001", encoding="utf-8")  # base 파일 없음 = 옛 파일
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="r", sections={})

    workflow.tune_exit_prompt(date(2026, 10, 30))

    # 코드 버전(v6) 기준으로 표본을 세고, 튜너에는 코드 기본 절이 간다
    assert len(workflow.exit_tuner.calls) == 1
    assert workflow.exit_prompt_store.load_version() == EXIT_PROMPT_TEMPLATE_VERSION


# ── 검증 메일에 매도 프롬프트 자동 수정 결과를 싣는다 ──
def test_exit_review_mail_reports_the_prompt_change(tmp_path):
    workflow = tuning_workflow(tmp_path)
    kodex_day(workflow)
    workflow.exit_tuner.result = SimpleNamespace(change=True, reason="놓침 5건", sections={"time": LONG_TIME})

    workflow.review_exits(DAY)

    assert len(workflow.exit_tuner.calls) == 1, "검증이 자동 수정을 직접 부른다"
    body = _review_mail_body(workflow)
    assert f"수정함 ({EXIT_PROMPT_TEMPLATE_VERSION} → 20261029)" in body
    assert "놓침 5건" in body
    assert any("매도 프롬프트 수정" in m[0] for m in workflow.email.sent)


def test_exit_review_mail_reports_the_sample_gate(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=3)
    kodex_day(workflow)  # 오늘 1건이 더해져도 4건 < 5건

    workflow.review_exits(DAY)

    assert workflow.exit_tuner.calls == []
    body = _review_mail_body(workflow)
    assert "수정 안 함 — 현 버전" in body and "4건" in body


def test_exit_tuning_still_runs_on_a_day_without_sells(tmp_path):
    """매도가 없는 날도 쌓인 표본으로는 고칠 수 있다."""
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="r", sections={})

    workflow.review_exits(DAY)

    assert len(workflow.exit_tuner.calls) == 1
    assert workflow.email.sent == []


def test_review_exits_is_scheduled_after_the_daily_report():
    """체결이 확정된 뒤에 대조해야 한다 — 리포트(15:35) 다음 시각이다."""
    from src.core import runtime

    assert runtime.REVIEW_EXITS_TIME > runtime.REPORT_TIME
