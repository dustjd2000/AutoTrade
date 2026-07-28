"""매도할 수 없는 종목 — 목록에 남겨두면 감시와 청산이 매 회 헛돈다.

상장폐지·거래정지 종목은 잔고에 계속 실려 오지만 매도 주문은 '종목 정보가 없습니다'로
영구 거부된다 (2026-07-28 118970/395680). 보유 목록으로 취급하지 않아야 한다.
"""
from types import SimpleNamespace

from src.api.account import BalanceSnapshot, Position
from src.api.client import KiwoomAPIError
from src.api.market_data import StockMaster
from src.core.engine import TradingEngine
from src.core.events import ExitReason, MarketData, OrderResult, OrderStatus

UNKNOWN_STOCK = (
    "[kt10001] return_code=7: 서비스를 처리하는 중에 오류가 발생했습니다"
    "[1902:종목 정보가 없습니다. 입력한 종목코드 값을 확인바랍니다. 종목코드=118970]"
)
NO_SUCH_STOCK = KiwoomAPIError(
    "ka10001", 7, "종목 정보가 없습니다. 입력한 종목코드 값을 확인바랍니다."
)


class FakeMarketData:
    """ka10001 응답을 흉내 낸다. 종목당 한 번만 묻는지 보려고 호출을 기록한다."""

    def __init__(self, masters=None):
        self.masters = masters or {}
        self.calls = []

    def get_stock_master(self, ticker):
        self.calls.append(ticker)
        result = self.masters.get(ticker, NO_SUCH_STOCK)
        if isinstance(result, Exception):
            raise result
        return result


class RejectingAccount:
    """매도해도 잔고에서 사라지지 않는 계좌 (실제 상폐 종목의 동작)."""

    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        # 엔진이 캐시본을 변형(current_price)하므로 매번 새 객체를 준다
        return {
            t: Position(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
                name=p.name,
            )
            for t, p in self.positions.items()
        }

    def get_balance_snapshot(self):
        return BalanceSnapshot(cash=0.0, positions=self.get_positions())


def make_engine(account, error_message=UNKNOWN_STOCK, exit_reason=None, market_data=None):
    orders = []

    def send_order(request):
        orders.append(request)
        return OrderResult(
            order_id=str(len(orders)),
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.REJECTED,
            quantity=request.quantity,
            error_message=error_message,
            name=request.name,
        )

    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=market_data,
        order_client=SimpleNamespace(send_order=send_order),
        account=account,
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=SimpleNamespace(
            initialize=lambda s: None,
            record_order=lambda *a, **kw: None,
            check_exit=lambda p: exit_reason,
        ),
    )
    return engine, orders


def delisted():
    return {
        "118970": Position(
            ticker="118970", quantity=18, avg_price=0.0, current_price=94.0, name="비에스제이홀딩스"
        )
    }


def normal(ticker="005930", current_price=70000.0):
    return {
        ticker: Position(
            ticker=ticker,
            quantity=10,
            avg_price=70000.0,
            current_price=current_price,
            name="삼성전자",
        )
    }


def test_delisted_position_is_excluded_before_any_order():
    """주문 거부를 기다리지 않고 잔고를 읽는 시점에 걸러야 한다 — 그전까진 목록에 그대로 뜬다."""
    engine, orders = make_engine(RejectingAccount(delisted()), market_data=FakeMarketData())
    engine.start()

    assert engine.open_tickers == []
    assert engine.position_snapshot() == []

    engine.force_close_all_positions(reason="day_end")
    assert orders == [], "제외된 종목에 청산 주문이 나갔다"


def test_position_without_a_master_name_is_excluded():
    market_data = FakeMarketData({"118970": StockMaster(ticker="118970", name="", price=94.0)})
    engine, _ = make_engine(RejectingAccount(delisted()), market_data=market_data)
    engine.start()

    assert engine.open_tickers == []


def test_zero_master_price_alone_does_not_exclude():
    """장 전에는 마스터 현재가가 0으로 올 수 있다 — 정상 종목을 빼면 청산이 통째로 멈춘다."""
    market_data = FakeMarketData(
        {"005930": StockMaster(ticker="005930", name="삼성전자", price=0.0)}
    )
    engine, _ = make_engine(
        RejectingAccount(normal(current_price=70000.0)), market_data=market_data
    )
    engine.start()

    assert engine.open_tickers == ["005930"]


