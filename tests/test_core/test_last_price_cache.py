"""추천 종목의 현재가 — 보유하지 않은 종목의 시세도 UI '매수 예정' 표가 읽는다."""
from types import SimpleNamespace

from src.api.account import Position
from src.core.engine import TradingEngine
from src.core.events import MarketData


class Account:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        return dict(self.positions)

    def get_balance_snapshot(self):
        return SimpleNamespace(total_asset=1_000_000, cash=1_000_000, positions=self.get_positions())


def make_engine(positions=None):
    return TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=lambda r: None),
        account=Account(positions or {}),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_portfolio_exit=lambda ps: None,
            check_simple_take_profits=lambda ps: [],
            portfolio_return=lambda ps: None,
        ),
    )


def test_tick_of_an_unheld_ticker_is_remembered():
    """추천만 나온 종목은 보유가 아니다 — 그래도 마지막 시세는 남아야 표에 찍힌다."""
    engine = make_engine()
    engine.start()

    engine.on_market_data(MarketData(ticker="035720", price=37_000.0, volume=1))

    assert engine.last_price("035720") == 37_000.0


def test_last_price_is_zero_before_the_first_tick():
    engine = make_engine()

    assert engine.last_price("005930") == 0.0


def test_later_ticks_replace_the_remembered_price():
    engine = make_engine({"005930": Position(ticker="005930", quantity=1, avg_price=70_000.0)})
    engine.start()

    engine.on_market_data(MarketData(ticker="005930", price=70_500.0, volume=1))
    engine.on_market_data(MarketData(ticker="005930", price=71_200.0, volume=1))

    assert engine.last_price("005930") == 71_200.0
