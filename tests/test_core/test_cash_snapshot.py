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
