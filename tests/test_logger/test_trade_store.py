import sqlite3
from datetime import date, datetime

from src.core.events import FillRecord, OrderResult, OrderSide, OrderStatus
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

    monthly = store.monthly_summary(2026, 7, up_to=date(2026, 7, 31))
    assert monthly.realized_pnl == 200.0 - 200.0  # +200 - 200 = 0


# ── 체결 반영 (apply_fills) ─────────────────────────────────
DAY = date(2026, 7, 29)


def record_pending(store, order_id, ticker, side, quantity, hour, avg_price=None, name=None):
    """주문 접수 직후 상태 — 체결 여부를 모르므로 pending으로 들어간다."""
    store.record_fill(
        OrderResult(
            order_id=order_id,
            ticker=ticker,
            side=side,
            status=OrderStatus.PENDING,
            quantity=quantity,
            timestamp=datetime(2026, 7, 29, hour, 0),
            name=name,
        ),
        avg_price=avg_price,
    )


def make_fill(order_id, ticker, side, quantity, price, commission=20.0, tax=0.0, unfilled=0):
    return FillRecord(
        order_id=order_id,
        ticker=ticker,
        side=side,
        filled_quantity=quantity,
        filled_price=price,
        unfilled_quantity=unfilled,
        commission=commission,
        tax=tax,
        name="카카오" if ticker == "035720" else "LG디스플레이",
    )


def seed_today(store):
    """2026-07-29 실제 매매 — 카카오 익절, LG디스플레이 손절."""
    record_pending(store, "0079364", "035720", OrderSide.BUY, 5, 9, name="카카오")
    record_pending(store, "0079368", "034220", OrderSide.BUY, 22, 9, name="LG디스플레이")
    record_pending(store, "0087730", "035720", OrderSide.SELL, 5, 10, avg_price=36300.0)
    record_pending(store, "0288581", "034220", OrderSide.SELL, 22, 11, avg_price=9080.0)
    return [
        make_fill("0079364", "035720", OrderSide.BUY, 5, 36300.0),
        make_fill("0079368", "034220", OrderSide.BUY, 22, 9080.0),
        make_fill("0087730", "035720", OrderSide.SELL, 5, 37000.0, tax=369.0),
        make_fill("0288581", "034220", OrderSide.SELL, 22, 8890.0, tax=390.0),
    ]


def test_pending_orders_are_not_counted_until_fills_applied(tmp_path):
    """체결 반영 전에는 매매가 한 건도 없는 것처럼 집계된다 — 리포트 0건의 원인."""
    store = make_store(tmp_path)
    seed_today(store)

    summary = store.daily_summary(DAY)
    assert summary.buy_count == 0
    assert summary.sell_count == 0
    assert summary.trades == []


def test_apply_fills_completes_daily_summary(tmp_path):
    store = make_store(tmp_path)
    fills = seed_today(store)

    assert store.apply_fills(fills, DAY) == 4

    summary = store.daily_summary(DAY)
    assert summary.buy_count == 2
    assert summary.sell_count == 2
    assert summary.realized_pnl == 3500.0 - 4180.0        # 카카오 +3,500 / LGD -4,180
    assert summary.cost == 36300.0 * 5 + 9080.0 * 22      # 투입원가 381,260원
    assert summary.fees == 20 + 20 + (20 + 369) + (20 + 390)
    assert round(summary.return_pct, 4) == -0.1784
    assert summary.net_pnl == -680.0 - 839.0
    assert round(summary.net_return_pct, 4) == -0.3984


def test_monthly_summary_nets_out_fees(tmp_path):
    """이번 달 실제 차익은 실현손익이 아니라 수수료·세금을 뺀 순손익이다."""
    store = make_store(tmp_path)
    store.apply_fills(seed_today(store), DAY)

    monthly = store.monthly_summary(2026, 7, up_to=DAY)

    assert monthly.realized_pnl == -680.0
    assert monthly.fees == 839.0          # 매수 수수료까지 포함 — 일간 집계와 같은 기준
    assert monthly.net_pnl == -1519.0


def test_monthly_return_pct_needs_base_asset(tmp_path):
    """분모는 계좌 잔고에서 오므로, 채워지기 전에는 0으로 두고 나누지 않는다."""
    store = make_store(tmp_path)
    store.apply_fills(seed_today(store), DAY)

    monthly = store.monthly_summary(2026, 7, up_to=DAY)
    assert monthly.return_pct == 0.0
    assert monthly.net_return_pct == 0.0

    monthly.base_asset = 1_000_000.0
    assert round(monthly.return_pct, 4) == -0.068
    assert round(monthly.net_return_pct, 4) == -0.1519


