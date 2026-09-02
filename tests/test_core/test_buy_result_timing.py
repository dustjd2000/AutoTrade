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
