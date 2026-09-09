"""AI 매도 판단 주기 워처 (PRD 5.5-B 'AI 매도 판단').

익절 자동 청산이 빠진 자리를 대신하는 워처라, "부르지 않아야 할 때 안 부르는지"와
"불렀을 때 정확히 옳은 사유로 매도(또는 보유)하는지"를 각각 확인한다.

게이트(`ai_exit_due`)와 실행(`run_ai_exit_cycle`)을 분리해 각각 가짜 런타임으로 한
사이클만 돌려 검증한다 — test_quote_watchdog.py가 순수 함수 + 가짜 런타임으로 워처를
검증하는 방식과 같다. "부르지 않는다"는 결과(매도 여부)만으로 판단하지 않고, 가짜
advisor의 호출 기록을 직접 확인한다 — 결과만 보면 게이트가 아니라 advisor의 판단
자체가 우연히 "팔지 않음"이었을 가능성을 배제하지 못한다.
"""
import asyncio
import threading
from datetime import datetime, timedelta, time as dt_time
from types import SimpleNamespace

import pytest

from src.api.account import Position
from src.core.events import ExitReason, MarketData
from src.core.exit_trace import ExitTrace
from src.core.runtime import (
    AI_EXIT_END_TIME,
    AI_EXIT_START_DELAY_MINUTES,
    ai_exit_call_limit,
    ai_exit_due,
    ai_exit_window_open,
    maybe_run_ai_exit_cycle,
    run_ai_exit_cycle,
    watch_ai_exit,
)
from src.llm.exit_advisor import ExitDecision

from tests.test_core.test_portfolio_exit import make_engine, two_holdings

# 2026-09-09는 수요일(거래일). 기본 매수 시각 09:08 + 15분 = 09:23이 창의 시작이다.
BUY_TIME = dt_time(9, 8)
IN_WINDOW = datetime(2026, 9, 9, 10, 0)
WINDOW_START = datetime(2026, 9, 9, 9, 23)
WINDOW_END = datetime(2026, 9, 9, 15, 0)
BEFORE_WINDOW = datetime(2026, 9, 9, 9, 20)
AFTER_WINDOW = datetime(2026, 9, 9, 15, 5)
SATURDAY = datetime(2026, 9, 12, 10, 0)  # 창 시간대이지만 거래일이 아니다

DEFAULT_INTERVAL_MINUTES = 15
# buy_time 09:08(+15분=09:23) ~ 15:00, 15분 주기 → (900-563)//15 + 1 = 23
DEFAULT_CALL_LIMIT = 23


def holding(ticker="005930", name="삼성전자", quantity=10, avg_price=1000.0, current_price=1010.0):
    return Position(
        ticker=ticker, quantity=quantity, avg_price=avg_price, current_price=current_price, name=name
    )


class FakeEngine:
    """`watch_ai_exit`이 실제로 쓰는 TradingEngine 표면만 흉내 낸 가짜 엔진.

    `exit_candidates(force=True)`가 `force=False`와 다른 값을 돌려주도록
    `fresh_holdings`를 따로 둘 수 있다 — LLM 응답을 기다리는 동안 보유가 바뀐 상황을
    흉내 낸다.
    """

    def __init__(
        self,
        holdings=None,
        fresh_holdings=None,
        ai_exit_enabled=True,
        ai_exit_calls=0,
        portfolio_return=0.01,
    ):
        self.ai_exit_enabled = ai_exit_enabled
        self._ai_exit_calls = ai_exit_calls
        self._holdings = list(holdings or [])
        self._fresh_holdings = list(holdings or []) if fresh_holdings is None else list(fresh_holdings)
        self.open_tickers = [p.ticker for p in self._holdings]
        self.exit_trace = ExitTrace()
        self.risk_manager = SimpleNamespace(
            portfolio_return=lambda hs: portfolio_return,
            stop_loss_ratio=0.02,
            take_profit_ratio=0.005,
        )
        self.trade_store = SimpleNamespace(recommendations_for=lambda day: [])
        self.exit_candidates_calls = []  # force 인자 기록
        self.executed = []  # [(holdings, reason), ...] — _execute_portfolio_exit 호출 기록

    @property
    def ai_exit_calls(self):
        return self._ai_exit_calls

    def note_ai_exit_call(self):
        self._ai_exit_calls += 1

    def exit_candidates(self, force=False):
        self.exit_candidates_calls.append(force)
        return list(self._fresh_holdings if force else self._holdings)

    def position_net_return(self, position):
        return 0.0  # 값 자체는 이 파일의 관심사가 아니다 (조립·배선만 확인한다)

    def _execute_portfolio_exit(self, holdings, reason):
        self.executed.append((holdings, reason))


