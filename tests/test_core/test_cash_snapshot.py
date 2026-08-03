"""엔진이 조회한 예수금을 UI가 API 호출 없이 읽을 수 있도록 캐싱한다 (설정 화면 표시용)."""
from types import SimpleNamespace

from src.api.account import BalanceSnapshot
from src.core.engine import TradingEngine


def make_engine(cash):
    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(),
        account=SimpleNamespace(
            get_balance_snapshot=lambda: BalanceSnapshot(cash=cash, positions={}),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(initialize=lambda s: None),
    )


def test_cash_snapshot_is_none_before_start():
    engine = make_engine(cash=5_000_000.0)

    assert engine.cash_snapshot() is None


def test_cash_snapshot_reflects_balance_after_start():
    engine = make_engine(cash=5_000_000.0)
    engine.start()

    assert engine.cash_snapshot() == 5_000_000.0


def test_cash_snapshot_updates_after_daily_reset():
    account_cash = {"value": 5_000_000.0}
    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(),
        account=SimpleNamespace(
            get_balance_snapshot=lambda: BalanceSnapshot(cash=account_cash["value"], positions={}),
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(initialize=lambda s: None),
    )
    engine.start()
    account_cash["value"] = 7_000_000.0

    engine.reset_for_new_day()

    assert engine.cash_snapshot() == 7_000_000.0


def test_refresh_cash_updates_from_account():
    """입금 등으로 예수금이 바뀌어도 다음 주기적 갱신에서 반영돼야 한다."""
    account_cash = {"value": 5_000_000.0}
    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(),
        account=SimpleNamespace(
            get_balance_snapshot=lambda: BalanceSnapshot(cash=account_cash["value"], positions={}),
            get_cash=lambda: account_cash["value"],
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(initialize=lambda s: None),
    )
    engine.start()
    account_cash["value"] = 8_000_000.0

    engine.refresh_cash()

    assert engine.cash_snapshot() == 8_000_000.0


def test_refresh_cash_keeps_stale_value_on_failure():
    """조회 실패로 갱신이 안 돼도 직전 값을 그대로 표시해야 한다 (잔고 캐시와 동일한 원칙)."""

    def failing_get_cash():
        raise RuntimeError("네트워크 오류")

    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(),
        account=SimpleNamespace(
            get_balance_snapshot=lambda: BalanceSnapshot(cash=5_000_000.0, positions={}),
            get_cash=failing_get_cash,
        ),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(initialize=lambda s: None),
    )
    engine.start()

    engine.refresh_cash()

    assert engine.cash_snapshot() == 5_000_000.0
