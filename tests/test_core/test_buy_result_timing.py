"""매수 결과 메일 조기 발송의 판정 — 체결내역 주문번호 대조 (PRD 5.11).

2026-09-01에 되돌린 잔고 대조 방식(cb93b2b)과 다르다. 잔고는 '이 종목을 들고 있는가'에
답할 뿐 '내 주문이 체결됐는가'에 답하지 못해, 이월 보유분이나 다른 인스턴스가 산 물량을
자기 체결로 오인하고 살아 있는 주문을 취소했다.
"""
from datetime import date

from src.core.events import BuyOutcome, BuyRecord, FillRecord, OrderSide

from tests.test_core.test_daily_workflow import (  # noqa: F401  (autouse fixture 재사용)
    buy_records_file,
    make_workflow,
    report_mark,
)

TODAY = date.today()


def ordered(ticker, order_id, quantity=10):
    return BuyRecord(
        ticker=ticker,
        name=ticker,
        outcome=BuyOutcome.ORDERED,
        quantity=quantity,
        reference_price=1000.0,
        order_id=order_id,
    )


def fill(order_id, ticker, filled=10, unfilled=0):
    return FillRecord(
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.BUY,
        filled_quantity=filled,
        filled_price=1000.0,
        unfilled_quantity=unfilled,
    )


def prepare(records, fills):
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    workflow._write_buy_records(12_000_000, 1_000_000, records)
    order_client.fills = fills
    return workflow, order_client


def test_all_orders_fully_filled_is_true():
    workflow, _ = prepare(
        [ordered("005930", "1"), ordered("000660", "2")],
        [fill("1", "005930"), fill("2", "000660")],
    )
    assert workflow.buy_orders_filled(TODAY) is True