class SpyAdvisor:
    """decide 호출 여부·인자·호출 스레드를 기록하는 가짜 ExitAdvisor."""

    def __init__(self, result=None):
        self.result = result
        self.calls = []
        self.threads = []

    def decide(
        self,
        holdings,
        trace,
        portfolio_return,
        stop_loss_ratio,
        take_profit_ratio,
        minutes_to_close,
        partial,
        timeout_seconds=120.0,
    ):
        self.threads.append(threading.get_ident())
        self.calls.append(
            dict(
                holdings=holdings,
                trace=trace,
                portfolio_return=portfolio_return,
                stop_loss_ratio=stop_loss_ratio,
                take_profit_ratio=take_profit_ratio,
                minutes_to_close=minutes_to_close,
                partial=partial,
            )
        )
        return self.result


def make_runtime(
    holdings=None,
    fresh_holdings=None,
    ai_exit_enabled=True,
    ai_exit_calls=0,
    portfolio_return=0.01,
    buy_time=BUY_TIME,
    interval_minutes=DEFAULT_INTERVAL_MINUTES,
    decide_result=None,
):
    engine = FakeEngine(
        holdings=holdings,
        fresh_holdings=fresh_holdings,
        ai_exit_enabled=ai_exit_enabled,
        ai_exit_calls=ai_exit_calls,
        portfolio_return=portfolio_return,
    )
    settings = SimpleNamespace(buy_time=buy_time, ai_exit_interval_minutes=interval_minutes)
    advisor = SpyAdvisor(result=decide_result)
    runtime = SimpleNamespace(settings=settings, engine=engine, exit_advisor=advisor)
    return runtime, engine, advisor


# ── 순수 함수: 호출 창 / 하루 상한 ──────────────────────────────
def test_ai_exit_window_boundaries():
    settings = SimpleNamespace(buy_time=BUY_TIME, ai_exit_interval_minutes=DEFAULT_INTERVAL_MINUTES)
    assert ai_exit_window_open(settings, BEFORE_WINDOW) is False
    assert ai_exit_window_open(settings, WINDOW_START) is True
    assert ai_exit_window_open(settings, WINDOW_END) is True  # 끝 시각 포함
    assert ai_exit_window_open(settings, AFTER_WINDOW) is False


def test_ai_exit_call_limit_is_window_over_interval_plus_one():
    settings = SimpleNamespace(buy_time=BUY_TIME, ai_exit_interval_minutes=DEFAULT_INTERVAL_MINUTES)
    assert ai_exit_call_limit(settings) == DEFAULT_CALL_LIMIT


# ── 게이트: 호출하지 않아야 할 사이클 (advisor 호출 자체를 확인) ──────
def test_no_call_without_holdings():
    runtime, engine, advisor = make_runtime(holdings=[])
    result = asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))
    assert advisor.calls == []
    assert result is None


def test_no_call_when_ai_exit_disabled():
    runtime, engine, advisor = make_runtime(holdings=[holding()], ai_exit_enabled=False)
    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))
    assert advisor.calls == []


def test_no_call_before_window_opens():
    runtime, engine, advisor = make_runtime(holdings=[holding()])
    asyncio.run(maybe_run_ai_exit_cycle(runtime, BEFORE_WINDOW, None))
    assert advisor.calls == []


