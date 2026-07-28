from types import SimpleNamespace

from src.api.account import AccountClient


def make_client(response):
    client = AccountClient.__new__(AccountClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=lambda *a, **kw: (response, {}))
    return client


def test_get_cash_reads_orderable_amount():
    client = make_client({"ord_alow_amt": "+0000010000000"})
    assert client.get_cash() == 10_000_000.0


def test_get_cash_falls_back_to_alternate_field_names():
    client = make_client({"entr": "5000000"})
    assert client.get_cash() == 5_000_000.0


def test_get_cash_returns_zero_when_no_known_field():
    client = make_client({"unexpected": "123"})
    assert client.get_cash() == 0.0


def test_get_positions_parses_holdings():
    client = make_client(
        {
            "stk_cntr_remn": [
                {"stk_cd": "A005930", "rmnd_qty": "10", "pur_pric": "70000", "cur_prc": "+71000"},
                {"stk_cd": "000660", "rmnd_qty": "5", "pur_pric": "180000", "cur_prc": "-179000"},
            ]
        }
    )

    positions = client.get_positions()

    assert set(positions) == {"005930", "000660"}   # 'A' 접두사 제거
    assert positions["005930"].quantity == 10
    assert positions["005930"].current_price == 71000.0
    assert positions["000660"].current_price == 179000.0  # 부호는 등락 방향일 뿐


def test_get_positions_skips_zero_quantity_rows():
    client = make_client(
        {"stk_cntr_remn": [{"stk_cd": "005930", "rmnd_qty": "0", "pur_pric": "70000"}]}
    )
    assert client.get_positions() == {}


def test_get_positions_returns_empty_when_no_list_field():
    client = make_client({"return_code": 0})
    assert client.get_positions() == {}


def test_get_positions_parses_the_live_balance_response():
    """kt00018(계좌평가잔고내역요청) 실제 응답 형태 — 2026-07-28 실계좌로 확인."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A032640",
                    "stk_nm": "LG유플러스",
                    "rmnd_qty": "000000000000022",
                    "pur_pric": "000000000014758",
                    "cur_prc": "000000014650",
                }
            ]
        }
    )

    positions = client.get_positions()

    assert set(positions) == {"032640"}
    held = positions["032640"]
    assert held.quantity == 22
    assert held.avg_price == 14758.0
    assert held.current_price == 14650.0
    assert held.name == "LG유플러스"


def test_get_positions_reads_alternate_quantity_and_price_names():
    """kt00005(체결잔고요청)는 수량이 cur_qty, 평단이 buy_uv로 이름이 다르다."""
    client = make_client(
        {
            "stk_cntr_remn": [
                {"stk_cd": "A032640", "cur_qty": "000000000022", "buy_uv": "000000014758",
                 "cur_prc": "000000014650"}
            ]
        }
    )

    positions = client.get_positions()

    assert positions["032640"].quantity == 22
    assert positions["032640"].avg_price == 14758.0


def test_unparsable_rows_are_logged_as_an_error(caplog):
    """행은 왔는데 하나도 해석하지 못하면 '보유 없음'으로 읽혀 청산이 조용히 멈춘다."""
    client = make_client({"acnt_evlt_remn_indv_tot": [{"미지의필드": "1", "값": "2"}]})

    with caplog.at_level("ERROR"):
        assert client.get_positions() == {}

    assert "하나도 해석하지 못했습니다" in caplog.text
