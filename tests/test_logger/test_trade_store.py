from datetime import date, datetime

from src.core.events import OrderResult, OrderSide, OrderStatus
from src.logger.trade_store import TradeStore


def make_store(tmp_path):
    return TradeStore(db_path=tmp_path / "trades.db")


def make_result(side, status, filled_price, timestamp, filled_quantity=10):
    return OrderResult(
        order_id="1",
        ticker="005930",
        side=side,
        status=status,
        quantity=filled_quantity,
        filled_quantity=filled_quantity,
        filled_price=filled_price,
        timestamp=timestamp,
    )


def test_record_fill_and_daily_summary(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 7, 27)

    buy = make_result(OrderSide.BUY, OrderStatus.FILLED, 1000.0, datetime(2026, 7, 27, 9, 0))
    store.record_fill(buy)

    sell = make_result(OrderSide.SELL, OrderStatus.FILLED, 1020.0, datetime(2026, 7, 27, 9, 30))
    store.record_fill(sell, avg_price=1000.0)

    summary = store.daily_summary(day)
    assert summary.buy_count == 1
    assert summary.sell_count == 1
    assert summary.realized_pnl == 200.0


def test_rejected_orders_excluded_from_summary(tmp_path):
    store = make_store(tmp_path)
    rejected = make_result(OrderSide.BUY, OrderStatus.REJECTED, None, datetime(2026, 7, 27, 9, 0))
    store.record_fill(rejected)

    summary = store.daily_summary(date(2026, 7, 27))
    assert summary.buy_count == 0
    assert summary.sell_count == 0
    assert summary.realized_pnl == 0.0


def test_monthly_realized_pnl_sums_within_month(tmp_path):
    store = make_store(tmp_path)

    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1020.0, datetime(2026, 7, 10, 9, 30)),
        avg_price=1000.0,
    )
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 980.0, datetime(2026, 7, 20, 9, 30)),
        avg_price=1000.0,
    )
    # 다음 달 데이터는 집계에서 제외되어야 한다
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1100.0, datetime(2026, 8, 1, 9, 30)),
        avg_price=1000.0,
    )

    total = store.monthly_realized_pnl(2026, 7, up_to=date(2026, 7, 31))
    assert total == 200.0 - 200.0  # +200 - 200 = 0
