"""여러 날 연속 실행 — 앱을 켜둔 채 날짜가 바뀌어도 하루 흐름이 성립해야 한다."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.api.account import BalanceSnapshot, Position
from src.core.engine import TradingEngine
from src.core.events import OrderResult, OrderSide, OrderStatus
from src.core.runtime import _trading_days_only, is_trading_day
from src.risk.manager import RiskManager


def make_engine(risk_manager, cash=1_000_000.0, positions=None):
    positions = positions if positions is not None else {}
    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=lambda r: None),
        account=SimpleNamespace(
            get_positions=lambda: dict(positions),
            get_balance_snapshot=lambda: BalanceSnapshot(cash=cash, positions=dict(positions)),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=risk_manager,
    )


def buy_request():
    return SimpleNamespace(
        ticker="005930", side=OrderSide.BUY, quantity=1, price=1000.0, label="(005930)"
    )


def losing_sell(amount):
    """`amount`원의 실현 손실을 내는 매도 체결."""
    return OrderResult(
        order_id="1",
        ticker="005930",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity=1,
        filled_quantity=1,
        filled_price=1000.0,
        error_message=None,
    ), 1000.0 + amount


# ── 일일 손실 한도 리셋 ──────────────────────────────────────


def test_daily_loss_limit_halts_buys_within_a_day():
    risk = RiskManager(max_daily_loss_ratio=0.02)
    engine = make_engine(risk)
    engine.start()  # 총자산 1,000,000 → 한도 20,000

    result, avg_price = losing_sell(30_000)
    risk.record_order(result, avg_price=avg_price)

    assert risk.approve(buy_request(), {}) is False


def test_new_day_reset_clears_the_daily_loss_and_halt():
    """리셋이 없으면 손실이 며칠치 누적되고 _halted가 재시작 전까지 안 풀린다."""
    risk = RiskManager(max_daily_loss_ratio=0.02)
    engine = make_engine(risk)
    engine.start()

    result, avg_price = losing_sell(30_000)
    risk.record_order(result, avg_price=avg_price)
    assert risk.approve(buy_request(), {}) is False

    engine.reset_for_new_day()

    assert risk.approve(buy_request(), {}) is True


def test_new_day_reset_rebases_total_asset():
    """자산 기준이 첫날 값에 묶이면 매수 수량과 노출 한도가 어긋난다."""
    positions = {"005930": Position(ticker="005930", quantity=1, avg_price=1000.0)}
    risk = RiskManager()
    engine = make_engine(risk, cash=2_000_000.0, positions=positions)
    engine.start()
    first_day_asset = risk._initial_asset

    engine.account.get_balance_snapshot = lambda: BalanceSnapshot(cash=3_000_000.0, positions={})
    engine.reset_for_new_day()

    assert risk._initial_asset == 3_000_000.0
    assert risk._initial_asset != first_day_asset


def test_new_day_reset_drops_the_position_cache():
    """전일 잔고 스냅샷을 그대로 들고 새 날을 시작해선 안 된다."""
    risk = RiskManager()
    engine = make_engine(risk)
    engine.start()
    assert engine._positions_fetched_at is not None

    engine.reset_for_new_day()

    assert engine._positions_fetched_at is None


# ── 주말 스킵 ────────────────────────────────────────────────


def test_is_trading_day_excludes_the_weekend():
    assert is_trading_day(datetime(2026, 7, 31)) is True   # 금
    assert is_trading_day(datetime(2026, 8, 1)) is False   # 토
    assert is_trading_day(datetime(2026, 8, 2)) is False   # 일
    assert is_trading_day(datetime(2026, 8, 3)) is True    # 월


def test_guard_skips_sync_job_off_trading_days(monkeypatch):
    calls = []
    monkeypatch.setattr("src.core.runtime.is_trading_day", lambda: False)

    _trading_days_only(lambda: calls.append("ran"), "execute_buys")()

    assert calls == []


def test_guard_runs_sync_job_on_trading_days(monkeypatch):
    calls = []
    monkeypatch.setattr("src.core.runtime.is_trading_day", lambda: True)

    _trading_days_only(lambda: calls.append("ran"), "execute_buys")()

    assert calls == ["ran"]


def test_guard_handles_off_loop_async_jobs(monkeypatch):
    """수집·LLM·리포트는 _off_loop로 감싸져 코루틴 함수로 들어온다."""
    calls = []

    async def job():
        calls.append("ran")

    monkeypatch.setattr("src.core.runtime.is_trading_day", lambda: False)
    asyncio.run(_trading_days_only(job, "llm_recommend")())
    assert calls == []

    monkeypatch.setattr("src.core.runtime.is_trading_day", lambda: True)
    asyncio.run(_trading_days_only(job, "llm_recommend")())
    assert calls == ["ran"]
