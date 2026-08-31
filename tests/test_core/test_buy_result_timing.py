"""매수 결과 메일 시점 — 접수분이 전부 체결되면 10:10을 기다리지 않고 보낸다."""
import asyncio
from datetime import date
from types import SimpleNamespace

from src.api.account import Position
from src.core.runtime import watch_buy_result

from tests.test_core.test_daily_workflow import (  # noqa: F401  (autouse fixture 재사용)
    board_recs,
    buy_records_file,
    make_workflow,
    report_mark,
)

BOARD_DAY = date(2026, 8, 24)


def held(ticker, quantity):
    return Position(
        ticker=ticker, quantity=quantity, avg_price=1000.0, current_price=1000.0
    )


def ordered_workflow(positions=()):
    """매수를 접수한 직후 상태의 워크플로 — `positions`가 그 시점의 잔고다."""
    recs = board_recs()
    workflow, email, order_client, _, strategy = make_workflow(recommendations=recs)
    strategy.set_recommendations(recs)
    workflow.engine.position_snapshot = lambda: list(positions)
    workflow.execute_buys()
    return workflow, email, order_client


# ── 잔고 대조 판정 ──────────────────────────────────────────
def test_not_settled_while_the_order_is_unfilled():
    workflow, _, _ = ordered_workflow()

    assert workflow.buy_orders_settled() is False


def test_settled_when_the_whole_order_is_in_the_balance():
    """종목당 200만 ÷ 1,000원 = 2,000주가 잔고에 잡히면 기다릴 이유가 없다."""
    workflow, _, _ = ordered_workflow([held("005930", 2000)])

    assert workflow.buy_orders_settled() is True


def test_not_settled_on_a_partial_fill():
    """부분체결은 남은 잔량이 아직 미체결이다 — 10:10까지 기다린다."""
    workflow, _, _ = ordered_workflow([held("005930", 1200)])

    assert workflow.buy_orders_settled() is False


def test_not_settled_without_any_order():
    """주문 전(매수 대기)에는 앞당길 결과 자체가 없다."""
    workflow, _, _, _, _ = make_workflow(recommendations=board_recs())
    workflow.recommend_and_notify(today=BOARD_DAY)

    assert workflow.buy_orders_settled(today=BOARD_DAY) is False


def test_not_settled_on_a_different_day():
    """엔진이 재시작돼 표가 비면 판정하지 않는다 — 10:10이 파일 기록으로 마무리한다."""
    workflow, _, _ = ordered_workflow([held("005930", 2000)])

    assert workflow.buy_orders_settled(today=date(2026, 8, 25)) is False


# ── 감시 코루틴 ─────────────────────────────────────────────
def run_watch(runtime, seconds, settle_seconds=0.02):
    async def main():
        task = asyncio.ensure_future(
            watch_buy_result(runtime, interval_seconds=0.01, settle_seconds=settle_seconds)
        )
        await asyncio.sleep(seconds)
        task.cancel()

    asyncio.run(main())


def make_runtime(settled):
    calls = []
    state = {"settled": settled}

    def cancel_unfilled_buys():
        calls.append(1)
        state["settled"] = False  # 메일을 보내고 나면 표가 체결로 바뀐다

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(
            buy_orders_settled=lambda: state["settled"],
            cancel_unfilled_buys=cancel_unfilled_buys,
        )
    )
    return runtime, calls


def test_result_mail_is_sent_once_when_all_orders_are_filled():
    runtime, calls = make_runtime(settled=True)

    run_watch(runtime, seconds=0.2)

    assert calls == [1]


def test_nothing_is_sent_while_an_order_is_unfilled():
    runtime, calls = make_runtime(settled=False)

    run_watch(runtime, seconds=0.2)

    assert calls == []


def test_sending_waits_for_the_fills_to_settle():
    """잔고에 잡히자마자 보내면 체결내역 조회가 아직 비어 체결가 없는 메일이 나간다."""
    runtime, calls = make_runtime(settled=True)

    run_watch(runtime, seconds=0.1, settle_seconds=10)

    assert calls == []
