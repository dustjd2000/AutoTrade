from types import SimpleNamespace

from src.api.order import (
    BUY_API_ID,
    FILLS_API_ID,
    FILLS_PATH,
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
    # 접수 성공은 체결이 아니다 — 체결은 당일 체결내역 조회로 확정된다
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


# ── 당일 체결내역 조회 (ka10076) ────────────────────────────
# 아래 두 행은 2026-07-29 실계좌 응답 원문 (scripts/check_fills.py)
SELL_FILL = {
    "ord_no": "0288581", "stk_nm": "LG디스플레이", "io_tp_nm": "-매도", "ord_pric": "0",
    "ord_qty": "22", "cntr_pric": "8890", "cntr_qty": "22", "oso_qty": "0",
    "tdy_trde_cmsn": "20", "tdy_trde_tax": "390", "ord_stt": "체결", "trde_tp": "시장가",
    "orig_ord_no": "0000000", "ord_tm": "104129", "stk_cd": "034220", "stex_tp": "1",
    "stex_tp_txt": "KRX", "sor_yn": "N", "stop_pric": "0",
}
BUY_FILL = {
    "ord_no": "0079368", "stk_nm": "LG디스플레이", "io_tp_nm": "+매수", "ord_pric": "0",
    "ord_qty": "22", "cntr_pric": "9080", "cntr_qty": "22", "oso_qty": "0",
    "tdy_trde_cmsn": "20", "tdy_trde_tax": "0", "ord_stt": "체결", "trde_tp": "시장가",
    "orig_ord_no": "0000000", "ord_tm": "090004", "stk_cd": "034220", "stex_tp": "1",
    "stex_tp_txt": "KRX", "sor_yn": "N", "stop_pric": "0",
}


def make_fills_client(pages):
    """pages: [(응답 본문, 응답 헤더), ...] — 연속조회를 흉내낸다."""
    calls = []

    def fake_request(path, api_id, body=None, cont_yn="N", next_key=""):
        calls.append({"path": path, "api_id": api_id, "cont_yn": cont_yn, "next_key": next_key})
        return pages[len(calls) - 1]

    client = OrderClient.__new__(OrderClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=fake_request)
    return client, calls


def test_get_today_fills_parses_real_response():
    client, calls = make_fills_client([({"cntr": [SELL_FILL, BUY_FILL]}, {"cont-yn": "N"})])

    fills = client.get_today_fills()

    assert calls[0]["api_id"] == FILLS_API_ID
    assert calls[0]["path"] == FILLS_PATH

    sell, buy = fills
    assert sell.order_id == "0288581"
    assert sell.ticker == "034220"
    assert sell.name == "LG디스플레이"
    assert sell.side == OrderSide.SELL
    assert sell.filled_quantity == 22
    assert sell.filled_price == 8890.0
    assert sell.commission == 20.0
    assert sell.tax == 390.0
    assert sell.status == OrderStatus.FILLED

    assert buy.side == OrderSide.BUY
    assert buy.filled_price == 9080.0


def test_partially_filled_when_unfilled_quantity_remains():
    partial = {**BUY_FILL, "cntr_qty": "10", "oso_qty": "12"}
    client, _ = make_fills_client([({"cntr": [partial]}, {"cont-yn": "N"})])

    fill = client.get_today_fills()[0]

    assert fill.status == OrderStatus.PARTIALLY_FILLED
    assert fill.filled_quantity == 10


def test_unfilled_order_stays_pending():
    unfilled = {**BUY_FILL, "cntr_qty": "0", "cntr_pric": "0", "oso_qty": "22"}
    client, _ = make_fills_client([({"cntr": [unfilled]}, {"cont-yn": "N"})])

    assert client.get_today_fills()[0].status == OrderStatus.PENDING


def test_get_today_fills_follows_continuation():
    client, calls = make_fills_client(
        [
            ({"cntr": [SELL_FILL]}, {"cont-yn": "Y", "next-key": "KEY2"}),
            ({"cntr": [BUY_FILL]}, {"cont-yn": "N"}),
        ]
    )

    fills = client.get_today_fills()

    assert len(fills) == 2
    assert calls[1]["cont_yn"] == "Y"
    assert calls[1]["next_key"] == "KEY2"


def test_ticker_prefix_is_stripped():
    client, _ = make_fills_client([({"cntr": [{**BUY_FILL, "stk_cd": "A034220"}]}, {"cont-yn": "N"})])

    assert client.get_today_fills()[0].ticker == "034220"
