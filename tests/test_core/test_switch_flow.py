"""장중 전환의 매수 단계 — 매도 체결을 기다렸다가 반대 ETF를 산다 (스펙 2026-11-11 5.1·5.3)."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.core.etf_switch import Decision, PendingSwitch, SwitchKind
from src.core.events import BuyOutcome, BuyRecord, FillRecord, OrderSide
from src.core.switch_flow import SwitchFlow, sell_filled

LONG, INVERSE = "069500", "114800"
NOW = datetime(2026, 11, 12, 10, 0, 0)


def pending():
    return PendingSwitch(
        sold_ticker=LONG, sold_summary="(069500)KODEX 200 100주", quantity=100, sell_order_id="S1",
        buy_ticker=INVERSE, buy_name="KODEX 인버스", index_entry=9940.0,
        decision=Decision(SwitchKind.SWITCH, 0.6, 33.0, -0.6), at=NOW,
    )


def sell_fill(filled=100, unfilled=0, order_id="S1"):
    return FillRecord(order_id=order_id, ticker=LONG, side=OrderSide.SELL,
                      filled_quantity=filled, filled_price=9940.0, unfilled_quantity=unfilled)


def bought(outcome=BuyOutcome.ORDERED, unknown=False):
    return (
        BuyRecord(ticker=INVERSE, name="KODEX 인버스", outcome=outcome, quantity=50,
                  reference_price=1000.0, order_id="B1"),
        unknown,
    )


def make(fill_pages, buy_results, now=NOW):
    """`fill_pages`는 체결내역 조회가 차례로 돌려줄 목록들(다 쓰면 마지막 것을 되풀이),
    `buy_results`는 `execute_switch_buy`가 차례로 돌려줄 값(예외 객체면 던진다)."""
    t = SimpleNamespace(notes=[], legs=[], sleeps=[], buys=[], clock=now)
    pages = list(fill_pages)

    def get_today_fills():
        return pages.pop(0) if len(pages) > 1 else pages[0]

    def execute_switch_buy(ticker, today, reason):
        t.buys.append((ticker, today, reason))
        result = buy_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def sleep(seconds):
        t.sleeps.append(seconds)
        t.clock += timedelta(seconds=seconds)

    engine = SimpleNamespace(
        pending_switch=pending(),
        order_client=SimpleNamespace(get_today_fills=get_today_fills),
        notify=t.notes.append,
        begin_switch_leg=lambda leg, ticker, index_entry=None, at=None: t.legs.append(
            (leg, ticker, index_entry)
        ),
    )
    engine.finish_switch = lambda: setattr(engine, "pending_switch", None)
    t.engine = engine
    t.flow = SwitchFlow(
        engine, SimpleNamespace(execute_switch_buy=execute_switch_buy),
        sleep=sleep, now=lambda: t.clock,
    )
    return t


def run(t):
    asyncio.run(t.flow.run())


def test_sell_filled_needs_the_whole_order_and_no_unfilled_rest():
    assert sell_filled([sell_fill()], "S1", 100) is True
    assert sell_filled([sell_fill(filled=60, unfilled=40)], "S1", 100) is False
    assert sell_filled([sell_fill(filled=60)], "S1", 100) is False
    assert sell_filled([sell_fill(order_id="S9")], "S1", 100) is False
    assert sell_filled([], "S1", 100) is False


def test_buys_the_opposite_etf_once_the_sell_is_filled():
    t = make([[sell_fill()]], [bought()])

    run(t)

    assert [(b[0], b[1]) for b in t.buys] == [(INVERSE, NOW.date())]
    assert "10:00 전환" in t.buys[0][2] and "체결강도 33" in t.buys[0][2]
    assert t.legs == [(2, INVERSE, 9940.0)]           # 기준 지수 = 매도 주문 시점 값
    assert len(t.notes) == 1
    assert "장중 전환" in t.notes[0] and "KODEX 인버스" in t.notes[0] and "50주" in t.notes[0]
    assert t.engine.pending_switch is None
    assert t.sleeps == []


def test_waits_for_the_fill_every_five_seconds():
    t = make([[], [sell_fill(filled=60, unfilled=40)], [sell_fill()]], [bought()])

    run(t)

    assert t.sleeps == [5.0, 5.0]
    assert len(t.buys) == 1


def test_slows_to_thirty_seconds_after_the_first_minute():
    t = make([[]] * 13 + [[sell_fill()]], [bought()])

    run(t)

    assert t.sleeps == [5.0] * 12 + [30.0]
    assert len(t.buys) == 1


def test_a_failed_fill_query_counts_as_not_filled_yet():
    t = make([[sell_fill()]], [bought()])
    calls = []
    real = t.engine.order_client.get_today_fills

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("502")
        return real()

    t.engine.order_client.get_today_fills = flaky

    run(t)

    assert t.sleeps == [5.0] and len(t.buys) == 1


def test_gives_up_after_1514_without_buying():
    t = make([[]], [], now=NOW.replace(hour=15, minute=13, second=50))

    run(t)

    assert t.buys == [] and t.legs == []
    assert len(t.notes) == 1 and "[경고]" in t.notes[0] and "사지 않았습니다" in t.notes[0]
    assert t.engine.pending_switch is None


def test_gives_up_five_minutes_after_the_decision_when_the_sell_never_fills():
    t = make([[]], [])

    run(t)

    assert 300 <= sum(t.sleeps) < 330
    assert t.buys == [] and t.legs == []
    assert len(t.notes) == 1 and "[경고]" in t.notes[0] and "사지 않았습니다" in t.notes[0]
    assert t.engine.pending_switch is None


def test_the_1514_cap_still_wins_over_the_five_minute_wait():
    late = NOW.replace(hour=15, minute=12)
    t = make([[]], [], now=late)
    t.engine.pending_switch = PendingSwitch(**{**vars(pending()), "at": late})

    run(t)

    assert 120 <= sum(t.sleeps) < 150                    # 15:14까지 — 5분 상한이 아니다
    assert t.buys == [] and len(t.notes) == 1


def test_a_risk_rejection_without_an_order_is_not_retried():
    rejected = BuyRecord(ticker=INVERSE, name="KODEX 인버스", outcome=BuyOutcome.FAILED, quantity=50,
                         note="리스크 관리 규칙에 걸려 주문하지 않았습니다")
    t = make([[sell_fill()]], [(rejected, False), bought()])

    run(t)

    assert len(t.buys) == 1 and t.sleeps == []
    assert "현금으로 마감" in t.notes[0]


def test_three_failed_buys_end_the_day_in_cash():
    t = make([[sell_fill()]], [bought(BuyOutcome.FAILED), RuntimeError("조회 실패"), bought(BuyOutcome.FAILED)])

    run(t)

    assert len(t.buys) == 3
    assert t.sleeps == [10.0, 10.0]
    assert t.legs == []
    assert len(t.notes) == 1 and "[경고]" in t.notes[0] and "현금으로 마감" in t.notes[0]
    assert t.engine.pending_switch is None


def test_unknown_buy_result_is_not_retried():
    t = make([[sell_fill()]], [bought(BuyOutcome.FAILED, unknown=True), bought()])

    run(t)

    assert len(t.buys) == 1
    assert t.legs == []
    assert len(t.notes) == 1 and "잔고를 확인" in t.notes[0]


def test_unaffordable_buy_is_not_retried():
    t = make([[sell_fill()]], [bought(BuyOutcome.SKIPPED), bought()])

    run(t)

    assert len(t.buys) == 1 and t.sleeps == []
    assert "현금으로 마감" in t.notes[0]


def test_nothing_happens_without_a_pending_switch():
    t = make([[sell_fill()]], [bought()])
    t.engine.pending_switch = None

    run(t)

    assert t.buys == [] and t.notes == []