def test_partial_fill_is_false():
    """부분체결이면 남은 잔량을 10:10에 취소해야 한다 — 지금 보내면 결과가 확정되지 않는다."""
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [fill("1", "005930", filled=6, unfilled=4)],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_order_missing_from_fills_is_false():
    """체결내역에 흔적조차 없는 주문은 '모르면 기다린다'로 판정한다."""
    workflow, _ = prepare(
        [ordered("005930", "1"), ordered("000660", "2")],
        [fill("1", "005930")],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_zero_filled_quantity_is_false():
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [fill("1", "005930", filled=0, unfilled=10)],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_order_with_both_complete_and_incomplete_rows_is_false():
    """같은 주문번호로 완전체결 행과 미체결 잔량이 남은 행이 함께 잡히면, 후자를 무시하지 않는다.

    ka10076이 한 주문번호에 행을 몇 개 싣는지는 확인된 바가 없다 — 완전체결 행 하나만
    보고 True로 판정하면 다른 행이 알려주는 살아 있는 잔량을 놓치고 주문을 취소해 버린다.
    """
    workflow, _ = prepare(
        [ordered("005930", "1", quantity=10)],
        [
            fill("1", "005930", filled=10, unfilled=0),
            fill("1", "005930", filled=4, unfilled=6),
        ],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_filled_quantity_short_of_ordered_quantity_is_false():
    """미체결 잔량이 0으로 보여도 체결수량이 주문수량에 못 미치면 완료로 보지 않는다.

    HTS에서 취소된 부분체결은 oso_qty가 빈 값으로 와 0으로 읽히므로(to_int), 주문수량과
    대조하지 않으면 실제로는 절반만 체결된 주문을 전량 체결로 오판한다.
    """
    workflow, _ = prepare(
        [ordered("005930", "1", quantity=10)],
        [fill("1", "005930", filled=6, unfilled=0)],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_sell_fill_with_the_same_order_id_is_ignored():
    """매도 체결이 같은 주문번호로 잡혀도 매수 판정에 쓰지 않는다."""
    workflow, _ = prepare(
        [ordered("005930", "1")],
        [
            FillRecord(
                order_id="1",
                ticker="005930",
                side=OrderSide.SELL,
                filled_quantity=10,
                filled_price=1000.0,
                unfilled_quantity=0,
            )
        ],
    )
    assert workflow.buy_orders_filled(TODAY) is False


def test_lookup_failure_is_false():
    """조회가 실패하면 10:10에 맡긴다 — 추측으로 메일을 앞당기지 않는다."""
    workflow, order_client = prepare([ordered("005930", "1")], [])

    def boom():
        raise RuntimeError("체결내역 조회 실패")

    order_client.get_today_fills = boom
    assert workflow.buy_orders_filled(TODAY) is False


def test_no_records_is_false_without_calling_the_api():
    """메일이 나가면 기록이 지워진다 — 그 뒤로 하루 종일 조회가 도는 일이 없어야 한다."""
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    called = []
    order_client.get_today_fills = lambda: called.append(1) or []

    assert workflow.buy_orders_filled(TODAY) is False
    assert called == []


def test_records_without_ordered_rows_are_false_without_calling_the_api():
    """전부 건너뛴 날은 접수한 주문이 없다 — 보낼 결과도, 조회할 것도 없다."""
    workflow, _, order_client, _, _ = make_workflow(recommendations=[])
    workflow._write_buy_records(
        12_000_000,
        1_000_000,
        [
            BuyRecord(
                ticker="005930",
                name="삼성전자",
                outcome=BuyOutcome.SKIPPED,
                reference_price=1000.0,
                note="시가 갭",
            )
        ],
    )
    called = []
    order_client.get_today_fills = lambda: called.append(1) or []

    assert workflow.buy_orders_filled(TODAY) is False
    assert called == []


# ── 감시 태스크 ─────────────────────────────────────────────
import asyncio
import threading
from types import SimpleNamespace

from src.core.runtime import watch_buy_result


def run_watch(filled_sequence, submitted, cycles=5):
    """buy_orders_filled가 순서대로 값을 돌려주게 하고 감시를 몇 바퀴 돌린다."""
    answers = list(filled_sequence)

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(
            buy_orders_filled=lambda: answers.pop(0) if answers else False
        ),
        runner=SimpleNamespace(submit=lambda action: submitted.append(action) or True),
    )

    async def scenario():
        task = asyncio.create_task(watch_buy_result(runtime, interval_seconds=0))
        for _ in range(cycles):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_watch_submits_cancel_unfilled_when_all_orders_are_filled():
    submitted = []
    run_watch([True], submitted)
    assert submitted == ["cancel_unfilled"]


def test_watch_stays_quiet_while_orders_are_unfilled():
    submitted = []
    run_watch([False, False, False], submitted)
    assert submitted == []


def test_watch_survives_a_failing_check():
    """판정이 터져도 감시는 계속 돌아야 한다 — 다음 바퀴에 다시 본다."""
    submitted = []
    answers = [None, True]  # None이면 예외를 던진다

    def check():
        value = answers.pop(0) if answers else False
        if value is None:
            raise RuntimeError("조회 실패")
        return value

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(buy_orders_filled=check),
        runner=SimpleNamespace(submit=lambda action: submitted.append(action) or True),
    )

    async def scenario():
        task = asyncio.create_task(watch_buy_result(runtime, interval_seconds=0))
        for _ in range(6):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert submitted == ["cancel_unfilled"]


def test_buy_orders_filled_check_runs_off_the_loop_thread():
    """체결내역 조회는 페이지네이션 걸린 블로킹 requests 호출이다 — 이벤트 루프에서

    그대로 돌리면 그 사이 WebSocket PING 응답도, 실시간 익절/손절 콜백도 멈춘다
    (`watch_closeout_report`·`watch_cash_refresh`와 같은 이유로 executor에 맡겨야 한다).
    """
    threads = {}

    def fake_filled():
        threads["check"] = threading.get_ident()
        return False

    runtime = SimpleNamespace(
        workflow=SimpleNamespace(buy_orders_filled=fake_filled),
        runner=SimpleNamespace(submit=lambda action: True),
    )

    async def scenario():
        threads["loop"] = threading.get_ident()
        task = asyncio.create_task(watch_buy_result(runtime, interval_seconds=0))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert "check" in threads
    assert threads["check"] != threads["loop"]


def test_watch_submits_only_once_per_day_even_if_still_true():
    """기록 삭제(`_clear_buy_records`)가 OSError로 실패해 판정이 계속 True로 남아도,

    같은 날짜 안에서는 재접수하지 않는다 — 아니면 15:15까지 분당 메일이 나간다.
    """
    submitted = []
    run_watch([True, True, True, True], submitted, cycles=8)
    assert submitted == ["cancel_unfilled"]
