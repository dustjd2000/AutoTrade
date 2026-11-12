"""엔진의 2호 장중 전환 — 판정이 기존 청산 뒤에서 돌고 매도 주문·다리 기록으로 이어지는가 (스펙 2026-11-11).

반대 ETF 매수(매도 체결 대기·재시도)는 `SwitchFlow`가 맡아 `test_switch_flow.py`에서 본다.
"""
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.api.account import BalanceSnapshot, Position
from src.core import engine as engine_module
from src.core.engine import TradingEngine
from src.core.events import MarketData, OrderResult, OrderSide, OrderStatus, Signal
from src.logger.trade_store import TradeStore
from src.risk.manager import RiskManager

LONG, INVERSE = "069500", "114800"
NOW = datetime(2026, 11, 12, 10, 0, 0)
BOUGHT = datetime(2026, 11, 12, 9, 5, 0)


class Clock(datetime):
    """엔진이 보는 현재 시각 — 판정이 15:14 마감과 틱 나이를 보므로 고정한다."""
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    Clock.current = NOW
    monkeypatch.setattr(engine_module, "datetime", Clock)


class FakeAccount:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        # 엔진이 캐시본을 변형(current_price)하므로 매번 새 객체를 준다
        return {
            t: Position(ticker=p.ticker, quantity=p.quantity, avg_price=p.avg_price,
                        current_price=p.current_price, name=p.name)
            for t, p in self.positions.items()
        }

    def get_cash(self):
        return 1_000_000.0

    def get_balance_snapshot(self):
        return BalanceSnapshot(cash=self.get_cash(), positions=self.get_positions())


def held(ticker, price):
    return Position(ticker=ticker, quantity=100, avg_price=price, current_price=price, name=ticker)


def make_engine(positions, tmp_path, sell_status=OrderStatus.FILLED, enabled=True):
    """비용 0인 실물 RiskManager와 실물 TradeStore를 붙인 엔진 — 가격 변동률이 곧 순손익률이다."""
    orders, alerts, switched = [], [], []

    def send_order(request):
        orders.append(request)
        return OrderResult(
            order_id=f"S{len(orders)}", ticker=request.ticker, side=request.side,
            status=sell_status, quantity=request.quantity, filled_quantity=request.quantity,
            filled_price=1000.0,
            error_message=None if sell_status != OrderStatus.REJECTED else "거부",
        )

    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=send_order),
        account=FakeAccount(positions),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: Signal.HOLD),
        risk_manager=RiskManager(
            stop_loss_ratio=0.02, commission_rate=0.0, tax_rate=0.0, slippage_rate=0.0
        ),
        notifier=SimpleNamespace(send=alerts.append),
        trade_store=TradeStore(tmp_path / "t.db"),
        etf_switch_enabled=enabled,
        etf_switch_index_percent=0.5,
        etf_switch_strength_minutes=5,
    )
    engine.on_switch = lambda: (switched.append(1), True)[1]   # 큐가 접수했다
    engine.start()
    return engine, orders, alerts, switched


def load_strength(engine, buy, sell, until=NOW):
    """`until` 직전 6분 동안 30초마다 매수 `buy`주·매도 `sell`주 체결을 넣어 5분 창을 채운다."""
    at = until - timedelta(minutes=6)
    while at < until:
        engine.trade_strength.add(at, buy)
        engine.trade_strength.add(at, -sell)
        at += timedelta(seconds=30)


def long_tick(price, signed_volume=-5):
    return MarketData(ticker=LONG, price=price, volume=1, signed_volume=signed_volume)


def legs(engine):
    return engine.trade_store.switch_legs_for(NOW.date())


def test_first_leg_switches_when_index_strength_and_loss_all_agree(tmp_path):
    engine, orders, alerts, switched = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))            # 지수·보유 종목 모두 -0.6%

    assert [(o.ticker, o.side) for o in orders] == [(LONG, OrderSide.SELL)]
    pending = engine.pending_switch
    assert (pending.buy_ticker, pending.sell_order_id, pending.quantity) == (INVERSE, "S1", 100)
    assert pending.index_entry == 9940.0                # 2번째 다리 기준 = 매도 주문 시점의 지수
    assert switched == [1]
    assert alerts == []                                 # 전환 메일은 매수 접수 뒤 한 통 (SwitchFlow)
    [row] = legs(engine)
    assert row.end_reason == "etf_switch"
    assert row.switch_against == pytest.approx(0.6)
    assert row.switch_net == pytest.approx(-0.6)
    assert engine.switch_state.switched_today is True
    assert engine.switch_pending is True


def test_refused_queue_submit_clears_the_pending_switch(tmp_path):
    """실행 큐가 접수를 거절하면 pending이 종일 남아 2번째 다리 판정·리포트를 막지 않게 비운다."""
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.on_switch = lambda: False
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert len(orders) == 1                              # 매도는 이미 나갔다
    assert engine.pending_switch is None
    assert engine.switch_state.switched_today is True


def test_stop_loss_runs_first_and_no_switch_follows(tmp_path):
    engine, orders, _, switched = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9700.0))            # -3% — 손절선(-2%)을 넘겼다

    assert len(orders) == 1
    assert engine.pending_switch is None and switched == []
    [row] = legs(engine)
    assert row.end_reason == "stop_loss" and row.switch_against is None


