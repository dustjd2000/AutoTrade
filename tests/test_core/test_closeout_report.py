"""보유 종목 전량 매도 시 결과 리포트 — 15:30을 기다리지 않고 보내고, 15:30은 생략한다."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.api.account import Position
from src.core import daily_workflow
from src.core.daily_workflow import DailyWorkflow
from src.core.engine import TradingEngine
from src.core.events import OrderResult, OrderStatus
from src.core.runtime import CLOSEOUT_SETTLE_SECONDS, closeout_report_due
from src.logger.trade_store import DailySummary, MonthlySummary


@pytest.fixture(autouse=True)
def report_mark(tmp_path, monkeypatch):
    """최종 리포트 발송 표시를 테스트마다 격리한다 (실제 data/ 를 건드리지 않도록)."""
    path = tmp_path / "final_report_sent"
    monkeypatch.setattr(daily_workflow, "DEFAULT_REPORT_MARK_PATH", path)
    return path


def make_engine(positions, sell_status=OrderStatus.FILLED):
    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(
            send_order=lambda r: OrderResult(
                order_id="1",
                ticker=r.ticker,
                side=r.side,
                status=sell_status,
                quantity=r.quantity,
                filled_quantity=r.quantity,
                filled_price=1000.0,
                error_message=None if sell_status == OrderStatus.FILLED else "거부",
            )
        ),
        account=SimpleNamespace(
            get_positions=lambda: positions,
            get_balance_snapshot=lambda: SimpleNamespace(
                total_asset=1_000_000, cash=1_000_000, positions=positions
            ),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_exit=lambda p: None,
        ),
    )


def held(ticker):
    return Position(ticker=ticker, quantity=10, avg_price=70000.0, current_price=71000.0)


# ── 엔진: 전량 매도 시각 기록 ───────────────────────────────
def test_closed_out_at_is_set_when_last_position_is_sold():
    engine = make_engine({"005930": held("005930")})
    engine.start()
    assert engine.closed_out_at is None

    engine.force_close_all_positions(reason="day_end")

    assert engine.closed_out_at is not None


def test_closed_out_at_stays_unset_while_a_position_remains():
    """한 종목이라도 남아 있으면 전량 매도가 아니다 — 매도가 거부된 경우."""
    engine = make_engine({"005930": held("005930")}, sell_status=OrderStatus.REJECTED)
    engine.start()

    engine.force_close_all_positions(reason="day_end")

    assert engine.open_tickers == ["005930"]
    assert engine.closed_out_at is None


def test_buying_again_clears_closed_out_at():
    """다시 매수하면 전량 매도 상태가 아니므로 리포트 근거를 되돌린다."""
    engine = make_engine({"005930": held("005930")})
    engine.start()
    engine.force_close_all_positions(reason="day_end")
    assert engine.closed_out_at is not None

    engine.note_open_position("000660")

    assert engine.closed_out_at is None


def test_new_day_clears_closed_out_at():
    engine = make_engine({})
    engine.start()
    engine._closed_out_at = datetime.now()

    engine.reset_for_new_day()

    assert engine.closed_out_at is None


def test_excluded_position_does_not_block_closeout():
    """매도 불가로 보유 목록에서 제외된 건은 보유로 보지 않는다."""
    engine = make_engine({"005930": held("005930"), "118970": held("118970")})
    engine.start()
    engine._exclude_untradable("118970", "(118970)", "종목 정보가 조회되지 않습니다")

    engine.force_close_all_positions(reason="day_end")

    assert engine.closed_out_at is not None


# ── 발송 시점 판정 ──────────────────────────────────────────
NOW = datetime(2026, 7, 29, 11, 0)


def make_runtime(closed_out_at, open_tickers=()):
    return SimpleNamespace(
        engine=SimpleNamespace(closed_out_at=closed_out_at, open_tickers=list(open_tickers))
    )


def test_not_due_without_closeout():
    assert closeout_report_due(make_runtime(None), NOW) is False


def test_not_due_before_fills_settle():
    """접수 직후 집계하면 방금 판 종목이 '보유중'으로 실린다."""
    runtime = make_runtime(NOW - timedelta(seconds=CLOSEOUT_SETTLE_SECONDS - 5))
    assert closeout_report_due(runtime, NOW) is False


def test_due_after_fills_settle():
    runtime = make_runtime(NOW - timedelta(seconds=CLOSEOUT_SETTLE_SECONDS))
    assert closeout_report_due(runtime, NOW) is True


def test_not_due_while_balance_still_shows_holding():
    """체결이 잔고에 반영되지 않았거나 다시 매수된 상태 — 아직 결과가 확정되지 않았다."""
    runtime = make_runtime(NOW - timedelta(minutes=10), open_tickers=["005930"])
    assert closeout_report_due(runtime, NOW) is False


def test_not_due_for_yesterdays_closeout():
    """자정을 넘겨 앱이 켜져 있어도 어제 청산으로 오늘 리포트를 보내지 않는다."""
    runtime = make_runtime(NOW - timedelta(days=1))
    assert closeout_report_due(runtime, NOW) is False


# ── 워크플로: 하루 한 번, 매수하면 초기화 ───────────────────
REPORT_DAY = date(2026, 7, 29)


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None):
        self.sent.append((subject, message, html))


def make_workflow():
    email = FakeEmail()
    workflow = DailyWorkflow(
        collector=SimpleNamespace(collect=lambda: []),
        recommender=SimpleNamespace(recommend=lambda d: []),
        strategy=SimpleNamespace(),
        engine=SimpleNamespace(
            order_client=SimpleNamespace(get_today_fills=lambda: []),
            unsellable_snapshot=lambda: [],
        ),
        account=SimpleNamespace(
            get_balance_snapshot=lambda: SimpleNamespace(total_asset=1_000_000, cash=500_000)
        ),
        trade_store=SimpleNamespace(
            apply_fills=lambda fills, day: 0,
            daily_summary=lambda day: DailySummary(
                day=day, buy_count=1, sell_count=1, realized_pnl=1000.0
            ),
            monthly_summary=lambda year, month, up_to: MonthlySummary(
                realized_pnl=1000.0, fees=100.0
            ),
        ),
        email=email,
    )
    return workflow, email


def test_scheduled_report_is_skipped_after_closeout_report():
    workflow, email = make_workflow()

    workflow.send_final_report(REPORT_DAY, closed_out=True)
    workflow.send_final_report(REPORT_DAY)  # 15:30 스케줄

    assert len(email.sent) == 1
    _, body, _ = email.sent[0]
    assert "전부 매도한 직후 집계" in body


def test_closeout_report_is_skipped_after_scheduled_report():
    """15:30이 먼저 나갔으면 뒤늦은 청산 트리거가 같은 리포트를 또 보내지 않는다."""
    workflow, email = make_workflow()

    workflow.send_final_report(REPORT_DAY)
    workflow.send_final_report(REPORT_DAY, closed_out=True)

    assert len(email.sent) == 1
    _, body, _ = email.sent[0]
    assert "정규장 마감(15:30) 직전 집계" in body


def test_scheduled_report_is_skipped_after_engine_restart():
    """설정 저장·앱 재실행으로 워크플로가 새로 만들어져도 이미 보낸 리포트는 다시 보내지 않는다.

    발송 표시가 인메모리 필드였을 때 오전 청산 리포트와 15:30 리포트가 둘 다 나갔다.
    """
    workflow, email = make_workflow()
    workflow.send_final_report(REPORT_DAY, closed_out=True)

    restarted, restarted_email = make_workflow()
    restarted.send_final_report(REPORT_DAY)  # 15:30 스케줄

    assert len(email.sent) == 1
    assert restarted_email.sent == []


def test_failed_send_leaves_report_pending():
    """발송이 실패하면 표시를 세우지 않아 15:30이 다시 시도한다."""
    workflow, email = make_workflow()
    calls = []

    def flaky(subject, message, html=None):
        calls.append(subject)
        if len(calls) == 1:
            raise RuntimeError("SMTP 실패")
        email.sent.append((subject, message, html))

    workflow.email = SimpleNamespace(send=flaky)

    try:
        workflow.send_final_report(REPORT_DAY, closed_out=True)
    except RuntimeError:
        pass
    workflow.send_final_report(REPORT_DAY)

    assert len(calls) == 2


def test_manual_report_does_not_suppress_scheduled_report():
    """④ 즉시 실행 버튼은 사용자가 직접 누른 것이므로 15:30을 죽이지 않는다."""
    workflow, email = make_workflow()

    workflow.send_daily_report(REPORT_DAY)
    workflow.send_final_report(REPORT_DAY)

    assert len(email.sent) == 2
