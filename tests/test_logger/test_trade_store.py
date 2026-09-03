import sqlite3
from datetime import date, datetime

from src.core.events import FillRecord, OrderResult, OrderSide, OrderStatus
from src.llm.recommender import StockRecommendation
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


def test_record_fill_stores_the_exit_reason(tmp_path):
    """어느 규칙이 팔았는지를 남긴다 — 없으면 성과 분석 때 로그를 파싱해야 한다."""
    store = make_store(tmp_path)

    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 980.0, datetime(2026, 8, 12, 11, 34)),
        avg_price=1000.0,
        exit_reason="stop_loss",
    )
    store.record_fill(make_result(OrderSide.BUY, OrderStatus.FILLED, 1000.0, datetime(2026, 8, 12, 9, 0)))

    with sqlite3.connect(store.db_path) as conn:
        rows = dict(conn.execute("select side, exit_reason from trades").fetchall())

    assert rows["sell"] == "stop_loss"
    assert rows["buy"] is None, "매수에는 청산 사유가 없다"


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


def test_manual_buy_is_recorded_even_though_this_program_did_not_order_it(tmp_path):
    """키움 앱에서 직접 낸 주문도 그날 계좌 손익의 일부다 (PRD 5.7, 확정 2026-08-11)."""
    store = make_store(tmp_path)
    seed_today(store)

    applied = store.apply_fills(
        [make_fill("9999999", "034220", OrderSide.BUY, 1, 9000.0)], DAY
    )

    assert applied == 1
    summary = store.daily_summary(DAY)
    assert summary.buy_count == 1
    assert summary.fees == 20.0  # 수수료는 매칭 여부와 무관하게 집계된다


def test_manual_sell_uses_the_same_day_buy_price_as_its_average(tmp_path):
    """수동 매도는 평단을 알 수 없어 같은 날 매수 체결가로 대신한다.

    2026-08-11 엘앤에프가 이 경로로 리포트에서 통째로 빠졌던 건이다.
    """
    store = make_store(tmp_path)
    record_pending(store, "0079364", "035720", OrderSide.BUY, 5, 9, name="카카오")

    store.apply_fills(
        [
            make_fill("0079364", "035720", OrderSide.BUY, 5, 36300.0),
            # 사용자가 앱에서 직접 판 물량 — 이 프로그램은 주문번호를 모른다
            make_fill("9999999", "035720", OrderSide.SELL, 5, 37000.0, tax=369.0),
        ],
        DAY,
    )

    summary = store.daily_summary(DAY)
    assert summary.sell_count == 1
    assert summary.realized_pnl == (37000.0 - 36300.0) * 5
    assert summary.cost == 36300.0 * 5
    assert summary.fees == 20.0 + 20.0 + 369.0


def test_manual_sell_of_a_carried_over_position_records_no_pnl(tmp_path):
    """전일 이월분을 판 경우 평단을 알 길이 없다 — 틀린 손익을 적느니 비워 둔다."""
    store = make_store(tmp_path)

    store.apply_fills(
        [make_fill("9999999", "035720", OrderSide.SELL, 5, 37000.0, tax=369.0)], DAY
    )

    summary = store.daily_summary(DAY)
    assert summary.sell_count == 1
    assert summary.realized_pnl == 0.0
    assert summary.trades[0].pnl is None
    assert summary.cost == 0.0          # 원가를 모르므로 수익률 분모에 넣지 않는다
    assert summary.fees == 20.0 + 369.0  # 비용은 실제로 나갔으므로 집계한다


def test_manual_fill_is_not_double_counted_on_rerun(tmp_path):
    """리포트 재발송·엔진 재시작으로 다시 실행돼도 결과가 같아야 한다."""
    store = make_store(tmp_path)
    record_pending(store, "0079364", "035720", OrderSide.BUY, 5, 9, name="카카오")
    fills = [
        make_fill("0079364", "035720", OrderSide.BUY, 5, 36300.0),
        make_fill("9999999", "035720", OrderSide.SELL, 5, 37000.0, tax=369.0),
    ]

    store.apply_fills(fills, DAY)
    first = store.daily_summary(DAY)
    store.apply_fills(fills, DAY)
    second = store.daily_summary(DAY)

    assert (second.buy_count, second.sell_count) == (first.buy_count, first.sell_count)
    assert second.realized_pnl == first.realized_pnl
    assert second.fees == first.fees


# ── 미체결 매도 판정 (청산 리포트 보류 근거, PRD 5.11) ──────
UNSETTLED_DAY = date(2026, 8, 12)


def record_sell(store, status, timestamp=datetime(2026, 8, 12, 15, 20)):
    store.record_fill(
        make_result(OrderSide.SELL, status, None, timestamp), avg_price=15290.0
    )