def test_no_call_after_window_closes():
    runtime, engine, advisor = make_runtime(holdings=[holding()])
    asyncio.run(maybe_run_ai_exit_cycle(runtime, AFTER_WINDOW, None))
    assert advisor.calls == []


def test_no_call_on_a_non_trading_day():
    """창 시간대(10:00)라도 거래일이 아니면 부르지 않는다."""
    runtime, engine, advisor = make_runtime(holdings=[holding()])
    asyncio.run(maybe_run_ai_exit_cycle(runtime, SATURDAY, None))
    assert advisor.calls == []


def test_no_call_before_the_interval_elapses():
    runtime, engine, advisor = make_runtime(holdings=[holding()])
    last_called_at = IN_WINDOW - timedelta(minutes=5)  # 주기(15분) 미만 경과
    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, last_called_at))
    assert advisor.calls == []


def test_no_call_over_the_daily_cap():
    runtime, engine, advisor = make_runtime(holdings=[holding()], ai_exit_calls=DEFAULT_CALL_LIMIT)
    asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, None))
    assert advisor.calls == []


def test_call_happens_once_every_condition_is_satisfied():
    """양성 대조군 — 위 게이트 테스트들이 우연히 통과한 게 아님을 확인한다."""
    runtime, engine, advisor = make_runtime(
        holdings=[holding()], decide_result=ExitDecision(sell=False, reason="관망")
    )
    last_called_at = IN_WINDOW - timedelta(minutes=DEFAULT_INTERVAL_MINUTES)  # 정확히 주기만큼 경과
    result = asyncio.run(maybe_run_ai_exit_cycle(runtime, IN_WINDOW, last_called_at))
    assert len(advisor.calls) == 1
    assert result == IN_WINDOW


# ── 한 사이클의 동작 (게이트를 통과했다고 가정하고 run_ai_exit_cycle을 직접 돈다) ──
def test_cycle_appends_one_trace_point():
    runtime, engine, advisor = make_runtime(
        holdings=[holding()], decide_result=ExitDecision(sell=False, reason="관망")
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert engine.exit_trace.count == 1
    [point] = engine.exit_trace.points()
    assert point.at == IN_WINDOW
    assert engine.executed == []


def test_sell_true_executes_portfolio_exit_with_ai_judgment_reason():
    h = holding()
    runtime, engine, advisor = make_runtime(
        holdings=[h], decide_result=ExitDecision(sell=True, reason="추세 이탈")
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert len(engine.executed) == 1
    sold_holdings, reason = engine.executed[0]
    assert reason is ExitReason.AI_JUDGMENT
    assert [p.ticker for p in sold_holdings] == [h.ticker]


def test_sell_false_does_not_execute_anything():
    runtime, engine, advisor = make_runtime(
        holdings=[holding()], decide_result=ExitDecision(sell=False, reason="관망")
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))
    assert engine.executed == []


def test_none_decision_does_not_execute_anything():
    """advisor 호출이 실패해 None이 오면(ExitAdvisor.decide의 계약) 아무것도 팔지 않는다."""
    runtime, engine, advisor = make_runtime(holdings=[holding()], decide_result=None)
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))
    assert engine.executed == []


def test_cycle_skips_when_portfolio_return_is_unavailable():
    """합산 순손익률을 계산할 수 없으면(현재가 미수신 등) 궤적도 쌓지 않고 advisor도 부르지 않는다."""
    runtime, engine, advisor = make_runtime(
        holdings=[holding()],
        portfolio_return=None,
        decide_result=ExitDecision(sell=True, reason="x"),
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))
    assert advisor.calls == []
    assert engine.exit_trace.count == 0
    assert engine.executed == []
    assert engine.ai_exit_calls == 0, "LLM을 부르지 않았는데 하루 호출 횟수가 늘었다"