def test_no_switch_while_buyers_still_lead(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_no_switch_while_the_strength_window_is_not_filled(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_no_switch_without_a_recorded_index_entry(tmp_path):
    """아침 매수 뒤 기준 지수 가격을 못 남긴 날은 판정이 돌지 않는다 (스펙 5.4)."""
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_rejected_sell_keeps_the_leg_open_for_a_retry(tmp_path):
    engine, orders, _, switched = make_engine(
        {LONG: held(LONG, 10000.0)}, tmp_path, sell_status=OrderStatus.REJECTED
    )
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert len(orders) == 1
    assert engine.pending_switch is None and switched == []
    assert engine.switch_state.switched_today is False
    assert engine.switch_state.open_leg(LONG, NOW.date()) is not None
    assert legs(engine)[0].ended_at is None


def test_inverse_holder_switches_to_long_on_a_fresh_index_tick(tmp_path):
    engine, orders, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(1, INVERSE, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)

    engine.on_market_data(long_tick(10060.0, signed_volume=5))   # 지수 +0.6% — 아직 인버스 손익 0
    assert orders == []
    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert [(o.ticker, o.side) for o in orders] == [(INVERSE, OrderSide.SELL)]
    assert engine.pending_switch.buy_ticker == LONG


def test_stale_index_tick_blocks_the_decision(tmp_path):
    engine, orders, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(1, INVERSE, index_entry=10000.0, at=BOUGHT)
    Clock.current = NOW - timedelta(seconds=90)
    load_strength(engine, buy=30, sell=10, until=Clock.current)
    engine.on_market_data(long_tick(10060.0, signed_volume=5))
    Clock.current = NOW                                          # 지수 틱이 90초 묵었다

    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert orders == []


def test_second_leg_only_sells_and_alerts_once(tmp_path):
    engine, orders, alerts, switched = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(2, INVERSE, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)
    engine.on_market_data(long_tick(10060.0, signed_volume=5))

    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert [(o.ticker, o.side) for o in orders] == [(INVERSE, OrderSide.SELL)]
    assert engine.pending_switch is None and switched == []
    assert len(alerts) == 1 and "현금으로 마감" in alerts[0]
    assert legs(engine)[0].end_reason == "etf_switch"


def test_disabled_switch_still_collects_the_index_but_never_sells(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path, enabled=False)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []
    assert engine.trade_strength.value(NOW) is not None


def test_restart_restores_the_open_leg(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)

    restarted, _, alerts, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)

    assert restarted.switch_state.open_leg(LONG, NOW.date()).index_entry == 10000.0
    assert alerts == []


def test_restart_between_the_switch_sell_and_buy_stays_in_cash_and_warns(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)
    engine.on_market_data(long_tick(9940.0))            # 전환 매도까지 나갔다

    restarted, _, alerts, _ = make_engine({}, tmp_path)

    assert restarted.switch_state.switched_today is True
    assert restarted.pending_switch is None
    assert len(alerts) == 1 and "재시작" in alerts[0]


def test_begin_leg_is_recorded_once(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    engine.begin_switch_leg(1, LONG, index_entry=9000.0, at=NOW)     # 안전망이 다시 불러도 무시

    assert [row.index_entry for row in legs(engine)] == [10000.0]


def test_begin_leg_uses_the_latest_index_tick_when_no_entry_is_given(tmp_path):
    engine, _, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.on_market_data(long_tick(10020.0, signed_volume=5))

    engine.begin_switch_leg(1, INVERSE)

    assert legs(engine)[0].index_entry == 10020.0


def test_index_strength_is_stored_once_per_minute(tmp_path):
    engine, _, _, _ = make_engine({}, tmp_path)
    engine.on_market_data(long_tick(10000.0))
    engine.on_market_data(long_tick(10010.0))                 # 같은 분 — 다시 쓰지 않는다
    Clock.current = NOW + timedelta(minutes=1)
    engine.on_market_data(long_tick(10020.0))

    rows = engine.trade_store.index_strength_for(NOW.date())
    assert [(m, p) for m, p, _, _ in rows] == [("1000", 10000.0), ("1001", 10020.0)]
    assert rows[0][2] is None                                  # 창이 아직 차지 않았다


def test_index_strength_is_stored_when_the_window_is_full(tmp_path):
    engine, _, _, _ = make_engine({}, tmp_path)
    load_strength(engine, buy=30, sell=10)
    engine.on_market_data(long_tick(10000.0))

    assert engine.trade_store.index_strength_for(NOW.date())[0][2] is not None


def test_index_strength_write_failure_does_not_break_the_tick_or_stop_loss(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)

    def boom(*args):
        raise sqlite3.OperationalError("locked")

    engine.trade_store.save_index_strength = boom
    engine.on_market_data(long_tick(10000.0))                  # 기록 실패 — 예외 없음
    Clock.current = NOW + timedelta(minutes=1)
    engine.on_market_data(long_tick(9700.0))                   # -3% — 손절

    assert len(orders) == 1 and orders[0].side == OrderSide.SELL


def test_restart_long_after_the_switch_sell_without_a_second_leg_is_quiet(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)
    engine.on_market_data(long_tick(9940.0))

    Clock.current = NOW + timedelta(minutes=10)
    restarted, _, alerts, _ = make_engine({}, tmp_path)

    assert restarted.switch_state.switched_today is True
    assert alerts == []


def test_restart_holding_an_etf_without_a_second_leg_warns_about_the_missing_record(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)
    engine.on_market_data(long_tick(9940.0))

    Clock.current = NOW + timedelta(minutes=10)
    restarted, _, alerts, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)

    assert restarted.switch_state.switched_today is True
    assert len(alerts) == 1 and "기록이 빠졌습니다" in alerts[0] and INVERSE in alerts[0]