def test_zero_price_on_both_sides_is_excluded():
    """마스터도 잔고도 현재가가 없으면 거래되지 않는 종목이다 (395680 사례)."""
    market_data = FakeMarketData(
        {"005930": StockMaster(ticker="005930", name="삼성전자", price=0.0)}
    )
    engine, _ = make_engine(RejectingAccount(normal(current_price=0.0)), market_data=market_data)
    engine.start()

    assert engine.open_tickers == []


def test_transient_lookup_failure_keeps_the_position():
    """유량 제한으로 조회가 실패했다고 보유 종목을 지우면 청산이 조용히 멈춘다."""
    market_data = FakeMarketData({"005930": RuntimeError("429 Client Error: null for url: ...")})
    engine, _ = make_engine(RejectingAccount(normal()), market_data=market_data)
    engine.start()

    assert engine.open_tickers == ["005930"]


def test_master_is_looked_up_once_per_ticker():
    """틱마다 다시 물으면 유량 제한에 걸린다."""
    market_data = FakeMarketData(
        {"005930": StockMaster(ticker="005930", name="삼성전자", price=70000.0)}
    )
    engine, _ = make_engine(RejectingAccount(normal()), market_data=market_data)
    engine.start()

    for _ in range(5):
        engine._invalidate_positions()
        engine.on_market_data(MarketData(ticker="005930", price=70500.0, volume=1))

    assert market_data.calls == ["005930"]


def test_new_trading_day_rechecks_excluded_tickers():
    """거래정지가 풀렸을 수 있으므로 매일 아침 다시 확인해야 한다."""
    market_data = FakeMarketData()
    engine, _ = make_engine(RejectingAccount(delisted()), market_data=market_data)
    engine.start()
    assert engine.open_tickers == []

    market_data.masters = {
        "118970": StockMaster(ticker="118970", name="비에스제이홀딩스", price=94.0)
    }
    engine.reset_for_new_day()
    engine.on_market_data(MarketData(ticker="118970", price=94.0, volume=1))

    assert engine.open_tickers == ["118970"]


def test_rejected_by_unknown_stock_leaves_the_holdings_list():
    account = RejectingAccount(delisted())
    engine, _ = make_engine(account)
    engine.start()
    assert engine.open_tickers == ["118970"]

    engine.force_close_all_positions(reason="manual")

    assert engine.open_tickers == []
    assert engine.position_snapshot() == []


def test_excluded_ticker_does_not_come_back_on_the_next_balance_read():
    """잔고에는 계속 실려 오므로 읽을 때마다 걸러야 한다."""
    account = RejectingAccount(delisted())
    engine, _ = make_engine(account)
    engine.start()
    engine.force_close_all_positions(reason="manual")

    engine._invalidate_positions()
    engine.on_market_data(MarketData(ticker="118970", price=94.0, volume=1))

    assert engine.open_tickers == []
    assert engine.position_snapshot() == []


def test_excluded_ticker_is_not_ordered_again():
    """재시도해도 결과가 같다 — 청산할 때마다 무의미한 주문이 나가면 안 된다."""
    account = RejectingAccount(delisted())
    engine, orders = make_engine(account)
    engine.start()
    engine.force_close_all_positions(reason="manual")
    assert len(orders) == 1

    engine.force_close_all_positions(reason="day_end")

    assert len(orders) == 1, f"제외된 종목에 주문이 {len(orders)}번 나갔다"


def test_exit_rejection_also_excludes_the_ticker():
    """익절/손절 청산이 같은 사유로 거부돼도 목록에서 빠져야 한다."""
    account = RejectingAccount(delisted())
    engine, orders = make_engine(account, exit_reason=ExitReason.STOP_LOSS)
    engine.start()

    for _ in range(5):
        engine._invalidate_positions()
        engine.on_market_data(MarketData(ticker="118970", price=90.0, volume=1))

    assert len(orders) == 1
    assert engine.open_tickers == []


def test_other_rejection_reasons_keep_the_position():
    """일시적인 거부(장 상황 등)까지 제외하면 팔 수 있는 포지션을 놓친다."""
    account = RejectingAccount(delisted())
    engine, _ = make_engine(
        account, error_message="[kt10001] return_code=20: [2000](509193:CB 발동중입니다.)"
    )
    engine.start()

    engine.force_close_all_positions(reason="manual")

    assert engine.open_tickers == ["118970"]