def test_sell_true_rereads_holdings_immediately_before_executing():
    """매도 직전에 force=True로 최신 잔고를 다시 읽는다.

    LLM 응답을 최대 120초까지 기다리는 동안 손절이 먼저 정리했을 수 있어, 판단 시점
    스냅샷을 그대로 팔면 이미 판 종목을 또 팔려는 시도가 될 수 있다.
    """
    h = holding()
    runtime, engine, advisor = make_runtime(
        holdings=[h], decide_result=ExitDecision(sell=True, reason="추세 이탈")
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))
    assert True in engine.exit_candidates_calls


def test_sell_true_does_not_execute_if_holdings_were_cleared_meanwhile():
    """재조회 시점에 보유가 이미 비었으면(예: 그 사이 손절) 빈 목록으로 또 매도하지 않는다."""
    h = holding()
    runtime, engine, advisor = make_runtime(
        holdings=[h],
        fresh_holdings=[],  # force=True로 다시 읽으면 이미 청산된 상태
        decide_result=ExitDecision(sell=True, reason="추세 이탈"),
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))
    assert engine.executed == []


# ── 스레드 분리 (LLM 호출은 별도 스레드, 매도 주문은 루프 스레드) ──────
def test_decide_runs_off_the_loop_thread():
    loop_thread = threading.get_ident()
    runtime, engine, advisor = make_runtime(
        holdings=[holding()], decide_result=ExitDecision(sell=False, reason="관망")
    )
    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert len(advisor.threads) == 1
    assert advisor.threads[0] != loop_thread


def test_execute_portfolio_exit_runs_on_the_loop_thread():
    loop_thread = threading.get_ident()
    threads = []
    h = holding()
    runtime, engine, advisor = make_runtime(
        holdings=[h], decide_result=ExitDecision(sell=True, reason="추세 이탈")
    )

    original = engine._execute_portfolio_exit

    def spy(holdings, reason):
        threads.append(threading.get_ident())
        original(holdings, reason)

    engine._execute_portfolio_exit = spy

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert threads == [loop_thread]


# ── 워처 루프 자체 (게이트를 반복 확인하며 도는지 가볍게 확인) ─────────
def test_watch_ai_exit_skips_quietly_with_no_holdings():
    """무한 루프이므로 실제로는 몇 바퀴만 돌리고 취소한다 — 예외 없이 아무것도 안 하는지만 본다."""
    runtime, engine, advisor = make_runtime(holdings=[])

    async def scenario():
        task = asyncio.create_task(watch_ai_exit(runtime, poll_seconds=0))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert advisor.calls == []


# ── 일일 초기화 ─────────────────────────────────────────────
def test_reset_for_new_day_clears_exit_trace_and_call_counter():
    engine, _, _ = make_engine(two_holdings())
    engine.exit_trace.append(datetime.now(), 0.01, {"005930": 0.01}, {"005930": 1010.0})
    engine.note_ai_exit_call()
    engine.note_ai_exit_call()
    assert engine.exit_trace.count == 1
    assert engine.ai_exit_calls == 2

    engine.reset_for_new_day()

    assert engine.exit_trace.count == 0
    assert engine.ai_exit_calls == 0


# ── engine.py에 새로 추가한 메서드 (실물 TradingEngine으로 직접 확인) ────
def test_exit_candidates_excludes_tickers_already_being_sold():
    engine, orders, _ = make_engine(two_holdings())
    # 000660이 -6%로 밀리면 합산 -2.5% → 손절선(-2%) 통과, 두 종목 다 전량 매도
    engine.on_market_data(MarketData(ticker="000660", price=940.0, volume=1))
    assert len(orders) == 2

    assert engine.exit_candidates() == []


def test_position_net_return_matches_position_snapshot_ratio():
    """position_snapshot의 net_pnl_percent(퍼센트)를 100으로 나눈 값과 같아야 한다."""
    engine, _, _ = make_engine(two_holdings())  # 005930: 평단 1000, 현재 1010 (+1%), 수수료 0
    position = engine._get_positions()["005930"]

    assert engine.position_net_return(position) == pytest.approx(0.01)
    [view] = [v for v in engine.position_snapshot() if v.ticker == "005930"]
    assert engine.position_net_return(position) == pytest.approx(view.net_pnl_percent / 100)
