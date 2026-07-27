from types import SimpleNamespace

from src.api.order import (
    BUY_API_ID,
    ORDER_PATH,
    SELL_API_ID,
    TRADE_TYPE_LIMIT,
    TRADE_TYPE_MARKET,
    OrderClient,
)
from src.core.events import OrderRequest, OrderSide, OrderStatus, OrderType


def make_client(response=None, raises=None):
    captured = {}

    def fake_request(path, api_id, body=None, **kwargs):
        captured["path"] = path
        captured["api_id"] = api_id
        captured["body"] = body
        if raises:
            raise raises
        return (response or {"ord_no": "0001234"}), {}

    client = OrderClient.__new__(OrderClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=fake_request)
    return client, captured


def test_market_buy_sends_empty_price_and_market_trade_type():
    client, captured = make_client()
    request = OrderRequest(
        ticker="005930", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10
    )

    result = client.send_order(request)

    assert captured["api_id"] == BUY_API_ID
    assert captured["path"] == ORDER_PATH
    assert captured["body"]["trde_tp"] == TRADE_TYPE_MARKET
    assert captured["body"]["ord_uv"] == ""      # 시장가는 단가를 비워 보낸다
    assert captured["body"]["ord_qty"] == "10"
    assert result.order_id == "0001234"
    # 접수 성공은 체결이 아니다 — 체결은 실시간 통보로 확정된다
    assert result.status == OrderStatus.PENDING


def test_limit_sell_sends_price_and_limit_trade_type():
    client, captured = make_client()
    request = OrderRequest(
        ticker="000660",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=5,
        price=123456.0,
    )

    client.send_order(request)

    assert captured["api_id"] == SELL_API_ID
    assert captured["body"]["trde_tp"] == TRADE_TYPE_LIMIT
    assert captured["body"]["ord_uv"] == "123456"


def test_failed_order_returns_rejected_after_retries():
    client, _ = make_client(raises=RuntimeError("주문가능금액 부족"))
    request = OrderRequest(
        ticker="005930", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10
    )

    result = client.send_order(request)

    assert result.status == OrderStatus.REJECTED
    assert "주문가능금액 부족" in result.error_message


def test_missing_order_number_is_treated_as_failure():
    client, _ = make_client(response={"return_code": 0})  # ord_no 없음
    request = OrderRequest(
        ticker="005930", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10
    )

    result = client.send_order(request)

    assert result.status == OrderStatus.REJECTED
