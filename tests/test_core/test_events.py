from src.api.account import Position
from src.core.events import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    format_stock,
)


def test_format_stock_puts_code_first():
    """로그·메일 공통 표기 — (종목코드)종목명."""
    assert format_stock("032640", "LG유플러스") == "(032640)LG유플러스"


def test_format_stock_without_name_shows_code_only():
    """종목명을 모르는 경로에서는 코드만 남긴다."""
    assert format_stock("032640") == "(032640)"
    assert format_stock("032640", None) == "(032640)"


def test_order_request_and_result_label():
    request = OrderRequest(
        ticker="068270",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        name="셀트리온",
    )
    result = OrderResult(
        order_id="1",
        ticker=request.ticker,
        side=request.side,
        status=OrderStatus.REJECTED,
        quantity=request.quantity,
        name=request.name,
    )

    assert request.label == "(068270)셀트리온"
    assert result.label == "(068270)셀트리온"


def test_position_label():
    position = Position(ticker="005930", quantity=1, avg_price=70000.0, name="삼성전자")
    assert position.label == "(005930)삼성전자"