def test_accepted_sell_without_a_fill_is_unsettled(tmp_path):
    """접수만 된 매도 — 집계에서 빠지므로 이 상태로 리포트를 확정하면 안 된다."""
    store = make_store(tmp_path)
    record_sell(store, OrderStatus.PENDING)

    assert store.has_unsettled_sells(UNSETTLED_DAY) is True


def test_filled_sell_is_settled(tmp_path):
    store = make_store(tmp_path)
    record_sell(store, OrderStatus.PENDING)

    store.apply_fills(
        [make_fill("1", "005930", OrderSide.SELL, 10, 15000.0)], UNSETTLED_DAY
    )

    assert store.has_unsettled_sells(UNSETTLED_DAY) is False


def test_rejected_sell_is_settled(tmp_path):
    """거부된 매도는 체결될 일이 없다 — 기다려도 채워지지 않으므로 리포트를 막지 않는다."""
    store = make_store(tmp_path)
    record_sell(store, OrderStatus.REJECTED)

    assert store.has_unsettled_sells(UNSETTLED_DAY) is False


def test_pending_buy_does_not_count_as_unsettled(tmp_path):
    """09:30에 취소된 미체결 매수는 그대로 남는다 — 팔 것이 없으니 리포트를 막을 이유가 없다."""
    store = make_store(tmp_path)
    store.record_fill(
        make_result(OrderSide.BUY, OrderStatus.PENDING, None, datetime(2026, 8, 12, 9, 0))
    )

    assert store.has_unsettled_sells(UNSETTLED_DAY) is False


def test_unsettled_sell_of_another_day_is_ignored(tmp_path):
    store = make_store(tmp_path)
    record_sell(store, OrderStatus.PENDING, timestamp=datetime(2026, 8, 11, 15, 20))

    assert store.has_unsettled_sells(UNSETTLED_DAY) is False


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


# ── 월 누적 꺾은선 그래프 입력 ──────────────────────────────
def test_monthly_cumulative_series_accumulates_by_trading_day(tmp_path):
    """매매가 있었던 날만 한 점씩, 월초부터의 누적 순손익으로 쌓인다."""
    store = make_store(tmp_path)

    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1020.0, datetime(2026, 7, 10, 9, 30)),
        avg_price=1000.0,
    )  # +200
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 980.0, datetime(2026, 7, 20, 9, 30)),
        avg_price=1000.0,
    )  # -200
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1100.0, datetime(2026, 7, 20, 10, 0)),
        avg_price=1000.0,
    )  # +1,000 — 같은 날이므로 한 점으로 합쳐진다
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1100.0, datetime(2026, 8, 1, 9, 30)),
        avg_price=1000.0,
    )  # 다음 달 — 제외

    series = store.monthly_cumulative_series(2026, 7, up_to=date(2026, 7, 31))

    assert [p.day for p in series] == [date(2026, 7, 10), date(2026, 7, 20)]
    assert [p.net_pnl for p in series] == [200.0, 800.0]
    assert [p.cumulative for p in series] == [200.0, 1000.0]


def test_monthly_cumulative_series_ends_at_monthly_net_pnl(tmp_path):
    """마지막 점은 리포트의 '누적 순손익' 숫자와 같아야 한다 — 수수료 기준도 같다."""
    store = make_store(tmp_path)
    store.apply_fills(seed_today(store), DAY)

    series = store.monthly_cumulative_series(2026, 7, up_to=DAY)

    assert series[-1].cumulative == store.monthly_summary(2026, 7, up_to=DAY).net_pnl


def test_monthly_cumulative_series_empty_without_trades(tmp_path):
    assert make_store(tmp_path).monthly_cumulative_series(2026, 7, up_to=DAY) == []


# ── 연 누적 (2026-09-01) ────────────────────────────────────
def test_yearly_summary_covers_the_whole_year(tmp_path):
    """월 집계와 기준은 같고 기간만 1월 1일부터다."""
    store = make_store(tmp_path)

    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1020.0, datetime(2026, 7, 10, 9, 30)),
        avg_price=1000.0,
    )  # +200
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1100.0, datetime(2026, 8, 3, 9, 30)),
        avg_price=1000.0,
    )  # +1,000 — 다른 달이지만 같은 해다
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1500.0, datetime(2025, 12, 30, 9, 30)),
        avg_price=1000.0,
    )  # 작년 — 제외

    yearly = store.yearly_summary(2026, up_to=date(2026, 8, 31))
    august = store.monthly_summary(2026, 8, up_to=date(2026, 8, 31))

    assert yearly.realized_pnl == 1200.0   # 7월 +200 + 8월 +1,000, 작년 것은 빠진다
    assert august.realized_pnl == 1000.0  # 월 집계는 그 달만