def test_trade_rows_carry_per_stock_return(tmp_path):
    store = make_store(tmp_path)
    store.apply_fills(seed_today(store), DAY)

    rows = {t.ticker: t for t in store.daily_summary(DAY).trades}

    kakao = rows["035720"]
    assert kakao.name == "카카오"
    assert kakao.buy_price == 36300.0
    assert kakao.sell_price == 37000.0
    assert kakao.quantity == 5
    assert kakao.pnl == 3500.0
    assert round(kakao.return_pct, 2) == 1.93

    lgd = rows["034220"]
    assert lgd.pnl == -4180.0
    assert round(lgd.return_pct, 2) == -2.09


def test_apply_fills_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    fills = seed_today(store)

    store.apply_fills(fills, DAY)
    first = store.daily_summary(DAY)
    store.apply_fills(fills, DAY)
    second = store.daily_summary(DAY)

    assert (second.buy_count, second.sell_count) == (first.buy_count, first.sell_count)
    assert second.realized_pnl == first.realized_pnl
    assert second.fees == first.fees


def test_unclosed_position_has_no_return(tmp_path):
    """매도가 안 된 종목은 표에 남기되 수익률과 합계에서는 뺀다."""
    store = make_store(tmp_path)
    record_pending(store, "0079364", "035720", OrderSide.BUY, 5, 9, name="카카오")
    store.apply_fills([make_fill("0079364", "035720", OrderSide.BUY, 5, 36300.0)], DAY)

    summary = store.daily_summary(DAY)
    row = summary.trades[0]

    assert row.sell_price is None
    assert row.return_pct is None
    assert row.pnl is None
    assert summary.cost == 0.0        # 청산되지 않았으므로 수익률 분모에 넣지 않는다
    assert summary.return_pct == 0.0


def test_partially_filled_orders_are_counted(tmp_path):
    store = make_store(tmp_path)
    record_pending(store, "0087730", "035720", OrderSide.SELL, 5, 10, avg_price=36300.0)
    store.apply_fills(
        [make_fill("0087730", "035720", OrderSide.SELL, 3, 37000.0, unfilled=2)], DAY
    )

    summary = store.daily_summary(DAY)
    assert summary.sell_count == 1
    assert summary.realized_pnl == (37000.0 - 36300.0) * 3
    assert store.monthly_summary(2026, 7, up_to=DAY).realized_pnl == 2100.0


def test_rejected_orders_are_reported_separately(tmp_path):
    store = make_store(tmp_path)
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.REJECTED, None, datetime(2026, 7, 29, 13, 0))
    )

    summary = store.daily_summary(DAY)
    assert summary.rejected_count == 1
    assert summary.trades == []


def test_unknown_order_number_is_ignored(tmp_path):
    """이 프로그램이 내지 않은 주문(수동 매매 등)은 기록 대상이 아니다."""
    store = make_store(tmp_path)
    seed_today(store)

    updated = store.apply_fills(
        [make_fill("9999999", "005930", OrderSide.BUY, 1, 70000.0)], DAY
    )

    assert updated == 0


def test_existing_db_gains_new_columns(tmp_path):
    """name/commission/tax 이전에 만들어진 DB도 열리면서 컬럼이 추가되어야 한다."""
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE trades (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   order_id TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
                   status TEXT NOT NULL, quantity INTEGER NOT NULL,
                   filled_quantity INTEGER NOT NULL, filled_price REAL, avg_price REAL,
                   realized_pnl REAL, error_message TEXT, timestamp TEXT NOT NULL)"""
        )
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity,
                   filled_quantity, filled_price, avg_price, realized_pnl, timestamp)
               VALUES ('0087730', '035720', 'sell', 'pending', 5, 0, NULL, 36300.0, NULL,
                       '2026-07-29T10:00:00')"""
        )

    store = TradeStore(db_path=db_path)
    store.apply_fills(
        [make_fill("0087730", "035720", OrderSide.SELL, 5, 37000.0, tax=369.0)], DAY
    )

    summary = store.daily_summary(DAY)
    assert summary.realized_pnl == 3500.0
    assert summary.fees == 389.0
    assert summary.trades[0].name == "카카오"
