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