def test_yearly_cumulative_series_accumulates_by_month(tmp_path):
    """점 하나가 한 달이고, 매매가 없던 달은 점을 만들지 않는다."""
    store = make_store(tmp_path)

    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1020.0, datetime(2026, 7, 10, 9, 30)),
        avg_price=1000.0,
    )  # +200
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 1080.0, datetime(2026, 7, 20, 9, 30)),
        avg_price=1000.0,
    )  # +800 — 같은 달이라 한 점으로 합쳐진다
    store.record_fill(
        make_result(OrderSide.SELL, OrderStatus.FILLED, 900.0, datetime(2026, 9, 1, 9, 30)),
        avg_price=1000.0,
    )  # -1,000 — 8월은 매매가 없어 건너뛴다

    series = store.yearly_cumulative_series(2026, up_to=date(2026, 9, 30))

    assert [p.day for p in series] == [date(2026, 7, 1), date(2026, 9, 1)]
    assert [p.net_pnl for p in series] == [1000.0, -1000.0]
    assert [p.cumulative for p in series] == [1000.0, 0.0]


def test_yearly_cumulative_series_ends_at_yearly_net_pnl(tmp_path):
    """마지막 점은 리포트의 '올해 누적 순손익' 숫자와 같아야 한다."""
    store = make_store(tmp_path)
    store.apply_fills(seed_today(store), DAY)

    series = store.yearly_cumulative_series(2026, up_to=DAY)

    assert series[-1].cumulative == store.yearly_summary(2026, up_to=DAY).net_pnl


def test_yearly_cumulative_series_empty_without_trades(tmp_path):
    assert make_store(tmp_path).yearly_cumulative_series(2026, up_to=DAY) == []


def _rec(ticker="005930", name="삼성전자", **kwargs):
    defaults = dict(
        target_price=70_000,
        target_sell_price=71_400,
        reason="전일 등락률 +2.15%",
        setup="rebound",
        outlook="오전 중 회복 시도",
        recommend_price=70_500.0,
    )
    defaults.update(kwargs)
    return StockRecommendation(ticker=ticker, name=name, **defaults)


def test_save_and_read_recommendations(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")

    rows = store.recommendations_for(day)
    assert len(rows) == 1
    row = rows[0]
    assert row.ticker == "005930"
    assert row.outlook == "오전 중 회복 시도"
    assert row.prompt_version == "v11"
    assert row.recommend_price == 70_500.0
    # 아직 검증 전이므로 실제값은 비어 있다
    assert row.actual_close is None
    assert row.buy_target_hit is None
    assert row.review == ""


def test_save_recommendations_is_idempotent_per_day_and_ticker(tmp_path):
    """① 버튼을 두 번 눌러도 행이 겹치지 않고, 나중 추천이 앞선 추천을 덮어쓴다."""
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec(target_price=70_000)], "v11")
    store.save_recommendations(day, [_rec(target_price=68_000)], "v11")

    rows = store.recommendations_for(day)
    assert len(rows) == 1
    assert rows[0].target_price == 68_000


def test_recommendations_for_other_day_is_empty(tmp_path):
    store = make_store(tmp_path)
    store.save_recommendations(date(2026, 9, 3), [_rec()], "v11")
    assert store.recommendations_for(date(2026, 9, 2)) == []


def test_save_recommendation_outcome(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    store.save_recommendation_outcome(
        day,
        "005930",
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
        buy_target_hit=True,
        sell_target_hit=True,
    )

    row = store.recommendations_for(day)[0]
    assert row.actual_high == 72_000.0
    assert row.actual_low == 69_500.0
    assert row.actual_close == 71_000.0
    assert row.actual_change_rate == 1.43
    assert row.buy_target_hit is True
    assert row.sell_target_hit is True


def test_save_recommendation_outcome_keeps_unknown_sell_hit_null(tmp_path):
    """목표 매도가가 산출되지 않은(0) 종목은 '미도달'이 아니라 '판정 안 함'이다."""
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec(target_sell_price=0)], "v11")
    store.save_recommendation_outcome(
        day,
        "005930",
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
        buy_target_hit=True,
        sell_target_hit=None,
    )
    assert store.recommendations_for(day)[0].sell_target_hit is None


def test_save_recommendation_review(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    store.save_recommendation_review(day, "005930", "오전 회복 시도는 맞았습니다.")
    assert store.recommendations_for(day)[0].review == "오전 회복 시도는 맞았습니다."


def test_recommendations_do_not_touch_trades_table(tmp_path):
    """추천 기록은 체결 집계와 별개다 — 일일 리포트 숫자가 흔들리면 안 된다."""
    store = make_store(tmp_path)
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    summary = store.daily_summary(day)
    assert summary.buy_count == 0
    assert summary.sell_count == 0
