"""UI에 넘기는 순손익 스냅샷 — 표의 종목별 합계가 요약줄과 맞아야 한다."""
from types import SimpleNamespace

import pytest

from src.api.account import Position
from src.core.engine import TradingEngine
from src.risk.manager import RiskManager


def make_engine(positions):
    # 엔진이 캐시본의 current_price를 제자리에서 고치므로 매번 새 객체를 준다
    def fresh():
        return {
            t: Position(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
            )
            for t, p in positions.items()
        }

    # start()는 get_positions가 아니라 get_balance_snapshot().positions를 쓴다
    account = SimpleNamespace(
        get_positions=fresh,
        get_balance_snapshot=lambda: SimpleNamespace(
            total_asset=10_000_000, cash=10_000_000, positions=fresh()
        ),
    )
    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=lambda r: None),
        account=account,
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=RiskManager(commission_rate=0.00015, tax_rate=0.002, slippage_rate=0.001),
    )
    engine.start()
    return engine


def held(ticker, avg, cur, qty):
    return Position(ticker=ticker, quantity=qty, avg_price=avg, current_price=cur)


def three_holdings():
    return {
        "047050": held("047050", 55600.0, 56400.0, 15),
        "012330": held("012330", 453000.0, 447500.0, 1),
        "161390": held("161390", 67100.0, 66900.0, 12),
    }


def test_position_view_carries_net_pnl():
    """2026-09-01 삼성E&A 실매매 — 실제 순손익 +10,068원."""
    engine = make_engine({"047050": held("047050", 55600.0, 56400.0, 15)})

    [view] = engine.position_snapshot()

    assert view.net_pnl == pytest.approx(10068.0, abs=1.0)
    assert view.net_pnl_percent == pytest.approx(10068.0 / (55600 * 15) * 100, abs=0.01)


def test_table_rows_sum_to_the_summary_line():
    """종목별 합계 ≠ 요약줄이면 버그처럼 보인다 — 같은 식으로 계산해야 한다."""
    engine = make_engine(three_holdings())

    rows = engine.position_snapshot()
    amount, _ = engine.portfolio_net_pnl_snapshot()

    assert sum(v.net_pnl for v in rows) == pytest.approx(amount)


def test_summary_amount_and_ratio_match_the_real_fills():
    engine = make_engine(three_holdings())

    amount, ratio = engine.portfolio_net_pnl_snapshot()

    assert amount == pytest.approx(-692.0, abs=1.0)
    assert ratio == pytest.approx(amount / (55600 * 15 + 453000 + 67100 * 12))


def test_display_value_is_better_than_the_exit_judgement():
    """표시는 슬리피지를 빼고 판정은 넣는다 — 표시값이 항상 판정값보다 높아야 한다."""
    engine = make_engine({"047050": held("047050", 55600.0, 56400.0, 15)})

    _, display = engine.portfolio_net_pnl_snapshot()
    judged = engine.portfolio_return_snapshot()

    assert display > judged


def test_snapshot_is_empty_when_nothing_is_held():
    engine = make_engine({})
    assert engine.position_snapshot() == []
    assert engine.portfolio_net_pnl_snapshot() == (0.0, None)
